"""Conservative legacy rendering: impossible conversions are explicit outcomes."""
from datetime import datetime
from .service_types import LegacyPresentation, Status, QueryOutcome, SourceOutcome, ManualEvent
from .types import QueryResult, QueryStatus, TrackingEvent, EventTime, LogisticsStatus, TimePrecision, ErrorType


def _event_valid(event):
    if type(event) is not TrackingEvent or type(event.time) is not EventTime:
        return False
    t = event.time
    return (type(event.provider) is str and bool(event.provider)
            and type(event.source_id) is str and bool(event.source_id)
            and type(event.location) is str and type(event.description) is str
            and type(event.status) is LogisticsStatus and event.status == LogisticsStatus.UNKNOWN
            and type(t.precision) is TimePrecision
            and (t.raw is None or type(t.raw) in (str, int, float))
            and (t.local_time is None or type(t.local_time) is datetime)
            and (t.occurred_at is None or type(t.occurred_at) is datetime))


def _result_valid(result):
    if type(result) is not QueryResult or type(result.status) is not QueryStatus:
        return False
    if (type(result.source_id) is not str or not result.source_id
            or type(result.provider) is not str or not result.provider
            or type(result.events) is not tuple
            or (result.error is not None and type(result.error) is not ErrorType)):
        return False
    if result.status in {QueryStatus.SUCCESS, QueryStatus.EMPTY}:
        if result.error is not None:
            return False
        if (result.status == QueryStatus.SUCCESS) != bool(result.events):
            return False
    if result.status == QueryStatus.ERROR and (result.events or result.error is None):
        return False
    return all(_event_valid(e) and e.source_id == result.source_id
               and e.provider == result.provider for e in result.events)


def _validate_outcome(outcome):
    """Fail closed before rendering; never repair or mutate caller-owned results."""
    if type(outcome) is not QueryOutcome or type(outcome.status) is not Status:
        return 'invalid_outcome_status_or_structure'
    if (type(outcome.manual) is not tuple or type(outcome.sources) is not tuple
            or type(outcome.customer_number) is not str or not outcome.customer_number.strip()):
        return 'invalid_outcome_structure'
    ids = set()
    for m in outcome.manual:
        if (type(m) is not ManualEvent or type(m.record_id) is not int or m.record_id <= 0
                or m.record_id in ids or not _event_valid(m.event) or m.event.provider != 'manual'):
            return 'invalid_manual_history'
        ids.add(m.record_id)
    sources = outcome.sources
    ids = set()
    for source in sources:
        if (type(source) is not SourceOutcome or type(source.status) is not Status
                or type(source.source_id) is not str or not source.source_id or source.source_id in ids):
            return 'invalid_or_duplicate_source'
        ids.add(source.source_id)
        if source.status in {Status.ERROR, Status.BLOCKED} and (
                type(source.error) is not str or not source.error):
            return 'missing_source_failure_reason'
        if source.status == Status.NOT_REQUESTED and (source.result is not None or source.error is not None):
            return 'inconsistent_not_requested'
        if source.result is not None:
            r = source.result
            if (not _result_valid(r) or r.source_id != source.source_id
                    or r.status.value != source.status.value
                    or source.error != (r.error.value if r.error else None)):
                return 'inconsistent_source_result'
        elif source.status in {Status.SUCCESS, Status.EMPTY, Status.UNVERIFIED}:
            return 'missing_source_result'
    if outcome.status not in {Status.SUCCESS, Status.EMPTY}:
        return 'non_success_requires_legacy_mapping'
    if outcome.error is not None:
        return 'success_with_error'
    selected = outcome.selected
    if selected is None:
        # Local-history mode must explicitly say that external querying was not requested.
        if (len(sources) != 1 or sources[0].status != Status.NOT_REQUESTED
                or sources[0].result is not None or sources[0].error is not None
                or outcome.status != (Status.SUCCESS if outcome.manual else Status.EMPTY)):
            return 'unexplained_local_result'
        return None
    if not _result_valid(selected) or selected.status not in {QueryStatus.SUCCESS, QueryStatus.EMPTY}:
        return 'invalid_selected_result'
    matches = [i for i, source in enumerate(sources) if source.source_id == selected.source_id]
    if len(matches) != 1:
        return 'selected_source_missing'
    index = matches[0]
    if sources[index].result != selected:
        return 'selected_result_mismatch'
    # Only the approved failure -> selected-success/empty sequence is representable.
    # Never merge unverified candidates, parallel successes or unaccounted later results.
    if (index != len(sources) - 1 or len(sources) > 2
            or any(source.status != Status.ERROR for source in sources[:index])):
        return 'unsupported_multi_source_combination'
    expected = Status.SUCCESS if selected.events or outcome.manual else Status.EMPTY
    if outcome.status != expected:
        return 'inconsistent_overall_status'
    return None


def present_legacy(outcome, contract):
    reason = _validate_outcome(outcome)
    if reason:
        return LegacyPresentation(False, incompatibilities=(reason,))
    if contract not in {'public_json', 'public_html', 'backend'}:
        return LegacyPresentation(False, incompatibilities=('unsupported_contract_or_raw_envelope',))
    if outcome.status == Status.UNVERIFIED:
        return LegacyPresentation(False, incompatibilities=('unverified_has_no_legacy_success_representation',))
    events = outcome.selected.events if outcome.selected else ()
    if outcome.selected and outcome.selected.provider not in {'rtb56', 'txfba'}:
        return LegacyPresentation(False, incompatibilities=('provider_text_contract_unconfirmed',))
    if outcome.status == Status.EMPTY:
        # Old supplier cnmessage can determine public empty text; adapter discards it.
        if contract != 'backend':
            return LegacyPresentation(False, incompatibilities=('supplier_empty_message_unavailable',))
    failed = outcome.status in {Status.ERROR, Status.BLOCKED}
    if failed:
        return LegacyPresentation(False, incompatibilities=('legacy_error_message_requires_approved_mapping',))
    error = None
    if contract != 'backend' and outcome.manual:
        return LegacyPresentation(False, incompatibilities=('public_manual_history_policy_unresolved',))
    def raw_time(event):
        return '' if event.time.raw is None else str(event.time.raw)
    if contract == 'backend':
        lines = []
        for m in outcome.manual:
            e = m.event
            # Legacy minute formatting cannot be recovered from arbitrary time text.
            if e.time.local_time is None:
                return LegacyPresentation(False, incompatibilities=('manual_minute_time_unavailable',))
            lines.append(f"{e.time.local_time.strftime('%Y-%m-%d %H:%M')} {e.location} {e.description}")
        lines.extend(f'{raw_time(e)} {e.location} {e.description}' for e in events)
        payload = {'tracks': '\n'.join(lines) if lines else '暂无轨迹', 'error': error}
    else:
        text = '\n\n'.join((' — '.join(x for x in (e.location.strip(), e.description.strip()) if x)
                             + '\n' + raw_time(e).strip()).strip() for e in events)
        payload = {'tracks': [] if failed and contract == 'public_json' else None if failed else text,
                   'error': error}
        if contract == 'public_json':
            payload = {'order_id': outcome.customer_number, **payload}
    return LegacyPresentation(True, payload)
