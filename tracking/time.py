"""Conservative timestamps: never infer host timezone or fabricate event time."""
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from .types import EventTime, TimePrecision


def parse_time(raw, *, source_timezone=None, epoch_seconds=False):
    zone = ZoneInfo(source_timezone) if source_timezone else None
    if raw is None or raw == '':
        return EventTime(raw, issue='missing_time')
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return EventTime(None, issue='invalid_time_type')
    if isinstance(raw, (int, float)):
        if not epoch_seconds:
            return EventTime(raw, issue='numeric_time_unit_unconfirmed')
        try:
            if not math.isfinite(raw):
                raise ValueError()
            value = datetime.fromtimestamp(raw, timezone.utc)
            return EventTime(raw, value, precision=TimePrecision.SUBSECOND if isinstance(raw, float) and not raw.is_integer() else TimePrecision.SECOND, timezone='UTC')
        except (ValueError, OverflowError, OSError):
            return EventTime(raw, issue='invalid_epoch')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        try:
            datetime.strptime(raw, '%Y-%m-%d')
            return EventTime(raw, precision=TimePrecision.DAY, issue='date_only')
        except ValueError:
            return EventTime(raw, issue='invalid_time')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', raw):
        return EventTime(raw, issue='unrecognized_time_format')
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return EventTime(raw, issue='invalid_time')
    clock = raw[11:]
    precision = TimePrecision.SUBSECOND if '.' in clock else (
        TimePrecision.SECOND if re.match(r'\d{2}:\d{2}:\d{2}', clock) else TimePrecision.MINUTE)
    if value.tzinfo is not None:
        return EventTime(raw, value.astimezone(timezone.utc), precision=precision, timezone=str(value.tzinfo))
    # DST gaps/folds cannot safely identify an instant without explicit offset.
    if zone:
        a, b = value.replace(tzinfo=zone, fold=0), value.replace(tzinfo=zone, fold=1)
        if a.utcoffset() != b.utcoffset():
            return EventTime(raw, local_time=value, precision=precision, timezone=source_timezone, issue='ambiguous_or_nonexistent_local_time')
        return EventTime(raw, a.astimezone(timezone.utc), value, precision, source_timezone)
    return EventTime(raw, local_time=value, precision=precision, issue='timezone_unconfirmed')
