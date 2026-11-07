"""TMS receivables and pricing validation. Confirmed charges replace the legacy fee for linked shipments."""
from decimal import Decimal, ROUND_HALF_UP
import re

from flask import g
from sqlalchemy import inspect, select

from models import db
from tms_models import TmsReceivable


UNITS = {"kg": "KG", "cbm": "CBM", "box": "箱", "shipment": "票"}
SOURCES = {"declared": "客户申报", "warehouse": "仓库实测", "carrier": "代理确认", "manual": "其他已核实资料"}
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
        "unit", "quantity", "quantity_source", "source_note", "rate", *FIXED, "currency",
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
        ("unit", 10), ("quantity", 24), ("quantity_source", 15), ("source_note", 200), ("rate", 24),
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


def confirm_price(values, form):
    valid_draft(values)
    unit, source, currency = (values[k] for k in ("unit", "quantity_source", "currency"))
    if unit not in UNITS:
        raise ValueError("请选择计费方式。")
    if source not in SOURCES or (source == "manual" and not values["source_note"]):
        raise ValueError("请选择计费数量来源；选择其他来源时请填写说明。")
    if currency not in CURRENCIES:
        raise ValueError("请选择账单币种。")
    quantity = decimal_field(values["quantity"], "计费数量", 3, 1_000_000, positive=True)
    if (unit == "box" and quantity != quantity.to_integral_value()) or (unit == "shipment" and quantity != 1):
        raise ValueError("按箱须为整数，按票计费的一票订单须填写 1。")
    rate = decimal_field(values["rate"], "计费单价", 4, 1_000_000)
    base = (quantity * rate).quantize(CENT, rounding=ROUND_HALF_UP)
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
    total = base + sum(charges.values()) + sum(amounts)
    return {
        **values, "quantity": str(quantity), "rate": str(rate), "base": str(base),
        **{key: str(amount) for key, amount in charges.items()},
        "other": [{**row, "amount": str(amount)} for row, amount in zip(other, amounts)],
        "other_total": str(sum(amounts, Decimal("0.00"))), "extras_total": str(total - base), "total": str(total),
    }


def receivable_totals(rows):
    totals = {}
    for shipment, receivable in rows:
        currency = receivable.currency if receivable else "CNY"
        amount = receivable.total if receivable else Decimal(str(shipment.fee or 0))
        totals[currency] = totals.get(currency, Decimal("0.00")) + amount
    return totals
