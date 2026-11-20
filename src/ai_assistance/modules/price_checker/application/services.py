from .commands import AddProcurementItemCommand, CreateReviewRunCommand
from .dto import ProcurementItemDto, ReviewRunDto
from .ports import UnitOfWorkFactory
from uuid import UUID
from ..domain.models import AuditEvent, ProcurementItem, ReviewRun, ReviewRunStatus, ValidationError


class PriceCheckerApplication:
    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
    ) -> None:
        self._unit_of_work = unit_of_work

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

