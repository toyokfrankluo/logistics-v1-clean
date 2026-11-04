"""Pure value types; no Flask, database, network, credentials or raw envelopes."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class QueryStatus(str, Enum):
    SUCCESS = 'success'
    EMPTY = 'empty'
    ERROR = 'error'
    UNVERIFIED = 'unverified'


class ErrorType(str, Enum):
    HTTP = 'http'
    INVALID_JSON = 'invalid_json'
    BUSINESS = 'business'
    SCHEMA = 'schema'
    UNCONFIRMED_PROTOCOL = 'unconfirmed_protocol'


class LogisticsStatus(str, Enum):
    UNKNOWN = 'unknown'


class TimePrecision(str, Enum):
    UNKNOWN = 'unknown'
    DAY = 'day'
    MINUTE = 'minute'
    SECOND = 'second'
    SUBSECOND = 'subsecond'


@dataclass(frozen=True)
class EventTime:
    raw: str | int | float | None
    occurred_at: datetime | None = None  # UTC only, if an absolute instant is known.
    local_time: datetime | None = None
    precision: TimePrecision = TimePrecision.UNKNOWN
    timezone: str | None = None
    issue: str | None = None


@dataclass(frozen=True)
class TrackingEvent:
    provider: str
    source_id: str
    time: EventTime
    location: str
    description: str
    status: LogisticsStatus = LogisticsStatus.UNKNOWN
    external_event_id: str | None = None


@dataclass(frozen=True)
class QueryResult:
    provider: str
    source_id: str
    status: QueryStatus
    events: tuple[TrackingEvent, ...] = ()
    error: ErrorType | None = None
    message: str | None = None
    pending_confirmation: tuple[str, ...] = ()
    service_number: str | None = None
    shipment_status: str = 'unknown'
