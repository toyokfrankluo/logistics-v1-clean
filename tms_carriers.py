"""Carrier configuration, audited supplier operations, and internal reports."""
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from uuid import uuid4

from flask import current_app, g
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import CarrierAgent, Shipment, db
import nextsls
import tms_measurements as measurements
from tms_invoice import InvoiceError, SERVICES, product_photos
from tms_models import TmsAgentApi, TmsReceivable, TmsSupplierEvent, TmsSupplierOrder


PROVIDERS = {"nextsls": "NextSLS V5", "rtb56": "RTB56", "other": "其他"}
DETAILS = {"measurement", "parcels", "charges"}
CENT = Decimal("0.01")
SETTLEMENTS = {"pending": "待结算", "settled": "已结算（人工确认）"}
COST_BASES = {"estimated": "预估费用", "actual": "实际费用"}
COST_OWNERSHIP = {"payable": "属于星睿应付", "not_payable": "已确认无星睿应付"}
CHANNEL_MAPPINGS = "sales_service_mappings"
CHANNEL_CATALOGUE = "channel_catalogue"
CHANNEL_CATALOGUE_TTL = timedelta(hours=24)


def carrier_schema():
    if "tms_carrier_schema" not in g:
        inspector = inspect(db.session.connection())
        g.tms_carrier_schema = all(inspector.has_table(model.__tablename__)
                                   for model in (TmsAgentApi, TmsSupplierOrder, TmsSupplierEvent))
    return g.tms_carrier_schema


def configure(agent_id, form):
    provider = form.get("provider")
    if provider not in PROVIDERS:
        raise nextsls.ProtocolError("请选择有效的代理类型。")
    origin = nextsls.base_url(form.get("base_url", "").strip(), provider)
    enabled = sorted(set(form.getlist("capability")))
    if any(key not in nextsls.CAPABILITIES for key in enabled):
        raise nextsls.ProtocolError("存在未知 API 功能。")
    sender = {key: form.get("sender_" + key, "").strip() for key in nextsls.ADDRESS}
    sender = {key: nextsls.supplied_text(value, 200) for key, value in sender.items() if value}
    if sender:
        nextsls.address_values(sender)
    profile = db.session.get(TmsAgentApi, agent_id)
    if profile is None:
        profile = TmsAgentApi(agent_id=agent_id, verified={})
        db.session.add(profile)
    same_account = (profile.provider, profile.base_url) == (provider, origin)
    codes = (form["service_codes"].split() if "service_codes" in form
             else (profile.service_codes or []) if same_account else [])
    if len(codes) > 50 or any(not nextsls.CODE.fullmatch(code) for code in codes):
        raise nextsls.ProtocolError("渠道代码格式无效，最多可保存 50 个代码。")
    if not same_account:
        profile.verified = {}
    elif "create" not in enabled:
        profile.verified = {key: value for key, value in profile.verified.items() if key != CHANNEL_CATALOGUE}
    profile.provider, profile.base_url = provider, origin
    profile.enabled, profile.service_codes, profile.sender = enabled, sorted(set(codes)), sender
    return profile


def channel_mappings(profile):
    mappings = (profile.verified or {}).get(CHANNEL_MAPPINGS, {}) if profile else {}
    catalogue = channel_catalogue(profile)
    return {sale: mapping for sale, mapping in mappings.items() if mapping["code"] in catalogue}


def channel_catalogue(profile):
    if not profile or profile.provider != "nextsls" or "create" not in profile.enabled:
        return {}
    cache = profile.verified.get(CHANNEL_CATALOGUE)
    if not cache:
        return {}
    age = datetime.utcnow() - datetime.fromisoformat(cache["fetched_at"])
    return cache["channels"] if timedelta(0) <= age < CHANNEL_CATALOGUE_TTL else {}


def store_channel_catalogue(profile, catalogue):
    profile.verified = {**profile.verified, CHANNEL_CATALOGUE: {
        "fetched_at": datetime.utcnow().isoformat(), "channels": catalogue,
    }}


def read_channel_catalogue(profile, agent):
    if not profile or profile.provider != "nextsls" or "create" not in profile.enabled:
        raise nextsls.ProtocolError("请先保存 NextSLS 代理配置并勾选创建运单能力，再读取渠道。")
    data = nextsls.request_data(agent, profile.base_url, "/shipment/get_services",
                                {"services": {"type": "all"}})
    rows = data.get("services")
    if not isinstance(rows, list):
        raise nextsls.ProtocolError("NextSLS 未返回渠道列表。")
    catalogue = {}
    for row in rows:
        if not isinstance(row, dict):
            raise nextsls.ProtocolError("NextSLS 渠道数据结构无效。")
        code, name = row.get("code"), row.get("name")
        if not isinstance(code, str) or not nextsls.CODE.fullmatch(code) or code in catalogue:
            raise nextsls.ProtocolError("NextSLS 渠道代码格式无效或重复。")
        catalogue[code] = nextsls.supplied_text(name, 200)
    return catalogue


def save_channel_mapping(profile, catalogue, sale, code):
    if sale not in SERVICES or code not in catalogue:
        raise nextsls.ProtocolError("请选择星睿销售服务及该账号当前可用的渠道代码。")
    mappings = dict(profile.verified.get(CHANNEL_MAPPINGS, {}))
    mappings[sale] = {"code": code, "name": catalogue[code]}
    profile.verified = {**profile.verified, CHANNEL_MAPPINGS: mappings}


def agent_profile(agent_id):
    if not carrier_schema():
        return None
    profiles = g.setdefault("nextsls_agent_profiles", {})
    if agent_id not in profiles:
        profiles[agent_id] = db.session.get(TmsAgentApi, agent_id)
    return profiles[agent_id]


def unavailable_reason(profile, agent, key):
    if not profile or profile.provider != "nextsls":
        return "仅适用于 NextSLS V5"
    if key not in nextsls.WIRED:
        return "接口尚未接入"
    if not profile.base_url:
        return "未填写 API 基础域名"
    if not agent.app_token:
        return "未配置 App Token"
    if not nextsls.legacy_tenant_matches(agent.api_url, profile.base_url):
        return "原有轨迹地址与基础域名租户不一致"
    if not agent.is_active or not agent.supports_api:
        return "代理已停用或不支持 API"
    if key not in profile.enabled:
        return "功能未勾选启用"
    if key == "create" and not channel_catalogue(profile):
        return "请先到代理编辑页刷新 NextSLS 渠道清单"
    switch = "TMS_NEXTSLS_CREATE_ENABLED" if key == "create" else "TMS_NEXTSLS_READ_ENABLED"
    if not current_app.config.get(switch):
        return "系统未开放真实下单" if key == "create" else "系统未开放 NextSLS 只读请求"
    return None


def available(profile, agent, key):
    return unavailable_reason(profile, agent, key) is None


def create_blockers(profile, agent, code, order_id):
    if not agent:
        return ["未找到订单所选代理"]
    reason = unavailable_reason(profile, agent, "create")
    blockers = [reason] if reason else []
    catalogue = channel_catalogue(profile)
    if catalogue and code not in catalogue:
        blockers.append("审核所选渠道未列入代理的已确认渠道代码")
    if not current_app.config.get("TMS_NEXTSLS_CREATE_ENABLED") and "系统未开放真实下单" not in blockers:
        blockers.append("系统未开放真实下单")
    if (profile and profile.base_url == "https://bckjkj.nextsls.com"
            and code in {"B888", "B888-1", "B888-2"} and not current_app.config.get("ISOLATED_MODE")):
        blockers.append("该渠道的真实商品图片传输尚待供应商确认")
    if current_app.config.get("TMS_NEXTSLS_CREATE_ENABLED") and not current_app.config.get("ISOLATED_MODE"):
        if current_app.config.get("TMS_NEXTSLS_CREATE_ORDER_ID") != str(order_id):
            blockers.append("此订单不在本次真实下单授权范围内")
        if not available(profile, agent, "reconcile"):
            blockers.append("真实下单前须启用只读对账")
    return blockers


def approved_shipment(order):
    if not carrier_schema() or order.status != "approved":
        raise nextsls.ProtocolError("请先完成迁移与订单审核。")
    charge = db.session.get(TmsReceivable, order.id)
    shipment = db.session.get(Shipment, charge.shipment_id) if charge and charge.shipment_id else None
    if (not shipment or shipment.customer_id != order.customer_id
            or shipment.tracking_number != order.invoice["customer_number"]
            or not order.agent_id or order.agent_id != shipment.agent_id or charge.agent_id != order.agent_id):
        raise nextsls.ProtocolError("订单、运单和代理关联不完整，请先人工核对。")
    return shipment, charge


def operation_context(order, capability):
    shipment, _ = approved_shipment(order)
    agent = db.session.get(CarrierAgent, order.agent_id)
    profile = db.session.get(TmsAgentApi, order.agent_id)
    keys = DETAILS if capability == "details" else {capability}
    if not agent or not any(available(profile, agent, key) for key in keys):
        raise nextsls.ProtocolError("该代理功能未启用、缺少凭据或尚未获准联网。")
    link = db.session.get(TmsSupplierOrder, order.id)
    if link and (link.agent_id != agent.id or (link.tenant_origin and link.tenant_origin != profile.base_url)):
        raise nextsls.ProtocolError("代理或租户已变化，须人工核对原提交记录。")
    return shipment, agent, profile, link


def ensure_link(order):
    link = db.session.get(TmsSupplierOrder, order.id)
    if link is None:
        link = TmsSupplierOrder(order_id=order.id, agent_id=order.agent_id,
                                client_reference="XR-" + uuid4().hex, state="new")
        db.session.add(link)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            raise nextsls.ProtocolError("订单状态刚被另一操作更新，请刷新后查看。") from None
    if link.agent_id != order.agent_id:
        raise nextsls.ProtocolError("供应商记录和订单代理不一致。")
    return link


def audit(link, kind, actor_id, details):
    db.session.add(TmsSupplierEvent(order_id=link.order_id, kind=kind, actor_id=actor_id, details=details))


def transition(link, previous, state, **changes):
    result = db.session.execute(update(TmsSupplierOrder).where(
        TmsSupplierOrder.order_id == link.order_id, TmsSupplierOrder.state == previous,
        TmsSupplierOrder.attempt_at == link.attempt_at,
    ).values(state=state, **changes))
    if result.rowcount != 1:
        raise nextsls.ProtocolError("订单状态刚被另一操作更新，请刷新后查看。")


def attach_number(shipment, link, number):
    existing = db.session.scalar(select(Shipment.id).where(
        Shipment.agent_id == shipment.agent_id, Shipment.shipment_id == number, Shipment.id != shipment.id,
    ).limit(1))
    if existing or (shipment.shipment_id and shipment.shipment_id != number):
        raise nextsls.ProtocolError("该供应商单号已有其他关联，须人工核对。")
    transition(link, link.state, "linked", supplier_number=number)
    shipment.shipment_id = number


def send_order(order, actor_id, *, transport=None, photo_publisher=None):
    if photo_publisher is not None and (transport is None or not current_app.config.get("ISOLATED_MODE")):
        raise nextsls.ProtocolError("商品图片发送目前只允许在隔离模拟中验证。")
    agent = db.session.get(CarrierAgent, order.agent_id) if order.agent_id else None
    blockers = create_blockers(agent_profile(order.agent_id) if agent else None, agent, order.agent_service, order.id)
    if blockers:
        raise nextsls.ProtocolError("；".join(blockers) + "。")
    shipment, agent, profile, link = operation_context(order, "create")
    if shipment.shipment_id or (link and link.state != "new"):
        raise nextsls.ProtocolError("订单已有供应商单号或提交记录。请先对账，避免重复下单。")
    code = order.agent_service or ""
    if code not in channel_catalogue(profile):
        raise nextsls.ProtocolError("审核选定的实际代理渠道尚未列入代理的已确认渠道代码。")
    origin, token, revision = profile.base_url, agent.app_token, order.revision
    info = nextsls.request_data(agent, origin, "/shipment/get_services", {"services": {"type": "all"}},
                                transport=transport)
    service = nextsls.service_profile(info, code)
    db.session.expire_all()
    db.session.refresh(order, with_for_update=True)
    shipment, agent, profile, link = operation_context(order, "create")
    db.session.refresh(shipment, with_for_update=True)
    if (profile.base_url != origin or agent.app_token != token or code not in channel_catalogue(profile)
            or order.agent_service != code or order.revision != revision
            or shipment.shipment_id or (link and link.state != "new")):
        raise nextsls.ProtocolError("订单或代理配置已变化，请刷新后重新核对。")
    link = link or ensure_link(order)

    def publish_photos():
        try:
            photos = product_photos(order.workbook, order.invoice)
        except InvoiceError as exc:
            raise nextsls.ProtocolError(str(exc)) from None
        return [photo_publisher(mime, data) for mime, data in photos]

    payload = nextsls.create_payload(
        order.invoice, code, link.client_reference, service, profile.sender, supplement=order_supplement(order),
        packing_host=current_app.config.get("TMS_NEXTSLS_PACKING_ATTACHMENT_HOST", ""),
        publish_photos=publish_photos if photo_publisher is not None else None,
    )
    transition(link, "new", "sending", attempt_at=datetime.utcnow(), tenant_origin=origin)
    audit(link, "attempt", actor_id, {"state": "sending"})
    db.session.commit()  # Commit the one-order reservation before contacting the supplier.

    try:
        _, agent, profile, link = operation_context(order, "create")
        if (link.state != "sending" or profile.base_url != origin or agent.app_token != token
                or shipment.shipment_id or order.revision != revision or code not in channel_catalogue(profile)):
            raise nextsls.ProtocolError("提交预留已变化。")
        data = nextsls.request_data(agent, origin, "/shipment/create", payload, transport=transport)
        attach_number(shipment, link, nextsls.parse_supplier_number(data))
        shipment.status = "代理已接单"
        if transport is None and not current_app.config.get("ISOLATED_MODE"):
            mark_verified(profile, {"create"})
        audit(link, "attempt", actor_id, {"state": "linked"})
        db.session.commit()
        return "linked"
    except (nextsls.ProtocolError, IntegrityError) as error:
        db.session.rollback()
        link = db.session.get(TmsSupplierOrder, order.id)
        transition(link, "sending", "uncertain")
        reason = str(error) if isinstance(error, nextsls.ProtocolError) else "供应商结果未能关联，请先对账。"
        audit(link, "attempt", actor_id, {"state": "uncertain", "reason": reason})
        db.session.commit()
        return "uncertain"


def order_supplement(order):
    supplement = order.invoice.get("nextsls_supplement", {})
    if (supplement.get("agent_id"), supplement.get("service")) == (order.agent_id, order.agent_service):
        return supplement
    return {}


def reconcile_order(order, actor_id, *, transport=None):
    shipment, agent, profile, link = operation_context(order, "reconcile")
    if not link or link.state not in {"sending", "uncertain", "not_found"}:
        raise nextsls.ProtocolError("此订单没有需要对账的提交记录。")
    if link.state == "sending" and (not link.attempt_at or datetime.utcnow() - link.attempt_at < timedelta(minutes=5)):
        raise nextsls.ProtocolError("代理提交处理中，稍后再查。")
    origin, token = profile.base_url, agent.app_token
    state, attempt, reference = link.state, link.attempt_at, link.client_reference
    response = nextsls.request_data(agent, origin, "/shipment/list", {
        "shipment": {"client_reference": reference, "page": 1, "page_size": 2},
    }, transport=transport)
    db.session.expire_all()
    shipment, agent, profile, link = operation_context(order, "reconcile")
    if not link or (link.state, link.attempt_at) != (state, attempt) or profile.base_url != origin or agent.app_token != token:
        raise nextsls.ProtocolError("对账期间提交记录或代理配置已变化，请刷新后再核对。")
    number = nextsls.lookup_created(response, reference)
    if number is None:
        transition(link, link.state, "not_found")
    else:
        attach_number(shipment, link, number)
        shipment.status = "代理已接单"
    audit(link, "reconcile", actor_id, {"state": "linked" if number else "not_found"})
    if transport is None and not current_app.config.get("ISOLATED_MODE"):
        mark_verified(profile, {"reconcile"})
    db.session.commit()
    return "linked" if number else "not_found"


def unlock_retry(order, actor_id, reason):
    shipment, _ = approved_shipment(order)
    link = db.session.get(TmsSupplierOrder, order.id)
    if not link or link.state != "not_found" or shipment.shipment_id:
        raise nextsls.ProtocolError("只有对账无匹配且经代理人工确认后才能解除重试锁。")
    transition(link, "not_found", "new")
    audit(link, "retry", actor_id, {"reason": nextsls.supplied_text(reason, 500)})
    db.session.commit()


def mark_verified(profile, keys):
    profile.verified = {**profile.verified, **{key: datetime.utcnow().isoformat(timespec="seconds") for key in keys}}


def record_tracking_verification(profile, agent):
    # Only called from the administrator's standalone tracking query, which has no other writes.
    if current_app.testing or current_app.config.get("ISOLATED_MODE") or profile.verified.get("tracking"):
        return
    origin, token = profile.base_url, agent.app_token
    try:
        db.session.refresh(agent, with_for_update=True)
        db.session.refresh(profile, with_for_update=True)
        if (profile.base_url == origin and agent.app_token == token
                and available(profile, agent, "tracking") and not profile.verified.get("tracking")):
            mark_verified(profile, {"tracking"})
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.warning("nextsls tracking verification status was not saved")


def latest_event(order_id, kind):
    return db.session.scalar(select(TmsSupplierEvent).where(
        TmsSupplierEvent.order_id == order_id, TmsSupplierEvent.kind == kind,
    ).order_by(TmsSupplierEvent.id.desc()).limit(1))


def sync_snapshot(order, actor_id, *, transport=None):
    shipment, agent, profile, link = operation_context(order, "details")
    supplier_number = shipment.shipment_id
    if not isinstance(supplier_number, str) or not nextsls.CODE.fullmatch(supplier_number):
        raise nextsls.ProtocolError("请先核对并保存供应商系统单号。")
    if link and (link.state not in {"new", "linked"} or (link.supplier_number and link.supplier_number != supplier_number)):
        raise nextsls.ProtocolError("运单关联正在对账或供应商单号不一致。")
    known_reference = link.client_reference if link and link.attempt_at else shipment.tracking_number
    origin, token = profile.base_url, agent.app_token
    started_at = datetime.utcnow()
    data = nextsls.request_data(agent, origin, "/shipment/get_info",
                                {"shipment": {"shipment_id": supplier_number}}, transport=transport)
    db.session.expire_all()
    db.session.refresh(order, with_for_update=True)
    shipment, agent, profile, link = operation_context(order, "details")
    reference_now = link.client_reference if link and link.attempt_at else shipment.tracking_number
    if (shipment.shipment_id != supplier_number or reference_now != known_reference
            or profile.base_url != origin or agent.app_token != token):
        raise nextsls.ProtocolError("同步期间供应商单号发生变化，请刷新后重试。")
    snapshot = nextsls.normalize_snapshot(data, supplier_number, reference=known_reference)
    snapshot["identity_verified"] = True
    snapshot["channel"] = order.agent_service
    if "measurement" not in profile.enabled:
        snapshot["summary"] = {}
        snapshot["raw"]["shipment"] = {}
    for capability, key in (("parcels", "parcels"), ("charges", "charges")):
        if capability not in profile.enabled:
            snapshot[key] = snapshot["raw"][key] = None
    if not snapshot["summary"] and not snapshot["parcels"] and not snapshot["charges"]:
        raise nextsls.ProtocolError("此账号或订单没有返回可用的计费或费用数据。")
    link = link or ensure_link(order)
    db.session.refresh(link, with_for_update=True)
    if link.synced_at and link.synced_at > started_at:
        raise nextsls.ProtocolError("已有更新的同步结果，请刷新页面。")
    if link.state == "new":
        attach_number(shipment, link, supplier_number)
    elif link.state != "linked" or link.supplier_number != supplier_number:
        raise nextsls.ProtocolError("同步期间供应商关联发生变化。")
    link.tenant_origin = profile.base_url
    previous = latest_event(order.id, "snapshot")
    prior = previous.details if previous else {}
    measurement_changed = not measurements.same_measurements(prior, snapshot) or prior.get("channel") != snapshot["channel"]
    prior_revision = prior.get("charge_revision")
    snapshot["charge_revision"] = (prior_revision if prior_revision and prior.get("charges") == snapshot["charges"]
                                   and prior.get("channel") == snapshot["channel"] else uuid4().hex)
    if prior.get("measurement_review") and not measurement_changed:
        snapshot["measurement_review"] = prior["measurement_review"]
    if measurement_changed and (measurements.has_measurements(prior) or measurements.has_measurements(snapshot)):
        flag_billing_review(order)
    link.synced_at = datetime.utcnow()
    if previous is None or previous.details != snapshot:
        audit(link, "snapshot", actor_id, snapshot)
    if transport is None and not current_app.config.get("ISOLATED_MODE"):
        verified = {"measurement"} if snapshot["summary"] else set()
        verified.update(key for key, field in (("parcels", "parcels"), ("charges", "charges")) if snapshot[field])
        mark_verified(profile, verified)
    db.session.commit()
    return snapshot


def snapshot_matches(link, shipment, snapshot, channel):
    return bool(link and shipment and snapshot and link.state == "linked" and link.synced_at
                and link.agent_id == shipment.agent_id and link.supplier_number == shipment.shipment_id
                and snapshot.details.get("channel", channel) == channel)


def verified_snapshot(order, shipment):
    """Resolve a snapshot through the approved order's verified, current supplier link."""
    if not carrier_schema() or not shipment:
        return None, None
    link = db.session.get(TmsSupplierOrder, order.id)
    snapshot = latest_event(order.id, "snapshot") if link else None
    return (link, snapshot) if snapshot_matches(link, shipment, snapshot, order.agent_service) else (None, None)


def review_measurements(order, actor_id, form):
    db.session.refresh(order, with_for_update=True)
    shipment, _ = approved_shipment(order)
    link, _ = verified_snapshot(order, shipment)
    if link is None:
        raise nextsls.ProtocolError("请先完成供应商测量同步。")
    db.session.refresh(link, with_for_update=True)
    snapshot = latest_event(order.id, "snapshot")
    if (not snapshot_matches(link, shipment, snapshot, order.agent_service)
            or not snapshot.details.get("identity_verified")):
        raise nextsls.ProtocolError("请先重新同步并核对运单身份。")
    if form.get("snapshot_id") != str(snapshot.id):
        raise nextsls.ProtocolError("测量数据已更新，请刷新后再确认。")
    units = {}
    for key, (_, choices) in measurements.available_unit_choices(snapshot.details).items():
        value = form.get("unit_" + key, "")
        if value:
            if value not in choices:
                raise nextsls.ProtocolError("测量单位不在可确认范围。")
            units[key] = value
    if "actual_volume" in units and "actual_weight" in units:
        raise nextsls.ProtocolError("本票运单实重只能确认一个来源字段。")
    reason = nextsls.supplied_text(form.get("reason"), 500)
    details = {**snapshot.details, "measurement_review": {
        "units": units, "reason": reason, "actor_id": actor_id,
        "confirmed_at": datetime.utcnow().isoformat(timespec="seconds"),
    }}
    if units != snapshot.details.get("measurement_review", {}).get("units", {}):
        flag_billing_review(order)
    audit(link, "snapshot", actor_id, details)
    db.session.commit()


def flag_billing_review(order):
    receivable = db.session.get(TmsReceivable, order.id, populate_existing=True)
    if receivable and receivable.final_total is not None and receivable.details.get("unit") in {"kg", "cbm"}:
        receivable.details = dict(receivable.details, billing_state="review")


def exchange_date(value, *, required):
    if not value and not required:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or parsed > (datetime.utcnow() + timedelta(hours=8)).date():
            raise ValueError
    except (ValueError, TypeError):
        raise nextsls.ProtocolError("请填写有效且不晚于今天的汇率日期（YYYY-MM-DD）。") from None
    return parsed.isoformat()


def record_cost(order, actor_id, form):
    shipment, receivable = approved_shipment(order)
    currency = form.get("currency", "").strip().upper()
    if not nextsls.CURRENCY.fullmatch(currency):
        raise nextsls.ProtocolError("请填写有效的三字母采购币种。")
    rate = nextsls.numeric(form.get("rate"))
    if rate <= 0 or rate > 100000 or (currency == receivable.currency and rate != 1):
        raise nextsls.ProtocolError("汇率须大于零，同币种须填 1。")
    rate_date = exchange_date(form.get("rate_date", ""), required=currency != receivable.currency)
    raw_amount = nextsls.numeric(form.get("payable"))
    raw_other = nextsls.numeric(form.get("other"))
    if raw_amount != raw_amount.quantize(CENT) or raw_other != raw_other.quantize(CENT):
        raise nextsls.ProtocolError("金额最多允许两位小数。")
    basis, ownership, settlement = (form.get(key) for key in ("cost_basis", "ownership", "settlement"))
    if basis not in COST_BASES or ownership not in COST_OWNERSHIP or settlement not in SETTLEMENTS:
        raise nextsls.ProtocolError("请选择费用归属、成本性质和结算状态。")
    if settlement == "settled" and basis != "actual":
        raise nextsls.ProtocolError("预估费用不能标记为已结算。")
    if ownership == "not_payable" and (raw_amount != 0 or basis != "actual"):
        raise nextsls.ProtocolError("确认无星睿应付时，请选择实际费用并填写零应付金额。")
    link = ensure_link(order)
    db.session.refresh(link, with_for_update=True)
    snapshot, previous = (latest_event(order.id, kind) for kind in ("snapshot", "cost"))
    if snapshot and (not snapshot_matches(link, shipment, snapshot, order.agent_service)
                     or not snapshot.details.get("identity_verified")):
        raise nextsls.ProtocolError("请先重新同步供应商数据并核对归属，再确认费用。")
    if (form.get("snapshot_id", "") != (str(snapshot.id) if snapshot else "")
            or form.get("cost_id", "") != (str(previous.id) if previous else "")):
        raise nextsls.ProtocolError("费用记录已更新，请刷新后重新核对。")
    audit(link, "cost", actor_id, {
        "payable": str(raw_amount.quantize(CENT)), "currency": currency, "rate": str(rate),
        "rate_date": rate_date, "ownership": ownership, "cost_basis": basis,
        "other": str(raw_other.quantize(CENT)), "base_currency": receivable.currency, "settlement": settlement,
        "note": nextsls.supplied_text(form.get("note"), 500),
        "source_charges": snapshot.details.get("charges") if snapshot else None,
        "charge_revision": snapshot.details.get("charge_revision") if snapshot else None,
    })
    db.session.commit()


def cost_status(receivable, cost, snapshot):
    if not cost:
        return "供应商费用待确认" if snapshot and snapshot.details.get("charges") else "成本未同步"
    values = cost.details
    if (values["base_currency"] != receivable.currency
            or (values["currency"] != receivable.currency and not values.get("rate_date"))
            or values.get("ownership") not in COST_OWNERSHIP or values.get("cost_basis") not in COST_BASES
            or (snapshot.details.get("charges") if snapshot else None) != values.get("source_charges")
            or (snapshot.details.get("charge_revision") if snapshot else None) != values.get("charge_revision")):
        return "成本或币种待复核"
    if values["cost_basis"] == "estimated":
        return "预估费用 · 待确认实际成本"
    return "实际成本已确认"


def profit(receivable, cost, snapshot):
    total = receivable.final_total
    if total is None or cost_status(receivable, cost, snapshot) != "实际成本已确认":
        return None
    values = cost.details
    converted = (Decimal(values["payable"]) * Decimal(values["rate"])).quantize(CENT, rounding=ROUND_HALF_UP)
    gross = total - converted - Decimal(values["other"])
    return {"converted": converted, "gross": gross, "margin": (
        (gross / total * 100).quantize(CENT, rounding=ROUND_HALF_UP) if total else None)}


def finance_costs(rows):
    pairs = [(shipment, item) for shipment, item in rows if item]
    order_ids = [item.order_id for _, item in pairs]
    if not order_ids or not carrier_schema():
        return {}
    records, links = {}, {}
    for offset in range(0, len(order_ids), 500):
        batch = order_ids[offset:offset + 500]
        links.update((link.order_id, link) for link in db.session.scalars(
            select(TmsSupplierOrder).where(TmsSupplierOrder.order_id.in_(batch))))
        latest = select(func.max(TmsSupplierEvent.id)).where(
            TmsSupplierEvent.order_id.in_(batch),
            TmsSupplierEvent.kind.in_(("cost", "snapshot")),
        ).group_by(TmsSupplierEvent.order_id, TmsSupplierEvent.kind)
        for event in db.session.scalars(select(TmsSupplierEvent).where(TmsSupplierEvent.id.in_(latest))):
            records[event.order_id, event.kind] = event
    result = {}
    for shipment, item in pairs:
        cost, snapshot = (records.get((item.order_id, key)) for key in ("cost", "snapshot"))
        if not snapshot_matches(links.get(item.order_id), shipment, snapshot, item.agent_service):
            snapshot = None
        result[item.order_id] = {"cost": cost, "profit": profit(item, cost, snapshot),
                                 "status": cost_status(item, cost, snapshot)}
    return result


def xlsx(sheet_names_and_rows):
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheet_names_and_rows:
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
        # Untrusted provider labels and customer IDs remain literal Excel text.
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        for cell in sheet[1]:
            cell.fill = PatternFill("solid", fgColor="123054")
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.row_dimensions[1].height = 32
        for column, cells in enumerate(sheet.iter_cols(), start=1):
            width = max(len(str(cell.value or "")) for cell in cells)
            sheet.column_dimensions[get_column_letter(column)].width = min(42, max(16, width + 8))
        sheet.auto_filter.ref = sheet.dimensions
        sheet.freeze_panes = "A2"
    buffer = BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer


def packing_draft(order):
    invoice = order.invoice
    boxes = [("货箱号", "申报箱重 (kg)", "箱长 (cm)", "箱宽 (cm)", "箱高 (cm)")]
    boxes.extend((box["box_number"], *(Decimal(box[key]) for key in
                                     ("weight_kg", "length_cm", "width_cm", "height_cm")))
                 for box in invoice["boxes"])
    products = [("货箱号", "中文品名", "英文品名", "数量", "单价 (USD)", "海关编码", "品牌", "型号")]
    supplemental = order_supplement(order).get("declarations", {})
    for index, item in enumerate(invoice["declarations"]):
        details = supplemental.get(str(index), {})
        products.append((item["box_number"], item["name_zh"], item["name_en"], item["quantity"],
                         Decimal(item["unit_price_usd"]), item["hs_code"], details.get("brand"), details.get("model")))
    return xlsx((("货箱", boxes), ("商品申报", products)))


def measurement_export(order, link, snapshot, display_time):
    details = snapshot.details
    view = measurements.measurement_view(order.invoice, details, internal=True)
    summary = [("项目", "数值", "单位", "状态", "API 原字段"),
               ("客户单号", order.invoice["customer_number"]), ("供应商运单号", link.supplier_number),
               ("数据来源", measurements.SOURCE), ("最后同步", display_time)]
    summary.extend((row["label"], row["value"], row["unit"], row["status"], row["field"]) for row in view["summary"])
    columns = ("系统箱号", "扩展箱号", "客户箱号", "申报重量 kg", "申报长 cm", "申报宽 cm", "申报高 cm")
    labels = tuple(measurements.PARCEL_FIELDS.values())
    parcels = [(*columns, *(title for label, _ in labels for title in (label, "单位 / 状态")))]
    for box in view["boxes"]:
        declared = box["declared"] or {}
        row = [box["number"], box["ext_number"], box["customer_box"],
               *(decimal_or_none(declared.get(key)) for key in ("weight_kg", "length_cm", "width_cm", "height_cm"))]
        for field in box["fields"]:
            row.extend((field["value"], field["unit"] or field["status"]))
        parcels.append(row)
    return xlsx([("运单汇总", summary), ("逐箱计费明细", parcels)])


def customer_measurement_export(invoice, view, display_time):
    summary = [("项目", "数值", "单位"), ("客户运单号", invoice["customer_number"]),
               ("星睿销售服务", invoice["service"]), ("目的地", invoice["address"]["country"]),
               ("仓库编码", invoice["address"].get("warehouse_code")), ("数据来源", "供应商测量，经星睿核对"),
               ("最后同步", display_time)]
    summary.extend((row["label"], row["value"], row["unit"]) for row in view["summary"])
    parcels = [("客户箱号", "项目", "数值", "单位")]
    for box in view["boxes"]:
        parcels.extend((box["customer_box"], row["label"], row["value"], row["unit"]) for row in box["fields"])
    return xlsx([("运单汇总", summary), ("客户箱明细", parcels)])


def profit_export(rows, costs):
    values = [("客户单号", "代理运单号", "应收", "应收币种", "代理应付", "采购币种", "汇率",
               "其他成本（应收币种）", "毛利润", "毛利率 %", "结算状态", "成本确认时间 UTC", "汇率日期", "费用归属", "成本性质", "复核状态")]
    for shipment, receivable in rows:
        if not receivable:
            continue
        entry = costs.get(receivable.order_id, {})
        cost, result = entry.get("cost"), entry.get("profit")
        details = cost.details if cost else {}
        values.append((shipment.tracking_number, shipment.shipment_id, receivable.final_total, receivable.currency,
                       decimal_or_none(details.get("payable")), details.get("currency"),
                       decimal_or_none(details.get("rate")), decimal_or_none(details.get("other")),
                       result["gross"] if result else None, result["margin"] if result else None,
                       SETTLEMENTS.get(details.get("settlement")),
                       cost.created_at.isoformat(timespec="seconds") if cost else None,
                       details.get("rate_date"), COST_OWNERSHIP.get(details.get("ownership")),
                       COST_BASES.get(details.get("cost_basis")), entry.get("status", "成本未同步")))
    return xlsx([("内部单票利润", values)])


def decimal_or_none(value):
    return Decimal(value) if value is not None else None
