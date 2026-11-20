import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from src.ai_assistance.modules.price_checker.domain.models import (
    AuditEvent,
    InvalidTransitionError,
    ProcurementItem,
    ProcurementItemStatus,
    ReviewRun,
    ReviewRunStatus,
    ValidationError,
)


class DomainTests(unittest.TestCase):
    def test_item_requires_name(self) -> None:
        with self.assertRaises(ValidationError):
            ProcurementItem("   ")

    def test_quantity_uses_decimal_and_must_be_positive(self) -> None:
        item = ProcurementItem("SSD", quantity=Decimal("2.50"))
        self.assertEqual(item.quantity, Decimal("2.50"))
        with self.assertRaises(ValidationError):
            ProcurementItem("SSD", quantity=Decimal("0"))

    def test_quantity_rejects_non_decimal_and_non_finite_values(self) -> None:
        for value in (1.0, Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.assertRaises(ValidationError):
                ProcurementItem("SSD", quantity=value)

    def test_review_run_state_machine(self) -> None:
        run = ReviewRun()
        run.transition_to(ReviewRunStatus.RUNNING)
        run.transition_to(ReviewRunStatus.COMPLETED)
        with self.assertRaises(InvalidTransitionError):
            run.transition_to(ReviewRunStatus.CANCELLED)

    def test_new_review_run_cannot_start_in_a_non_initial_state(self) -> None:
        for status in (
            ReviewRunStatus.RUNNING,
            ReviewRunStatus.CANCELLED,
            ReviewRunStatus.COMPLETED,
        ):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                ReviewRun(status=status)

    def test_review_run_status_cannot_be_assigned_directly(self) -> None:
        run = ReviewRun()
        with self.assertRaises(AttributeError):
            setattr(run, "status", ReviewRunStatus.COMPLETED)

    def test_all_review_run_transitions_and_invalid_transitions(self) -> None:
        allowed = {
            ReviewRunStatus.DRAFT: {ReviewRunStatus.RUNNING, ReviewRunStatus.CANCELLED},
            ReviewRunStatus.RUNNING: {ReviewRunStatus.COMPLETED, ReviewRunStatus.CANCELLED},
            ReviewRunStatus.CANCELLED: set(),
            ReviewRunStatus.COMPLETED: set(),
        }
        for source, targets in allowed.items():
            for target in ReviewRunStatus:
                run = self._reconstituted_run(source)
                if target in targets:
                    run.transition_to(target)
                    self.assertEqual(run.status, target)
                else:
                    with self.subTest(source=source, target=target):
                        with self.assertRaises(InvalidTransitionError):
                            run.transition_to(target)

    def test_procurement_item_state_machine(self) -> None:
        item = ProcurementItem("SSD")
        item.transition_to(ProcurementItemStatus.PENDING)
        item.transition_to(ProcurementItemStatus.FAILED)
        item.transition_to(ProcurementItemStatus.PENDING)
        item.transition_to(ProcurementItemStatus.COMPLETED)
        with self.assertRaises(InvalidTransitionError):
            item.transition_to(ProcurementItemStatus.DRAFT)

    def test_new_procurement_item_cannot_start_in_a_non_initial_state(self) -> None:
        with self.assertRaises(ValidationError):
            ProcurementItem("SSD", status=ProcurementItemStatus.COMPLETED)

    def test_procurement_item_status_cannot_be_assigned_directly(self) -> None:
        item = ProcurementItem("SSD")
        with self.assertRaises(AttributeError):
            setattr(item, "status", ProcurementItemStatus.COMPLETED)

    def test_all_procurement_item_transitions_and_invalid_transitions(self) -> None:
        allowed = {
            ProcurementItemStatus.DRAFT: {ProcurementItemStatus.PENDING},
            ProcurementItemStatus.PENDING: {
                ProcurementItemStatus.FAILED,
                ProcurementItemStatus.COMPLETED,
            },
            ProcurementItemStatus.FAILED: {ProcurementItemStatus.PENDING},
            ProcurementItemStatus.COMPLETED: set(),
        }
        for source, targets in allowed.items():
            for target in ProcurementItemStatus:
                item = self._reconstituted_item(source)
                if target in targets:
                    item.transition_to(target)
                    self.assertEqual(item.status, target)
                else:
                    with self.subTest(source=source, target=target):
                        with self.assertRaises(InvalidTransitionError):
                            item.transition_to(target)

    def test_reconstitution_runs_regular_field_validation(self) -> None:
        with self.assertRaises(ValidationError):
            ProcurementItem._reconstitute(
                name="   ",
                status=ProcurementItemStatus.COMPLETED,
            )
        with self.assertRaises(ValidationError):
            ProcurementItem._reconstitute(
                name="SSD",
                quantity=Decimal("0"),
                status=ProcurementItemStatus.COMPLETED,
            )

    def test_audit_event_copies_nested_source_dictionaries(self) -> None:
        data = {"nested": {"value": "original"}}
        event = AuditEvent("created", "run", ReviewRun().id, data)
        data["nested"]["value"] = "changed"
        self.assertEqual(event.data["nested"]["value"], "original")

    def test_audit_event_copies_nested_source_lists(self) -> None:
        data = {"items": [{"value": "original"}]}
        event = AuditEvent("created", "run", ReviewRun().id, data)
        data["items"][0]["value"] = "changed"
        data["items"].append("later")
        self.assertEqual(event.data["items"], ({"value": "original"},))

    def test_nested_audit_data_cannot_be_mutated_through_event(self) -> None:
        event = AuditEvent(
            "created",
            "run",
            ReviewRun().id,
            {"nested": {"value": "original"}, "items": ["first"]},
        )
        with self.assertRaises(TypeError):
            event.data["nested"]["value"] = "changed"
        with self.assertRaises(TypeError):
            event.data["items"][0] = "changed"

    def test_audit_event_serializes_frozen_data_to_json(self) -> None:
        data = {
            "null": None,
            "bool": True,
            "int": 7,
            "float": 1.25,
            "string": "text",
            "nested": {"items": [1, {"ok": False}]},
        }
        event = AuditEvent("created", "run", ReviewRun().id, data)
        self.assertEqual(json.loads(event.to_json()), data)

    def test_audit_event_rejects_unsupported_json_values(self) -> None:
        invalid_values = (
            float("nan"),
            float("inf"),
            float("-inf"),
            {1: "not a string key"},
            {"nested": {1: "not a string key"}},
            {"nested": (1, 2)},
            {"nested": {1, 2}},
            {"nested": object()},
        )
        for value in invalid_values:
            with self.subTest(value=repr(value)), self.assertRaises(ValidationError):
                AuditEvent("created", "run", ReviewRun().id, {"value": value})
        with self.assertRaises(ValidationError):
            AuditEvent("created", "run", ReviewRun().id, {1: "not a string key"})

    def _reconstituted_run(self, status: ReviewRunStatus) -> ReviewRun:
        now = datetime.now(timezone.utc)
        return ReviewRun._reconstitute(
            id=uuid4(),
            status=status,
            created_at=now,
            updated_at=now,
        )

    def _reconstituted_item(self, status: ProcurementItemStatus) -> ProcurementItem:
        return ProcurementItem._reconstitute(name="SSD", status=status)

    def test_audit_event_data_is_immutable_and_json_serializable(self) -> None:
        data = {"source": "unit"}
        event = AuditEvent("created", "run", ReviewRun().id, data)
        data["source"] = "changed"
        self.assertEqual(event.data["source"], "unit")
        with self.assertRaises(TypeError):
            event.data["new"] = "value"
        with self.assertRaises(ValidationError):
            AuditEvent("created", "run", ReviewRun().id, {"bad": object()})

