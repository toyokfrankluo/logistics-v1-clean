"""Read-only query orchestration. Authorization stays at the caller boundary.

verified=True is an explicit protocol approval supplied by policy configuration,
not inferred from a successfully parsed response. Only synthetic policies are
used in this stage. No retries, caching, persistence or hidden fallback calls.
"""
from .adapters import parse_response
from .policies import plan_attempt
from .service_types import QueryPlan, QueryOutcome, SourceOutcome, Status


def resolve_query(request, repository):
    if request.audience not in {'public', 'backend'}:
        return QueryPlan(request.audience, None, blocked_reason='invalid_audience')
    if (request.local_id is None) == (request.customer_number is None):
        return QueryPlan(request.audience, None, blocked_reason='invalid_identity')
    if request.local_id is not None:
        shipment = repository.by_id(request.local_id)
    else:
        matches = repository.by_customer_number(request.customer_number)
        if len(matches) > 1:
            return QueryPlan(request.audience, None, blocked_reason='ambiguous_identity')
        shipment = matches[0] if matches else None
    if shipment is None:
        return QueryPlan(request.audience, None, blocked_reason='not_found')
    if ((request.local_id is not None and shipment.local_id != request.local_id)
            or (request.customer_number is not None and shipment.customer_number != request.customer_number)):
        return QueryPlan(request.audience, None, blocked_reason='identity_mismatch')
    sources = []
    if not request.local_only:
        if request.audience == 'public' and shipment.third_party_number and shipment.third_party:
            sources.append(shipment.third_party)
        if shipment.agent:
            sources.append(shipment.agent)
    attempts = tuple(plan_attempt(s, shipment) for s in sources)
    if len({a.source.source_id for a in attempts}) != len(attempts):
        return QueryPlan(request.audience, shipment, blocked_reason='duplicate_source_identity')
    return QueryPlan(request.audience, shipment, attempts, local_only=request.local_only)


def query_tracking(plan, response_provider):
    shipment = plan.shipment
    manual = ()
    if shipment:
        # Only repeated references to the same local record are collapsed.
        seen = set()
        manual = tuple(m for m in shipment.manual
                       if m.record_id not in seen and not seen.add(m.record_id))
    number = shipment.customer_number if shipment else None
    if plan.blocked_reason:
        return QueryOutcome(Status.BLOCKED, number, manual, error=plan.blocked_reason)
    # Rebuild the canonical plan from the trusted snapshot, then compare every attempt.
    class SnapshotRepository:
        def by_id(self, key): return shipment
    from .service_types import QueryRequest
    if shipment is None or type(plan.local_only) is not bool:
        return QueryOutcome(Status.BLOCKED, number, error='invalid_plan')
    canonical = resolve_query(QueryRequest(plan.audience, local_id=shipment.local_id,
                                           local_only=plan.local_only), SnapshotRepository())
    if canonical.blocked_reason:
        return QueryOutcome(Status.BLOCKED, number, manual, error=canonical.blocked_reason)
    if plan.attempts != canonical.attempts:
        reason = 'invalid_plan'
        if len(plan.attempts) == len(canonical.attempts) == 1:
            a, b = plan.attempts[0], canonical.attempts[0]
            if a.source == b.source and a.number != b.number:
                reason = 'query_number_mismatch'
            elif a.source == b.source and b.blocked_reason:
                reason = b.blocked_reason
        return QueryOutcome(Status.BLOCKED, number, manual, error=reason)
    if plan.local_only or not plan.attempts:
        return QueryOutcome(Status.SUCCESS if manual else Status.EMPTY, number, manual,
                            (SourceOutcome('external', Status.NOT_REQUESTED),))
    outcomes = []
    for planned in plan.attempts:
        # Recheck policy at execution too; manually constructed plans cannot bypass it.
        checked = plan_attempt(planned.source, shipment)
        attempt = checked if checked.blocked_reason else planned
        if not attempt.blocked_reason and attempt.number != checked.number:
            attempt = type(attempt)(attempt.source, attempt.number, "query_number_mismatch")
        if attempt.blocked_reason:
            outcomes.append(SourceOutcome(attempt.source.source_id, Status.BLOCKED,
                                          error=attempt.blocked_reason))
            # Blocking is never an authorization/protocol fallback opportunity.
            break
        try:
            response = response_provider.fetch(attempt)
            result = parse_response(attempt.source.provider, attempt.source.source_id,
                                    response.http_status, response.body)
            state = Status(result.status.value)
            outcomes.append(SourceOutcome(attempt.source.source_id, state, result,
                                          result.error.value if result.error else None))
        except TimeoutError:
            outcomes.append(SourceOutcome(attempt.source.source_id, Status.ERROR, error='timeout'))
        except Exception:
            # Never expose transport exception text (may contain credentials).
            outcomes.append(SourceOutcome(attempt.source.source_id, Status.ERROR, error='transport_or_adapter'))
        last = outcomes[-1]
        if last.status in {Status.SUCCESS, Status.EMPTY, Status.UNVERIFIED}:
            return QueryOutcome(Status.SUCCESS if last.status == Status.EMPTY and manual else last.status,
                                number, manual, tuple(outcomes), last.result)
        # Public third-party failure -> associated agent, as in the old route.
        # Backend has only one source. No fallback on empty or unverified.
    last = outcomes[-1]
    return QueryOutcome(last.status, number, manual, tuple(outcomes), error=last.error)
