from .commands import AddProcurementItemCommand, CreateReviewRunCommand
from .dto import ProcurementItemDto, ReviewRunDto
from .ports import UnitOfWorkFactory
from ..domain.models import AuditEvent, ProcurementItem, ReviewRun


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
            item = ProcurementItem(
                name=command.name,
                run_id=run.id,
                description=command.description,
                quantity=command.quantity,
                unit=command.unit,
            )
            uow.items.save(item)
            uow.audit.append(AuditEvent("procurement_item_created", "procurement_item", item.id))
            return ProcurementItemDto(item.id, run.id, item.name, item.description, item.quantity, item.unit, item.status)

    def dashboard(self) -> list[ReviewRunDto]:
        with self._unit_of_work() as uow:
            return [
                ReviewRunDto(run.id, run.status, run.created_at, len(uow.items.list_for_run(run.id)))
                for run in uow.runs.list()
            ]

