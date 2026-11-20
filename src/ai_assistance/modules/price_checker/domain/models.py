from dataclasses import dataclass, field
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import json
import math
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DomainError(ValueError):
    """Base error for invalid Price Checker domain state."""


class InvalidTransitionError(DomainError):
    """Raised when an aggregate receives an invalid status transition."""


class ValidationError(DomainError):
    """Raised when a domain value is malformed."""


class ReviewRunStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ProcurementItemStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    FAILED = "failed"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ReviewPolicySnapshot:
    version: str = "price-checker-base-v1"
    required_fields: tuple[str, ...] = ("name",)


@dataclass(frozen=True, slots=True)
class ReviewRun:
    id: UUID = field(default_factory=uuid4)
    status: ReviewRunStatus = ReviewRunStatus.DRAFT
    policy: ReviewPolicySnapshot = field(default_factory=ReviewPolicySnapshot)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    __hash__ = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReviewRunStatus):
            raise ValidationError("review run status must be a ReviewRunStatus")
        if self.status is not ReviewRunStatus.DRAFT:
            raise ValidationError("new review runs must start in DRAFT")

    @classmethod
    def _reconstitute(
        cls,
        *,
        id: UUID,
        status: ReviewRunStatus,
        policy: ReviewPolicySnapshot = ReviewPolicySnapshot(),
        created_at: datetime,
        updated_at: datetime,
    ) -> "ReviewRun":
        """Rebuild a persisted aggregate; call only from a persistence adapter."""
        if not isinstance(status, ReviewRunStatus):
            raise ValidationError("reconstituted review run status must be a ReviewRunStatus")
        run = cls(id=id, policy=policy, created_at=created_at, updated_at=updated_at)
        object.__setattr__(run, "status", status)
        return run

    def transition_to(self, status: ReviewRunStatus) -> None:
        allowed = {
            ReviewRunStatus.DRAFT: {ReviewRunStatus.RUNNING, ReviewRunStatus.CANCELLED},
            ReviewRunStatus.RUNNING: {ReviewRunStatus.COMPLETED, ReviewRunStatus.CANCELLED},
            ReviewRunStatus.CANCELLED: set(),
            ReviewRunStatus.COMPLETED: set(),
        }
        if not isinstance(status, ReviewRunStatus) or status not in allowed[self.status]:
            raise InvalidTransitionError(f"cannot transition review run from {self.status} to {status}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "updated_at", utc_now())


@dataclass(frozen=True, slots=True)
class ProcurementItem:
    name: str
    id: UUID = field(default_factory=uuid4)
    run_id: UUID | None = None
    description: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    status: ProcurementItemStatus = ProcurementItemStatus.DRAFT
    created_at: datetime = field(default_factory=utc_now)
    __hash__ = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProcurementItemStatus):
            raise ValidationError("procurement item status must be a ProcurementItemStatus")
        if self.status is not ProcurementItemStatus.DRAFT:
            raise ValidationError("new procurement items must start in DRAFT")
        if not isinstance(self.name, str):
            raise ValidationError("procurement item name must be a string")
        name = self.name.strip()
        if not name:
            raise ValidationError("procurement item name must not be empty")
        object.__setattr__(self, "name", name)
        if self.quantity is not None:
            if not isinstance(self.quantity, Decimal):
                raise ValidationError("quantity must be a Decimal")
            if not self.quantity.is_finite() or self.quantity <= Decimal("0"):
                raise ValidationError("quantity must be a finite Decimal greater than zero")
        if self.unit is not None:
            if not isinstance(self.unit, str):
                raise ValidationError("unit must be a string")
            unit = self.unit.strip()
            if not unit:
                raise ValidationError("unit must not be empty")
            object.__setattr__(self, "unit", unit)

    @classmethod
    def _reconstitute(
        cls,
        *,
        name: str,
        status: ProcurementItemStatus,
        id: UUID | None = None,
        run_id: UUID | None = None,
        description: str | None = None,
        quantity: Decimal | None = None,
        unit: str | None = None,
        created_at: datetime | None = None,
    ) -> "ProcurementItem":
        """Rebuild a persisted entity; call only from a persistence adapter."""
        if not isinstance(status, ProcurementItemStatus):
            raise ValidationError("reconstituted procurement item status must be a ProcurementItemStatus")
        item = cls(
            name=name,
            id=id if id is not None else uuid4(),
            run_id=run_id,
            description=description,
            quantity=quantity,
            unit=unit,
            created_at=created_at if created_at is not None else utc_now(),
        )
        object.__setattr__(item, "status", status)
        return item

    def transition_to(self, status: ProcurementItemStatus) -> None:
        allowed = {
            ProcurementItemStatus.DRAFT: {ProcurementItemStatus.PENDING},
            ProcurementItemStatus.PENDING: {ProcurementItemStatus.FAILED, ProcurementItemStatus.COMPLETED},
            ProcurementItemStatus.FAILED: {ProcurementItemStatus.PENDING},
            ProcurementItemStatus.COMPLETED: set(),
        }
        if not isinstance(status, ProcurementItemStatus) or status not in allowed[self.status]:
            raise InvalidTransitionError(f"cannot transition procurement item from {self.status} to {status}")
        object.__setattr__(self, "status", status)


def _freeze_json_value(value: Any, ancestors: set[int]) -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValidationError("audit event data must not contain non-finite floats")
        return value
    if type(value) not in (dict, list):
        raise ValidationError("audit event data contains an unsupported JSON value")

    value_id = id(value)
    if value_id in ancestors:
        raise ValidationError("audit event data must not contain circular references")
    ancestors.add(value_id)
    try:
        if type(value) is list:
            return tuple(_freeze_json_value(item, ancestors) for item in value)

        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValidationError("audit event data dictionary keys must be strings")
            frozen[key] = _freeze_json_value(item, ancestors)
        return MappingProxyType(frozen)
    finally:
        ancestors.remove(value_id)


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    data: Mapping[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValidationError("audit event type must not be empty")
        if not isinstance(self.aggregate_type, str) or not self.aggregate_type.strip():
            raise ValidationError("audit aggregate type must not be empty")
        if not isinstance(self.data, Mapping):
            raise ValidationError("audit event data must be a mapping")
        frozen = _freeze_json_value(dict(self.data), set())
        object.__setattr__(self, "data", frozen)

    def to_json(self) -> str:
        return json.dumps(_thaw_json_value(self.data), allow_nan=False)

