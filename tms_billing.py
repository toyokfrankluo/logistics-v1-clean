"""TMS receivables and pricing validation. Confirmed charges replace the legacy fee for linked shipments."""
from decimal import Decimal, ROUND_HALF_UP
import re

from flask import g
from sqlalchemy import inspect, select

from models import db
from tms_models import TmsReceivable


UNITS = {"kg": "KG", "cbm": "CBM", "box": "箱", "shipment": "票"}
SETTLEMENT_SOURCES = {"snapshot": "代理接口已核对计费量", "manual": "人工核实凭证"}
CURRENCIES = ("CNY", "USD", "EUR", "HKD", "GBP")
FIXED = {"surcharge": "附加费", "operation": "操作费", "high_value": "超值费"}
MAX_OTHER_FEES = 200
CENT = Decimal("0.01")


def billing_available():
    # Historical installations can still read their legacy data before the additive migration.
    if "tms_billing_available" not in g:
        g.tms_billing_available = inspect(db.session.connection()).has_table(TmsReceivable.__tablename__)
    return g.tms_billing_available


def linked_receivable(shipment_id):
    if not billing_available():
        return None
    return db.session.scalar(select(TmsReceivable).where(TmsReceivable.shipment_id == shipment_id))


def price_form(form):
    values = {name: form.get(name, "").strip() for name in (
        "unit", "rate", *FIXED, "currency",
        "other_mode", "review_note",
    )}
    fields = [form.getlist(name) for name in ("other_name", "other_amount", "other_currency", "other_note")]
    if len(set(map(len, fields))) != 1 or len(fields[0]) > MAX_OTHER_FEES:
        raise ValueError("其他费用格式不正确，单票最多 200 项。")
    values["other"] = [dict(zip(("name", "amount", "currency", "note"), (value.strip() for value in row)))
                       for row in zip(*fields)]
    return values


def valid_draft(values):
    if any(len(values[key]) > limit for key, limit in (
        ("unit", 10), ("rate", 24),
        ("surcharge", 24), ("operation", 24), ("high_value", 24), ("currency", 3),
        ("other_mode", 10), ("review_note", 500),
    )):
        raise ValueError("收费草稿的字段超出长度限制。")
    if any(len(row[key]) > limit for row in values["other"] for key, limit in (
        ("name", 100), ("amount", 24), ("currency", 3), ("note", 200),
    )):
        raise ValueError("其他费用的字段超出长度限制。")


def decimal_field(value, label, scale, maximum, *, positive=False):
    if not re.fullmatch(rf"[0-9]{{1,9}}(?:\.[0-9]{{1,{scale}}})?", value):
        raise ValueError(f"请填写有效的{label}（最多 {scale} 位小数）。")
    amount = Decimal(value)
    if amount > maximum or (positive and amount == 0):
        raise ValueError(f"{label}超出允许范围。")
    return amount


def confirm_sales_terms(values, form):
    valid_draft(values)
    unit, currency = values["unit"], values["currency"]
    if unit not in UNITS:
        raise ValueError("请选择计费方式。")
    if currency not in CURRENCIES:
        raise ValueError("请选择账单币种。")
    rate = decimal_field(values["rate"], "计费单价", 4, 1_000_000)
    charges = {key: decimal_field(values[key], label, 2, 100_000_000).quantize(CENT)
               for key, label in FIXED.items()}
    other = values["other"]
    if not ((values["other_mode"] == "none" and not other) or (values["other_mode"] == "items" and other)):
        raise ValueError("请完整添加其他费用，或明确选择「无其他费用」。")
    for index, row in enumerate(other, 1):
        if not row["name"] or row["currency"] != currency:
            raise ValueError(f"第 {index} 项其他费用须填写名称，且币种须与账单一致。")
    amounts = [decimal_field(row["amount"], f"第 {i} 项其他费用金额", 2, 100_000_000).quantize(CENT)
               for i, row in enumerate(other, 1)]
    if form.get("confirmed") != "yes":
        raise ValueError("请确认收费后再提交。")
    extras = sum(charges.values(), Decimal("0.00")) + sum(amounts)
    return {
        **values, "rate": str(rate), "billing_state": "pending",
        **{key: str(amount) for key, amount in charges.items()},
        "other": [{**row, "amount": str(amount)} for row, amount in zip(other, amounts)],
        "other_total": str(sum(amounts, Decimal("0.00"))), "extras_total": str(extras),
    }


def settle_charge(terms, raw_quantity, source, note):
    quantity = decimal_field(raw_quantity, "计费数量", 12, 1_000_000, positive=True)
    unit = terms["unit"]
    if (unit == "box" and quantity != quantity.to_integral_value()) or (unit == "shipment" and quantity != 1):
        raise ValueError("按箱须为整数，按票计费的一票订单须填写 1。")
    base = (quantity * Decimal(terms["rate"])).quantize(CENT, rounding=ROUND_HALF_UP)
    total = base + Decimal(terms["extras_total"])
    return dict(terms, billing_state="confirmed", quantity=str(quantity), quantity_source=source,
                source_note=note, base=str(base), total=str(total))


def receivable_totals(rows):
    totals = {}
    for shipment, receivable in rows:
        currency = receivable.currency if receivable else "CNY"
        amount = receivable.final_total if receivable else Decimal(str(shipment.fee or 0))
        if amount is None:
            continue
        totals[currency] = totals.get(currency, Decimal("0.00")) + amount
    return totals
