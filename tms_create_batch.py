"""Persist a confirmed set of existing single-order grants and dispatch each once."""
from uuid import uuid4

from flask import abort, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import IntegrityError

import nextsls
import tms_carriers as carriers
import tms_create_control as control
from models import CarrierAgent, db
from tms_models import TmsAgentApi, TmsCreateBatch, TmsCreateBatchItem, TmsOrder, TmsSupplierOrder

LIMIT = 50
TABLES = (TmsCreateBatch.__table__, TmsCreateBatchItem.__table__)


def init_app(app):
    # Render's existing databases receive only additive tables.
    import os
    app.config["TMS_BATCH_ENABLED"] = app.config["TMS_ENABLED"] and (
        os.getenv("RENDER") == "true" or app.config.get("ISOLATED_MODE"))
    if app.config["TMS_BATCH_ENABLED"] and not app.config.get("ISOLATED_MODE"):
        with app.app_context(), db.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(651360348)"))
            if inspect(conn).has_table("tms_order"):
                for table in TABLES:
                    table.create(conn, checkfirst=True)


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="tms-create-batch")


def eligibility(order, *, preflight=True, confirm_business=False):
    shipment, agent, profile, link = carriers.operation_context(order, "create")
    if shipment.shipment_id or (link and (link.state != "new" or link.attempt_at)):
        raise nextsls.ProtocolError("本票已有提交记录，不纳入新批次。")
    approval = carriers.sample_create_approval(order, profile, agent)
    blocker = carriers.supplier_sample_blocker(order.invoice, approval)
    if blocker:
        raise nextsls.ProtocolError(blocker)
    grant = carriers.current_create_grant(order, profile, approval)
    if (not grant or grant.get("fees_and_pickup_confirmed") is not True) and (
            not confirm_business or order.invoice.get("workbook_test_sample")):
        raise nextsls.ProtocolError("本票缺少有效的单票授权。")
    if preflight:
        blockers = carriers.supplier_preparation(order, profile, agent)["blockers"]
        if blockers:
            raise nextsls.ProtocolError("；".join(blockers) + "。")
    return [order.id, order.revision, agent.id, order.agent_service]


def ids(raw):
    if len(raw) > 1000:
        abort(400)
    try:
        values = list(dict.fromkeys(int(value) for value in raw.split(",") if value.strip()))
    except ValueError:
        abort(400)
    if not values or len(values) > LIMIT or any(not 0 < value < 2**31 for value in values):
        abort(400)
    return values


def own_batch(batch_id):
    batch = db.session.get(TmsCreateBatch, batch_id)
    if not batch or batch.actor_id != current_user.id:
        abort(404)
    return batch


def register_routes(staff):
    @staff.before_request
    def batch_scope():
        if request.endpoint in {"tms_staff.create_batches", "tms_staff.create_batch_detail",
                                "tms_staff.run_batch_item"} and not current_app.config["TMS_BATCH_ENABLED"]:
            abort(404)

    @staff.route("/nextsls-batches", methods=["GET", "POST"])
    def create_batches():
        if not all(inspect(db.engine).has_table(table.name) for table in TABLES):
            abort(503, "批量下单表尚未初始化。")
        agents = db.session.execute(select(CarrierAgent.id, CarrierAgent.name).join(
            TmsAgentApi, TmsAgentApi.agent_id == CarrierAgent.id
        ).where(TmsAgentApi.provider == "nextsls", CarrierAgent.is_active.is_(True))
          .order_by(CarrierAgent.name)).all()
        actor = current_user.id
        latest = db.session.scalars(select(TmsCreateBatch).where(
            TmsCreateBatch.actor_id == actor).order_by(TmsCreateBatch.created_at.desc()).limit(20)).all()
        rows, rejected, confirmation, next_after, unknown_ids = [], [], None, None, []
        if request.method == "POST" and request.form.get("action") == "confirm":
            try:
                version, who, batch_id, manifest = signer().loads(request.form.get("preview", ""), max_age=600)
                if version != 2:
                    raise BadData("stale preview")
            except (BadData, ValueError):
                abort(409, "批次预览已过期，请重新核对。")
            if who != actor or request.form.get("confirmed") != "yes":
                abort(403)
            if db.session.get(TmsCreateBatch, batch_id):
                return redirect(url_for("tms_staff.create_batch_detail", batch_id=batch_id))
            if not control.enabled():
                abort(409, "站点真实下单许可尚未开启。")
            staged = []
            for snapshot, digest in manifest:
                order = db.session.get(TmsOrder, snapshot[0], with_for_update=True)
                try:
                    if order is None or eligibility(order, confirm_business=True) != snapshot:
                        raise nextsls.ProtocolError("订单版本、渠道或授权已变化。")
                    profile = db.session.get(TmsAgentApi, order.agent_id)
                    rules = carriers.channel_rules(profile, order.agent_service)
                    if nextsls.rules_digest(rules) != digest:
                        raise nextsls.ProtocolError("渠道规则已变化，请重新预览。")
                    agent = db.session.get(CarrierAgent, order.agent_id)
                    grant = carriers.current_create_grant(order, profile,
                        carriers.sample_create_approval(order, profile, agent))
                    if not grant or grant.get("fees_and_pickup_confirmed") is not True:
                        invoice = carriers.make_create_grant(order, profile, actor,
                            preparation={"sample_approval": None}, batch_id=batch_id,
                            unknown_confirmed=request.form.get("unknown_confirmed") == "yes",
                            unknown_basis=request.form.get("unknown_basis", ""))
                        staged.append((order, invoice))
                        rows.append([snapshot[0], snapshot[1] + 1, *snapshot[2:]])
                    else:
                        rows.append(snapshot)
                except nextsls.ProtocolError as error:
                    rejected.append((snapshot[0], str(error)))
            # Do not silently change the confirmed set. No requests are made on this route.
            if not rejected:
                from tms_portal import history, update_order
                for order, invoice in staged:
                    update_order(order, {"invoice": invoice})
                    history(order, "supplier_grant")
                db.session.add(TmsCreateBatch(id=batch_id, actor_id=actor))
                db.session.add_all(TmsCreateBatchItem(batch_id=batch_id, order_id=o, revision=r,
                                                      agent_id=a, channel=c) for o, r, a, c in rows)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    if not db.session.get(TmsCreateBatch, batch_id):
                        abort(409, "其中订单已进入批次，请查看既有批次。")
                return redirect(url_for("tms_staff.create_batch_detail", batch_id=batch_id, start=1))
        elif request.method == "POST" and request.form.get("action") == "preview":
            chosen = set(ids(",".join(request.form.getlist("agents"))))
            if not chosen <= {agent.id for agent in agents}:
                abort(400)
            after = request.form.get("after", 0, type=int)
            if not 0 <= after < 2**31:
                abort(400)
            candidates = db.session.scalars(select(TmsOrder).where(
                TmsOrder.agent_id.in_(chosen), TmsOrder.status == "approved", TmsOrder.id > after,
                TmsOrder.invoice["order_archive"]["at"].as_string().is_(None),
                ~select(TmsCreateBatchItem.order_id).where(TmsCreateBatchItem.order_id == TmsOrder.id,
                    TmsCreateBatchItem.revision == TmsOrder.revision).exists()
            ).order_by(TmsOrder.id).limit(LIMIT + 1)).all()
            if len(candidates) > LIMIT:
                next_after = candidates[LIMIT - 1].id
            manifest = []
            for order in candidates[:LIMIT]:
                try:
                    snapshot = eligibility(order, confirm_business=True)
                    profile = db.session.get(TmsAgentApi, order.agent_id)
                    rules = carriers.channel_rules(profile, order.agent_service)
                    if rules.get("unknown_rules"):
                        unknown_ids.append(order.id)
                    rows.append(snapshot)
                    manifest.append([snapshot, nextsls.rules_digest(rules)])
                except nextsls.ProtocolError as error:
                    rejected.append((order.id, str(error)))
            if rows:
                confirmation = signer().dumps([2, actor, uuid4().hex, manifest])
        return render_template("tms/create_batch.html", staff=True, agents=agents, rows=rows,
                               rejected=rejected, preview=confirmation, latest=latest, enabled=control.enabled(),
                               unknown_ids=unknown_ids, next_after=next_after, chosen=request.form.getlist("agents"))

    @staff.get("/nextsls-batches/<batch_id>")
    def create_batch_detail(batch_id):
        batch = own_batch(batch_id)
        items = db.session.scalars(select(TmsCreateBatchItem).where(
            TmsCreateBatchItem.batch_id == batch_id).order_by(TmsCreateBatchItem.order_id)).all()
        return render_template("tms/create_batch.html", staff=True, batch=batch, items=items,
                               enabled=control.enabled(), start=request.args.get("start") == "1")

    @staff.post("/nextsls-batches/<batch_id>/items/<int:order_id>")
    def run_batch_item(batch_id, order_id):
        batch = own_batch(batch_id)
        item = db.session.get(TmsCreateBatchItem, (batch_id, order_id))
        if not item:
            abort(404)
        action = request.form.get("action")
        if action not in {"run", "query"}:
            abort(400)
        return dispatch(item, batch.actor_id, query_only=action == "query")


def dispatch(item, actor, *, query_only=False):
    batch_id, order_id = item.batch_id, item.order_id
    snapshot = [order_id, item.revision, item.agent_id, item.channel]
    if not query_only:
        if item.state != "queued":
            return jsonify(state=item.state)
        if not control.enabled():
            return jsonify(state="queued"), 409
        claim = db.session.execute(update(TmsCreateBatchItem).where(
            TmsCreateBatchItem.batch_id == batch_id, TmsCreateBatchItem.order_id == order_id,
            TmsCreateBatchItem.state == "queued").values(state="processing"))
        if claim.rowcount != 1:
            db.session.rollback()
            return jsonify(state="processing")
        db.session.commit()
    elif item.state not in {"processing", "uncertain", "linked", "queried"}:
        abort(409, "该订单尚未提交。")

    number, detail, state = None, None, "blocked"
    try:
        order = db.session.get(TmsOrder, order_id)
        if not query_only:
            if eligibility(order, preflight=False) != snapshot:
                raise nextsls.ProtocolError("订单版本或授权已变化。")
            state = carriers.send_order(order, actor, require_grant=True, expected_snapshot=snapshot)
        else:
            state = item.state
        link = db.session.get(TmsSupplierOrder, order_id)
        if link and link.state in {"sending", "uncertain", "not_found"}:
            state = "uncertain"
            try:
                event = carriers.latest_event(order_id, "attempt")
                rejected = bool(event and event.details.get("supplier_rejected"))
                if rejected:
                    detail = "供应商明确拒绝；保持锁定。请在订单记录查看原因。"
                if carriers.reconcile_order(order, actor) == "linked":
                    state = "linked"
                elif not rejected:
                    detail = "对账暂未找到；保持锁定，请勿重发。"
            except nextsls.ProtocolError as error:
                db.session.rollback()
                detail = f"{detail} 对账：{error}" if detail else str(error)
        link = db.session.get(TmsSupplierOrder, order_id)
        if link and link.state == "linked":
            state, number = "linked", link.supplier_number
            try:
                _, channel, reference = carriers.query_linked_shipment(order)
                if channel and reference:
                    state = "queried"
                else:
                    detail = "已查到单号，但渠道或客户参考号未确认。"
            except nextsls.ProtocolError as error:
                db.session.rollback()
                detail = str(error)
        elif query_only and not link:
            return jsonify(state=state), 409
    except nextsls.ProtocolError as error:
        db.session.rollback()
        detail = str(error)
    except Exception as error:
        db.session.rollback()
        current_app.logger.warning("NextSLS batch item interrupted (%s)", type(error).__name__)
        state, detail = "uncertain", "处理意外中断；请查原提交记录，不会自动重发。"
    item = db.session.get(TmsCreateBatchItem, (batch_id, order_id), with_for_update=True)
    if item.state == "queried" and state != "queried":
        return jsonify(state=item.state)
    item.state, item.number, item.detail = state, number, (detail or "")[:500]
    db.session.commit()
    return jsonify(state=state)
