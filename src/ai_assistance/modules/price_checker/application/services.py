from .commands import (
    AddProcurementItemCommand,
    CancelReviewRunCommand,
    CompleteReviewRunCommand,
    ConfirmImportCommand,
    CreateImportPreviewCommand,
    CreateReviewRunCommand,
    ExcludeImportRowCommand,
    StartReviewRunCommand,
    UpdateImportRowCommand,
)
from .dto import ProcurementItemDto, ReviewRunDto
from .errors import ConcurrencyError
from .imports import ImportOperationStatus, ImportPreviewDto, ImportRowDto, ImportRowStatus
from .import_ports import XlsxReader
from .ports import UnitOfWorkFactory
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4
from ..domain.models import (
    AuditEvent,
    InvalidTransitionError,
    ProcurementItem,
    ProcurementItemStatus,
    ReviewRun,
    ReviewRunStatus,
    ValidationError,
)


class PriceCheckerApplication:
    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        xlsx_reader: XlsxReader | None = None,
        *,
        write_unit_of_work: UnitOfWorkFactory | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._write_unit_of_work = write_unit_of_work or unit_of_work
        self._xlsx_reader = xlsx_reader

    def create_review_run(self, command: CreateReviewRunCommand) -> ReviewRunDto:
        with self._unit_of_work() as uow:
            run = ReviewRun()
            uow.runs.save(run)
            uow.audit.append(AuditEvent("review_run_created", "review_run", run.id))
            return ReviewRunDto(run.id, run.status, run.created_at, 0)

    def add_procurement_item(self, command: AddProcurementItemCommand) -> ProcurementItemDto:
        with self._unit_of_work() as uow:
            run = uow.runs.get(command.run_id)
            if run is None:
                raise LookupError(f"review run not found: {command.run_id}")
            if run.status is not ReviewRunStatus.DRAFT:
                raise ValidationError("procurement items can only be added to a draft review run")
            if command.quantity is None:
                raise ValidationError("quantity is required")
            if command.unit is None or not command.unit.strip():
                raise ValidationError("unit must not be empty")
            item = ProcurementItem(
                name=command.name,
                run_id=run.id,
                description=command.description,
                quantity=command.quantity,
                unit=command.unit,
            )
            uow.items.save(item)
            uow.audit.append(AuditEvent(
                "procurement_item_created", "procurement_item", item.id,
                {"run_id": str(run.id), "item_id": str(item.id)},
            ))
            return ProcurementItemDto(
                item.id, run.id, item.name, item.description, item.quantity,
                item.unit, item.status, item.created_at,
            )

    def list_procurement_items(self, run_id: UUID) -> list[ProcurementItemDto]:
        with self._unit_of_work() as uow:
            if uow.runs.get(run_id) is None:
                raise LookupError(f"review run not found: {run_id}")
            return [
                ProcurementItemDto(
                    item.id, run_id, item.name, item.description, item.quantity,
                    item.unit, item.status, item.created_at,
                )
                for item in uow.items.list_for_run(run_id)
            ]

    def dashboard(self) -> list[ReviewRunDto]:
        with self._unit_of_work() as uow:
            return [
                ReviewRunDto(run.id, run.status, run.created_at, len(uow.items.list_for_run(run.id)))
                for run in uow.runs.list()
            ]

    def start_review_run(self, command: StartReviewRunCommand) -> ReviewRunDto:
        with self._write_unit_of_work() as uow:
            run = uow.runs.get(command.run_id)
            if run is None:
                raise LookupError(f"review run not found: {command.run_id}")
            if run.status is not ReviewRunStatus.DRAFT:
                raise InvalidTransitionError(
                    f"cannot start review run from {run.status.value}"
                )
            items = uow.items.list_for_run(command.run_id)
            if not items:
                raise ValidationError("cannot start a review run without items")
            if any(item.status is not ProcurementItemStatus.DRAFT for item in items):
                raise InvalidTransitionError("all items must be draft before the run starts")
            run.transition_to(ReviewRunStatus.RUNNING)
            now = datetime.now(timezone.utc)
            if not uow.runs.transition_status(
                command.run_id, ReviewRunStatus.DRAFT, ReviewRunStatus.RUNNING, now
            ):
                raise ConcurrencyError("review run changed concurrently")
            for item in items:
                item.transition_to(ProcurementItemStatus.PENDING)
                if not uow.items.update_status(
                    item.id, ProcurementItemStatus.DRAFT, ProcurementItemStatus.PENDING
                ):
                    raise ConcurrencyError("procurement item changed concurrently")
            uow.audit.append(AuditEvent(
                "review_run_started",
                "review_run",
                command.run_id,
                {"item_count": len(items)},
            ))
            return ReviewRunDto(run.id, run.status, run.created_at, len(items))

    def complete_review_run(self, command: CompleteReviewRunCommand) -> ReviewRunDto:
        with self._write_unit_of_work() as uow:
            run = uow.runs.get(command.run_id)
            if run is None:
                raise LookupError(f"review run not found: {command.run_id}")
            if run.status is not ReviewRunStatus.RUNNING:
                raise InvalidTransitionError(
                    f"cannot complete review run from {run.status.value}"
                )
            items = uow.items.list_for_run(command.run_id)
            if any(item.status is not ProcurementItemStatus.COMPLETED for item in items):
                raise ValidationError("all items must be completed before completing the run")
            run.transition_to(ReviewRunStatus.COMPLETED)
            now = datetime.now(timezone.utc)
            if not uow.runs.transition_status(
                command.run_id, ReviewRunStatus.RUNNING, ReviewRunStatus.COMPLETED, now
            ):
                raise ConcurrencyError("review run changed concurrently")
            uow.audit.append(AuditEvent(
                "review_run_completed",
                "review_run",
                command.run_id,
                {"item_count": len(items)},
            ))
            return ReviewRunDto(run.id, run.status, run.created_at, len(items))

    def cancel_review_run(self, command: CancelReviewRunCommand) -> ReviewRunDto:
        with self._write_unit_of_work() as uow:
            run = uow.runs.get(command.run_id)
            if run is None:
                raise LookupError(f"review run not found: {command.run_id}")
            previous = run.status
            run.transition_to(ReviewRunStatus.CANCELLED)
            now = datetime.now(timezone.utc)
            if not uow.runs.transition_status(
                command.run_id, previous, ReviewRunStatus.CANCELLED, now
            ):
                raise ConcurrencyError("review run changed concurrently")
            uow.audit.append(AuditEvent(
                "review_run_cancelled",
                "review_run",
                command.run_id,
                {"previous_status": previous.value},
            ))
            return ReviewRunDto(run.id, run.status, run.created_at, len(uow.items.list_for_run(command.run_id)))

    def create_import_preview(self, command: CreateImportPreviewCommand) -> ImportPreviewDto:
        try:
            return self._create_import_preview(command)
        except Exception as exc:
            now = datetime.now(timezone.utc)
            failed = ImportPreviewDto(
                id=uuid4(), run_id=command.run_id, status=ImportOperationStatus.FAILED,
                sheets=(), selected_sheet=command.sheet, header_row=command.header_row, rows=(),
                source_fingerprint=command.source_fingerprint, idempotency_key=command.idempotency_key,
                final_fingerprint=None, created_at=now, expires_at=now + timedelta(hours=24),
                diagnostic=f"{type(exc).__name__}: {str(exc)[:256]}",
            )
            try:
                with self._unit_of_work() as uow:
                    if uow.runs.get(command.run_id) is not None and uow.previews.find_by_key(command.run_id, command.idempotency_key) is None:
                        uow.previews.create(failed)
            except Exception:
                pass
            raise

    def _create_import_preview(self, command: CreateImportPreviewCommand) -> ImportPreviewDto:
        if self._xlsx_reader is None:
            raise RuntimeError("XLSX reader is not configured")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=24)
        path = Path(command.source_path)
        with self._unit_of_work() as uow:
            run = uow.runs.get(command.run_id)
            if run is None:
                raise LookupError(f"review run not found: {command.run_id}")
            if run.status is not ReviewRunStatus.DRAFT:
                raise ValidationError("imports are allowed only for draft review runs")
            existing = uow.previews.find_by_key(command.run_id, command.idempotency_key)
            if existing is not None:
                if existing.source_fingerprint != command.source_fingerprint:
                    raise ValidationError("idempotency key conflicts with another file")
                return existing
            sheets = self._xlsx_reader.list_sheets(path)
            duplicate_keys: set[tuple[str, str, str]] = set()
            rows: list[ImportRowDto] = []
            for workbook_row in self._xlsx_reader.iter_rows(path, command.sheet, command.header_row, command.mapping):
                values = workbook_row.values
                name = values.get(command.mapping.get("name", ""))
                quantity_value = values.get(command.mapping.get("quantity", ""))
                unit = values.get(command.mapping.get("unit", ""))
                description = values.get(command.mapping.get("description", "")) if command.mapping.get("description") else None
                errors: list[str] = []
                warnings: list[str] = []
                if command.mapping.get("name") in workbook_row.formula_fields:
                    errors.append("формула в назві не підтримується")
                if command.mapping.get("quantity") in workbook_row.formula_fields:
                    errors.append("формула в кількості не підтримується")
                if command.mapping.get("unit") in workbook_row.formula_fields:
                    errors.append("формула в одиниці виміру не підтримується")
                if command.mapping.get("description") in workbook_row.formula_fields:
                    warnings.append("характеристики містять формулу та потребують перевірки")
                name_text = str(name).strip() if name is not None else ""
                unit_text = str(unit).strip() if unit is not None else ""
                quantity = self._parse_quantity(quantity_value, errors)
                if not name_text:
                    errors.append("порожня назва")
                if not unit_text:
                    errors.append("порожня одиниця виміру")
                if quantity is None:
                    errors.append("некоректна кількість")
                key = (name_text.casefold(), str(quantity), unit_text.casefold())
                if key in duplicate_keys:
                    warnings.append("дублювання рядка")
                duplicate_keys.add(key)
                status = ImportRowStatus.INVALID if errors else ImportRowStatus.WARNING if warnings else ImportRowStatus.VALID
                rows.append(ImportRowDto(
                    uuid4(), workbook_row.excel_row_number, values, name_text or None,
                    quantity, unit_text or None, str(description) if description is not None else None,
                    tuple(errors), tuple(warnings), status,
                ))
            preview = ImportPreviewDto(
                id=uuid4(), run_id=command.run_id, status=ImportOperationStatus.PREVIEW_READY,
                sheets=sheets, selected_sheet=command.sheet, header_row=command.header_row, rows=tuple(rows),
                source_fingerprint=command.source_fingerprint, idempotency_key=command.idempotency_key,
                final_fingerprint=None, created_at=now, expires_at=expires_at,
            )
            uow.previews.create(preview)
            return preview

    @staticmethod
    def _parse_quantity(value: object, errors: list[str]) -> Decimal | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        try:
            text = str(value).strip().replace(",", ".")
            quantity = Decimal(text)
            if not quantity.is_finite() or quantity <= 0:
                return None
            return quantity
        except (InvalidOperation, ValueError):
            return None

    def get_import_preview(self, preview_id: UUID, run_id: UUID) -> ImportPreviewDto:
        with self._unit_of_work() as uow:
            preview = uow.previews.get(preview_id)
            if preview is None:
                raise LookupError("import preview not found")
            if preview.run_id != run_id:
                raise LookupError("import preview belongs to another review run")
            return preview

    def update_import_row(self, command: UpdateImportRowCommand) -> ImportPreviewDto:
        now = datetime.now(timezone.utc)
        with self._unit_of_work() as uow:
            preview = self._require_editable_preview(uow, command.preview_id)
            if preview.run_id != command.run_id:
                raise LookupError("import preview belongs to another review run")
            preview = uow.previews.get_for_run(command.preview_id, preview.run_id)
            if preview is None:
                raise LookupError("import preview not found")
            old = next((row for row in preview.rows if row.id == command.row_id), None)
            if old is None:
                raise LookupError("preview row not found")
            errors: list[str] = []
            quantity = self._parse_quantity(command.quantity, errors)
            if not command.name or not command.name.strip(): errors.append("порожня назва")
            if not command.unit or not command.unit.strip(): errors.append("порожня одиниця виміру")
            if quantity is None: errors.append("некоректна кількість")
            row = ImportRowDto(old.id, old.excel_row_number, old.raw_values, command.name.strip() if command.name else None, quantity, command.unit.strip() if command.unit else None, command.description, tuple(errors), (), ImportRowStatus.INVALID if errors else ImportRowStatus.VALID)
            if not uow.previews.update_row(command.preview_id, row, now + timedelta(hours=24), now):
                raise ValidationError("import preview changed or expired during update")
            return uow.previews.get(command.preview_id)

    def exclude_import_row(self, command: ExcludeImportRowCommand) -> ImportPreviewDto:
        now = datetime.now(timezone.utc)
        with self._unit_of_work() as uow:
            preview = self._require_editable_preview(uow, command.preview_id)
            if preview.run_id != command.run_id:
                raise LookupError("import preview belongs to another review run")
            if not uow.previews.exclude_row(command.preview_id, command.row_id, now + timedelta(hours=24), now):
                raise ValidationError("import preview changed or expired during exclusion")
            return uow.previews.get(command.preview_id)

    def confirm_import(self, command: ConfirmImportCommand) -> tuple[UUID, ...]:
        now = datetime.now(timezone.utc)
        with self._unit_of_work() as uow:
            preview = uow.previews.get(command.preview_id)
            if preview is None: raise LookupError("import preview not found")
            if preview.run_id != command.run_id:
                raise LookupError("import preview belongs to another review run")
            if preview.idempotency_key != command.idempotency_key: raise ValidationError("idempotency key mismatch")
            if preview.status is ImportOperationStatus.CONFIRMED: return preview.created_item_ids
            if preview.status is not ImportOperationStatus.PREVIEW_READY or now >= preview.expires_at: raise ValidationError("import preview expired or unavailable")
            run = uow.runs.get(preview.run_id)
            if run is None: raise LookupError("review run not found")
            if run.status is not ReviewRunStatus.DRAFT: raise ValidationError("review run is not draft")
            if any(row.status is ImportRowStatus.INVALID for row in preview.rows): raise ValidationError("invalid rows must be fixed or excluded")
            if any(row.status is ImportRowStatus.WARNING for row in preview.rows) and not command.acknowledge_warnings: raise ValidationError("warnings require explicit confirmation")
            final_fingerprint = self._fingerprint(preview.rows)
            if preview.final_fingerprint is not None and preview.final_fingerprint != final_fingerprint: raise ValidationError("preview fingerprint conflict")
            if not uow.previews.save_final_fingerprint(preview.id, final_fingerprint, now):
                confirmed_preview = uow.previews.get(preview.id)
                if confirmed_preview is not None and confirmed_preview.status is ImportOperationStatus.CONFIRMED:
                    return confirmed_preview.created_item_ids
                raise ValidationError("import preview changed or expired during confirmation")
            item_ids: list[UUID] = []
            for row in preview.rows:
                if row.status is ImportRowStatus.EXCLUDED: continue
                item = ProcurementItem(name=row.name or "", run_id=run.id, description=row.description, quantity=row.quantity, unit=row.unit)
                item_ids.append(item.id)
            if not uow.previews.mark_confirmed(preview.id, tuple(item_ids), now, final_fingerprint):
                confirmed_preview = uow.previews.get(preview.id)
                if confirmed_preview is not None and confirmed_preview.status is ImportOperationStatus.CONFIRMED:
                    return confirmed_preview.created_item_ids
                raise ValidationError("import preview changed or expired during confirmation")
            for row, item_id in zip(
                (row for row in preview.rows if row.status is not ImportRowStatus.EXCLUDED),
                item_ids,
            ):
                item = ProcurementItem(id=item_id, name=row.name or "", run_id=run.id, description=row.description, quantity=row.quantity, unit=row.unit)
                uow.items.save(item)
                uow.audit.append(AuditEvent("procurement_item_created", "procurement_item", item.id, {"run_id": str(run.id), "item_id": str(item.id), "import_id": str(preview.id)}))
            return tuple(item_ids)

    @staticmethod
    def _fingerprint(rows: tuple[ImportRowDto, ...]) -> str:
        payload = [(str(row.id), row.name, str(row.quantity) if row.quantity is not None else None, row.unit, row.description, row.status.value) for row in rows]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _require_editable_preview(uow, preview_id: UUID) -> ImportPreviewDto:
        preview = uow.previews.get(preview_id)
        if preview is None: raise LookupError("import preview not found")
        if preview.status is not ImportOperationStatus.PREVIEW_READY or datetime.now(timezone.utc) >= preview.expires_at: raise ValidationError("import preview expired or unavailable")
        return preview

    def cleanup_imports(self) -> int:
        with self._unit_of_work() as uow:
            return uow.previews.delete_expired(datetime.now(timezone.utc))

