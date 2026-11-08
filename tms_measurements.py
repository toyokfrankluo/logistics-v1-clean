"""Interpret a NextSLS measurement snapshot and project the fields a customer may see."""
from collections import Counter
from decimal import Decimal

from nextsls import FBA_BOX_CODE


SOURCE = "NextSLS 运单详情"
UNIT_CHOICES = {
    "actual_volume": ("运单 actual_volume（确认作实重时选 kg）", ("kg",)),
    "actual_weight": ("运单实重", ("kg",)),
    "received_weight": ("运单收货重", ("kg",)),
    "chargeable_weight": ("运单计费量", ("kg", "m³")),
    "volume": ("运单体积", ("m³",)),
    "volume_weight": ("运单材积重", ("kg",)),
    "parcel_actual_weight": ("逐箱实重", ("kg",)),
    "parcel_chargeable_weight": ("逐箱计费量", ("kg", "m³")),
    "parcel_dimensions": ("逐箱计费长宽高", ("cm",)),
}
SUMMARY_LABELS = {
    "parcel_count": "供应商箱数", "picked_count": "已收货箱数", "actual_volume": "供应商原字段 actual_volume",
    "actual_weight": "运单实重", "received_weight": "收货重", "chargeable_weight": "计费量",
    "volume": "总体积", "volume_weight": "材积重",
}
PARCEL_FIELDS = {
    "actual_weight": ("逐箱实重", "parcel_actual_weight"),
    "chargeable_weight": ("逐箱计费量", "parcel_chargeable_weight"),
    "chargeable_length": ("计费长", "parcel_dimensions"),
    "chargeable_width": ("计费宽", "parcel_dimensions"),
    "chargeable_height": ("计费高", "parcel_dimensions"),
}


def summary_values(details):
    summary = details.get("summary", {})
    if "actual_volume" not in summary and details.get("unconfirmed_actual_volume") is not None:
        return {**summary, "actual_volume": details["unconfirmed_actual_volume"]}
    return summary


def has_measurements(details):
    return bool(summary_values(details) or details.get("parcels"))


def same_measurements(before, after):
    # Raw types may change between otherwise identical responses. Box identity and normalized values matter.
    def values(details):
        return {number: {key: row.get(key) for key in (*PARCEL_FIELDS, "ext_number", "reference")}
                for number, row in (details.get("parcels") or {}).items()}
    return summary_values(before) == summary_values(after) and values(before) == values(after)


def available_unit_choices(details):
    values = summary_values(details)
    present = set(values) & UNIT_CHOICES.keys()
    if values.get("volume_weight") is not None and Decimal(values["volume_weight"]) == 0:
        present.discard("volume_weight")
    for row in (details.get("parcels") or {}).values():
        present.update(unit_key for field, (_, unit_key) in PARCEL_FIELDS.items() if field in row)
    return {key: choice for key, choice in UNIT_CHOICES.items() if key in present}


def field_value(value, unit):
    if value is None:
        return {"value": None, "unit": "", "status": "未返回"}
    return {"value": Decimal(value), "unit": unit, "status": "已确认" if unit else "单位待确认"}


def matched_boxes(invoice, supplier_boxes):
    declared = {box["box_number"]: box for box in invoice.get("boxes", [])}
    candidates = {}
    for number, row in supplier_boxes.items():
        references = {row.get(key) for key in ("ext_number", "reference")} & declared.keys()
        if not references and number in declared and FBA_BOX_CODE.fullmatch(number):
            references = {number}
        if len(references) == 1:
            candidates[number] = references.pop()
    counts = Counter(candidates.values())
    return {number: declared[code] for number, code in candidates.items() if counts[code] == 1}


def measurement_view(invoice, details, *, internal=False):
    """Build an explicit allowlist; customer callers never receive the snapshot or supplier identifiers."""
    units = details.get("measurement_review", {}).get("units", {})
    if not internal and not details.get("identity_verified"):
        return None
    summary, values = [], summary_values(details)
    for key, label in SUMMARY_LABELS.items():
        value = values.get(key)
        unit = ("箱" if key in ("parcel_count", "picked_count") else units.get(key, ""))
        row = field_value(value, unit)
        if key == "actual_volume":
            if unit == "kg":
                label = "运单实重"
            elif value is not None:
                row["status"] = "含义及单位待确认"
        if key == "volume_weight" and row["value"] == 0:
            row.update(unit="", status="零值含义待确认")
        if key == "chargeable_weight" and unit:
            label = "计费体积" if unit == "m³" else "计费重量"
        if internal or row["status"] == "已确认":
            summary.append({**row, "label": label, "field": key} if internal else {**row, "label": label})
    supplier_boxes = details.get("parcels") or {}
    matches = matched_boxes(invoice, supplier_boxes)
    boxes = []
    for number, parcel in supplier_boxes.items():
        declared = matches.get(number)
        if not internal and declared is None:
            continue
        fields = []
        for key, (label, unit_key) in PARCEL_FIELDS.items():
            row = field_value(parcel.get(key), units.get(unit_key, ""))
            if key == "chargeable_weight" and row["unit"]:
                label = "逐箱计费体积" if row["unit"] == "m³" else "逐箱计费重量"
            if internal or row["status"] == "已确认":
                fields.append({**row, "label": label, "field": key} if internal else {**row, "label": label})
        if not internal and not fields:
            continue
        box = {"customer_box": declared["box_number"] if declared else None,
               "declared": declared if internal else None, "fields": fields}
        if internal:
            box.update(number=number, ext_number=parcel.get("ext_number"))
        boxes.append(box)
    return {"summary": summary, "boxes": boxes,
            "unmatched_boxes": len(supplier_boxes) - len(matches) if internal else 0}
