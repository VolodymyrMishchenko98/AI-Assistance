from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID


class ImportOperationStatus(StrEnum):
    UPLOADED = "uploaded"
    PREVIEW_READY = "preview_ready"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    FAILED = "failed"


class ImportRowStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    WARNING = "warning"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class ImportSheetDto:
    name: str
    max_row: int
    max_column: int


@dataclass(frozen=True, slots=True)
class ImportRowDto:
    id: UUID
    excel_row_number: int
    raw_values: dict[str, Any]
    name: str | None
    quantity: Decimal | None
    unit: str | None
    description: str | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    status: ImportRowStatus


@dataclass(frozen=True, slots=True)
class ImportPreviewDto:
    id: UUID
    run_id: UUID
    status: ImportOperationStatus
    sheets: tuple[ImportSheetDto, ...]
    selected_sheet: str | None
    header_row: int | None
    rows: tuple[ImportRowDto, ...]
    source_fingerprint: str
    idempotency_key: str
    final_fingerprint: str | None
    created_at: datetime
    expires_at: datetime
    diagnostic: str | None = None
    created_item_ids: tuple[UUID, ...] = ()
