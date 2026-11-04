"""Explicit source policies; URL guessing and customer-number fallbacks forbidden."""
from .service_types import Attempt

NUMBER_KINDS = {'rtb56': 'customer', 'txfba': 'customer',
                'nextsls': 'provider', '17track': 'third_party'}


def plan_attempt(source, shipment):
    reason = None
    if any(type(v) is not bool for v in (source.active, source.supports_api, source.verified)):
        reason = 'invalid_source_flags'
    elif not source.active:
        reason = 'inactive_source'
    elif not source.supports_api:
        reason = 'api_disabled'
    elif not source.verified:
        reason = 'protocol_unverified'
    elif source.provider not in NUMBER_KINDS:
        reason = 'unsupported_protocol'
    elif source.number_kind != NUMBER_KINDS[source.provider]:
        reason = 'number_policy_mismatch'
    number = {'customer': shipment.customer_number,
              'provider': shipment.provider_number,
              'third_party': shipment.third_party_number}.get(source.number_kind)
    if not reason and (not isinstance(number, str) or not number.strip()):
        reason = 'missing_query_number'
    return Attempt(source, number, reason)
