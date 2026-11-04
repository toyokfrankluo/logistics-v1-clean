"""Validate partial edits before changing the ORM object."""
import math

from models import Shipment


def update_shipment(shipment, form, allow_manual_fee=False):
    values = {}
    for field in ("tracking_number", "shipment_id", "third_party_tracking_number",
                  "carrier_id", "origin", "destination", "channel", "product_type",
                  "note", "status"):
        if field in form:
            values[field] = form[field].strip()
    if "shipment_id" not in form and "agent_tracking_number" in form:
        values["shipment_id"] = form["agent_tracking_number"].strip()
    if "tracking_number" in values:
        if not values["tracking_number"]:
            raise ValueError("运单号不能为空")
        duplicate = Shipment.query.filter(
            Shipment.tracking_number == values["tracking_number"],
            Shipment.id != shipment.id,
        ).first()
        if duplicate:
            raise ValueError("运单号已存在")
    for field in ("customer_id", "agent_id"):
        if field in form:
            values[field] = int(form[field]) if form[field] else None
    if "pieces" in form:
        values["pieces"] = int(form["pieces"] or 1)
        if values["pieces"] < 1:
            raise ValueError("件数必须大于零")
    fees = ("weight", "unit_price", "surcharge_extra", "operation_fee", "high_value_fee")
    for field in fees:
        if field in form:
            number = float(form[field] or 0)
            if not math.isfinite(number) or number < 0:
                raise ValueError("费用和重量必须为非负有限数字")
            values[field] = number
    if any(field in values for field in fees):
        get = lambda field: values.get(field, getattr(shipment, field) or 0)
        values["fee"] = round(get("weight") * get("unit_price") +
                              sum(get(field) for field in fees[2:]), 2)
    if allow_manual_fee and form.get("fee", "").strip():
        fee = float(form["fee"])
        if not math.isfinite(fee) or fee < 0:
            raise ValueError("费用必须为非负有限数字")
        values["fee"] = fee
    for field, value in values.items():
        setattr(shipment, field, value)
