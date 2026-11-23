"""Administrator-only NextSLS integration center. Never sends shipment/create itself."""
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from flask import abort, current_app, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import inspect, or_, select, text
from sqlalchemy.exc import IntegrityError

import nextsls
import tms_carriers as carriers
import tms_create_control as control
from models import CarrierAgent, Customer, db
from tms_invoice import InvoiceError, MAX_UPLOAD_BYTES, parse_invoice, product_photos
from tms_models import TmsAccount, TmsAgentApi, TmsNextslsTestPolicy, TmsOrder, TmsSupplierOrder

TEST_CUSTOMER = "NextSLS 联调专用（非客户委托）"
PATH = "/nextsls-lab"


def init_app(app):
    import os
    app.config["TMS_NEXTSLS_LAB_ENABLED"] = app.config["TMS_ENABLED"] and (
        os.getenv("RENDER") == "true" or app.config.get("ISOLATED_MODE"))
    if app.config["TMS_NEXTSLS_LAB_ENABLED"] and not app.config.get("ISOLATED_MODE"):
        with app.app_context(), db.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(651360348)"))
            if inspect(conn).has_table("tms_order"):
                TmsNextslsTestPolicy.__table__.create(conn, checkfirst=True)


def now():
    return int(datetime.now(timezone.utc).timestamp())


def account_binding(profile, agent, code):
    return {"id": agent.id, "origin": profile.base_url, "service": code,
            "token_sha256": sha256(agent.app_token.encode()).hexdigest()}


def applicable(policy, profile, agent, code):
    return bool(policy and policy.revoked_by is None and policy.expires_at > now()
                and profile and agent and agent.is_active and agent.app_token
                and profile.provider == "nextsls" and profile.agent_id == agent.id
                and account_binding(profile, agent, code) in policy.scope)


def approval_for(order, profile, agent):
    """Return the shape consumed by the existing per-order authorization and send checks."""
    case = order.invoice.get("nextsls_lab_case")
    if not isinstance(case, dict) or order.status != "approved":
        return None
    policy = db.session.get(TmsNextslsTestPolicy, case.get("policy_id"), populate_existing=True)
    if (not applicable(policy, profile, agent, order.agent_service)
            or case.get("binding") != account_binding(profile, agent, order.agent_service)
            or case.get("file_sha256") != order.file_sha256 or case.get("fees_confirmed") is not True
            or not order.workbook or sha256(order.workbook).hexdigest() != order.file_sha256):
        return None
    return {"evidence_id": policy.id, "digest": sha256(repr((policy.id, policy.expires_at, order.id,
            order.file_sha256, case["binding"], policy.fees)).encode()).hexdigest()}


def checked_account(agent_id):
    agent = db.session.get(CarrierAgent, agent_id) if agent_id else None
    profile = db.session.get(TmsAgentApi, agent_id) if agent else None
    error = carriers.channel_setup_error(profile, agent) if agent and profile else "请选择已配置的 NextSLS 代理。"
    if error or profile.provider != "nextsls":
        raise nextsls.ProtocolError(error or "请选择 NextSLS V5 代理。")
    return agent, profile


def register_routes(staff):
    @staff.route(PATH, methods=["GET", "POST"])
    def nextsls_lab():
        if not current_app.config.get("TMS_NEXTSLS_LAB_ENABLED"):
            abort(404)
        if not inspect(db.engine).has_table(TmsNextslsTestPolicy.__tablename__):
            abort(503, "联调中心数据库尚未就绪。")
        output, error, selected, catalogue = None, None, None, {}
        action = request.form.get("action") if request.method == "POST" else None
        try:
            if action == "policy":
                if request.form.get("no_pickup") != "yes" or request.form.get("evidence_confirm") != "yes":
                    raise nextsls.ProtocolError("请明确核对测试范围与不揽收、不实际承运条件。")
                evidence = nextsls.supplied_text(request.form.get("evidence"), 500)
                fees = nextsls.supplied_text(request.form.get("fees"), 200)
                fee_type = request.form.get("fee_type")
                if fee_type not in {"free", "confirmed"}:
                    raise nextsls.ProtocolError("费用未明确，不能登记实际联调依据。")
                fees = ("免计费：" if fee_type == "free" else "费用已另行获准：") + fees[:180]
                days = request.form.get("days", type=int)
                if days is None or not 1 <= days <= 366:
                    raise nextsls.ProtocolError("有效期应为 1–366 天。")
                scope = []
                lines = list(dict.fromkeys(request.form.get("scope", "").split()))
                if not 1 <= len(lines) <= 1000:
                    raise nextsls.ProtocolError("请填写 1–1000 个代理 ID:渠道代码。")
                pairs = []
                for line in lines:
                    aid, _, code = line.partition(":")
                    if not aid.isascii() or not aid.isdecimal() or len(aid) > 9 or not nextsls.CODE.fullmatch(code):
                        raise nextsls.ProtocolError("联调范围须为代理 ID:渠道代码。")
                    pairs.append((int(aid), code))
                bound_accounts = {agent.id: (agent, profile) for agent, profile in db.session.execute(
                    select(CarrierAgent, TmsAgentApi).join(TmsAgentApi)
                    .where(CarrierAgent.id.in_({aid for aid, _ in pairs}))).all()}
                for aid, code in pairs:
                    matched = bound_accounts.get(aid)
                    if not matched or carriers.channel_setup_error(matched[1], matched[0]):
                        raise nextsls.ProtocolError(f"#{aid} 不属于已启用并配置的 NextSLS 账号。")
                    agent, profile = matched
                    if code not in carriers.channel_catalogue(profile):
                        raise nextsls.ProtocolError(f"#{aid} 渠道未在有效清单中；先读取该账号渠道。")
                    scope.append(account_binding(profile, agent, code))
                db.session.add(TmsNextslsTestPolicy(id=uuid4().hex, actor_id=current_user.id,
                    evidence=evidence, scope=scope, fees=fees, expires_at=now() + days * 86400))
                db.session.commit()
                return redirect(url_for("tms_staff.nextsls_lab"))
            if action == "revoke":
                policy = db.session.get(TmsNextslsTestPolicy, request.form.get("policy_id"), with_for_update=True)
                if not policy:
                    abort(404)
                if policy.revoked_by is None:
                    policy.revoked_by = current_user.id
                    db.session.commit()
                return redirect(url_for("tms_staff.nextsls_lab"))
            if action in {"refresh", "preview", "stage"}:
                selected = request.form.get("agent_id", type=int)
                agent, profile = checked_account(selected)
                if action == "refresh" or not carriers.fresh_channel_cache(profile):
                    carriers.refresh_channel_catalogue(profile, agent)
                    db.session.commit()
                catalogue = carriers.channel_catalogue(profile)
                if action != "refresh":
                    code = request.form.get("code", "").strip()
                    if code not in catalogue:
                        raise nextsls.ProtocolError("请选择该账号当前渠道清单内的代码。")
                    uploaded = request.files.get("workbook")
                    if not uploaded or not (uploaded.filename or "").lower().endswith(".xlsx"):
                        raise nextsls.ProtocolError("请选择 XLSX 测试样本。")
                    raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
                    if not raw or len(raw) > MAX_UPLOAD_BYTES:
                        abort(413)
                    invoice = parse_invoice(raw)
                    if not invoice.get("workbook_test_sample"):
                        raise nextsls.ProtocolError("联调中心仅接受明确标记的测试样本；真实发票请走正常业务入口。")
                    digest = sha256(raw).hexdigest()
                    # Preserve the parsed source identifier for image integrity checks.
                    invoice = dict(invoice,
                        supplier_tenant_origin=profile.base_url,
                        supplier_channel={"code":code,"name":catalogue[code]})
                    candidate = SimpleNamespace(id=0, agent_id=agent.id, agent_service=code, invoice=invoice,
                        workbook=raw, file_sha256=digest, revision=1, customer_id=0)
                    prep = carriers.supplier_preparation(candidate, profile, agent)
                    pictures = product_photos(raw, invoice)
                    packing = carriers.packing_draft(candidate).getvalue()
                    output = {"code":code, "boxes":len(invoice["boxes"]),
                        "rows":len(invoice["declarations"]), "pictures":len(pictures), "packing_bytes":len(packing),
                        "blockers":prep["blockers"], "unknown":prep.get("unknown_rules",[]),
                        "packing_required":prep.get("packing_required"), "photo_fields":prep.get("required_photo_fields")}
                    if action == "stage":
                        policy = db.session.get(TmsNextslsTestPolicy, request.form.get("policy_id"))
                        if not applicable(policy, profile, agent, code) or request.form.get("fees_confirm") != "yes":
                            raise nextsls.ProtocolError("此账号和渠道没有适用的联调依据，或未确认费用；未保存测试单。")
                        if db.engine.dialect.name == "postgresql":
                            db.session.execute(text("SELECT pg_advisory_xact_lock(651360349)"))
                        customer = db.session.scalar(select(Customer).where(Customer.name == TEST_CUSTOMER))
                        if customer is None:
                            customer = Customer(name=TEST_CUSTOMER)
                            db.session.add(customer)
                            db.session.flush()
                        if db.session.scalar(select(TmsAccount.id).where(TmsAccount.customer_id == customer.id).limit(1)):
                            raise nextsls.ProtocolError("联调客户不得关联客户登录账号。")
                        invoice["nextsls_lab_case"] = {"policy_id":policy.id, "file_sha256":digest,
                            "binding":account_binding(profile,agent,code), "fees_confirmed":True,
                            "staged_by":current_user.id}
                        order = TmsOrder(customer_id=customer.id, number_key=invoice["customer_number"].upper(), file_sha256=digest,
                            invoice=invoice, workbook=raw, status="pending", agent_id=agent.id, agent_service=code)
                        db.session.add(order)
                        db.session.flush()
                        from tms_portal import history
                        history(order, "lab_stage")
                        db.session.commit()
                        return redirect(url_for("tms_staff.review", order_id=order.id))
            elif action is not None:
                abort(400)
        except (nextsls.ProtocolError, InvoiceError, ValueError) as exc:
            db.session.rollback()
            error = str(exc)
        except IntegrityError:
            db.session.rollback()
            error = "测试文件已登记或数据发生变化；请查看已有联调记录。"
        account_rows = db.session.execute(select(CarrierAgent.id, CarrierAgent.name, TmsAgentApi.base_url,
                TmsAgentApi.enabled, TmsAgentApi.verified, CarrierAgent.app_token).join(TmsAgentApi)
                .where(TmsAgentApi.provider == "nextsls", CarrierAgent.is_active.is_(True))
                .order_by(CarrierAgent.name)).all()
        current_time = now()
        policy_list = select(TmsNextslsTestPolicy).order_by(TmsNextslsTestPolicy.created_at.desc())
        valid_policies = db.session.scalars(policy_list.where(TmsNextslsTestPolicy.revoked_by.is_(None),
                                                               TmsNextslsTestPolicy.expires_at > current_time)).all()
        archived_policies = db.session.scalars(policy_list.where(or_(TmsNextslsTestPolicy.revoked_by.is_not(None),
                                                                     TmsNextslsTestPolicy.expires_at <= current_time))
                                               .limit(100)).all()
        policies = [*valid_policies, *archived_policies]
        active = {policy.id for policy in valid_policies}
        binding_by_account = {row.id: (row.base_url, sha256(row.app_token.encode()).hexdigest())
                              for row in account_rows if row.app_token}
        scopes = {}
        for policy in valid_policies:
            for scope in policy.scope:
                aid = scope["id"]
                if binding_by_account.get(aid) == (scope["origin"], scope["token_sha256"]):
                    scopes.setdefault(aid, set()).add(scope["service"])
        accounts = [SimpleNamespace(id=row.id, name=row.name, base_url=row.base_url,
                    enabled=row.enabled, verified=row.verified, authorized_codes=sorted(scopes.get(row.id, ())))
                    for row in account_rows]
        cases = db.session.execute(select(TmsOrder.id, TmsOrder.status, TmsSupplierOrder.state,
                TmsSupplierOrder.supplier_number).outerjoin(TmsSupplierOrder)
                .where(TmsOrder.invoice["nextsls_lab_case"]["policy_id"].as_string().is_not(None))
                .order_by(TmsOrder.id.desc()).limit(30)).all()
        return render_template("tms/nextsls_lab.html", staff=True, error=error, output=output, accounts=accounts,
                               selected=selected, catalogue=catalogue, policies=policies, cases=cases,
                               active=active,
                               expiry={p.id: datetime.fromtimestamp(p.expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                                       for p in policies}, enabled=control.enabled())
