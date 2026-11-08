"""账单工作簿生成入口：保留后台明细版与旧 invoice 页格式。"""
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime
from decimal import Decimal
import math

# =========================
# Excel 导出
# =========================
def currency_format(currency):
    return '￥#,##0.00' if currency == "CNY" else f'"{currency}" #,##0.00'


def export_invoice(company_info, customer_name, orders, bank_accounts,
                   filename=None, date_from=None, date_to=None):
    """
    导出账单 Excel（xlsx）。
    company_info: dict 或 str。如果是 dict，尝试读取 key "公司名称" 和 "地址"；
                  如果是字符串则直接当作公司名。
    customer_name: 客户名称字符串
    orders: 列表，每项为 dict，包含 keys: date, order_no, service_no, qty, type,
           weight, destination, channel, fee (float), summary
    bank_accounts: BankAccount ORM 对象列表（或元素含 bank_name, account_name, account_no 属性/键）
    filename: 默认返回独立 BytesIO；显式传路径时才保存文件并返回路径
    date_from/date_to: 账单导出的日期范围
    """

    # 解析 company_info（兼容 dict 或 str）
    if isinstance(company_info, dict):
        company_name = company_info.get("公司名称") or company_info.get("name") or "公司名称"
        company_addr = company_info.get("地址") or company_info.get("address") or ""
    else:
        company_name = str(company_info or "")
        company_addr = ""

    wb = Workbook()
    ws = wb.active
    ws.title = "账单"

    # 样式
    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")
    header_font = Font(size=12, bold=True)
    thin = Side(border_style="thin", color="000000")
    thin_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # 固定列数
    MAX_COL = 11

    # ===== 顶部公司名 =====
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=MAX_COL)
    c = ws.cell(row=1, column=1, value=company_name)
    c.alignment = align_center
    c.font = Font(size=18, bold=True)

    # ===== 标题（第 2 行）=====
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=MAX_COL)
    sub = ws.cell(row=2, column=1, value="快件运费收款通知单")
    sub.alignment = align_center
    sub.font = Font(size=14, bold=True)

    # ===== 客户名 + 日期范围（第 3 行）=====
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=MAX_COL - 1)
    client_cell = ws.cell(row=3, column=1, value=f"客户名称: {customer_name}")
    client_cell.alignment = align_left
    client_cell.font = Font(size=11)

    # 日期范围
    if date_from or date_to:
        if isinstance(date_from, datetime):
            date_from_str = date_from.strftime("%Y-%m-%d")
        else:
            date_from_str = str(date_from) if date_from else ""
        if isinstance(date_to, datetime):
            date_to_str = date_to.strftime("%Y-%m-%d")
        else:
            date_to_str = str(date_to) if date_to else ""
        date_range_str = f"{date_from_str} ~ {date_to_str}" if (date_from_str or date_to_str) else ""
    else:
        date_range_str = datetime.today().strftime("%Y-%m-%d")

    ws.cell(row=3, column=MAX_COL, value=f"账单日期: {date_range_str}").alignment = align_right

    # ===== 表头 =====
    headers = ["序号", "日期", "订单号", "服务商单号", "件数", "类型",
               "计费重/Kg", "目的地", "运输渠道", "合计费用", "账单摘要"]
    header_row = 4
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=h)
        cell.alignment = align_center
        cell.font = header_font
        cell.border = thin_border

    # ===== 写订单数据 =====
    start_data_row = header_row + 1
    currency_totals = {}
    total_weight = None
    for idx, order in enumerate(orders, start=1):
        r = start_data_row + (idx - 1)
        ws.cell(row=r, column=1, value=idx).alignment = align_center
        ws.cell(row=r, column=2, value=order.get("date", "")).alignment = align_center
        ws.cell(row=r, column=3, value=order.get("order_no", "")).alignment = align_left
        ws.cell(row=r, column=4, value=order.get("service_no", "")).alignment = align_left
        ws.cell(row=r, column=5, value=order.get("qty", "")).alignment = align_center
        ws.cell(row=r, column=6, value=order.get("type", "")).alignment = align_center
        w = order.get("weight")
        ws.cell(row=r, column=7, value=w).alignment = align_center
        ws.cell(row=r, column=8, value=order.get("destination", "")).alignment = align_center
        ws.cell(row=r, column=9, value=order.get("channel", "")).alignment = align_center
        fee_val = Decimal(str(order.get("fee") or 0))
        currency = order.get("currency", "CNY")
        fee_cell = ws.cell(row=r, column=10, value=fee_val)
        fee_cell.number_format = currency_format(currency)
        fee_cell.alignment = align_right
        ws.cell(row=r, column=11, value=order.get("summary", "")).alignment = align_left

        for col_idx in range(1, MAX_COL + 1):
            ws.cell(row=r, column=col_idx).border = thin_border

        currency_totals[currency] = currency_totals.get(currency, Decimal("0.00")) + fee_val
        if w not in (None, ""):
            total_weight = (total_weight or Decimal(0)) + Decimal(str(w))

    after_data_row = start_data_row + max(0, len(orders)) + 1

    # ===== 合计 =====
    ws.cell(row=after_data_row, column=1, value="总票数:").alignment = align_left
    ws.cell(row=after_data_row, column=2, value=len(orders)).alignment = align_left

    ws.cell(row=after_data_row + 1, column=1, value="总重量:").alignment = align_left
    if total_weight is not None:
        ws.cell(row=after_data_row + 1, column=2, value=f"{total_weight:.3f} Kg").alignment = align_left

    for index, (currency, total) in enumerate(sorted((currency_totals or {"CNY": Decimal("0.00")}).items())):
        row = after_data_row + 2 + index
        label = "总费用:" if currency_totals.keys() in ({"CNY"}, set()) else f"总费用（{currency}）:"
        ws.cell(row=row, column=1, value=label).alignment = align_left
        total_fee_cell = ws.cell(row=row, column=2, value=total)
        total_fee_cell.number_format = currency_format(currency)
        total_fee_cell.alignment = align_right

    # ===== 银行账户 =====
    bank_start_row = after_data_row + 3 + max(1, len(currency_totals))
    ws.cell(row=bank_start_row, column=1, value="收款银行账号").font = Font(bold=True)
    cur = bank_start_row + 1
    for ba in bank_accounts:
        if hasattr(ba, "bank_name"):
            bank_name = getattr(ba, "bank_name")
            account_name = getattr(ba, "account_name")
            account_no = getattr(ba, "account_no")
        elif isinstance(ba, dict):
            bank_name = ba.get("bank_name", "")
            account_name = ba.get("account_name", "")
            account_no = ba.get("account_no", "")
        else:
            bank_name = str(getattr(ba, "bank_name", "") or "")
            account_name = str(getattr(ba, "account_name", "") or "")
            account_no = str(getattr(ba, "account_no", ""))

        ws.cell(row=cur, column=1, value=f"开户行：{bank_name}").alignment = align_left
        ws.cell(row=cur + 1, column=1, value=f"户名：{account_name}").alignment = align_left
        ws.cell(row=cur + 2, column=1, value=f"账号：{account_no}").alignment = align_left
        cur += 4

    # ===== 页脚 =====
    footer_row = max(cur, bank_start_row + 1) + 2
    ws.merge_cells(start_row=footer_row, start_column=1, end_row=footer_row, end_column=MAX_COL)
    footer_cell = ws.cell(row=footer_row, column=1,
                          value=f"公司地址：{company_addr or '深圳市宝安区福永镇福海街道同富路3号惠明盛工业园5栋一楼'}")
    footer_cell.alignment = Alignment(horizontal="center", vertical="center")
    footer_cell.font = Font(size=9, color="808080")

    # ===== 列宽 =====
    col_widths = {
        1: 5,    # 序号
        2: 12,   # 日期
        3: 18,   # 订单号
        4: 18,   # 服务商单号
        5: 8,    # 件数
        6: 10,   # 类型
        7: 12,   # 计费重
        8: 12,   # 目的地
        9: 14,   # 运输渠道
        10: 14,  # 合计费用
        11: 40   # 摘要（加宽）
    }
    for col_idx, width in col_widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    if any(order.get("pricing") for order in orders):
        breakdown = wb.create_sheet("TMS 收费明细")
        breakdown.append(("客户单号", "收费项目", "币种", "金额"))
        breakdown.freeze_panes = "A2"
        breakdown.column_dimensions["A"].width = 22
        breakdown.column_dimensions["B"].width = 32
        for order in orders:
            details = order.get("pricing")
            if not details:
                continue
            charges = [(label, details[key]) for key, label in (
                ("base", "基础运费"), ("surcharge", "附加费"), ("operation", "操作费"), ("high_value", "超值费"),
            )]
            charges.extend((item["name"], item["amount"]) for item in details["other"])
            charges.append(("应收总额", details["total"]))
            for name, amount in charges:
                breakdown.append((order["order_no"], name, details["currency"], Decimal(amount)))

    for worksheet in wb:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.data_type == 'f':
                    cell.data_type = 's'

    if filename is None:
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        return output
    wb.save(filename)
    return filename


def generate_invoice_xlsx(company_name, customer_name, bank_info, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "账单"

    ws.merge_cells("A1:I1")
    ws["A1"] = company_name

    ws["A3"] = "客户名称:"
    ws["B3"] = customer_name

    headers = ["序号", "日期", "订单号", "服务商单号", "件数", "计费重/KG", "目的地", "运输渠道", "合计费用", "账单摘要"]
    ws.append(headers)

    start_row = ws.max_row + 1
    for idx, r in enumerate(rows, start=1):
        r2 = r + [""] * (9 - len(r))
        ws.append([idx] + r2[:9])

    total = 0.0
    data_start = start_row
    for i in range(len(rows)):
        try:
            cell = ws.cell(row=data_start + i, column=9)
            if cell.value is not None:
                total += float(str(cell.value))
        except Exception:
            pass

    summary_row = data_start + len(rows) + 1
    ws[f"H{summary_row}"] = "合计费用"
    ws[f"I{summary_row}"] = total

    info_row = summary_row + 2
    ws[f"A{info_row}"] = "收款银行信息"
    ws[f"A{info_row+1}"] = bank_info

    for col in ws.columns:
        max_length = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                val = str(cell.value) if cell.value else ""
                if len(val) > max_length:
                    max_length = len(val)
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_length + 2, 60)

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio
