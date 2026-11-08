"""NextSLS V5 protocol. Outbound paths are defined here, never supplied by request data."""
from decimal import Decimal, InvalidOperation
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import carrier_http


CAPABILITIES = {
    "create": ("创建运单", "/shipment/create", "写入"),
    "reconcile": ("查询创建结果", "/shipment/list", "只读"),
    "labels": ("获取标签", "/shipment/get_labels", "只读"),
    "tracking_numbers": ("获取转单号", "/shipment/get_tracking_numbers", "只读"),
    "cancel": ("取消运单", "/shipment/void", "写入"),
    "tracking": ("查询物流轨迹", "/shipment/get_tracking", "只读"),
    "measurement": ("查询重量体积", "/shipment/get_info", "只读"),
    "parcels": ("查询每箱计费明细", "/shipment/get_info", "只读"),
    "charges": ("查询代理费用", "/shipment/get_info", "只读"),
    "settlement": ("查询结算状态", None, "待确认"),
}
WIRED = {"create", "reconcile", "tracking", "measurement", "parcels", "charges"}
PATHS = {path for _, path, _ in CAPABILITIES.values() if path} | {"/shipment/get_services"}
CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,49}\Z")
FBA_BOX_CODE = re.compile(r"FBA[A-Za-z0-9]{6,47}\Z", re.I)
CURRENCY = re.compile(r"[A-Z]{3}\Z")
ADDRESS = {
    "name": "recipient", "company": "company", "tel": "phone", "mobile": "mobile",
    "address_1": "street", "address_2": "street2", "address_3": "street3",
    "city": "city", "state": "region", "country": "country", "postcode": "postal_code",
    "email": "email",
}
REQUIRED_ADDRESS = ("name", "address_1", "city", "country", "postcode")
BOX_DIMENSIONS = {"weight": "weight_kg", "length": "length_cm", "width": "width_cm", "height": "height_cm"}
DECLARATIONS = {"name_zh": "name_zh", "name_en": "name_en", "material": "material", "usage": "usage", "hscode": "hs_code"}


class ProtocolError(ValueError):
    pass


def base_url(value, provider):
    if not value:
        return ""
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (len(value) > 255 or url.scheme != "https" or not host.isascii()
                or not re.fullmatch(r"[A-Za-z0-9.-]+", host) or "." not in host
                or url.username is not None or url.password is not None or url.port not in (None, 443)
                or url.query or url.fragment or url.path not in ("", "/")
                or (provider == "nextsls" and not host.endswith(".nextsls.com"))):
            raise ValueError()
        return f"https://{host.lower()}"
    except ValueError:
        raise ProtocolError("请填写不含路径或凭据的 HTTPS 基础域名。NextSLS 须使用其 nextsls.com 租户域名。") from None


def tenant_host(value):
    try:
        host = (urlsplit(value or "").hostname or "").rstrip(".")
    except ValueError:
        return None
    return host if host.endswith(".nextsls.com") else None


def legacy_tenant_matches(legacy_url, origin):
    try:
        urlsplit(legacy_url or "").hostname
    except ValueError:
        return False
    host = tenant_host(legacy_url)
    return not host or host == urlsplit(origin).hostname


def endpoint(origin, path):
    return origin + "/api/v5" + path


def request_data(agent, origin, path, payload, *, transport=None):
    if path not in PATHS or not origin or base_url(origin, "nextsls") != origin:
        raise ProtocolError("未配置 NextSLS 租户或接口未获支持。")
    if not agent.is_active or not agent.supports_api or not agent.app_token:
        raise ProtocolError("代理已停用、未启用 API 或缺少 App Token。")
    # An established NextSLS legacy URL provides a second tenant boundary for its existing token.
    if not legacy_tenant_matches(agent.api_url, origin):
        raise ProtocolError("基础域名与现有 NextSLS 轨迹租户不一致，请检查代理配置。")
    target = SimpleNamespace(api_url=endpoint(origin, path), is_active=agent.is_active,
                             supports_api=agent.supports_api, app_token=agent.app_token,
                             app_key=agent.app_key, password=agent.password)
    try:
        response = (transport or carrier_http.post)(target, json=payload, headers={
            "Authorization": "Bearer " + agent.app_token, "Accept": "application/json",
            "Accept-Language": "zh-CN", "Content-Type": "application/json",
        })
        data = response.json()
    except (ValueError, carrier_http.CarrierRequestError):
        raise ProtocolError("NextSLS 请求失败或响应不可解析。") from None
    if response.status_code != 200 or not isinstance(data, dict) or type(data.get("status")) is not int:
        raise ProtocolError("NextSLS 返回了无效响应。")
    if data["status"] != 1:
        raise ProtocolError("NextSLS 拒绝请求，请核对账号权限及渠道必填项。")
    return object_value(data.get("data"))


def object_value(value):
    if not isinstance(value, dict):
        raise ProtocolError("NextSLS 数据结构与文档不符。")
    return value


def numeric(value, *, signed=False):
    if type(value) not in (str, int, float, Decimal) or len(str(value)) > 40:
        raise ProtocolError("数字格式无效。")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ProtocolError("数字格式无效。") from None
    if (not result.is_finite() or abs(result) > Decimal("1000000000000")
            or result.as_tuple().exponent < -12 or (not signed and result < 0)):
        raise ProtocolError("数字超出支持范围。")
    return result


def supplied_text(value, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ProtocolError("必要文本字段缺失或格式无效。")
    return value.strip()


def parse_supplier_number(data):
    number = object_value(data.get("shipment")).get("shipment_id")
    if not isinstance(number, str) or not CODE.fullmatch(number):
        raise ProtocolError("NextSLS 未返回有效的系统运单号。")
    return number


def service_profile(data, code):
    services = data.get("services")
    if not isinstance(services, list):
        raise ProtocolError("无法核实代理渠道的必填项。")
    found = [row for row in services if isinstance(row, dict) and row.get("code") == code]
    if len(found) != 1:
        raise ProtocolError("所选渠道不在该账号的可用服务列表中。")
    return found[0]


def address_values(values):
    for field in REQUIRED_ADDRESS:
        supplied_text(values.get(field), 200)
    if not re.fullmatch(r"[A-Z]{2}", values["country"]):
        raise ProtocolError("地址国家须为两位大写代码。")
    return {key: val for key, val in values.items() if val}


def create_payload(invoice, code, reference, profile, sender):
    """Translate a reviewed invoice; do not manufacture fields missing for the selected service."""
    if not CODE.fullmatch(code) or not CODE.fullmatch(reference):
        raise ProtocolError("渠道代码或系统对账参考号无效。")
    to_address = address_values({target: invoice["address"].get(source, "") for target, source in ADDRESS.items()})
    meta = invoice["metadata"]
    shipment = {
        "service": code, "client_reference": reference, "reference_1": invoice["customer_number"],
        "reference_2": meta["po_number"], "to_address": to_address,
        "declaration_currency": "USD",
        "attrs": [value for key, value in (("battery", "elec"), ("magnet", "magnetic"),
                                          ("liquid", "liquid"), ("powder", "powder")) if invoice["flags"].get(key)],
        "remark": meta.get("product_note", ""),
    }
    if invoice["address"].get("warehouse_code"):
        shipment["to_warehouse_code"] = invoice["address"]["warehouse_code"]
    for source, target, codes in (
        ("declaration_mode", "exportwith", {"普通报关": 3, "报关退税": 2}),
        ("clearance_mode", "importwith", {"一般贸易清关": 1, "快件清关": 2}),
        ("tax_mode", "taxwith", {"不包税": 1, "包税": 2, "自主税号": 3, "自税递延": 4}),
    ):
        if meta.get(source):
            if meta[source] not in codes:
                raise ProtocolError("报关、清关或交税方式尚无已确认的 NextSLS 映射。")
            shipment[target] = codes[meta[source]]
    tax_id = meta.get("vat_eori", "")
    if tax_id:
        match = re.fullmatch(r"(VAT|EORI)\s*[:：]\s*(\S+)", tax_id, re.I)
        if not match:
            raise ProtocolError("税号须用 VAT: 或 EORI: 标记，以免发送到错误字段。")
        shipment["vat_number" if match[1].upper() == "VAT" else "eori"] = match[2]
    for setting, field in (("tel_required", "tel"), ("email_required", "email"),
                           ("company_required", "company"), ("city_required", "city")):
        if required_flag(profile, setting) and not to_address.get(field):
            raise ProtocolError(f"渠道要求收件资料字段 {field}。")
    for setting, field in (("vat_number_required", "vat_number"), ("eori_required", "eori")):
        if required_flag(profile, setting) and not shipment.get(field):
            raise ProtocolError(f"渠道要求 {field}。")
    sender_requirement = profile.get("from_address")
    if sender_requirement not in ("yes", "no"):
        raise ProtocolError("渠道的发件地址规则待核实。")
    if sender or sender_requirement == "yes":
        shipment["from_address"] = address_values(
            {**sender, "company": sender.get("company") or meta.get("sender", "")})
    requirements = profile.get("require_declaration_list")
    if not isinstance(requirements, list) or any(not isinstance(key, str) for key in requirements):
        raise ProtocolError("渠道的产品申报必填项待核实。")
    if required_flag(profile, "packing_list_required"):
        raise ProtocolError("渠道要求额外装箱单附件，附件传输规则待核实。")
    parcels = {}
    for box in invoice["boxes"]:
        number = supplied_text(box["box_number"], 50)
        if number in parcels:
            raise ProtocolError("Excel 货箱号重复。")
        dimensions = {"client_" + key: float(numeric(box[source])) for key, source in BOX_DIMENSIONS.items()}
        if any(value <= 0 for value in dimensions.values()):
            raise ProtocolError("货箱重量尺寸须为正数。")
        # Numeric sequences are documented. Preserve the original customer box ID in reference.
        box_code = number if FBA_BOX_CODE.fullmatch(number) else str(len(parcels) + 1)
        parcels[number] = {"number": box_code, "reference": number, **dimensions, "declarations": []}
    if not parcels:
        raise ProtocolError("没有可提交的箱货数据。")
    for item in invoice["declarations"]:
        declaration = {target: supplied_text(item.get(source)) for target, source in DECLARATIONS.items()}
        qty = item["quantity"]
        if type(qty) is not int or qty <= 0:
            raise ProtocolError("产品申报数量无效。")
        declaration.update(unit_value=float(numeric(item["unit_price_usd"])), qty=qty,
                           is_battery=int(item.get("battery") is True))
        if any(key not in declaration or declaration[key] in (None, "") for key in requirements):
            raise ProtocolError("发票缺少代理渠道要求的申报项，请核对渠道规则。")
        if item.get("box_number") not in parcels:
            raise ProtocolError("申报商品没有对应货箱。")
        parcels[item["box_number"]]["declarations"].append(declaration)
    if any(not parcel["declarations"] for parcel in parcels.values()):
        raise ProtocolError("每个货箱至少需要一项申报。")
    shipment.update(parcels=list(parcels.values()), parcel_count=str(len(parcels)))
    return {"shipment": shipment}


def required_flag(profile, name):
    value = profile.get(name)
    if type(value) not in (int, str) or value not in (0, 1, "0", "1"):
        raise ProtocolError("渠道必填规则待核实。")
    return value in (1, "1")


def lookup_created(data, reference):
    matches = data.get("shipment")
    if not isinstance(matches, list) or len(matches) > 1:
        raise ProtocolError("查询结果不唯一或格式无效，须人工对账。")
    if not matches:
        return None
    record = object_value(matches[0])
    if record.get("client_reference") != reference:
        raise ProtocolError("返回的对账参考号与本票不一致。")
    return parse_supplier_number({"shipment": record})


SUMMARY_NUMBERS = (
    "parcel_count", "picked_count", "actual_volume", "actual_weight", "received_weight",
    "chargeable_weight", "volume_weight", "volume",
)
PARCEL_NUMBERS = (
    "actual_weight", "chargeable_weight", "chargeable_length", "chargeable_width", "chargeable_height",
)
PARCEL_IDENTIFIERS = ("number", "ext_number", "reference", "tracking_number")
FEE_NUMBERS = ("charge", "unit_price", "exchange_rate", "invoice_time")
FEE_METADATA = ("status", "settlement_status", "updated_at", "update_time")


def source_fields(row, fields):
    """Store only known scalar fields, including nulls and original JSON types."""
    return {key: row[key] for key in fields
            if key in row and type(row[key]) in (int, float, str, type(None))}


def normalize_snapshot(data, supplier_number, *, reference=None):
    shipment = object_value(data.get("shipment"))
    if parse_supplier_number(data) != supplier_number or (reference is not None and shipment.get("client_reference") != reference):
        raise ProtocolError("NextSLS 返回的运单身份不匹配。")
    summary = decimals(shipment, SUMMARY_NUMBERS)
    if any(Decimal(summary[key]) % 1 for key in ("parcel_count", "picked_count") if key in summary):
        raise ProtocolError("供应商箱数应为整数。")
    raw = {"shipment": source_fields(shipment, SUMMARY_NUMBERS), "parcels": None, "charges": None}
    parcels = None
    if shipment.get("parcels") is not None:
        parcels, raw["parcels"] = {}, {}
        for item in bounded_list(shipment["parcels"], 500):
            box = object_value(item)
            number = supplied_text(box.get("number"), 80)
            if number in parcels:
                raise ProtocolError("同一批次出现重复系统箱号。")
            parcels[number] = {"number": number, **decimals(box, PARCEL_NUMBERS)}
            for key in PARCEL_IDENTIFIERS[1:]:
                if box.get(key):
                    parcels[number][key] = supplied_text(box[key], 80)
            raw["parcels"][number] = source_fields(box, (*PARCEL_IDENTIFIERS, *PARCEL_NUMBERS))
    charges = None
    if shipment.get("charge_list") is not None:
        charges, raw["charges"] = [], []
        for item in bounded_list(shipment["charge_list"], 500):
            charge = object_value(item)
            currency = supplied_text(charge.get("currency"), 3)
            if not CURRENCY.fullmatch(currency):
                raise ProtocolError("代理费用币种无效。")
            amounts = decimals(charge, FEE_NUMBERS, signed=("charge", "unit_price"))
            if "charge" not in amounts:
                raise ProtocolError("代理费用缺少金额。")
            record = {"charge_type": supplied_text(charge.get("charge_type")), "currency": currency,
                      **amounts, "unit": supplied_text(charge["unit"], 40) if charge.get("unit") else ""}
            metadata = source_fields(charge, FEE_METADATA)
            for key, value in metadata.items():
                if value not in (None, ""):
                    record[key] = supplied_text(str(value))
            charges.append(record)
            raw["charges"].append(source_fields(charge, ("charge_type", "currency", "unit", *FEE_NUMBERS)) | metadata)
        charges.sort(key=lambda row: tuple(sorted(row.items())))
        raw["charges"].sort(key=lambda row: tuple(sorted((key, type(value).__name__, str(value))
                                                       for key, value in row.items())))
    return {"summary": summary, "parcels": parcels, "charges": charges, "raw": raw}


def decimals(data, fields, signed=()):
    return {key: format(numeric(data[key], signed=key in signed).normalize(), "f")
            for key in fields if data.get(key) not in (None, "")}


def bounded_list(value, max_items):
    if not isinstance(value, list) or len(value) > max_items:
        raise ProtocolError("接口列表为空缺格式或超出支持上限。")
    return value
