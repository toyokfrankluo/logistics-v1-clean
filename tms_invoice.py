"""Server-side reader for the Xingrui 11.6 single-order invoice."""
from contextlib import closing, contextmanager
from copy import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from itertools import islice
import re
from unicodedata import normalize
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

from openpyxl import Workbook, load_workbook
from openpyxl.formula.tokenizer import Tokenizer, TokenizerError
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from xml.etree.ElementTree import ParseError, fromstring

from tms_xlsx_images import (
    PHOTO_CELL, WPS_IMAGE_FORMULA, XLSX_NS, PhotoError, anchored_images, invoice_sheet_part,
    read_photo, validate_photos, wps_images,
)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
SHEETS = {"星睿发票", "地址库", "服务选项"}
SERVICES = (
    "美森限时达卡派", "美森限时达海派（CLX）", "超级经济线卡派",
    "合德/以星限时达海派", "合德/以星限时达卡派", "OA普船卡派",
    "OA普船海派", "美国空派", "欧洲空派", "欧洲海运", "欧洲铁路",
    "欧洲卡航", "UPS红单",
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. /-]{0,49}\Z")
ADDRESS_COLUMNS = {
    "recipient": 5, "company": 6, "phone": 7, "mobile": 8, "street": 9,
    "street2": 10, "street3": 11, "city": 12, "region": 13,
    "country": 14, "postal_code": 15,
}
ADDRESS_HEADERS = (
    "地址简称*", "地址编码*", "映射代码", "FBA仓库代码", "联系人*", "公司名",
    "联系电话", "联系手机", "地址一*", "地址二", "地址三", "城市*", "省/洲", "国家*", "邮编*",
)
ADDRESS_LOOKUPS = {
    5: "recipient", 6: "company", 7: "street", 8: "street2", 9: "street3",
    10: "city", 11: "region", 12: "postal_code", 13: "country", 14: "phone", 15: "mobile",
}
REQUIRED_ADDRESS = {
    "recipient": "L5（收件人姓名）", "street": "L7（收件地址一）", "city": "L10（城市）",
    "region": "L11（省份 / 州）", "postal_code": "L12（邮编）", "country": "L13（国家二字代码）",
}
REQUIRED_ORDER = {
    (4, 4): "客户 / FBA 订单号", (5, 4): "运输服务 / 渠道",
    (7, 4): "是否带电", (8, 4): "是否带磁", (9, 4): "是否带液体", (10, 4): "是否带粉末",
    (11, 4): "报关方式", (14, 4): "PO Number", (15, 4): "箱数", (4, 12): "地址库编码",
}
REQUIRED_LINE = {
    "A": "货箱编号 / FBA箱号", "B": "英文品名", "C": "中文品名", "D": "材质",
    "E": "用途", "F": "海关编码 / HS Code", "G": "申报单价", "H": "申报数量",
    "J": "货箱重量", "K": "箱长", "L": "箱宽", "M": "箱高", "N": "是否带电",
}
SAFE_FUNCTIONS = {"CHAR", "COUNTIF", "IF", "IFERROR", "INDEX", "MATCH", "OR", "ROUND", "SUM"}
SAFE_RANGE = re.compile(r"(?:(?:'?地址库'?)!)?\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?\Z")


class InvoiceError(ValueError):
    pass


def check_sheet_xml(xml, image_refs, *, invoice_photos=None):
    shared = set()
    for cell in fromstring(xml).iter(XLSX_NS + "c"):
        formula = cell.find(XLSX_NS + "f")
        if formula is None:
            continue
        expression = formula.text or ""
        coordinate = cell.get("r", "")
        image = WPS_IMAGE_FORMULA.fullmatch(expression)
        if image and not formula.attrib and invoice_photos is not None and PHOTO_CELL.fullmatch(coordinate):
            part = image_refs.get(image[1])
            if not part or coordinate in invoice_photos:
                raise InvoiceError(f"{coordinate}（产品图片）无法读取唯一的内嵌图片，请重新插入 PNG 或 JPEG。")
            invoice_photos[coordinate] = part
            continue
        if formula.get("t") == "shared":
            key = formula.get("si", "")
            ref = formula.get("ref")
            if not key.isdecimal() or set(formula.attrib) - {"t", "si", "ref"}:
                raise InvoiceError("工作簿包含不受支持的共享公式。")
            if not expression and key in shared and not ref:
                continue
            if key in shared or not ref or not SAFE_RANGE.fullmatch(ref):
                raise InvoiceError("工作簿包含不受支持的共享公式。")
            shared.add(key)
        elif formula.attrib:
            raise InvoiceError("工作簿包含不受支持的公式。")
        if not 1 <= len(expression) <= 512:
            raise InvoiceError("工作簿包含不受支持的公式。")
        for token in Tokenizer("=" + expression).items:
            if (token.type == "FUNC" and token.subtype == "OPEN"
                    and token.value[:-1].upper() not in SAFE_FUNCTIONS):
                raise InvoiceError("工作簿包含不受支持的公式函数。")
            if token.subtype == "RANGE" and not SAFE_RANGE.fullmatch(token.value.upper()):
                raise InvoiceError("工作簿包含外部或不受支持的公式引用。")


@contextmanager
def workbook_from_bytes(raw):
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise InvoiceError("每份 .xlsx 文件上限为 2 MiB。")
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries) > 200 or sum(e.file_size for e in entries) > 16 * 1024 * 1024
                    or len({entry.filename for entry in entries}) != len(entries)):
                raise InvoiceError("工作簿解压后超出大小限制。")
            for entry in entries:
                name = entry.filename.lower()
                if any(key in name for key in ("vbaproject", "externallinks/", "embeddings/", "activex/")):
                    raise InvoiceError("工作簿含宏、外部链接或嵌入对象。")
                if name.endswith((".xml", ".rels")):
                    xml = archive.read(entry)
                    # Reject XML entities even in UTF-16/32 workbooks.
                    if b"<!DOCTYPE" in xml.replace(b"\x00", b"").upper():
                        raise InvoiceError("工作簿包含不受支持的 XML 定义。")
                    if name.endswith(".rels") and any(
                        element.get("TargetMode", "").casefold() == "external"
                        for element in fromstring(xml)
                    ):
                        raise InvoiceError("工作簿包含外部链接。")
            invoice_part = invoice_sheet_part(archive)
            photos = anchored_images(archive, invoice_part)
            image_refs = wps_images(archive)
            for entry in entries:
                name = entry.filename
                if name == invoice_part or re.fullmatch(r"xl/worksheets/[^/]+\.xml", name, re.IGNORECASE):
                    check_sheet_xml(archive.read(entry), image_refs,
                                    invoice_photos=photos if name == invoice_part else None)
            validate_photos(archive, photos)
        book = load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    except PhotoError as exc:
        raise InvoiceError(str(exc)) from None
    except (BadZipFile, InvalidFileException, KeyError, ValueError, OSError, ParseError,
            RuntimeError, EOFError, NotImplementedError, CompressionError, TokenizerError) as exc:
        if isinstance(exc, InvoiceError):
            raise
        raise InvoiceError("无法读取 .xlsx，请使用星睿发票模板。") from None
    try:
        yield book, photos
    except (BadZipFile, InvalidFileException, KeyError, ParseError, TypeError, OverflowError, IndexError):
        raise InvoiceError("工作簿格式不正确。") from None
    finally:
        book.close()


def cell_text(cell, label, *, required=True, limit=200):
    raw = cell.value
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise InvoiceError(f"请填写{label}。")
        return ""
    if cell.data_type in {"f", "e"} or not isinstance(raw, str):
        raise InvoiceError(f"{label}须填写为文本，避免公式或前导零丢失。")
    text = raw.strip()
    if len(text) > limit or any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise InvoiceError(f"{label}格式或长度不正确。")
    return text


def identifier(cell, label):
    value = cell_text(cell, label, limit=50)
    if not IDENTIFIER.fullmatch(value):
        raise InvoiceError(f"{label}含不支持的字符。")
    return value


def number(cell, label, *, whole=False):
    raw = cell.value
    if isinstance(raw, bool) or cell.data_type in {"f", "e"} or not isinstance(raw, (str, int, float)):
        raise InvoiceError(f"{label}须填写数字。")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise InvoiceError(f"{label}须填写数字。") from None
    if (not value.is_finite() or not 0 < value <= 1_000_000
            or value.as_tuple().exponent < -6 or (whole and value != value.to_integral_value())):
        raise InvoiceError(f"{label}数值或精度超出范围。")
    return int(value) if whole else format(value.normalize(), "f")


def yes_no(cell, label):
    text = cell_text(cell, label, limit=20).casefold()
    if text in {"是", "yes", "y"}:
        return True
    if text in {"否", "no", "n"}:
        return False
    raise InvoiceError(f"{label}请选择「是」或「否」。")


def missing_value(cell):
    return cell.value is None or (isinstance(cell.value, str) and not cell.value.strip())


def catalogue_rows(book):
    if "地址库" not in book:
        raise InvoiceError("工作簿缺少「地址库」工作表。")
    sheet = book["地址库"]
    sheet.reset_dimensions()
    rows = sheet.iter_rows(max_col=15)
    header = next(rows, None)
    if header is None or tuple(cell.value for cell in header) != ADDRESS_HEADERS:
        raise InvoiceError("地址库表头与星睿模板不符。")
    for index, row in enumerate(rows, start=2):
        if index > 5000:
            raise InvoiceError("地址库超出 5,000 行。")
        if not missing_value(row[1]):
            yield row


def validate_address_row(row):
    missing = [label for key, label in REQUIRED_ADDRESS.items() if missing_value(row[ADDRESS_COLUMNS[key] - 1])]
    if missing:
        raise InvoiceError("地址库匹配行缺少必填资料：" + "、".join(missing) + "。")
    warehouse = cell_text(row[3], "地址库 FBA 仓库代码", required=False, limit=50)
    values = ["", "", "", warehouse] + [cell_text(cell, "地址库收件资料", required=False) for cell in row[4:]]
    values[13] = values[13].upper()
    if not re.fullmatch(r"[A-Z]{2}", values[13]):
        raise InvoiceError("L13（国家二字代码）须为两位字母。")
    return values


def validated_catalogue(raw):
    """Keep only checked address cells, never formulas, images or other sheets."""
    addresses = []
    codes = set()
    with workbook_from_bytes(raw) as (book, _photos):
        for row in catalogue_rows(book):
            code = identifier(row[1], "地址库编码").casefold()
            if code in codes:
                raise InvoiceError("地址库含重复编码，请先核对。")
            codes.add(code)
            values = validate_address_row(row)
            values[:3] = [cell_text(cell, "地址库资料", required=False) for cell in row[:3]]
            addresses.append(values)
    if not addresses:
        raise InvoiceError("地址库至少需要一条有效地址。")
    return addresses


def address_values(row):
    return {**{key: row[col - 1] for key, col in ADDRESS_COLUMNS.items()}, "warehouse_code": row[3]}


def workbook_address(book, code):
    seen = set()
    match = None
    for row in catalogue_rows(book):
        key = identifier(row[1], "地址库编码").casefold()
        if key in seen:
            raise InvoiceError("地址库含重复编码，请先核对。")
        seen.add(key)
        if key == code.casefold():
            match = row
    if match is None:
        raise InvoiceError("L4 地址编码在此工作簿的「地址库」中不存在。")
    return address_values(validate_address_row(match))


def comparable_address(value):
    return " ".join(normalize("NFC", value).split()).casefold()


def check_displayed_address(raw, rows, address):
    """Check visible values when present; never calculate or trust Excel formulas."""
    expected = {line: address[field] for line, field in ADDRESS_LOOKUPS.items()}
    expected[17] = "\n".join(filter(None, (
        address["company"], address["street"], address["street2"], address["street3"],
        f'{address["city"]}, {address["region"]} {address["postal_code"]}', address["country"],
    )))
    formulas = {line for line in expected if rows[line - 1][11].data_type == "f"}
    cached = {}
    if formulas:
        # The package and all formula text have already passed workbook_from_bytes.
        with closing(load_workbook(BytesIO(raw), read_only=True, data_only=True, keep_links=False)) as book:
            cached = {line: row[0] for line, row in enumerate(
                book["星睿发票"].iter_rows(min_row=5, max_row=17, min_col=12, max_col=12), start=5
            ) if line in formulas and row[0].data_type != "e"}
    for line, value in expected.items():
        cell = cached.get(line) if line in formulas else rows[line - 1][11]
        if cell is None or missing_value(cell):
            continue
        if comparable_address(cell_text(cell, f"L{line}（收件资料）", limit=1500)) != comparable_address(value):
            raise InvoiceError(f"L{line} 的收件资料与本工作簿地址库冲突。请核对 L4 并重新保存发票。")


def required_fields(rows, photos):
    missing = [
        f"{get_column_letter(col)}{line}（{label}）"
        for (line, col), label in REQUIRED_ORDER.items() if missing_value(rows[line - 1][col - 1])
    ]
    used_rows = []
    for line, row in enumerate(rows[22:52], start=23):
        if f"O{line}" not in photos and all(missing_value(cell) for i, cell in enumerate(row) if i != 8):
            continue
        used_rows.append((line, row))
        missing.extend(f"{col}{line}（{label}）" for col, label in REQUIRED_LINE.items()
                       if missing_value(row[ord(col) - ord("A")]))
        if f"O{line}" not in photos:
            missing.append(f"O{line}（产品图片，须内嵌 PNG 或 JPEG）")
    if missing:
        raise InvoiceError("请补齐以下必填项：" + "、".join(missing) + "。")
    if not used_rows:
        raise InvoiceError("请填写至少一行货箱及产品申报。")
    return used_rows


def hs_code(cell, line):
    if cell.data_type == "n" and type(cell.value) is int:
        code = str(cell.value)
        number_format = cell.number_format
        if re.fullmatch(r"0{2,20}", number_format):
            code = code.zfill(len(number_format))
    else:
        code = cell_text(cell, f"F{line}（海关编码 / HS Code）", limit=20)
    if not re.fullmatch(r"[0-9]{1,20}", code):
        raise InvoiceError(f"F{line}（海关编码 / HS Code）须填写数字编码。")
    return code


def parse_invoice(raw):
    with workbook_from_bytes(raw) as (book, photos):
        if set(book.sheetnames) != SHEETS:
            raise InvoiceError("请上传星睿三工作表单票模板。")
        sheet = book["星睿发票"]
        if sheet.max_row is None or sheet.max_row > 56 or sheet.max_column is None or sheet.max_column > 15:
            raise InvoiceError("仅支持 11.6 版单票模板和现有明细行，其他版式请单独确认。")
        sheet.reset_dimensions()
        rows = list(islice(sheet.iter_rows(min_row=1, max_col=15), 57))
        if (
            len(rows) != 56
            or "订单号" not in str(rows[3][0].value)
            or "地址库编码" not in str(rows[3][8].value)
            or "货箱编号" not in str(rows[21][0].value)
            or "英文品名" not in str(rows[21][1].value)
            or "货箱重量" not in str(rows[21][9].value)
        ):
            raise InvoiceError("发票表头与星睿 11.6 模板不符。")
        used_rows = required_fields(rows, photos)
        customer_number = identifier(rows[3][3], "客户单号")
        service = cell_text(rows[4][3], "运输服务")
        if service not in SERVICES:
            raise InvoiceError("运输服务不在星睿的 13 种服务中。")
        address_code = identifier(rows[3][11], "地址库编码")
        address = workbook_address(book, address_code)
        check_displayed_address(raw, rows, address)
        address["email"] = cell_text(rows[15][11], "收件邮箱", required=False)
        flags = {name: yes_no(rows[index][3], label) for index, name, label in (
            (6, "battery", "是否带电"), (7, "magnet", "是否带磁"),
            (8, "liquid", "是否带液体"), (9, "powder", "是否带粉末"),
        )}
        metadata = {key: cell_text(rows[index][3], label, required=key in {"declaration_mode", "po_number"}, limit=limit)
                    for index, key, label, limit in (
            (5, "sender", "发货公司", 200), (10, "declaration_mode", "报关方式", 200),
            (11, "clearance_mode", "清关方式", 200), (12, "tax_mode", "交税方式", 200),
            (13, "po_number", "PO Number", 100), (15, "vat_eori", "VAT / EORI", 100),
            (16, "product_note", "产品备注", 1000),
        )}
        boxes = {}
        declarations = []
        for line, row in used_rows:
            box_key = parse_box(row, line, boxes)
            unit_price = number(row[6], f"第 {line} 行申报单价")
            quantity = number(row[7], f"第 {line} 行申报数量", whole=True)
            total = (Decimal(unit_price) * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            declarations.append({
                "box_number": boxes[box_key]["box_number"],
                "name_en": cell_text(row[1], f"第 {line} 行英文品名"),
                "name_zh": cell_text(row[2], f"第 {line} 行中文品名"),
                "material": cell_text(row[3], f"D{line}（材质）"),
                "usage": cell_text(row[4], f"E{line}（用途）"),
                "hs_code": hs_code(row[5], line),
                "unit_price_usd": unit_price, "quantity": quantity,
                "total_usd": format(total, "f"),
                "battery": yes_no(row[13], f"第 {line} 行是否带电"),
                "image_part": photos[f"O{line}"], "image_cell": f"O{line}",
            })
        if number(rows[14][3], "D15（箱数）", whole=True) != len(boxes):
            raise InvoiceError("发票箱数与不同箱号的数量不一致。")
        return {
            "customer_number": customer_number, "service": service,
            "address_code": address_code, "address": address, "address_source": "workbook", "flags": flags,
            "metadata": metadata, "boxes": list(boxes.values()), "declarations": declarations,
        }


def product_photos(raw, invoice):
    """Recheck saved declaration order against the original workbook before transmission."""
    original = parse_invoice(raw)
    items = invoice["declarations"]
    fields = ("box_number", "name_zh", "name_en", "material", "usage", "hs_code",
              "unit_price_usd", "quantity", "battery", "image_part")
    if (original["customer_number"] != invoice["customer_number"]
            or len(items) != len(original["declarations"])):
        raise InvoiceError("保存的产品申报与原始发票不一致，请人工核对。")
    photos = []
    with ZipFile(BytesIO(raw)) as archive:
        for item, source in zip(items, original["declarations"]):
            if (any(item.get(field) != source[field] for field in fields)
                    or item.get("image_cell", source["image_cell"]) != source["image_cell"]):
                raise InvoiceError(f'{source["image_cell"]}（产品图片）与保存的申报不一致，请人工核对。')
            photos.append(read_photo(archive, source["image_part"]))
    return photos


def parse_box(row, line, boxes):
    box_number = identifier(row[0], f"A{line}（货箱编号）")
    key = box_number.casefold()
    measures = {
        field: number(cell, f"{col}{line}（{label}）")
        for field, col, label, cell in zip(
            ("weight_kg", "length_cm", "width_cm", "height_cm"), "JKLM",
            ("箱重", "箱长", "箱宽", "箱高"), row[9:13],
        )
    }
    if key in boxes and any(boxes[key][field] != value for field, value in measures.items()):
        raise InvoiceError(f"第 {line} 行的重复箱号有不同重量或尺寸。")
    boxes.setdefault(key, {"box_number": box_number, **measures})
    return key


def blank_invoice(catalogue=None, *, layout=None):
    """Generate a clean 11.6 invoice, including only this customer's assigned address rows."""
    book = Workbook()
    sheet = book.active
    sheet.title = "星睿发票"
    sheet.merge_cells("A1:O2")
    sheet["A1"] = "星睿物流发票模板 / SHIPPING INVOICE"
    left = (
        "客户 / FBA 订单号 *", "运输服务 / 渠道 *", "发货公司", "是否带电 *",
        "是否带磁 *", "是否带液体 *", "是否带粉末 *", "报关方式 *", "清关方式",
        "交税方式", "PO Number *", "箱数 *", "VAT / EORI", "产品备注",
    )
    right = (
        "地址库编码 *", "收件人姓名 *", "收件人公司", "收件地址一 *", "收件地址二",
        "收件地址三", "城市 *", "省份 / 州 *", "邮编 *", "国家二字代码 *", "收件人电话",
        "收件人手机", "收件人邮箱", "完整收件地址",
    )
    for index, label in enumerate(left, 4):
        if index < 17:
            sheet.merge_cells(f"A{index}:C{index}")
            sheet.merge_cells(f"D{index}:H{index}")
        sheet.cell(index, 1, label)
    for index, label in enumerate(right, 4):
        if index < 17:
            sheet.merge_cells(f"I{index}:K{index}")
            end_column = "N" if index == 4 else "O"
            sheet.merge_cells(f"L{index}:{end_column}{index}")
        sheet.cell(index, 9, label)
    for region, label in (
        ("A3:H3", "01 发件人信息 / 订单资料"), ("I3:O3", "02 收件人信息 / 地址库自动填充"),
        ("A20:O20", "03 产品与货物申报资料 / CUSTOMS DECLARATION"),
        ("A21:O21", "在 L4 选择地址编码，在 D5 选择服务。每条产品明细填写所有星号字段，图片插入 O 列。"),
        ("A54:E54", "合计"), ("A56:O56", "一份文件填写一票。按工作簿内的地址库匹配收件资料，提交后由星睿审核。"),
    ):
        sheet.merge_cells(region)
        cell = sheet[region.split(":")[0]]
        cell.value = label
        cell.font = Font(color="9C1F15", bold=True)
        cell.fill = PatternFill("solid", fgColor="EEEEEE")
    sheet.merge_cells("D17:H19")
    sheet.merge_cells("L17:O19")
    for column, label in REQUIRED_LINE.items():
        sheet[f"{column}22"] = label + " *"
    for column, label in {"F": "海关编码* / HS Code*", "G": "申报单价 (USD) *", "I": "总申报价值 (USD)",
                          "J": "货箱重量 (kg) *", "K": "箱长 (cm) *", "L": "箱宽 (cm) *",
                          "M": "箱高 (cm) *", "O": "产品图片*"}.items():
        sheet[f"{column}22"] = label
    for cell in sheet[22]:
        cell.font = Font(color="9C1F15" if "*" in cell.value else "111111", bold=True)
        cell.fill = PatternFill("solid", fgColor="FFE69A")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.column_dimensions[cell.column_letter].width = 19
    sheet.column_dimensions["O"].width = 31
    sheet.row_dimensions[22].height = 36
    for row in range(23, 53):
        sheet[f"I{row}"] = f'=IF(OR(G{row}="",H{row}=""),"",ROUND(G{row}*H{row},2))'
    sheet["F54"], sheet["G54"], sheet["H54"], sheet["I54"] = "总数量", "=SUM(H23:H52)", "总货值 (USD)", "=SUM(I23:I52)"

    for row, field in ADDRESS_LOOKUPS.items():
        col = get_column_letter(ADDRESS_COLUMNS[field])
        result = f"INDEX(地址库!${col}$2:${col}$5000,MATCH($L$4,地址库!$B$2:$B$5000,0))"
        sheet[f"L{row}"] = f'=IF($L$4="","",IFERROR(IF({result}="","",{result})&"",""))'
    sheet["O4"] = '=IF(L4="","请输入编码",IF(COUNTIF(地址库!$B$2:$B$5000,L4)=1,"地址匹配成功","地址编码不存在或重复"))'
    sheet["L17"] = ('=IF(L4="","",IF(L6="","",L6&CHAR(10))&L7&CHAR(10)&'
                    'IF(L8="","",L8&CHAR(10))&IF(L9="","",L9&CHAR(10))&L10&", "&L11&" "&L12&CHAR(10)&L13)')
    sheet["L17"].alignment = Alignment(wrap_text=True)
    sheet.freeze_panes = "A4"

    addresses = book.create_sheet("地址库")
    addresses.append(ADDRESS_HEADERS)
    if isinstance(catalogue, bytes):
        with workbook_from_bytes(catalogue) as (source, _photos):
            rows = list(catalogue_rows(source))
            if any(cell.data_type in {"f", "e"} for row in rows for cell in row):
                raise InvoiceError("分配的地址库必须是纯数据，请联系星睿核对。")
            catalogue = [[cell.value.strip() if isinstance(cell.value, str) else cell.value for cell in row]
                         for row in rows]
    if catalogue is not None:
        if not catalogue:
            raise InvoiceError("尚无可下载的地址，请联系星睿开通本客户的地址库。")
        for index, row in enumerate(catalogue, start=2):
            addresses.append(row)
            for cell in addresses[index]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
    services = book.create_sheet("服务选项")
    services.append(("服务 / 运输渠道",))
    for service in SERVICES:
        services.append((service,))
    for cells, values in (
        (("L4",), f"""INDIRECT("'地址库'!$B$2:$B${max(2, addresses.max_row)}")"""),
        (("D5",), """INDIRECT("'服务选项'!$A$2:$A$14")"""),
        (("D11",), '"普通报关,报关退税"'), (("D13",), '"包税,不包税"'),
        (("D7:D10", "N23:N52"), '"是,否"'),
    ):
        picker = DataValidation(type="list", formula1=values, allow_blank=False, showErrorMessage=True)
        sheet.add_data_validation(picker)
        for cell_range in cells:
            picker.add(cell_range)
    if layout is not None:
        copy_invoice_format(sheet, layout)
    for field in ("D4", "D14", "D16", "L4"):
        sheet[field].number_format = "@"
    for line in range(23, 53):
        sheet[f"A{line}"].number_format = sheet[f"F{line}"].number_format = "@"
    return book


def copy_invoice_format(sheet, raw):
    # Only presentation is copied. Original values, notes, images and address rows never follow the layout.
    with workbook_from_bytes(raw) as (validated, _photos):
        if "星睿发票" not in validated or (validated["星睿发票"].max_row, validated["星睿发票"].max_column) != (56, 15):
            raise InvoiceError("配置的模板版式须为星睿 11.6 单票模板。")
    source_book = load_workbook(BytesIO(raw), keep_links=False)
    try:
        source = source_book["星睿发票"]
        sheet.column_dimensions.clear()
        for key, dimension in source.column_dimensions.items():
            target = sheet.column_dimensions[key]
            target.width, target.min, target.max = dimension.width, dimension.min, dimension.max
        for key, dimension in source.row_dimensions.items():
            sheet.row_dimensions[key].height = dimension.height
        for row in source.iter_rows(min_row=1, max_row=56, max_col=15):
            for original in row:
                if original.has_style:
                    target = sheet[original.coordinate]
                    for key in ("font", "fill", "border", "alignment", "number_format"):
                        setattr(target, key, copy(getattr(original, key)))
    finally:
        source_book.close()
