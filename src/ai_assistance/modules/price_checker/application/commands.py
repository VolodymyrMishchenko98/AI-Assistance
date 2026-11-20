from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CreateReviewRunCommand:
    pass


@dataclass(frozen=True, slots=True)
class AddProcurementItemCommand:
    run_id: UUID
    name: str
    description: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class CreateImportPreviewCommand:
    run_id: UUID
    source_path: str
    source_fingerprint: str
    idempotency_key: str
    sheet: str
    header_row: int
    mapping: dict[str, str]


@dataclass(frozen=True, slots=True)
class UpdateImportRowCommand:
    preview_id: UUID
    row_id: UUID
    name: str | None
    quantity: Decimal | None
    unit: str | None
    description: str | None
    run_id: UUID


@dataclass(frozen=True, slots=True)
class ExcludeImportRowCommand:
    preview_id: UUID
    row_id: UUID
    run_id: UUID


@dataclass(frozen=True, slots=True)
class ConfirmImportCommand:
    preview_id: UUID
    idempotency_key: str
    run_id: UUID
    acknowledge_warnings: bool = False

