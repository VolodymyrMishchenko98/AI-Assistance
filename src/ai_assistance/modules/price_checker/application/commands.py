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

