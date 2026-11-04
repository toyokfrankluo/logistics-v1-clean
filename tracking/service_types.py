"""Immutable snapshots and service outcomes, independent of any application runtime."""
from dataclasses import dataclass
from enum import Enum
from .types import TrackingEvent, QueryResult


class Status(str, Enum):
    SUCCESS = 'success'
    EMPTY = 'empty'
    ERROR = 'error'
    UNVERIFIED = 'unverified'
    BLOCKED = 'blocked'
    NOT_REQUESTED = 'not_requested'


@dataclass(frozen=True)
class Source:
    source_id: str
    provider: str
    number_kind: str
    verified: bool = False
    active: bool = True
    supports_api: bool = True


@dataclass(frozen=True)
class ManualEvent:
    record_id: int
    event: TrackingEvent


@dataclass(frozen=True)
class ShipmentSnapshot:
    local_id: int
    customer_number: str
    provider_number: str | None = None
    third_party_number: str | None = None
    agent: Source | None = None
    third_party: Source | None = None
    manual: tuple[ManualEvent, ...] = ()


@dataclass(frozen=True)
class QueryRequest:
    audience: str
    local_id: int | None = None
    customer_number: str | None = None
    local_only: bool = False


@dataclass(frozen=True)
class Attempt:
    source: Source
    number: str | None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class QueryPlan:
    audience: str
    shipment: ShipmentSnapshot | None
    attempts: tuple[Attempt, ...] = ()
    blocked_reason: str | None = None
    local_only: bool = False


@dataclass(frozen=True)
class SourceOutcome:
    source_id: str
    status: Status
    result: QueryResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class QueryOutcome:
    status: Status
    customer_number: str | None
    manual: tuple[ManualEvent, ...] = ()
    sources: tuple[SourceOutcome, ...] = ()
    selected: QueryResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class Response:
    http_status: int
    body: str


@dataclass(frozen=True)
class LegacyPresentation:
    compatible: bool
    payload: object = None
    http_status: int = 200
    incompatibilities: tuple[str, ...] = ()
