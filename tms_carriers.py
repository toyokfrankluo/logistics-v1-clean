"""Carrier configuration, audited supplier operations, and internal reports."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO
import json
import re
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

from flask import current_app, g
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, inspect, select, update, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import CarrierAgent, Shipment, db
import nextsls
import carrier_http
import tms_create_control
import tms_measurements as measurements
import tms_supplier_images as images
import tms_supplier_files as files
from tms_invoice import InvoiceError, SERVICES, parse_invoice, product_photos
from tms_models import TmsAgentApi, TmsOrder, TmsReceivable, TmsSupplierEvent, TmsSupplierOrder


PROVIDERS = {"nextsls": "NextSLS V5", "rtb56": "RTB56", "other": "其他"}
DETAILS = {"measurement", "parcels", "charges"}
CENT = Decimal("0.01")
SETTLEMENTS = {"pending": "待结算", "settled": "已结算（人工确认）"}
COST_BASES = {"estimated": "预估费用", "actual": "实际费用"}
COST_OWNERSHIP = {"payable": "属于星睿应付", "not_payable": "已确认无星睿应付"}
CHANNEL_MAPPINGS = "sales_service_mappings"
CHANNEL_CATALOGUE = "channel_catalogue"
BRAND_ONLY_RULES = {"require_declaration_list": ["brand"]}
CHANNEL_CATALOGUE_TTL = timedelta(hours=24)
CREATE_DISABLED = "系统未开放真实下单"
ACCEPTANCE_COPY_BLOCKER = "这张订单是历史接口验收副本，不能向供应商下单。请选择独立的真实货件。"
WORKBOOK_SAMPLE_BLOCKER = "原发票标注为非真实货件测试样本，缺少与本票、账号及渠道匹配的有效供应商测试许可"
SAMPLE_RETRY_BLOCKER = "此测试样本只允许提交一次，不能解除重试锁。"
CREATE_GRANT = "nextsls_create_grant"
CREATE_GRANT_REQUIRED = "请先授权本票真实下单"
READ_DISABLED = "系统未开放 NextSLS 只读请求"
DECLARATION_CONTRACTS = json.loads(Path(__file__).with_name("nextsls_declaration_contracts.json").read_text())


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


def fresh_channel_cache(profile):
    if not profile or profile.provider != "nextsls" or "create" not in profile.enabled:
        return None
    cache = profile.verified.get(CHANNEL_CATALOGUE)
    if not cache:
        return None
    age = datetime.utcnow() - datetime.fromisoformat(cache["fetched_at"])
    return cache if timedelta(0) <= age < CHANNEL_CATALOGUE_TTL else None


def channel_catalogue(profile):
    cache = fresh_channel_cache(profile)
    return cache["channels"] if cache is not None else {}


def store_channel_catalogue(profile, catalogue, rules=None):
    profile.verified = {**profile.verified, CHANNEL_CATALOGUE: {
        "fetched_at": datetime.utcnow().isoformat(), "channels": catalogue, "rules": rules or {},
        "auto_refresh": True,
    }}


def channel_rules(profile, code):
    if code in channel_catalogue(profile):
        saved = profile.verified[CHANNEL_CATALOGUE].get("rules", {}).get(code)
        return nextsls.preparation_rules(saved or {})
    return None


def uses_nextsls(profile, agent):
    return (profile.provider == "nextsls" if profile else bool(nextsls.tenant_host(agent.api_url)))


def channel_setup_error(profile, agent):
    if not agent.is_active or not agent.supports_api:
        return "代理已停用或未启用 API。"
    if not profile or profile.provider != "nextsls" or not profile.base_url:
        return "此供应商尚未配置 NextSLS V5 API 基础域名。"
    try:
        if nextsls.base_url(profile.base_url, "nextsls") != profile.base_url:
            return "此供应商的 HTTPS 租户域名格式不正确。"
    except nextsls.ProtocolError as error:
        return str(error)
    if not agent.app_token:
        return "此供应商尚未配置 App Token。"
    if not nextsls.legacy_tenant_matches(agent.api_url, profile.base_url):
        return "API 租户与原有 NextSLS 轨迹地址不一致。"
    if "create" not in profile.enabled:
        return "请先在代理设置中启用创建运单能力并刷新渠道；站点下单总开关可以保持关闭。"
    return None


def read_channel_catalogue(profile, agent):
    if error := channel_setup_error(profile, agent):
        raise nextsls.ProtocolError(error)
    data = nextsls.request_data(agent, profile.base_url, "/shipment/get_services",
                                {"services": {"type": "all"}})
    rows = data.get("services")
    if not isinstance(rows, list):
        raise nextsls.ProtocolError("NextSLS 未返回渠道列表。")
    catalogue = {}
    rules = {}
    for row in rows:
        if not isinstance(row, dict):
            raise nextsls.ProtocolError("NextSLS 渠道数据结构无效。")
        code, name = row.get("code"), row.get("name")
        if not isinstance(code, str) or not nextsls.CODE.fullmatch(code) or code in catalogue:
            raise nextsls.ProtocolError("NextSLS 渠道代码格式无效或重复。")
        catalogue[code] = nextsls.supplied_text(name, 200)
        try:
            rules[code] = nextsls.preparation_rules(row)
        except nextsls.ProtocolError:
            # A selectable service may return no rules. Keep it visible, but require verification.
            continue
    return catalogue, rules


def quick_connect(name, origin, token):
    """Verify a new account before persisting anything. Never updates an existing account."""
    name = nextsls.supplied_text(name, 200)
    origin = nextsls.base_url(origin.strip(), "nextsls")
    if (not origin or not isinstance(token, str) or not 20 <= len(token.strip()) <= 255
            or any(ord(c) <= 32 for c in token.strip())):
        raise nextsls.ProtocolError("请填写有效的 HTTPS 基础域名和 App Token。")
    token = token.strip()
    if db.session.scalar(select(CarrierAgent.id).where(CarrierAgent.name == name)):
        raise nextsls.ProtocolError("代理名称已存在，请编辑现有账号；快速接入不会覆盖它。")
    if db.session.scalar(select(CarrierAgent.id).join(TmsAgentApi).where(
            TmsAgentApi.provider == "nextsls", TmsAgentApi.base_url == origin, CarrierAgent.app_token == token)):
        raise nextsls.ProtocolError("此租户和 Token 已有账号，未重复创建或覆盖。")
    candidate = SimpleNamespace(is_active=True, supports_api=True, api_url="", app_token=token)
    temporary = SimpleNamespace(provider="nextsls", base_url=origin, enabled=sorted(nextsls.WIRED))
    docs = {}
    for path in ("/api/v5/docs", "/api/v5/docs/markdown"):
        try:
            document = carrier_http.get_document(SimpleNamespace(is_active=True, supports_api=True, api_url=origin+path))
            valid = bool(document.strip()) and (path.endswith("/docs") or all(
                value in document for value in ("/shipment/create", "/shipment/get_services")))
            docs[path.rsplit("/", 1)[-1]] = {"available":valid,"sha256":sha256(document.encode()).hexdigest(),
                "test_terms_present": bool(re.search(r"(?i)\bsandbox\b|test[_ -]mode|沙箱|测试环境|测试建单|测试账号", document))}
        except carrier_http.CarrierRequestError:
            docs[path.rsplit("/", 1)[-1]] = {"available":False}
    catalogue, rules = read_channel_catalogue(temporary, candidate)
    if not catalogue:
        raise nextsls.ProtocolError("认证响应未返回可用渠道，未创建代理。")
    if db.engine.dialect.name == "postgresql":
        lock_key = int(sha256((origin + "|" + token).encode()).hexdigest()[:15], 16)
        db.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key":lock_key})
    if db.session.scalar(select(CarrierAgent.id).join(TmsAgentApi).where(
            TmsAgentApi.provider == "nextsls", TmsAgentApi.base_url == origin, CarrierAgent.app_token == token)):
        raise nextsls.ProtocolError("此账号刚刚已完成接入，未重复创建。")
    agent = CarrierAgent(name=name, is_active=True, supports_api=True, app_token=token)
    db.session.add(agent)
    db.session.flush()
    profile = TmsAgentApi(agent_id=agent.id, provider="nextsls", base_url=origin,
        enabled=sorted(nextsls.WIRED), service_codes=[], sender={}, verified={"onboarding":{
            "docs":docs,"at":datetime.now(timezone.utc).isoformat(),"authentication":"bearer",
            "create_acceptance_verified":False}})
    store_channel_catalogue(profile, catalogue, rules)
    db.session.add(profile)
    return agent, profile


def refresh_channel_catalogue(profile, agent):
    """Fetch an account's catalogue and discard it if credentials changed during the request."""
    origin = profile.base_url if profile else None
    token = agent.app_token
    catalogue, rules = read_channel_catalogue(profile, agent)
    db.session.refresh(agent, with_for_update=True)
    db.session.refresh(profile, with_for_update=True)
    if (profile.provider != "nextsls" or "create" not in profile.enabled or profile.base_url != origin
            or agent.app_token != token or not agent.is_active or not agent.supports_api
            or not nextsls.legacy_tenant_matches(agent.api_url, origin)):
        raise nextsls.ProtocolError("代理配置已经变更，请重新读取渠道。")
    store_channel_catalogue(profile, catalogue, rules)
    return catalogue


def refresh_expired_channel_catalogue(profile, agent):
    if (profile and agent and profile.provider == "nextsls" and "create" in profile.enabled
            and CHANNEL_CATALOGUE in profile.verified and fresh_channel_cache(profile) is None):
        cache = profile.verified[CHANNEL_CATALOGUE]
        if not cache.get("auto_refresh"):
            return "请核对当前凭据，手动刷新一次渠道清单后启用自动刷新。"
        try:
            refresh_channel_catalogue(profile, agent)
            db.session.commit()
        except nextsls.ProtocolError as error:
            db.session.rollback()
            return str(error)
        except SQLAlchemyError:
            db.session.rollback()
            return "渠道清单暂时无法保存，请重试。"
    return None


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


def capability_blockers(profile, agent, key):
    if not profile or profile.provider != "nextsls":
        return ["仅适用于 NextSLS V5"]
    if key not in nextsls.WIRED:
        return ["接口尚未接入"]
    blockers = []
    if not profile.base_url:
        blockers.append("未填写 API 基础域名")
    if not agent.app_token:
        blockers.append("未配置 App Token")
    if profile.base_url and not nextsls.legacy_tenant_matches(agent.api_url, profile.base_url):
        blockers.append("原有轨迹地址与基础域名租户不一致")
    if not agent.is_active or not agent.supports_api:
        blockers.append("代理已停用或不支持 API")
    if key not in profile.enabled:
        blockers.append("功能未勾选启用")
    elif key == "create" and not channel_catalogue(profile):
        blockers.append("请先到代理编辑页刷新 NextSLS 渠道清单")
    permitted = tms_create_control.enabled() if key == "create" else current_app.config.get("TMS_NEXTSLS_READ_ENABLED")
    if not permitted:
        blockers.append(CREATE_DISABLED if key == "create" else READ_DISABLED)
    return blockers


def unavailable_reason(profile, agent, key):
    return next(iter(capability_blockers(profile, agent, key)), None)


def available(profile, agent, key):
    return unavailable_reason(profile, agent, key) is None


def create_blockers(profile, agent, code):
    if not agent:
        return ["未找到订单所选代理"]
    blockers = capability_blockers(profile, agent, "create")
    catalogue = channel_catalogue(profile)
    if catalogue and code not in catalogue:
        blockers.append("审核所选渠道未列入代理的已确认渠道代码")
    if not tms_create_control.enabled() and CREATE_DISABLED not in blockers:
        blockers.append(CREATE_DISABLED)
    return blockers


def supplier_blocker_groups(blockers):
    groups = {"A. 管理员可修正的资料和代理配置": [], "B. 待供应商确认的协议": [], "C. 站点创建开关": []}
    admin, protocol, authorization = groups.values()
    for message in dict.fromkeys(blockers):
        if message == CREATE_DISABLED:
            authorization.append(message)
        elif (message in (nextsls.PHOTOS_PROTOCOL_BLOCKER,
                          nextsls.PHOTO_URL_PUBLICATION_BLOCKER)
              or message.startswith(nextsls.UNSUPPORTED_DECLARATION_BLOCKER)):
            protocol.append(message)
        else:
            admin.append(message)
    return {label: messages for label, messages in groups.items() if messages}


def destination_for_create(order):
    invoice = order.invoice
    pending = set(nextsls.unverified_destination_fields(invoice)) & {"mobile", "street2", "street3"}
    if not pending or invoice.get("address_source") != "workbook":
        return invoice, []
    raw = order.workbook
    if not raw or sha256(raw).hexdigest() != order.file_sha256:
        return invoice, []
    try:
        original = parse_invoice(raw)
    except InvoiceError:
        return invoice, []
    if (original["address_code"] != invoice["address_code"]
            or original["customer_number"] != invoice["customer_number"]):
        return invoice, []
    omitted = sorted(pending.intersection(original["legacy_optional_placeholders"]))
    return dict(invoice, address={key: value for key, value in invoice["address"].items() if key not in omitted}), omitted


def sample_create_approval(order, profile, agent):
    """Read operator-verified supplier permission from protected deployment configuration."""
    if ("acceptance_source" in order.invoice or not order.invoice.get("workbook_test_sample")
            or not profile or not agent or not agent.app_token or profile.provider != "nextsls"):
        return None
    if "nextsls_lab_case" in order.invoice:
        from tms_nextsls_lab import approval_for
        return approval_for(order, profile, agent)
    raw = current_app.config.get("TMS_NEXTSLS_SAMPLE_CREATE_APPROVALS", "")
    try:
        approvals = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return None
    approval = approvals.get(str(order.id)) if isinstance(approvals, dict) else None
    if not isinstance(approval, dict):
        return None
    evidence_id, expires_at = approval.get("evidence_id"), approval.get("expires_at")
    if (not isinstance(evidence_id, str) or not nextsls.CODE.fullmatch(evidence_id)
            or type(expires_at) is not int or expires_at <= datetime.now(timezone.utc).timestamp()
            or type(approval.get("agent_id")) is not int
            or approval.get("no_pickup") is not True
            or (approval.get("agent_id"), approval.get("origin"), approval.get("service"),
                approval.get("workbook_sha256"), approval.get("token_sha256"))
            != (agent.id, profile.base_url, order.agent_service, order.file_sha256,
                sha256(agent.app_token.encode()).hexdigest())
            or agent.id != order.agent_id or profile.agent_id != order.agent_id
            or not order.workbook or sha256(order.workbook).hexdigest() != order.file_sha256):
        return None
    return {"evidence_id": evidence_id,
            "digest": sha256(json.dumps(approval, sort_keys=True).encode()).hexdigest()}


def supplier_sample_blocker(invoice, approval):
    if "acceptance_source" in invoice:
        return ACCEPTANCE_COPY_BLOCKER
    if invoice.get("workbook_test_sample") and not approval:
        return WORKBOOK_SAMPLE_BLOCKER
    return None


def supplier_preparation(order, profile, agent):
    code = order.agent_service
    blockers = create_blockers(profile, agent, code)
    approval = sample_create_approval(order, profile, agent)
    if sample_blocker := supplier_sample_blocker(order.invoice, approval):
        blockers.append(sample_blocker)
    result = {"blockers": blockers, "missing": {}, "packing_required": None, "required_photo_fields": None,
              "allow_no_model": bool(profile and profile.provider == "nextsls"),
              "image_count": sum(bool(item.get("image_part")) for item in order.invoice["declarations"]),
              "sample_approval": approval}
    invoice, omitted = destination_for_create(order)
    result["omitted_address_fields"] = omitted
    if nextsls.unverified_destination_fields(invoice):
        blockers.append(nextsls.DESTINATION_PLACEHOLDER_BLOCKER)
    if not profile or profile.provider != "nextsls":
        return result
    origin = order.invoice.get("supplier_tenant_origin")
    if origin and origin != profile.base_url:
        blockers.append("代理租户已与审核记录不符")
        return result
    result["photos_protocol_confirmed"] = images.photos_encoder(profile, code) is not None
    result["direct_files_ready"] = files.direct_store(order, profile) is not None
    rules = channel_rules(profile, code)
    if rules is None:
        blockers.append("此渠道的完整必填规则尚未读取或已过期，请到代理设置刷新 NextSLS 渠道清单")
        result["missing"] = nextsls.missing_supplements(order.invoice, order_supplement(order), BRAND_ONLY_RULES)
        return result
    result["unknown_rules"] = rules.get("unknown_rules", [])
    result["packing_required"] = nextsls.required_flag(rules, "packing_list_required")
    result["required_photo_fields"] = (None if rules["require_declaration_list"] is None else
        [field for field in ("photos", "photo_url") if field in nextsls.declaration_requirements(rules)])
    store, encoders = images.image_delivery(order, profile, rules)
    result["photo_delivery_ready"] = bool(store and (bool(encoders) if result["required_photo_fields"] is None
        else set(result["required_photo_fields"]) <= encoders.keys()))
    packing_store = images.media_store(order, profile) if result["packing_required"] is not False else None
    result["packing_delivery_ready"] = bool(packing_store)
    supplement = order_supplement(order)
    use_invoice_po = invoice_po_allowed(profile, code)
    result["missing"] = nextsls.supplement_inputs(order.invoice, supplement, rules, use_invoice_po=use_invoice_po)
    result["missing_taxes"] = nextsls.tax_inputs(order.invoice, rules)
    result["tax_fields"] = nextsls.TAX_FIELDS
    result["invoice_po_rows"] = invoice_po_rows(order.invoice, supplement, rules, use_invoice_po)
    result["invoice_header_po_used"] = bool(result["invoice_po_rows"] and not nextsls.has_line_amazon_po(order.invoice))
    result["amazon_po_candidate"] = (not nextsls.has_line_amazon_po(order.invoice)
                                     and any("amazon_ref_id" in fields for fields in result["missing"].values()))
    try:
        nextsls.create_payload(invoice, code, "XR-PREFLIGHT", rules, profile.sender, supplement=supplement,
                               image_fields=encoders, packing_host=packing_store.host if packing_store else "",
                               photos_protocol_confirmed=result["photos_protocol_confirmed"],
                               use_invoice_po=use_invoice_po,
                               include_packing=bool(packing_store),
                               preflight=True)
    except nextsls.PreparationError as exc:
        blockers.extend(exc.blockers)
    except nextsls.ProtocolError as exc:
        blockers.append(str(exc))
    return result


def invoice_po_allowed(profile, code):
    if profile.provider != "nextsls":
        return False
    defaults = DECLARATION_CONTRACTS.get(profile.base_url, {}).get("invoice_po", {})
    configured = (getattr(profile, "verified", None) or {}).get("declaration_contracts", {}).get("invoice_po", {})
    contract = configured.get(code, defaults.get(code))
    return bool(isinstance(contract, dict) and contract.get("source") == nextsls.INVOICE_PO_SOURCE
                and contract.get("evidence"))


def packing_attachment_file_type(profile):
    contract = DECLARATION_CONTRACTS.get(profile.base_url, {}).get("packing_attachment", {})
    return contract.get("file_type", "order")


def invoice_po_rows(invoice, supplement, rules, use_invoice_po):
    if not use_invoice_po or "amazon_ref_id" not in nextsls.declaration_requirements(rules):
        return []
    return [{"row": i + 1, "source": source} for i in range(len(invoice["declarations"]))
            if (source := nextsls.amazon_reference(invoice, supplement, i, use_invoice_po=True)[1])
            == nextsls.INVOICE_PO_SOURCE]


def make_create_grant(order, profile, actor_id, *, preparation, unknown_confirmed=False,
                      unknown_basis="", batch_id=None):
    rules = channel_rules(profile, order.agent_service)
    unknown = rules.get("unknown_rules", []) if rules else []
    if unknown and not unknown_confirmed:
        raise nextsls.ProtocolError("请明确确认渠道存在未知规则，且本票真实资料已完整核对。")
    now = datetime.now(timezone.utc)
    grant = {
        "revision": order.revision + 1, "agent_id": order.agent_id, "service": order.agent_service,
        "origin": profile.base_url, "actor_id": actor_id, "fees_and_pickup_confirmed": True,
        "expires_at": int((now + timedelta(minutes=30)).timestamp()),
        "rules_digest": nextsls.rules_digest(rules) if rules is not None else None,
    }
    if approval := preparation["sample_approval"]:
        grant["sample_approval"] = approval
    if unknown:
        grant.update(unknown_rules_confirmed=True, review_basis=nextsls.supplied_text(unknown_basis, 500),
                     unknown_rules=unknown)
    if batch_id:
        grant["batch_id"] = batch_id
    invoice = dict(order.invoice)
    invoice[CREATE_GRANT] = grant
    invoice["supplier_authorization_history"] = [*invoice.get("supplier_authorization_history", []),
        {**grant, "at": now.isoformat(), "compatibility_verified": False if unknown else None}]
    return invoice


def current_create_grant(order, profile, approval=None):
    grant = order.invoice.get(CREATE_GRANT)
    if not grant or not profile:
        return None
    if (grant["revision"], grant["agent_id"], grant["service"], grant["origin"]) != (
            order.revision, order.agent_id, order.agent_service, profile.base_url):
        return None
    if order.invoice.get("workbook_test_sample") and (
            not approval or grant.get("sample_approval") != approval or grant.get("fees_and_pickup_confirmed") is not True):
        return None
    return grant if grant["expires_at"] > datetime.now(timezone.utc).timestamp() else None


def approved_shipment(order):
    if order.invoice.get("order_archive"):
        raise nextsls.ProtocolError("订单已删除，请先恢复。")
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
    approved_origin = order.invoice.get("supplier_tenant_origin")
    if ((approved_origin and approved_origin != profile.base_url)
            or (link and (link.agent_id != agent.id
                          or (link.tenant_origin and link.tenant_origin != profile.base_url)))):
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


def send_order(order, actor_id, *, transport=None, photo_publisher=None, packing_publisher=None,
               require_grant=False, expected_snapshot=None):
    if ((photo_publisher is not None or packing_publisher is not None)
            and (transport is None or not current_app.config.get("ISOLATED_MODE"))):
        raise nextsls.ProtocolError("商品图片和装箱单发送目前只允许在隔离模拟中验证。")
    if transport is not None and not current_app.config.get("ISOLATED_MODE"):
        raise nextsls.ProtocolError("真实环境禁止使用模拟供应商传输。")
    agent = db.session.get(CarrierAgent, order.agent_id) if order.agent_id else None
    profile = agent_profile(order.agent_id) if agent else None
    approval = sample_create_approval(order, profile, agent)
    if sample_blocker := supplier_sample_blocker(order.invoice, approval):
        raise nextsls.ProtocolError(sample_blocker)
    blockers = create_blockers(profile, agent, order.agent_service)
    if blockers:
        raise nextsls.ProtocolError("；".join(blockers) + "。")
    shipment, agent, profile, link = operation_context(order, "create")
    if shipment.shipment_id or (link and (link.state != "new" or (approval and link.attempt_at))):
        raise nextsls.ProtocolError("订单已有供应商单号或提交记录。请先对账，避免重复下单。")
    if expected_snapshot is not None and expected_snapshot != [order.id, order.revision, order.agent_id, order.agent_service]:
        raise nextsls.ProtocolError("订单版本、代理或渠道已变化。")
    grant_required = require_grant or current_app.config.get("TMS_NEXTSLS_REQUIRE_ORDER_GRANT") or bool(approval)
    if grant_required and not current_create_grant(order, profile, approval):
        raise nextsls.ProtocolError(CREATE_GRANT_REQUIRED)
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
            or (expected_snapshot is not None and order.agent_id != expected_snapshot[2])
            or shipment.shipment_id or (link and (link.state != "new" or (approval and link.attempt_at)))):
        raise nextsls.ProtocolError("订单或代理配置已变化，请刷新后重新核对。")
    if approval and sample_create_approval(order, profile, agent) != approval:
        raise nextsls.ProtocolError(WORKBOOK_SAMPLE_BLOCKER)
    if grant_required and not current_create_grant(order, profile, approval):
        raise nextsls.ProtocolError(CREATE_GRANT_REQUIRED)
    rules = nextsls.preparation_rules(service)
    grant = current_create_grant(order, profile, approval)
    if grant and grant.get("rules_digest") != nextsls.rules_digest(rules):
        raise nextsls.ProtocolError("渠道规则已变化，请刷新代理渠道清单并重新预检和授权。")
    if rules.get("unknown_rules"):
        if not grant or not grant.get("unknown_rules_confirmed") or not grant.get("review_basis"):
            raise nextsls.ProtocolError("渠道规则未知或已变化，请管理员核对真实资料并明确授权本票首次提交。")
    link = link or ensure_link(order)
    store, encoders = images.image_delivery(order, profile, rules)
    packing_store = images.media_store(order, profile) if rules["packing_list_required"] != 0 else None
    packing_host = current_app.config.get("TMS_NEXTSLS_PACKING_ATTACHMENT_HOST", "")
    if packing_store:
        packing_host = packing_store.host
        packing_publisher = lambda data: images.publish_packing(packing_store, order, profile, data)
    invoice, omitted = destination_for_create(order)
    supplement = order_supplement(order)
    use_invoice_po = invoice_po_allowed(profile, code)

    def publish_photos():
        return [photo_publisher(mime, data) for mime, data in original_photos(order)]

    payload = nextsls.create_payload(
        invoice, code, link.client_reference, rules, profile.sender, supplement=supplement,
        packing_host=packing_host,
        publish_photos=publish_photos if photo_publisher is not None else None,
        image_fields=encoders,
        photos_protocol_confirmed=images.photos_encoder(profile, code) is not None,
        use_invoice_po=use_invoice_po,
        include_packing=bool(packing_store),
        packing_file_type=packing_attachment_file_type(profile),
        publish_image_fields=(lambda: images.publish_images(store, order, profile, original_photos(order), encoders))
        if encoders else None,
        publish_packing=(lambda: packing_publisher(packing_draft(order).getvalue()))
        if packing_publisher is not None else None,
    )
    grant = current_create_grant(order, profile, approval)
    if grant_required and not grant:
        raise nextsls.ProtocolError(CREATE_GRANT_REQUIRED)
    if grant:
        order.invoice = {key: value for key, value in order.invoice.items() if key != CREATE_GRANT}
    transition(link, "new", "sending", attempt_at=datetime.utcnow(), tenant_origin=origin)
    image_delivery = image_delivery_summary(payload["shipment"], encoders)
    image_rows = image_delivery["rows_in_request"]
    delivery_audit = {"image_rows_verified": image_rows if not isinstance(store, files.DirectStore) else 0,
                      "image_delivery": image_delivery, "omitted_legacy_address_fields": omitted}
    if isinstance(store, files.DirectStore):
        delivery_audit["image_rows_published"] = image_rows
    if po_rows := invoice_po_rows(invoice, supplement, rules, use_invoice_po):
        delivery_audit["amazon_ref_sources"] = po_rows
    if grant:
        delivery_audit["authorized_by"] = grant["actor_id"]
    if approval:
        delivery_audit["sample_approval"] = approval
    if rules.get("unknown_rules"):
        delivery_audit["unknown_rule_attempt"] = {"fields":rules["unknown_rules"],
            "review_basis":grant["review_basis"], "rules_digest":grant["rules_digest"],
            "compatibility_verified":False}
    delivery_audit["photos_encoding"] = "v5_https_url" if any(
        "photos" in row for box in payload["shipment"]["parcels"] for row in box["declarations"]) else None
    audit(link, "attempt", actor_id, {"state": "sending", **delivery_audit})
    db.session.commit()  # Commit the one-order reservation before contacting the supplier.

    receipt = {} if "nextsls_lab_case" in order.invoice else None
    try:
        _, agent, profile, link = operation_context(order, "create")
        if (link.state != "sending" or profile.base_url != origin or agent.app_token != token
                or shipment.shipment_id or order.revision != revision or code not in channel_catalogue(profile)):
            raise nextsls.ProtocolError("提交预留已变化。")
        if approval and sample_create_approval(order, profile, agent) != approval:
            raise nextsls.ProtocolError(WORKBOOK_SAMPLE_BLOCKER)
        if receipt is not None:
            data = nextsls.request_data(agent, origin, "/shipment/create", payload, transport=transport, receipt=receipt)
            delivery_audit["lab_response"] = receipt
        else:
            data = nextsls.request_data(agent, origin, "/shipment/create", payload, transport=transport)
        attach_number(shipment, link, nextsls.parse_supplier_number(data))
        if receipt is not None:
            receipt["shipment_id"] = link.supplier_number
        shipment.status = "代理已接单"
        if transport is None and not current_app.config.get("ISOLATED_MODE"):
            mark_verified(profile, {"create"})
        audit(link, "attempt", actor_id, {"state": "linked", **delivery_audit})
        db.session.commit()
        return "linked"
    except (nextsls.ProtocolError, IntegrityError) as error:
        db.session.rollback()
        link = db.session.get(TmsSupplierOrder, order.id)
        transition(link, "sending", "uncertain")
        reason = str(error) if isinstance(error, nextsls.ProtocolError) else "供应商结果未能关联，请先对账。"
        details = {"state": "uncertain", "reason": reason, **delivery_audit}
        if receipt is not None:
            details["lab_response"] = receipt
        if approval:
            details["sample_approval"] = approval
        if isinstance(error, nextsls.CreateRejected):
            details["supplier_rejected"] = True
            details["missing_fields"] = list(error.fields)
            if error.photos_url_rejected and transport is None and not current_app.config.get("ISOLATED_MODE"):
                saved = db.session.get(TmsAgentApi, order.agent_id, with_for_update=True, populate_existing=True)
                if saved and saved.base_url == origin:
                    saved.verified = {**saved.verified, "photos_protocols":{
                        **saved.verified.get("photos_protocols", {}), code:{"encoding":"unsupported",
                            "evidence":"V5 create status=0 explicitly rejected photos URL",
                            "at":datetime.now(timezone.utc).isoformat()}}}
        audit(link, "attempt", actor_id, details)
        db.session.commit()
        return "uncertain"


def order_supplement(order):
    supplement = order.invoice.get("nextsls_supplement", {})
    if (supplement.get("agent_id"), supplement.get("service")) == (order.agent_id, order.agent_service):
        return supplement
    return {}


def original_photos(order):
    raw = order.workbook
    if not raw or sha256(raw).hexdigest() != order.file_sha256:
        raise nextsls.ProtocolError("保存的原始发票指纹不符，不能发布商品图片。")
    try:
        return product_photos(raw, order.invoice)
    except InvoiceError as exc:
        raise nextsls.ProtocolError(str(exc)) from None


def image_delivery_summary(shipment, fields):
    rows = [row for parcel in shipment.get("parcels", []) for row in parcel["declarations"]]
    encoded = nextsls.IMAGE_FIELDS.intersection(key for row in rows for key in row)
    return {"fields_in_request": sorted(encoded),
            "rows_in_request": sum(bool(nextsls.IMAGE_FIELDS & row.keys()) for row in rows),
            "status": ("included" if encoded else
                       "optional_publication_failed" if fields else "not_included")}


def preview_supplier_files(order, profile):
    """Stage short-lived copies using the real publishers, without initiating a carrier operation."""
    store = files.direct_store(order, profile)
    if not store:
        raise nextsls.ProtocolError("星睿 HTTPS 文件服务暂未就绪。")
    rules = channel_rules(profile, order.agent_service)
    encoders = images.image_delivery(order, profile, rules)[1] if rules is not None else {}
    photo_error = None
    photos, references = [], []
    try:
        if all(item.get("image_part") for item in order.invoice["declarations"]):
            photos = original_photos(order)
            references = images.publish_images(store, order, profile, photos,
                {"photo_url": nextsls.product_photo_url}, preview=True) if photos else []
    except nextsls.ProtocolError as error:
        photo_error = error

    def preview_image_fields():
        if photo_error:
            raise photo_error
        return images.encode_image_urls((ref["photo_url"] for ref in references), encoders)

    packing = packing_draft(order).getvalue()
    attachment = images.publish_packing(store, order, profile, packing, preview=True)
    if nextsls.packing_url_host(attachment) != store.host:
        raise nextsls.ProtocolError("装箱单链接域名或长度不符合要求。")
    pairs = [(ref["photo_url"], mime, data) for ref, (mime, data) in zip(references, photos)]
    pairs.append((attachment, files.PACKING_MIME, packing))
    manifest = [{"url": url, "mime": mime, "bytes": len(data), "sha256": sha256(data).hexdigest()}
                for url, mime, data in pairs]
    if rules is None:
        return manifest, {"error": "当前渠道规则未就绪，文件验收独立于下单。"}
    use_invoice_po = invoice_po_allowed(profile, order.agent_service)
    invoice, _ = destination_for_create(order)
    supplement = order_supplement(order)
    try:
        payload = nextsls.create_payload(
            invoice, order.agent_service, "XR-PREVIEW", rules, profile.sender, supplement=supplement,
            packing_host=store.host, publish_packing=lambda: attachment,
            image_fields=encoders, photos_protocol_confirmed=images.photos_encoder(profile, order.agent_service) is not None,
            publish_image_fields=preview_image_fields,
            use_invoice_po=use_invoice_po,
            include_packing=rules["packing_list_required"] is None,
            packing_file_type=packing_attachment_file_type(profile),
        )["shipment"]
    except nextsls.ProtocolError as error:
        return manifest, {"error": str(error)}
    rows = [row for parcel in payload["parcels"] for row in parcel["declarations"]]
    return manifest, {
        "status": "规则未知，首次真实提交待验证" if rules.get("unknown_rules") else "通用请求预检通过",
        "unknown_rules":rules.get("unknown_rules", []), "payload":{"shipment":payload},
        "image_delivery": image_delivery_summary(payload, encoders),
        "combined": bool(payload.get("attachments") and any(nextsls.IMAGE_FIELDS & row.keys() for row in rows)),
        "po_rows": len(invoice_po_rows(invoice, supplement, rules, use_invoice_po)),
    }


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
    if order.invoice.get("workbook_test_sample"):
        raise nextsls.ProtocolError(SAMPLE_RETRY_BLOCKER)
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
    event = db.session.scalar(select(TmsSupplierEvent).where(
        TmsSupplierEvent.order_id == order_id, TmsSupplierEvent.kind == kind,
    ).order_by(TmsSupplierEvent.id.desc()).limit(1))
    if event and kind in {"cost", "snapshot"}:
        order = db.session.get(TmsOrder, order_id)
        if event.id <= order.invoice.get("supplier_event_floor", 0):
            return None
    return event


def query_linked_shipment(order):
    """Confirm an existing link by number without importing supplier measurements or charges."""
    shipment, agent, profile, link = operation_context(order, "details")
    if not link or link.state != "linked" or not link.supplier_number or shipment.shipment_id != link.supplier_number:
        raise nextsls.ProtocolError("请先保存并核对供应商运单号。")
    number = link.supplier_number
    data = nextsls.request_data(agent, profile.base_url, "/shipment/get_info", {"shipment": {"shipment_id": number}})
    if nextsls.parse_supplier_number(data) != number:
        raise nextsls.ProtocolError("供应商查询返回的运单号与本票不符。")
    record = data["shipment"]
    service = record.get("service_code", record.get("service"))
    if isinstance(service, dict):
        service = service.get("code")
    if service and service != order.agent_service:
        raise nextsls.ProtocolError("供应商查询返回的渠道与本票不符。")
    expected_reference = link.client_reference if link.attempt_at else shipment.tracking_number
    return (number, bool(service), record.get("client_reference") == expected_reference)


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
    db.session.refresh(order, with_for_update=True)
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
    records, links, floors = {}, {}, {}
    for offset in range(0, len(order_ids), 500):
        batch = order_ids[offset:offset + 500]
        links.update((link.order_id, link) for link in db.session.scalars(
            select(TmsSupplierOrder).where(TmsSupplierOrder.order_id.in_(batch))))
        floors.update(db.session.execute(select(TmsOrder.id, TmsOrder.invoice["supplier_event_floor"].as_integer())
                       .where(TmsOrder.id.in_(batch))).all())
        latest = select(func.max(TmsSupplierEvent.id)).where(
            TmsSupplierEvent.order_id.in_(batch),
            TmsSupplierEvent.kind.in_(("cost", "snapshot")),
        ).group_by(TmsSupplierEvent.order_id, TmsSupplierEvent.kind)
        for event in db.session.scalars(select(TmsSupplierEvent).where(TmsSupplierEvent.id.in_(latest))):
            if event.id > (floors.get(event.order_id) or 0):
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
    supplemental = order_supplement(order)
    for index, item in enumerate(invoice["declarations"]):
        details = nextsls.declaration_supplements(invoice, supplemental, index, ("brand", "model"))
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
