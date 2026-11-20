from typing import Protocol
from datetime import datetime
from uuid import UUID

from ..domain.models import (
    AuditEvent,
    ProcurementItem,
    ProcurementItemStatus,
    ReviewRun,
    ReviewRunStatus,
)


class ReviewRunRepository(Protocol):
    def save(self, run: ReviewRun) -> None: ...
    def get(self, run_id: UUID) -> ReviewRun | None: ...
    def list(self) -> list[ReviewRun]: ...
    def transition_status(
        self,
        run_id: UUID,
        expected: ReviewRunStatus,
        target: ReviewRunStatus,
        now: datetime,
    ) -> bool: ...


class ProcurementItemRepository(Protocol):
    def save(self, item: ProcurementItem) -> None: ...
    def get(self, run_id: UUID, item_id: UUID) -> ProcurementItem | None: ...
    def list_for_run(self, run_id: UUID) -> list[ProcurementItem]: ...
    def update_fields(self, item: ProcurementItem) -> bool: ...
    def update_status(
        self,
        item_id: UUID,
        expected: ProcurementItemStatus,
        target: ProcurementItemStatus,
    ) -> bool: ...
    def soft_delete(self, run_id: UUID, item_id: UUID, expected: ProcurementItemStatus) -> bool: ...


class AuditEventRepository(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class PriceCheckerUnitOfWork(Protocol):
    runs: ReviewRunRepository
    items: ProcurementItemRepository
    audit: AuditEventRepository

    def __enter__(self) -> "PriceCheckerUnitOfWork": ...
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> PriceCheckerUnitOfWork: ...

