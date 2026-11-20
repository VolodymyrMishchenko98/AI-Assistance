from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from ..domain.models import ProcurementItemStatus, ReviewRunStatus


@dataclass(frozen=True, slots=True)
class ReviewRunDto:
    id: UUID
    status: ReviewRunStatus
    created_at: datetime
    item_count: int


@dataclass(frozen=True, slots=True)
class ProcurementItemDto:
    id: UUID
    run_id: UUID
    name: str
    description: str | None
    quantity: Decimal | None
    unit: str | None
    status: ProcurementItemStatus

