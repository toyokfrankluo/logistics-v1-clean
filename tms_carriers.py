"""Carrier configuration, audited supplier operations, and internal reports."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from uuid import uuid4

from flask import current_app, g
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import IntegrityError

from models import CarrierAgent, Shipment, db
import nextsls
from tms_models import TmsAgentApi, TmsReceivable, TmsSupplierEvent, TmsSupplierOrder


PROVIDERS = {"nextsls": "NextSLS V5", "rtb56": "RTB56", "other": "其他"}
DETAILS = {"measurement", "parcels", "charges"}
CENT = Decimal("0.01")
SETTLEMENTS = {"pending": "待结算", "settled": "已结算（人工确认）"}


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
    codes = form.get("service_codes", "").split()
    if len(codes) > 50 or any(not nextsls.CODE.fullmatch(code) for code in codes):
        raise nextsls.ProtocolError("渠道代码格式无效。请输入最多 50 个代码，以空格分隔。")
    sender = {key: form.get("sender_" + key, "").strip() for key in nextsls.ADDRESS}
    sender = {key: nextsls.supplied_text(value, 200) for key, value in sender.items() if value}
    if sender:
        nextsls.address_values(sender)
    profile = db.session.get(TmsAgentApi, agent_id)
    if profile is None:
        profile = TmsAgentApi(agent_id=agent_id, verified={})
        db.session.add(profile)
    if (profile.provider, profile.base_url) != (provider, origin):
        profile.verified = {}
    profile.provider, profile.base_url = provider, origin
    profile.enabled, profile.service_codes, profile.sender = enabled, sorted(set(codes)), sender


def available(profile, agent, key):
    switch = "TMS_NEXTSLS_CREATE_ENABLED" if key == "create" else "TMS_NEXTSLS_READ_ENABLED"
    return bool(profile and profile.provider == "nextsls" and profile.base_url and key in profile.enabled
                and key in nextsls.WIRED and agent.is_active and agent.supports_api and agent.app_token
                and current_app.config.get(switch))


def approved_shipment(order):
    if not carrier_schema() or order.status != "approved":
        raise nextsls.ProtocolError("请先完成迁移与订单审核。")
    charge = db.session.get(TmsReceivable, order.id)
    shipment = db.session.get(Shipment, charge.shipment_id) if charge and charge.shipment_id else None
    if (not shipment or shipment.customer_id != order.customer_id
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


def send_order(order, actor_id, *, transport=None):
    shipment, agent, profile, link = operation_context(order, "create")
    if shipment.shipment_id or (link and link.state != "new"):
        raise nextsls.ProtocolError("订单已有供应商单号或提交记录。请先对账，避免重复下单。")
    code = order.agent_service or ""
    if code not in profile.service_codes:
        raise nextsls.ProtocolError("审核选定的实际代理渠道尚未列入代理的已确认渠道代码。")
    origin, token = profile.base_url, agent.app_token
    info = nextsls.request_data(agent, origin, "/shipment/get_services", {"services": {"type": "all"}},
                                transport=transport)
    service = nextsls.service_profile(info, code)
    db.session.expire_all()
    shipment, agent, profile, link = operation_context(order, "create")
    db.session.refresh(shipment, with_for_update=True)
    if (profile.base_url != origin or agent.app_token != token or code not in profile.service_codes
            or shipment.shipment_id or (link and link.state != "new")):
        raise nextsls.ProtocolError("订单或代理配置已变化，请刷新后重新核对。")
    link = link or ensure_link(order)
    payload = nextsls.create_payload(order.invoice, code, link.client_reference, service, profile.sender)
    transition(link, "new", "sending", attempt_at=datetime.utcnow(), tenant_origin=origin)
    audit(link, "attempt", actor_id, {"state": "sending"})
    db.session.commit()  # Commit the one-order reservation before contacting the supplier.

    try:
        _, agent, profile, link = operation_context(order, "create")
        if link.state != "sending" or profile.base_url != origin or agent.app_token != token or shipment.shipment_id:
            raise nextsls.ProtocolError("提交预留已变化。")
        data = nextsls.request_data(agent, origin, "/shipment/create", payload, transport=transport)
        attach_number(shipment, link, nextsls.parse_supplier_number(data))
        audit(link, "attempt", actor_id, {"state": "linked"})
        db.session.commit()
        return "linked"
    except (nextsls.ProtocolError, IntegrityError):
        db.session.rollback()
        link = db.session.get(TmsSupplierOrder, order.id)
        transition(link, "sending", "uncertain")
        audit(link, "attempt", actor_id, {"state": "uncertain"})
        db.session.commit()
        return "uncertain"


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
    known_reference = link.client_reference if link and link.attempt_at else None
    origin, token = profile.base_url, agent.app_token
    started_at = datetime.utcnow()
    data = nextsls.request_data(agent, origin, "/shipment/get_info",
                                {"shipment": {"shipment_id": supplier_number}}, transport=transport)
    db.session.expire_all()
    shipment, agent, profile, link = operation_context(order, "details")
    if shipment.shipment_id != supplier_number or profile.base_url != origin or agent.app_token != token:
        raise nextsls.ProtocolError("同步期间供应商单号发生变化，请刷新后重试。")
    snapshot = nextsls.normalize_snapshot(data, supplier_number, reference=known_reference)
    if "measurement" not in profile.enabled:
        snapshot["summary"] = {}
        snapshot["unconfirmed_actual_volume"] = None
    if "parcels" not in profile.enabled:
        snapshot["parcels"] = None
    if "charges" not in profile.enabled:
        snapshot["charges"] = None
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
    link.synced_at = datetime.utcnow()
    if previous is None or previous.details != snapshot:
        audit(link, "snapshot", actor_id, snapshot)
    if transport is None and not current_app.config.get("ISOLATED_MODE"):
        verified = {"measurement"} if snapshot["summary"] else set()
        verified.update(key for key, field in (("parcels", "parcels"), ("charges", "charges")) if snapshot[field])
        mark_verified(profile, verified)
    db.session.commit()
    return snapshot


def record_cost(order, actor_id, form):
    _, receivable = approved_shipment(order)
    currency = form.get("currency", "").strip().upper()
    if not nextsls.CURRENCY.fullmatch(currency):
        raise nextsls.ProtocolError("请填写有效的三字母采购币种。")
    rate = nextsls.numeric(form.get("rate"))
    if rate <= 0 or rate > 100000 or (currency == receivable.currency and rate != 1):
        raise nextsls.ProtocolError("汇率须大于零，同币种须填 1。")
    raw_amount = nextsls.numeric(form.get("payable"))
    raw_other = nextsls.numeric(form.get("other"))
    if raw_amount != raw_amount.quantize(CENT) or raw_other != raw_other.quantize(CENT):
        raise nextsls.ProtocolError("金额最多允许两位小数。")
    settlement = form.get("settlement")
    if settlement not in SETTLEMENTS:
        raise nextsls.ProtocolError("请选择已经核对的结算状态。")
    link = ensure_link(order)
    db.session.refresh(link, with_for_update=True)
    snapshot = latest_event(order.id, "snapshot")
    audit(link, "cost", actor_id, {
        "payable": str(raw_amount.quantize(CENT)), "currency": currency, "rate": str(rate),
        "other": str(raw_other.quantize(CENT)), "base_currency": receivable.currency, "settlement": settlement,
        "note": nextsls.supplied_text(form.get("note"), 500),
        "source_charges": snapshot.details["charges"] if snapshot else None,
    })
    db.session.commit()


def profit(receivable, cost, snapshot):
    if not cost:
        return None
    values = cost.details
    if (values["base_currency"] != receivable.currency
            or (snapshot and snapshot.details["charges"] != values["source_charges"])):
        return None
    converted = (Decimal(values["payable"]) * Decimal(values["rate"])).quantize(CENT, rounding=ROUND_HALF_UP)
    gross = receivable.total - converted - Decimal(values["other"])
    return {"converted": converted, "gross": gross, "margin": (
        (gross / receivable.total * 100).quantize(CENT, rounding=ROUND_HALF_UP) if receivable.total else None)}


def finance_costs(receivables):
    order_ids = [item.order_id for item in receivables]
    if not order_ids or not carrier_schema():
        return {}
    records = {}
    for offset in range(0, len(order_ids), 500):
        latest = select(func.max(TmsSupplierEvent.id)).where(
            TmsSupplierEvent.order_id.in_(order_ids[offset:offset + 500]),
            TmsSupplierEvent.kind.in_(("cost", "snapshot")),
        ).group_by(TmsSupplierEvent.order_id, TmsSupplierEvent.kind)
        for event in db.session.scalars(select(TmsSupplierEvent).where(TmsSupplierEvent.id.in_(latest))):
            records[event.order_id, event.kind] = event
    result = {}
    for item in receivables:
        cost, snapshot = (records.get((item.order_id, key)) for key in ("cost", "snapshot"))
        result[item.order_id] = {"cost": cost, "profit": profit(item, cost, snapshot)}
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


SUMMARY_FIELDS = {"picked_count": "已收货箱数", "chargeable_weight": "计费重",
                  "volume_weight": "材积重", "volume": "体积"}
PARCEL_FIELDS = {"number": "系统箱号", "ext_number": "扩展箱号", "tracking_number": "承运单号",
                 "chargeable_weight": "计费重", "chargeable_length": "计费长",
                 "chargeable_width": "计费宽", "chargeable_height": "计费高"}


def measurement_export(order, link, snapshot, display_time):
    values = snapshot.details
    summary = [("项目", "数值"), ("客户单号", order.invoice["customer_number"]),
               ("供应商运单号", link.supplier_number), ("最后同步", display_time),
               ("仓库实重", "未确认"), ("收货重", "未确认"), ("仓库逐箱扫描数据", "未确认")]
    summary.extend((label, decimal_or_none(values["summary"].get(key))) for key, label in SUMMARY_FIELDS.items())
    parcels = [list(PARCEL_FIELDS.values())]
    for row in (values["parcels"] or {}).values():
        parcels.append([decimal_or_none(row.get(key)) if key.startswith("chargeable_") else row.get(key)
                        for key in PARCEL_FIELDS])
    return xlsx([("运单汇总", summary), ("逐箱计费明细", parcels)])



def profit_export(rows, costs):
    values = [("客户单号", "代理运单号", "应收", "应收币种", "代理应付", "采购币种", "汇率",
               "其他成本（应收币种）", "毛利润", "毛利率 %", "结算状态", "成本确认时间 UTC")]
    for shipment, receivable in rows:
        if not receivable:
            continue
        entry = costs.get(receivable.order_id, {})
        cost, result = entry.get("cost"), entry.get("profit")
        details = cost.details if cost else {}
        values.append((shipment.tracking_number, shipment.shipment_id, receivable.total, receivable.currency,
                       decimal_or_none(details.get("payable")), details.get("currency"),
                       decimal_or_none(details.get("rate")), decimal_or_none(details.get("other")),
                       result["gross"] if result else None, result["margin"] if result else None,
                       SETTLEMENTS.get(details.get("settlement"), "成本未同步") if result else
                       ("成本待复核" if cost else "成本未同步"),
                       cost.created_at.isoformat(timespec="seconds") if cost else None))
    return xlsx([("内部单票利润", values)])


def decimal_or_none(value):
    return Decimal(value) if value is not None else None
