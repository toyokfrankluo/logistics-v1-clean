"""Offline response adapters derived from existing app.py/views.py only.

Input is HTTP status + JSON text already obtained by a caller. No transport or
routing is included. Success markers are typed allowlists, never truthiness.
"""
import json
from .time import parse_time
from .types import ErrorType, QueryResult, QueryStatus, TrackingEvent

PENDING = ('supplier_protocol_not_verified', 'status_mapping_unconfirmed', 'external_event_id_mapping_unconfirmed')


def _matches(value, allowed):
    return any(type(value) is type(candidate) and value == candidate for candidate in allowed)


def parse_response(provider, source_id, http_status, body, *, source_timezone=None, expected_number=None):
    """Providers: rtb56, txfba, nextsls, 17track, generic.

    Generic envelopes without explicit recognized success markers are unverified,
    even if events can be extracted. Unknown fields are discarded, not logged.
    A response must represent one shipment; multi-shipment arrays are rejected.
    """
    if provider not in {'rtb56', 'txfba', 'nextsls', '17track', 'generic'}:
        raise ValueError('Unsupported provider')
    if not isinstance(source_id, str) or not source_id:
        raise ValueError('source_id is required')
    def result(status, events=(), error=None, message=None, pending=()):
        official_ok = expected_number is not None and status in (QueryStatus.SUCCESS, QueryStatus.EMPTY)
        shipment_status = 'in_transit' if official_ok and data['data'][0].get('track_status') == 'NT' else 'unknown'
        return QueryResult(provider, source_id, status, tuple(events), error, message, PENDING + pending,
                           expected_number if official_ok else None, shipment_status)
    def fail(error, message):
        return result(QueryStatus.ERROR, error=error, message=message)
    if type(http_status) is not int or http_status != 200:
        return fail(ErrorType.HTTP, 'HTTP response rejected')
    try:
        if not isinstance(body, (str, bytes, bytearray)):
            raise ValueError()
        data = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        return fail(ErrorType.INVALID_JSON, 'Response is not valid JSON')
    if not isinstance(data, dict):
        return fail(ErrorType.SCHEMA, 'Expected object envelope')
    # Official RTB56 profile is opt-in to preserve offline legacy comparisons.
    # expected_number must come from independently confirmed service-number mapping.
    if expected_number is not None:
        if provider != 'rtb56' or not isinstance(expected_number, str) or not expected_number.strip():
            return fail(ErrorType.SCHEMA, 'Invalid expected service number')
        if type(data.get('success')) is not int or data['success'] not in (0, 1):
            return fail(ErrorType.BUSINESS, 'RTB56 requires integer success')
        if data['success'] == 0:
            return fail(ErrorType.BUSINESS, 'Supplier reported failure')
        batches = data.get('data')
        if batches == []:
            return result(QueryStatus.UNVERIFIED, error=ErrorType.UNCONFIRMED_PROTOCOL,
                          message='Empty data identity and meaning unconfirmed')
        if (not isinstance(batches, list) or len(batches) != 1
                or not isinstance(batches[0], dict)
                or batches[0].get('server_hawbcode') != expected_number):
            return fail(ErrorType.SCHEMA, 'RTB56 response identity mismatch')
    markers = {
        'success': (1, '1'),
        'status': (1, '1', 200, '200', True) if provider == 'nextsls' else (200,),
        'code': (200,),
    }
    required = {'rtb56': 'success', 'txfba': 'success', 'nextsls': 'status', '17track': 'status'}.get(provider)
    if required and required not in data:
        return fail(ErrorType.SCHEMA, 'Missing success marker')
    # Explicit errors take precedence over apparently valid event data.
    if data.get('error') not in (None, '', False):
        return fail(ErrorType.BUSINESS, 'Supplier reported failure')
    for key, allowed in markers.items():
        if required and key != required:
            continue
        if key in data and not _matches(data[key], allowed):
            return fail(ErrorType.BUSINESS, 'Supplier success marker rejected')
    verified = any(key in data for key in markers)
    pending = []
    try:
        if provider in {'rtb56', 'txfba', '17track'}:
            batches = data['data']
            if not isinstance(batches, list) or len(batches) != 1 or not isinstance(batches[0], dict):
                raise ValueError()
            events = batches[0]['track' if provider == '17track' else 'details']
        elif provider == 'nextsls':
            if isinstance(data.get('data'), dict):
                events = data['data']['shipment']['traces']
            elif 'tracks' in data:
                events = data['tracks']
                pending.append('alternate_nextsls_shape_unconfirmed')
            else:
                events = data['data']
                pending.append('alternate_nextsls_shape_unconfirmed')
        else:
            pending.append('generic_field_semantics_unconfirmed')
            if 'tracks' in data:
                events = data['tracks']
            elif 'data' in data:
                events = data['data']
            else:
                events = data['result']['list']
        if not isinstance(events, list):
            raise ValueError()
        normalized = []
        for event in events:
            if not isinstance(event, dict):
                raise ValueError()
            if provider in {'rtb56', 'txfba'}:
                raw_time = event.get('track_occur_date')
                location = event.get('track_location', '')
                description = event.get('track_description', event.get('track_description_en', ''))
                if expected_number is not None and (description is None or description == ''):
                    description = event.get('track_description_en', '')
            else:
                raw_time = event.get('time', event.get('occur_date', event.get('happen_time')))
                location = event.get('location', '')
                description = event.get('info', event.get('description', '')) if provider == 'nextsls' else event.get('description', event.get('info', ''))
            # A missing description cannot be silently reported as a valid event.
            if not isinstance(description, str) or not description.strip() or not isinstance(location, (str, type(None))):
                raise ValueError()
            normalized.append(TrackingEvent(provider, source_id,
                parse_time(raw_time, source_timezone=source_timezone, epoch_seconds=provider == 'nextsls'),
                location or '', description))
    except (KeyError, TypeError, ValueError):
        return fail(ErrorType.SCHEMA, 'Malformed or unsupported event structure')
    if not verified:
        return result(QueryStatus.UNVERIFIED, normalized, ErrorType.UNCONFIRMED_PROTOCOL,
                      'Generic success convention is unconfirmed', tuple(pending))
    return result(QueryStatus.SUCCESS if normalized else QueryStatus.EMPTY, normalized, pending=tuple(pending))
