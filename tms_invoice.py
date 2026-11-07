"""Server-side reader for the Xingrui 11.6 single-order invoice."""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from io import BytesIO
from itertools import islice
import re
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

from openpyxl import Workbook, load_workbook
from openpyxl.formula.tokenizer import Tokenizer, TokenizerError
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from xml.etree.ElementTree import ParseError, fromstring

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
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SAFE_FUNCTIONS = {"CHAR", "COUNTIF", "IF", "IFERROR", "INDEX", "MATCH", "OR", "ROUND", "SUM"}
SAFE_RANGE = re.compile(r"(?:(?:'?地址库'?)!)?\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?\Z")


class InvoiceError(ValueError):
    pass


def check_sheet_xml(xml):
    shared = set()
    for formula in fromstring(xml).iter(XLSX_NS + "f"):
        expression = formula.text or ""
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
                    if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
                        check_sheet_xml(xml)
        book = load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, InvalidFileException, KeyError, ValueError, OSError, ParseError,
            RuntimeError, EOFError, NotImplementedError, CompressionError, TokenizerError) as exc:
        if isinstance(exc, InvoiceError):
            raise
        raise InvoiceError("无法读取 .xlsx，请使用星睿发票模板。") from None
    try:
        yield book
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


def resolve_address(raw, code):
    """Resolve only against the catalogue assigned to this customer by Xingrui."""
    with workbook_from_bytes(raw) as book:
        if "地址库" not in book:
            raise InvoiceError("客户地址库缺少「地址库」工作表。")
        sheet = book["地址库"]
        sheet.reset_dimensions()
        rows = sheet.iter_rows(max_col=15)
        header = next(rows, None)
        if header is None or header[1].value != "地址编码*":
            raise InvoiceError("客户地址库表头与星睿模板不符。")
        matches = []
        for index, row in enumerate(rows, start=2):
            if index > 5000:
                raise InvoiceError("客户地址库超出 5,000 行。")
            cell = row[1]
            if cell.data_type == "s" and cell.value.strip().casefold() == code.casefold():
                matches.append(row)
        if len(matches) != 1:
            raise InvoiceError("此地址编码在分配给本客户的地址库中不存在或重复。")
        row = matches[0]
        address = {
            key: cell_text(row[col - 1], "地址库收件资料",
                           required=key in {"recipient", "street", "city", "country", "postal_code"})
            for key, col in ADDRESS_COLUMNS.items()
        }
        address["country"] = address["country"].upper()
        if not re.fullmatch(r"[A-Z]{2}", address["country"]):
            raise InvoiceError("地址库国家须为两位字母代码。")
        return address


def parse_invoice(raw, catalogue):
    with workbook_from_bytes(raw) as book:
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
        customer_number = identifier(rows[3][3], "客户单号")
        service = cell_text(rows[4][3], "运输服务")
        if service not in SERVICES:
            raise InvoiceError("运输服务不在星睿的 13 种服务中。")
        address_code = identifier(rows[3][11], "地址库编码")
        address = resolve_address(catalogue, address_code)
        address["email"] = cell_text(rows[15][11], "收件邮箱", required=False)
        flags = {name: yes_no(rows[index][3], label) for index, name, label in (
            (6, "battery", "是否带电"), (7, "magnet", "是否带磁"),
            (8, "liquid", "是否带液体"), (9, "powder", "是否带粉末"),
        )}
        metadata = {key: cell_text(rows[index][3], label, required=False, limit=limit)
                    for index, key, label, limit in (
            (5, "sender", "发货公司", 200), (10, "declaration_mode", "报关方式", 200),
            (11, "clearance_mode", "清关方式", 200), (12, "tax_mode", "交税方式", 200),
            (13, "po_number", "PO Number", 100), (15, "vat_eori", "VAT / EORI", 100),
            (16, "product_note", "产品备注", 1000),
        )}
        boxes = {}
        declarations = []
        current_box = None
        for line, row in enumerate(rows[22:52], start=23):
            if not any(cell.value not in (None, "") for col, cell in enumerate(row) if col != 8):
                current_box = None
                continue
            current_box = parse_box(row, line, boxes, current_box)
            unit_price = number(row[6], f"第 {line} 行申报单价")
            quantity = number(row[7], f"第 {line} 行申报数量", whole=True)
            declarations.append({
                "box_number": boxes[current_box]["box_number"],
                "name_en": cell_text(row[1], f"第 {line} 行英文品名"),
                "name_zh": cell_text(row[2], f"第 {line} 行中文品名"),
                "material": cell_text(row[3], "材质", required=False),
                "usage": cell_text(row[4], "用途", required=False),
                "hs_code": cell_text(row[5], "HS Code", required=False),
                "unit_price_usd": unit_price, "quantity": quantity,
                "total_usd": format(Decimal(unit_price) * quantity, "f"),
                "battery": yes_no(row[13], f"第 {line} 行是否带电"),
                "notes": cell_text(row[14], "产品说明", required=False, limit=1000),
            })
        if not boxes:
            raise InvoiceError("请填写至少一行货箱及产品申报。")
        if rows[14][3].value not in (None, "") and number(rows[14][3], "箱数", whole=True) != len(boxes):
            raise InvoiceError("发票箱数与不同箱号的数量不一致。")
        return {
            "customer_number": customer_number, "service": service,
            "address_code": address_code, "address": address, "flags": flags,
            "metadata": metadata, "boxes": list(boxes.values()), "declarations": declarations,
        }


def parse_box(row, line, boxes, current_box):
    measurement_cells = row[9:13]
    has_dimensions = any(cell.value not in (None, "") for cell in measurement_cells)
    if row[0].value in (None, ""):
        if current_box is None or has_dimensions:
            raise InvoiceError(f"第 {line} 行须填写货箱编号。")
        return current_box
    box_number = identifier(row[0], f"第 {line} 行货箱编号")
    key = box_number.casefold()
    if key not in boxes or has_dimensions:
        measures = {
            field: number(cell, f"第 {line} 行{label}")
            for field, label, cell in zip(
                ("weight_kg", "length_cm", "width_cm", "height_cm"),
                ("箱重", "箱长", "箱宽", "箱高"), measurement_cells,
            )
        }
        if key in boxes and any(boxes[key][field] != value for field, value in measures.items()):
            raise InvoiceError(f"第 {line} 行的重复箱号有不同重量或尺寸。")
        boxes.setdefault(key, {"box_number": box_number, **measures})
    return key


def blank_invoice():
    """Generate an empty, layout-compatible template without a private address catalogue."""
    book = Workbook()
    sheet = book.active
    sheet.title = "星睿发票"
    sheet.merge_cells("A1:O2")
    sheet["A1"] = "星睿国际物流 · 单票发票"
    left = (
        "客户 / FBA 订单号 *", "运输服务 / 渠道 *", "发货公司", "是否带电 *",
        "是否带磁 *", "是否带液体 *", "是否带粉末 *", "报关方式", "清关方式",
        "交税方式", "PO Number", "箱数", "VAT / EORI", "产品备注",
    )
    right = (
        "地址库编码 *", "收件人姓名", "收件人公司", "收件地址一", "收件地址二",
        "收件地址三", "城市", "省份 / 州", "邮编", "国家二字代码", "收件人电话",
        "收件人手机", "收件人邮箱",
    )
    for index, label in enumerate(left, 4):
        sheet.merge_cells(start_row=index, start_column=1, end_row=index, end_column=3)
        sheet.merge_cells(start_row=index, start_column=4, end_row=index, end_column=8)
        sheet.cell(index, 1, label)
    for index, label in enumerate(right, 4):
        sheet.merge_cells(start_row=index, start_column=9, end_row=index, end_column=11)
        sheet.merge_cells(start_row=index, start_column=12, end_row=index, end_column=15)
        sheet.cell(index, 9, label)
    headers = (
        "货箱编号 / FBA箱号 *", "英文品名 *", "中文品名 *", "材质", "用途",
        "海关编码 / HS Code", "申报单价 (USD) *", "申报数量 *", "总申报价值 (USD)",
        "货箱重量 (kg) *", "箱长 (cm) *", "箱宽 (cm) *", "箱高 (cm) *",
        "是否带电 *", "产品图片 / 说明",
    )
    for index, label in enumerate(headers, 1):
        cell = sheet.cell(22, index, label)
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="17385F")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.column_dimensions[cell.column_letter].width = 17
    sheet.row_dimensions[22].height = 36
    for row in range(23, 53):
        for column in (1, 6):
            sheet.cell(row, column).number_format = "@"
        sheet[f"I{row}"] = f'=IF(OR(G{row}="",H{row}=""),"",G{row}*H{row})'
    for field in ("D4", "D14", "D16", "L4"):
        sheet[field].number_format = "@"
    sheet.freeze_panes = "D23"
    sheet["A56"] = "一份文件填写一票。地址编码由星睿分配，上传时以服务端核验地址为准。"
    sheet.merge_cells("A56:O56")
    addresses = book.create_sheet("地址库")
    addresses.append(("地址简称*", "地址编码*", "映射代码", "FBA仓库代码", "联系人*",
                      "公司名", "联系电话", "联系手机", "地址一*", "地址二", "地址三",
                      "城市*", "省/洲", "国家*", "邮编*"))
    services = book.create_sheet("服务选项")
    services.append(("服务 / 运输渠道",))
    for service in SERVICES:
        services.append((service,))
    picker = DataValidation(type="list", formula1='INDIRECT("\'服务选项\'!$A$2:$A$14")', allow_blank=False)
    sheet.add_data_validation(picker)
    picker.add("D5")
    yes_no_picker = DataValidation(type="list", formula1='"是,否"', allow_blank=False)
    sheet.add_data_validation(yes_no_picker)
    yes_no_picker.add("D7:D10")
    yes_no_picker.add("N23:N52")
    return book
