from typing import Protocol
from uuid import UUID

from ..domain.models import AuditEvent, ProcurementItem, ReviewRun


class ReviewRunRepository(Protocol):
    def save(self, run: ReviewRun) -> None: ...
    def get(self, run_id: UUID) -> ReviewRun | None: ...
    def list(self) -> list[ReviewRun]: ...


class ProcurementItemRepository(Protocol):
    def save(self, item: ProcurementItem) -> None: ...
    def list_for_run(self, run_id: UUID) -> list[ProcurementItem]: ...


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

