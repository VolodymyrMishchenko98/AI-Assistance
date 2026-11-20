from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from zipfile import BadZipFile, ZipFile
import posixpath
from urllib.parse import unquote, urlsplit

from ..application.import_ports import WorkbookRow
from ..application.imports import ImportSheetDto


MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 10_000
MAX_SHEETS = 32
MAX_COLUMNS = 256
MAX_ROWS = 100_000
MAX_CELLS = 200_000
MAX_CELL_TEXT_BYTES = 16 * 1024
MAX_XML_FILE_BYTES = 8 * 1024 * 1024
SUSPICIOUS_RELATIONSHIP_TYPES = (
    "/vbaproject",
    "/vba",
    "/activex",
    "/control",
    "/oleobject",
    "/ole",
    "/embeddedpackage",
    "/embeddings",
    "/embedding",
    "/externallink",
    "/externallinks",
)
SUSPICIOUS_CONTENT_TYPES = (
    "vnd.ms-office.vba",
    "vnd.ms-excel.sheet.macroenabled",
    "vnd.ms-office.activex",
    "vnd.openxmlformats-officedocument.oleobject",
    "vnd.ms-excel.externallink",
    "oleobject",
    "embedding",
    "embedded",
    "activex",
    "externallink",
)

CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
PACKAGE_REL_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SPREADSHEETML_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

DANGEROUS_RELATIONSHIP_SUFFIXES = (
    "/oleobject",
    "/ole",
    "/embeddedpackage",
    "/embeddings",
    "/embedding",
    "/activex",
    "/control",
    "/vbaproject",
    "/vba",
    "/externallink",
    "/externallinks",
)


def _exact_xml_tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _normalized_relationship_type(value: str) -> str:
    stripped = value.strip().lower()
    parsed = urlsplit(stripped)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return stripped
    return stripped.rstrip("/")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve_package_target(source_part: str, target: str, names: set[str]) -> str:
    if not target or "\\" in target or "\x00" in target:
        raise XlsxImportError("OOXML relationship має некоректну ціль")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise XlsxImportError("OOXML relationship має некоректний URI")

    raw_path = parsed.path
    decoded = raw_path
    decode_passes = 0
    for _ in range(4):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
        decode_passes += 1
    if decode_passes > 1:
        raise XlsxImportError("OOXML relationship має неоднозначний double-encoded path")
    if "%" in decoded:
        raise XlsxImportError("OOXML relationship має неоднозначний encoded path")
    if "\\" in decoded or "\x00" in decoded:
        raise XlsxImportError("OOXML relationship має некоректну ціль")

    segments = decoded.replace("\\", "/").split("/")
    if any(segment == ".." for segment in segments):
        traversal = True
    else:
        traversal = False
    if any(segment in ("", ".") for segment in segments if segment != "" and not decoded.startswith("/")):
        raise XlsxImportError("OOXML relationship має неоднозначний encoded path")

    if decoded.startswith("/"):
        if traversal:
            raise XlsxImportError("OOXML relationship має небезпечний package-absolute path")
        candidate = posixpath.normpath(decoded.lstrip("/"))
    else:
        base = posixpath.dirname(source_part)
        candidate = posixpath.normpath(posixpath.join(base, decoded))
        if traversal and not candidate.startswith("xl/"):
            raise XlsxImportError("OOXML relationship виходить за межі дозволеної області xl/")
    if candidate in ("", ".", "..") or candidate.startswith("../") or candidate.startswith("/"):
        raise XlsxImportError("OOXML relationship виходить за межі пакета")
    if candidate not in names:
        raise XlsxImportError("OOXML relationship вказує на відсутню частину пакета")
    return candidate


class XlsxImportError(ValueError):
    pass


def _preflight(path: Path) -> None:
    if path.suffix.lower() != ".xlsx":
        raise XlsxImportError("підтримуються лише файли .xlsx")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise XlsxImportError("розмір файлу перевищує дозволений ліміт")
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise XlsxImportError("файл містить забагато ZIP-записів")
            names: list[str] = [info.filename.replace("\\", "/") for info in infos]
            if len(set(names)) != len(names):
                raise XlsxImportError("файл містить дубльовані ZIP-записи")
            total = 0
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or posixpath.normpath(name).startswith("../"):
                    raise XlsxImportError("файл містить небезпечний шлях")
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise XlsxImportError("розпакований файл перевищує дозволений ліміт")
            names_set = set(names)
            required = {"[Content_Types].xml", "xl/workbook.xml", "_rels/.rels", "xl/_rels/workbook.xml.rels"}
            if not required.issubset(names_set):
                raise XlsxImportError("файл не має коректної структури XLSX")
            try:
                from defusedxml import ElementTree
                for info in infos:
                    if info.filename.endswith(".xml"):
                        if info.file_size > MAX_XML_FILE_BYTES:
                            raise XlsxImportError("розмір XML-компонента XLSX перевищує дозволений ліміт")
                        ElementTree.fromstring(archive.read(info))
            except ImportError as exc:
                raise XlsxImportError("для безпечного читання потрібна defusedxml") from exc
            except Exception as exc:
                if isinstance(exc, XlsxImportError):
                    raise
                raise XlsxImportError("пошкоджений або небезпечний XML у XLSX") from exc
            try:
                from defusedxml import ElementTree
                for info in infos:
                    if not info.filename.lower().endswith(".rels"):
                        continue
                    if info.file_size > MAX_XML_FILE_BYTES:
                        raise XlsxImportError("розмір XML-компонента XLSX перевищує дозволений ліміт")
                    relationships_root = ElementTree.fromstring(archive.read(info.filename))
                    if relationships_root.tag != _exact_xml_tag(PACKAGE_REL_NAMESPACE, "Relationships"):
                        raise XlsxImportError("некоректний namespace для package relationships")
                    seen_relationship_ids: set[str] = set()
                    for node in relationships_root:
                        if node.tag != _exact_xml_tag(PACKAGE_REL_NAMESPACE, "Relationship"):
                            raise XlsxImportError("невідомий елемент у relationship-файлі")
                        rid = node.attrib.get("Id")
                        target = node.attrib.get("Target")
                        if not rid or not target:
                            raise XlsxImportError("relationship має некоректні обов'язкові атрибути")
                        if rid in seen_relationship_ids:
                            raise XlsxImportError("relationship-файл містить дубльований Id")
                        seen_relationship_ids.add(rid)
                        if node.attrib.get("TargetMode", "").lower() == "external":
                            raise XlsxImportError("зовнішній relationship у XLSX не підтримується")
            except ImportError as exc:
                raise XlsxImportError("для безпечного читання потрібна defusedxml") from exc
            except XlsxImportError:
                raise
            except Exception as exc:
                raise XlsxImportError("пошкоджені або некоректні package relationships") from exc
            lowercase_names = {name.lower() for name in names_set}
            if any("vba" in name or name.endswith(".bin") for name in lowercase_names):
                raise XlsxImportError("макроси у XLSX не підтримуються")
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name.lower().endswith(".rels") or name == "[Content_Types].xml":
                    if info.file_size > MAX_XML_FILE_BYTES:
                        raise XlsxImportError("розмір XML-компонента XLSX перевищує дозволений ліміт")
                    payload = archive.read(info)
                    lowered = payload.lower()
                    if b"externallink" in lowered or b"vba" in lowered or b"/macroenabled" in lowered:
                        raise XlsxImportError("макроси або зовнішні посилання у XLSX не підтримуються")
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name.startswith("xl/worksheets/") and name.lower().endswith(".xml"):
                    if info.file_size > MAX_XML_FILE_BYTES:
                        raise XlsxImportError("розмір XML-компонента XLSX перевищує дозволений ліміт")
                    payload = archive.read(info)
                    if b"<mergeCells" in payload or b":mergeCells" in payload:
                        raise XlsxImportError("листи з merged cells не підтримуються у потоковому імпорті")
                    try:
                        from defusedxml import ElementTree
                        worksheet = ElementTree.fromstring(payload)
                        if worksheet.tag != _exact_xml_tag(SPREADSHEETML_NAMESPACE, "worksheet"):
                            raise XlsxImportError("некоректний namespace для worksheet")
                    except ImportError as exc:
                        raise XlsxImportError("для безпечного читання потрібна defusedxml") from exc
                    except XlsxImportError:
                        raise
                    except Exception as exc:
                        raise XlsxImportError("пошкоджений worksheet XML") from exc
            try:
                from defusedxml import ElementTree
                content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
                if content_types.tag != _exact_xml_tag(CONTENT_TYPES_NAMESPACE, "Types"):
                    raise XlsxImportError("некоректний кореневий елемент [Content_Types].xml")
                defaults: dict[str, str] = {}
                overrides: dict[str, str] = {}
                for node in content_types.iter():
                    if node is content_types:
                        continue
                    local_name = _local_name(node.tag)
                    if local_name == "Default":
                        if node.tag != _exact_xml_tag(CONTENT_TYPES_NAMESPACE, "Default"):
                            raise XlsxImportError("невідомий namespace для Default у [Content_Types].xml")
                        extension = node.attrib.get("Extension", "").lower()
                        content_type = node.attrib.get("ContentType", "").lower()
                        if not extension or "/" in extension or "\\" in extension or extension in defaults:
                            raise XlsxImportError("конфліктуюче або некоректне Default у [Content_Types].xml")
                        if not content_type:
                            raise XlsxImportError("порожній ContentType у [Content_Types].xml")
                        defaults[extension] = content_type
                        if any(marker in content_type for marker in SUSPICIOUS_CONTENT_TYPES):
                            raise XlsxImportError("макроси, ActiveX або embedded objects у XLSX не підтримуються")
                    elif local_name == "Override":
                        if node.tag != _exact_xml_tag(CONTENT_TYPES_NAMESPACE, "Override"):
                            raise XlsxImportError("невідомий namespace для Override у [Content_Types].xml")
                        part_name = node.attrib.get("PartName")
                        content_type = node.attrib.get("ContentType")
                        if not part_name or not content_type:
                            raise XlsxImportError("некоректний [Content_Types].xml")
                        normalized_part = part_name.lstrip("/")
                        normalized_type = content_type.lower()
                        if not normalized_part or normalized_part in overrides:
                            raise XlsxImportError("конфліктуюче або дубльоване Override у [Content_Types].xml")
                        overrides[normalized_part] = normalized_type
                        if any(marker in normalized_type for marker in SUSPICIOUS_CONTENT_TYPES):
                            raise XlsxImportError("макроси, ActiveX або embedded objects у XLSX не підтримуються")
                    else:
                        raise XlsxImportError("невідомий елемент у [Content_Types].xml")
                for name in names_set:
                    if name == "[Content_Types].xml":
                        continue
                    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    content_type = overrides.get(name) or defaults.get(extension)
                    if content_type is None:
                        raise XlsxImportError("частина XLSX не має однозначного ContentType")
                    if any(marker in content_type for marker in SUSPICIOUS_CONTENT_TYPES):
                        raise XlsxImportError("небезпечний ContentType у XLSX не підтримується")
                workbook_override = overrides.get("xl/workbook.xml")
                if workbook_override is None:
                    raise XlsxImportError("відсутній обов'язковий Override для xl/workbook.xml")
                if "spreadsheetml.sheet.main" not in workbook_override:
                    raise XlsxImportError("некоректний content type для xl/workbook.xml")
                required_overrides = {"xl/workbook.xml"}
                sheet_overrides = [
                    (part_name, content_type)
                    for part_name, content_type in overrides.items()
                    if part_name.startswith("xl/worksheets/")
                ]
                if not sheet_overrides:
                    raise XlsxImportError("відсутній обов'язковий Override для worksheet")
                root_rels = ElementTree.fromstring(archive.read("_rels/.rels"))
                workbook_rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                if root_rels.tag != _exact_xml_tag(PACKAGE_REL_NAMESPACE, "Relationships"):
                    raise XlsxImportError("некоректний namespace кореневого relationship-файлу")
                if workbook_rels.tag != _exact_xml_tag(PACKAGE_REL_NAMESPACE, "Relationships"):
                    raise XlsxImportError("некоректний namespace workbook relationship-файлу")
                relationships: dict[str, str] = {}
                sheet_relationships: dict[str, str] = {}
                for node in workbook_rels.iter():
                    if node.tag == _exact_xml_tag(PACKAGE_REL_NAMESPACE, "Relationship"):
                        rid = node.attrib.get("Id")
                        target = node.attrib.get("Target")
                        target_mode = node.attrib.get("TargetMode")
                        relationship_type = _normalized_relationship_type(node.attrib.get("Type", ""))
                        if not rid or not target:
                            raise XlsxImportError("relationship має некоректні обов'язкові атрибути")
                        if rid in relationships:
                            raise XlsxImportError("workbook relationship-файл містить дубльований Id")
                        relationships[rid] = target
                        if target_mode and target_mode.lower() == "external":
                            raise XlsxImportError("зовнішні посилання у XLSX не підтримуються")
                        if any(marker in relationship_type for marker in SUSPICIOUS_RELATIONSHIP_TYPES):
                            raise XlsxImportError("OLE, embedded, VBA, ActiveX або external relationship не підтримується")
                        if "/worksheet" in relationship_type:
                            sheet_relationships[rid] = target
                    elif node is not workbook_rels:
                        raise XlsxImportError("невідомий namespace або елемент у workbook relationship-файлу")
                if not relationships:
                    raise XlsxImportError("workbook.xml не має коректних зв'язків з листами")
                root_workbook_found = False
                for node in root_rels.iter():
                    if node is root_rels:
                        continue
                    if node.tag != _exact_xml_tag(PACKAGE_REL_NAMESPACE, "Relationship"):
                        raise XlsxImportError("невідомий namespace або елемент у кореневому relationship-файлі")
                    target = node.attrib.get("Target", "")
                    target_mode = node.attrib.get("TargetMode", "")
                    if target_mode.lower() == "external":
                        raise XlsxImportError("зовнішній relationship у кореневому пакеті XLSX не підтримується")
                    resolved = _resolve_package_target("", target, names_set)
                    relationship_type = _normalized_relationship_type(node.attrib.get("Type", ""))
                    if relationship_type in (
                        "http://schemas.openxmlformats.org/officedocument/2006/relationships/officedocument",
                        "http://purl.oclc.org/ooxml/officedocument/relationships/officedocument",
                    ) and resolved == "xl/workbook.xml":
                        root_workbook_found = True
                if not root_workbook_found:
                    raise XlsxImportError("кореневий пакет не пов'язаний з workbook.xml")
                for rid, target in relationships.items():
                    _resolve_package_target("xl/workbook.xml", target, names_set)
                workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
                if workbook.tag != _exact_xml_tag(SPREADSHEETML_NAMESPACE, "workbook"):
                    raise XlsxImportError("некоректний namespace для xl/workbook.xml")
                sheet_targets: set[str] = set()
                for node in workbook.iter():
                    if node.tag == _exact_xml_tag(SPREADSHEETML_NAMESPACE, "sheet"):
                        rel_id = node.attrib.get(_exact_xml_tag(OFFICE_REL_NAMESPACE, "id"))
                        if not rel_id or rel_id not in relationships:
                            raise XlsxImportError("лист workbook.xml не пов'язаний коректним relationship")
                        target = relationships[rel_id]
                        normalized = _resolve_package_target("xl/workbook.xml", target, names_set)
                        override_type = overrides.get(normalized)
                        if override_type is None:
                            raise XlsxImportError("worksheet не має обов'язкового Override")
                        if "spreadsheetml.worksheet" not in override_type:
                            raise XlsxImportError("некоректний content type для worksheet")
                        sheet_targets.add(rel_id)
                if not sheet_targets:
                    raise XlsxImportError("workbook.xml не містить листів")
            except ImportError as exc:
                raise XlsxImportError("для безпечного читання потрібна defusedxml") from exc
            except XlsxImportError:
                raise
            except Exception as exc:
                raise XlsxImportError("пошкоджені або некоректні OOXML relationships") from exc
    except BadZipFile as exc:
        raise XlsxImportError("файл не є коректним XLSX-архівом") from exc


class OpenpyxlXlsxReader:
    def _open(self, path: Path):
        _preflight(path)
        try:
            from openpyxl import load_workbook
            from openpyxl.xml import DEFUSEDXML
        except ImportError as exc:
            raise XlsxImportError("для імпорту потрібна openpyxl==3.1.5") from exc
        if not DEFUSEDXML:
            raise XlsxImportError("openpyxl має працювати з увімкненою defusedxml")
        return load_workbook(path, read_only=True, data_only=False, keep_links=False)

    def list_sheets(self, path: Path) -> tuple[ImportSheetDto, ...]:
        workbook = self._open(path)
        try:
            if len(workbook.worksheets) > MAX_SHEETS:
                raise XlsxImportError("книга містить забагато листів")
            sheets = []
            for sheet in workbook.worksheets:
                max_row = sheet.max_row or 0
                max_column = sheet.max_column or 0
                if max_row > MAX_ROWS:
                    raise XlsxImportError("лист містить забагато рядків")
                if max_column > MAX_COLUMNS:
                    raise XlsxImportError("лист містить забагато стовпців")
                sheets.append(ImportSheetDto(sheet.title, max_row, max_column))
            return tuple(sheets)
        finally:
            workbook.close()

    def iter_rows(
        self, path: Path, sheet: str, header_row: int, mapping: dict[str, str]
    ) -> Iterable[WorkbookRow]:
        if header_row < 1:
            raise XlsxImportError("рядок заголовків має бути додатним")
        workbook = self._open(path)
        try:
            if len(workbook.worksheets) > MAX_SHEETS:
                raise XlsxImportError("книга містить забагато листів")
            if sheet not in workbook.sheetnames:
                raise XlsxImportError("лист не знайдено")
            worksheet = workbook[sheet]
            if worksheet.max_row and worksheet.max_row > MAX_ROWS:
                raise XlsxImportError("лист містить забагато рядків")
            if worksheet.max_column and worksheet.max_column > MAX_COLUMNS:
                raise XlsxImportError("лист містить забагато стовпців")
            # ReadOnlyWorksheet does not expose merged_cells; _preflight() has already
            # rejected mergeCells XML before openpyxl opens the workbook.
            headers: list[str] = []
            seen: set[str] = set()
            cell_count = 0
            for row_number, row in enumerate(worksheet.iter_rows(), start=1):
                if row_number > MAX_ROWS:
                    raise XlsxImportError("лист містить забагато рядків")
                if len(row) > MAX_COLUMNS:
                    raise XlsxImportError("лист містить забагато стовпців")
                cell_count += len(row)
                if cell_count > MAX_CELLS:
                    raise XlsxImportError("лист містить забагато комірок")
                for cell in row:
                    if isinstance(cell.value, str) and len(cell.value.encode("utf-8")) > MAX_CELL_TEXT_BYTES:
                        raise XlsxImportError("вміст комірки перевищує дозволену довжину")
                if row_number == header_row:
                    headers = [str(cell.value).strip() if cell.value is not None else "" for cell in row]
                    if any(not header or header in seen for header in headers if header):
                        raise XlsxImportError("заголовки мають бути непорожніми та унікальними")
                    seen.update(header for header in headers if header)
                    missing = [column for column in mapping.values() if column not in seen]
                    if missing:
                        raise XlsxImportError("вказаний стовпець відсутній у заголовках")
                    continue
                if row_number <= header_row or not headers:
                    continue
                values: dict[str, Any] = {}
                formulas: set[str] = set()
                for index, cell in enumerate(row):
                    if index >= len(headers) or not headers[index]:
                        continue
                    value = cell.value
                    if isinstance(value, datetime):
                        value = value.isoformat()
                    elif isinstance(value, date):
                        value = value.isoformat()
                    values[headers[index]] = value
                    if cell.data_type == "f":
                        formulas.add(headers[index])
                if not any(value not in (None, "") for value in values.values()):
                    continue
                yield WorkbookRow(row_number, values, frozenset(formulas))
        finally:
            workbook.close()
