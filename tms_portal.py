"""Customer ordering and administrator review. This module performs no carrier calls."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
from secrets import token_hex
from unicodedata import normalize
from zipfile import BadZipFile, ZipFile

import click
from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request,
    send_file, session, url_for,
)
from flask_login import current_user, logout_user
from sqlalchemy import func, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from models import CarrierAgent, Customer, ManualTrack, Shipment, db
from tms_invoice import (
    IDENTIFIER, REQUIRED_ADDRESS, InvoiceError, MAX_UPLOAD_BYTES, SERVICES, address_values,
    blank_invoice, comparable_address, parse_invoice, validated_catalogue,
)
from tms_models import TmsAccount, TmsAddressBook, TmsAudit, TmsOrder, TmsReceivable, TmsChargeAdjustment
from tms_billing import (
    CURRENCIES, FIXED, SOURCES, UNITS, billing_available, confirm_price, price_form, valid_draft,
)
from tms_xlsx_images import MEDIA_PART, image_type
import nextsls
import tms_carriers
import tms_measurements as measurements
from tms_models import TmsAgentApi, TmsSupplierEvent, TmsSupplierOrder

client = Blueprint("tms_client", __name__, url_prefix="/tms/customer")
staff = Blueprint("tms_staff", __name__, url_prefix="/tms/admin")
SESSION_KEY = "xingrui_customer"
CLIENT_POSTS = frozenset(f"tms_client.{name}" for name in ("sign_in", "sign_out", "upload", "confirm"))
SESSION_TTL = timedelta(hours=8)
DUMMY_HASH = generate_password_hash(token_hex(32))
STATUS_LABELS = {"draft": "待确认", "pending": "待审核", "approved": "审核通过", "rejected": "审核驳回"}
AUDIT_LABELS = {
    "upload": "上传发票", "replace": "更正发票", "confirm": "提交审核",
    "approve": "审核通过", "reject": "审核驳回", "pricing_draft": "保存收费草稿", "adjust": "调整费用",
}
BUSINESS_TIME = timezone(timedelta(hours=8))
ORDER_FILTERS = {
    "q": 300, "service": 60, "country": 2, "address": 50, "recipient": 100,
    "created_from": 10, "created_to": 10,
}
DESTINATION_FIELDS = {
    "warehouse_code": "FBA 仓库代码", "recipient": "收件人", "company": "公司",
    "street": "收件地址一", "street2": "收件地址二", "street3": "收件地址三",
    "city": "城市", "region": "省份 / 州", "postal_code": "邮编", "country": "国家二字代码",
    "phone": "电话", "mobile": "手机", "email": "邮箱",
}


def account_name(value):
    return normalize("NFKC", value.strip()).casefold()


def valid_account_name(value):
    return (2 <= len(value) <= 80 and value[0].isalnum()
            and all(char.isalnum() or char in "_.@+-" for char in value))


def init_tms(app):
    app.config.update(
        TMS_ENABLED=app.config.get("ISOLATED_MODE") or os.getenv("TMS_ENABLED") == "1",
        TMS_ADDRESS_BOOK_DIR=os.getenv("TMS_ADDRESS_BOOK_DIR"),
        TMS_INVOICE_TEMPLATE_PATH=os.getenv("TMS_INVOICE_TEMPLATE_PATH"),
        TMS_NEXTSLS_READ_ENABLED=os.getenv("TMS_NEXTSLS_READ_ENABLED") == "1",
        TMS_NEXTSLS_CREATE_ENABLED=os.getenv("TMS_NEXTSLS_CREATE_ENABLED") == "1",
    )
    if app.config["TMS_ENABLED"] and not app.config.get("ISOLATED_MODE"):
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.register_blueprint(client)
    app.register_blueprint(staff)

    @app.before_request
    def check_tms_availability():
        if request.blueprint not in {client.name, staff.name}:
            return
        if not app.config["TMS_ENABLED"]:
            abort(404)
        if request.endpoint in {"tms_client.upload", "tms_staff.address_book"} and request.method == "POST":
            if request.content_length is None or request.content_length > MAX_UPLOAD_BYTES + 64 * 1024:
                abort(413, "每份 Excel 文件上限为 2 MiB。")

    @app.after_request
    def protect_tms_responses(response):
        if request.blueprint in {client.name, staff.name}:
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.cli.command("tms-upgrade-v1")
    @click.option("--confirm", is_flag=True, help="Apply the additive TMS v1 schema.")
    def upgrade(confirm):
        if not confirm:
            raise click.UsageError("请先核对数据库目标并备份，再明确使用 --confirm。")
        create_tms_schema()
        click.echo("TMS 表已就绪。")


def create_tms_schema():
    with db.engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(651360348)"))
        for model in (TmsAccount, TmsAddressBook, TmsOrder, TmsAudit, TmsReceivable, TmsChargeAdjustment,
                      TmsAgentApi, TmsSupplierOrder, TmsSupplierEvent):
            model.__table__.create(connection, checkfirst=True)


@client.before_request
def customer_required():
    if request.endpoint == "tms_client.sign_in":
        return
    identity = session.get(SESSION_KEY)
    if isinstance(identity, dict) and identity.get("expiry", 0) > datetime.now().timestamp():
        account = db.session.get(TmsAccount, identity["account_id"])
        if account and account.active and account.customer_id == identity["customer_id"]:
            g.tms_account = account
            return
    session.pop(SESSION_KEY, None)
    return redirect(url_for("tms_client.sign_in"))


@staff.before_request
def admin_required():
    if not current_user.is_authenticated:
        return redirect(url_for("login"))
    if not current_user.is_admin:
        abort(403)


def page(template, **context):
    return render_template(
        f"tms/{template}.html", staff=request.blueprint == staff.name,
        status_labels=STATUS_LABELS, **context,
    )


@client.route("/login", methods=["GET", "POST"])
def sign_in():
    if request.method == "GET":
        return page("login")
    username = account_name(request.form.get("username", ""))
    password = request.form.get("password", "")
    if len(password) > 128:
        return page("login", error="账号或密码不正确。"), 401
    account = TmsAccount.query.filter_by(username=username).one_or_none() if valid_account_name(username) else None
    valid = check_password_hash(account.password_hash if account else DUMMY_HASH, password)
    if not valid or not account or not account.active:
        return page("login", error="账号或密码不正确。"), 401
    session.clear()
    logout_user()
    session[SESSION_KEY] = {
        "account_id": account.id, "customer_id": account.customer_id,
        "expiry": (datetime.now() + SESSION_TTL).timestamp(),
    }
    return redirect(url_for("tms_client.orders"))


@client.post("/logout")
def sign_out():
    session.pop(SESSION_KEY, None)
    return redirect(url_for("tms_client.sign_in"))


def owned_orders():
    return TmsOrder.query.filter_by(customer_id=g.tms_account.customer_id)


def customer_order(order_id):
    return owned_orders().filter_by(id=order_id).first_or_404()


def update_order(order, changes):
    result = db.session.execute(update(TmsOrder).where(
        TmsOrder.id == order.id, TmsOrder.revision == order.revision,
    ).values(**changes, revision=order.revision + 1))
    if result.rowcount != 1:
        db.session.rollback()
        abort(409, "订单刚刚变更，请刷新后再试。")


def history(order, action):
    by_staff = request.blueprint == staff.name
    db.session.add(TmsAudit(order_id=order.id, actor_type="staff" if by_staff else "customer",
                            actor_id=current_user.id if by_staff else g.tms_account.id, action=action))


def legacy_duplicate(customer_id, key):
    return db.session.scalar(select(Shipment.id).where(
        Shipment.customer_id == customer_id, func.upper(func.trim(Shipment.tracking_number)) == key,
    ).limit(1)) is not None


def invoice_totals(invoice):
    declared_weight = sum(Decimal(box["weight_kg"]) for box in invoice["boxes"])
    declared_value = sum(Decimal(item["total_usd"]) for item in invoice["declarations"])
    volume = sum((Decimal(box["length_cm"]) * Decimal(box["width_cm"]) * Decimal(box["height_cm"])
                  for box in invoice["boxes"]), Decimal("0")) / Decimal("1000000")
    return {"weight": format(declared_weight.normalize(), "f"), "value": format(declared_value, ",.2f"),
            "volume": format(volume, ".3f")}


def business_date(value):
    return value.replace(tzinfo=timezone.utc).astimezone(BUSINESS_TIME).strftime("%Y-%m-%d %H:%M")


def filter_orders(query, filters):
    keywords = [word.strip().upper() for word in re.split(r"[,，\n]", filters["q"]) if word.strip()]
    if len(keywords) > 20:
        raise ValueError("一次最多搜索 20 个单号。")
    conditions = []
    for word in keywords:
        if len(word) > 50:
            raise ValueError("每个单号最多 50 个字符。")
        if len(keywords) == 1:
            conditions.append(TmsOrder.number_key.contains(word, autoescape=True))
        else:
            conditions.append(TmsOrder.number_key == word)
        if re.fullmatch(r"XR-[0-9]{1,10}", word) and int(word[3:]) <= 2_147_483_647:
            conditions.append(TmsOrder.id == int(word[3:]))
    if conditions:
        query = query.filter(or_(*conditions))
    if filters["service"]:
        if filters["service"] not in SERVICES:
            raise ValueError("请选择列表中的运输服务。")
        query = query.filter(TmsOrder.invoice["service"].as_string() == filters["service"])
    if filters["country"]:
        if not re.fullmatch(r"[A-Za-z]{2}", filters["country"]):
            raise ValueError("国家或地区请填写两位代码，例如 US。")
        query = query.filter(TmsOrder.invoice["address"]["country"].as_string() == filters["country"].upper())
    if filters["address"]:
        query = query.filter(TmsOrder.invoice["address_code"].as_string().icontains(
            filters["address"], autoescape=True))
    if filters["recipient"]:
        query = query.filter(TmsOrder.invoice["address"]["recipient"].as_string().icontains(
            filters["recipient"], autoescape=True))
    try:
        start, end = (date.fromisoformat(filters[key]) if filters[key] else None
                      for key in ("created_from", "created_to"))
        if start and end and start > end:
            raise ValueError
        if start:
            query = query.filter(TmsOrder.created_at >= utc_midnight(start))
        if end:
            query = query.filter(TmsOrder.created_at < utc_midnight(end + timedelta(days=1)))
    except (ValueError, OverflowError):
        raise ValueError("请检查创建日期范围，开始日期不能晚于结束日期。") from None
    return query


def utc_midnight(day):
    return datetime.combine(day, time.min, BUSINESS_TIME).astimezone(timezone.utc).replace(tzinfo=None)


def order_list(query, endpoint, default_status):
    filters = {key: request.args.get(key, "").strip() for key in ORDER_FILTERS}
    status = request.args.get("status", default_status)
    statuses = {key: value for key, value in STATUS_LABELS.items()
                if endpoint == "tms_client.orders" or key != "draft"}
    if status not in statuses and status != "all":
        status = default_status
    error = None
    try:
        if any(len(filters[key]) > limit for key, limit in ORDER_FILTERS.items()):
            raise ValueError("筛选内容超出长度限制，请缩短后再查。")
        query = filter_orders(query, filters)
    except ValueError as exc:
        error = str(exc)
        query = query.filter(False)
    counts = dict(query.with_entities(TmsOrder.status, func.count(TmsOrder.id))
                  .group_by(TmsOrder.status).all())
    if status != "all":
        query = query.filter_by(status=status)
    pagination = query.order_by(TmsOrder.id.desc()).paginate(per_page=25, error_out=False)
    args = {key: value for key, value in filters.items() if value}
    return page("orders", pagination=pagination, filters=filters, filter_args=args, counts=counts,
                statuses=statuses, status=status, services=SERVICES, endpoint=endpoint,
                invoice_totals=invoice_totals, business_date=business_date, error=error), (400 if error else 200)


@client.get("/")
def orders():
    return order_list(owned_orders(), "tms_client.orders", "all")


def read_configured_workbook(path):
    try:
        with Path(path).open("rb") as file:
            return file.read(MAX_UPLOAD_BYTES + 1)
    except OSError:
        raise InvoiceError("无法读取配置的工作簿，请联系星睿。") from None


def customer_catalogue(customer_id):
    address_book = db.session.get(TmsAddressBook, customer_id)
    if address_book is not None:
        return address_book.rows
    config = current_app.config
    local_catalogue = config.get("TMS_LOCAL_CATALOGUE")
    if config.get("ISOLATED_MODE") and local_catalogue and local_catalogue[0] == customer_id:
        return read_configured_workbook(local_catalogue[1])
    directory = config.get("TMS_ADDRESS_BOOK_DIR")
    if not directory:
        raise InvoiceError("暂未提供在线下载的地址库模板。可直接上传已有的星睿三工作表发票。")
    return read_configured_workbook(Path(directory) / f"{customer_id}.xlsx")


@client.route("/import", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        replacement = request.args.get("replace", type=int)
        order = customer_order(replacement) if replacement is not None else None
        if order and order.status == "approved":
            abort(409, "该订单已通过审核。")
        return page("upload", replacement=order)
    replacement = request.form.get("replace", type=int)
    order = customer_order(replacement) if replacement is not None else None
    if order and (order.status == "approved"
                  or request.form.get("revision", type=int) != order.revision):
        abort(409, "订单刚刚变更，请刷新后再修改。")
    files = request.files.getlist("invoice")
    if len(files) != 1 or not (files[0].filename or "").lower().endswith(".xlsx"):
        return page("upload", error="请选择一份 .xlsx 发票。", replacement=order), 400
    raw = files[0].stream.read(MAX_UPLOAD_BYTES + 1)
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        return page("upload", error="每份 .xlsx 文件上限为 2 MiB。", replacement=order), 400
    customer_id = g.tms_account.customer_id
    try:
        invoice = parse_invoice(raw)
    except InvoiceError as error:
        return page("upload", error=str(error), replacement=order), 400
    key = invoice["customer_number"].upper()
    digest = sha256(raw).hexdigest()
    candidates = owned_orders().filter(
        (TmsOrder.number_key == key) | (TmsOrder.file_sha256 == digest)
    )
    if order:
        candidates = candidates.filter(TmsOrder.id != order.id)
    if candidates.first() or legacy_duplicate(customer_id, key):
        return page("upload", error="本客户已有相同客户单号或文件，请在运单列表查看。", replacement=order), 409
    try:
        values = dict(number_key=key, file_sha256=digest, invoice=invoice, workbook=raw)
        if order:
            update_order(order, dict(**values, status="draft", rejection_reason=None))
            if billing_available():
                draft_charge = db.session.get(TmsReceivable, order.id)
                if draft_charge and draft_charge.shipment_id is None:
                    db.session.delete(draft_charge)
        else:
            order = TmsOrder(customer_id=customer_id, **values)
            db.session.add(order)
            db.session.flush()
        history(order, "replace" if replacement is not None else "upload")
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return page("upload", error="本客户已有相同客户单号或文件。"), 409
    return redirect(url_for("tms_client.detail", order_id=order.id))


def customer_shipment(order, receivable):
    if order.status != "approved" or not receivable or not receivable.shipment_id:
        return None
    shipment = db.session.get(Shipment, receivable.shipment_id)
    if (shipment and shipment.customer_id == order.customer_id
            and shipment.tracking_number == order.invoice["customer_number"]
            and shipment.agent_id == order.agent_id == receivable.agent_id):
        return shipment
    return None


def customer_measurement_view(order, shipment):
    link, snapshot = tms_carriers.verified_snapshot(order, shipment)
    if not snapshot or not measurements.has_measurements(snapshot.details):
        return None
    view = measurements.measurement_view(order.invoice, snapshot.details)
    if view:
        return {**view, "synced_at": link.synced_at}
    return None


@client.get("/orders/<int:order_id>")
def detail(order_id):
    return order_detail(customer_order(order_id))


@client.get("/orders/<int:order_id>/measurements.xlsx")
def customer_measurements(order_id):
    order = customer_order(order_id)
    receivable = db.session.get(TmsReceivable, order.id) if billing_available() else None
    view = customer_measurement_view(order, customer_shipment(order, receivable))
    if not view or not (view["summary"] or view["boxes"]):
        abort(404)
    report = tms_carriers.customer_measurement_export(order.invoice, view, business_date(view["synced_at"]))
    return send_file(report, download_name="measurements.xlsx", as_attachment=True)


def order_detail(order, **context):
    template = "detail_content" if request.args.get("drawer") == "1" else "detail"
    audit = db.session.execute(select(TmsAudit.action, TmsAudit.created_at)
                               .where(TmsAudit.order_id == order.id)
                               .order_by(TmsAudit.id.desc()).limit(30)).all()
    events = [(AUDIT_LABELS[action], business_date(timestamp)) for action, timestamp in audit
              if action in AUDIT_LABELS and timestamp]
    receivable = db.session.get(TmsReceivable, order.id) if billing_available() else None
    shipment, tracks, customer_view = None, [], None
    if request.blueprint == staff.name:
        if receivable and receivable.shipment_id:
            shipment = db.session.get(Shipment, receivable.shipment_id)
            tracks = db.session.scalars(select(ManualTrack).where(ManualTrack.shipment_id == shipment.id)
                                        .order_by(ManualTrack.happen_time.desc())).all() if shipment else []
    else:
        owned_shipment = customer_shipment(order, receivable)
        if owned_shipment:
            # Manual descriptions and locations can contain private supplier notes or numbers.
            times = db.session.scalars(select(ManualTrack.happen_time).where(
                ManualTrack.shipment_id == owned_shipment.id,
            ).order_by(ManualTrack.happen_time.desc())).all()
            tracks = [{"happen_time": when, "description": "物流轨迹已更新"} for when in times]
            customer_view = customer_measurement_view(order, owned_shipment)
    return page(template, order=order, totals=invoice_totals(order.invoice), shipment=shipment, tracks=tracks,
                business_date=business_date, events=events, receivable=receivable, customer_measurements=customer_view,
                units=UNITS, sources=SOURCES, currencies=CURRENCIES, fixed=FIXED, **context)


@client.get("/template")
def template_file():
    try:
        return download_invoice_template(customer_catalogue(g.tms_account.customer_id))
    except InvoiceError as error:
        return page("upload", error=str(error)), 400


def download_invoice_template(catalogue):
    layout_path = current_app.config.get("TMS_INVOICE_TEMPLATE_PATH")
    layout = read_configured_workbook(layout_path) if layout_path else None
    result = BytesIO()
    workbook = blank_invoice(catalogue, layout=layout)
    try:
        workbook.save(result)
    finally:
        workbook.close()
    result.seek(0)
    return send_file(result, as_attachment=True, download_name="xingrui-invoice-template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@client.post("/orders/<int:order_id>/confirm")
def confirm(order_id):
    order = customer_order(order_id)
    if order.status != "draft" or request.form.get("revision", type=int) != order.revision:
        abort(409, "订单刚刚变更，请刷新预览。")
    if legacy_duplicate(order.customer_id, order.number_key):
        abort(409, "本客户已存在同一单号的运单。")
    update_order(order, {"status": "pending"})
    history(order, "confirm")
    db.session.commit()
    return redirect(url_for("tms_client.detail", order_id=order.id))


def original_workbook(order):
    return send_file(BytesIO(order.workbook), as_attachment=True,
                     download_name=f"xingrui-{order.id}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@client.get("/orders/<int:order_id>/invoice")
def download(order_id):
    return original_workbook(customer_order(order_id))


def product_photo(order, item_index):
    items = order.invoice["declarations"]
    if item_index >= len(items):
        abort(404)
    part = items[item_index].get("image_part")
    if not part or not MEDIA_PART.fullmatch(part):
        abort(404)
    try:
        with ZipFile(BytesIO(order.workbook)) as archive:
            if archive.getinfo(part).file_size > 16 * 1024 * 1024:
                abort(404)
            data = archive.read(part)
    except (BadZipFile, KeyError, RuntimeError):
        abort(404)
    mimetype = image_type(part, data)
    if mimetype is None:
        abort(404)
    return send_file(BytesIO(data), mimetype=mimetype, download_name="product-photo")


@client.get("/orders/<int:order_id>/photos/<int:item_index>")
def photo(order_id, item_index):
    return product_photo(customer_order(order_id), item_index)


@staff.route("/accounts", methods=["GET", "POST"])
def accounts():
    if request.method == "GET":
        return accounts_page()
    customer_id = request.form.get("customer_id", type=int)
    raw_username = request.form.get("username", "").strip()
    username = account_name(raw_username)
    password = request.form.get("password", "")
    errors = {}
    if not customer_id or not 0 < customer_id <= 2_147_483_647 or not db.session.get(Customer, customer_id):
        errors["customer_id"] = "请选择一个已有客户。"
    if not valid_account_name(username):
        errors["username"] = "账号须为 2–80 个字符，以中文、字母或数字开头，可包含 _ . @ + -。"
    if not 12 <= len(password) <= 128 or not password.strip():
        errors["password"] = "初始密码须为 12–128 个字符，不能全是空格。"
    if errors:
        return accounts_page(errors=errors, customer_id=customer_id, username=raw_username[:80]), 422
    db.session.add(TmsAccount(customer_id=customer_id, username=username,
                              password_hash=generate_password_hash(password)))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return accounts_page(errors={"username": "这个登录账号已被使用，请换一个。"},
                             customer_id=customer_id, username=raw_username[:80]), 409
    flash("客户账号已创建，可从客户登录页进入。", "success")
    return redirect(url_for("tms_staff.accounts"))


def accounts_page(*, errors=None, customer_id=None, username=""):
    customers = Customer.query.order_by(Customer.name).all()
    accounts = db.session.execute(select(TmsAccount.id, TmsAccount.username, TmsAccount.active, Customer.name)
                                 .join(Customer, Customer.id == TmsAccount.customer_id)
                                 .order_by(TmsAccount.id.desc())).all()
    return page("accounts", customers=customers, accounts=accounts, errors=errors or {},
                selected_customer=customer_id, username=username)


@staff.route("/accounts/<int:customer_id>/address-book", methods=["GET", "POST"])
def address_book(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    if request.method == "POST":
        files = request.files.getlist("catalogue")
        try:
            if len(files) != 1 or not (files[0].filename or "").lower().endswith(".xlsx"):
                raise InvoiceError("请选择一份 .xlsx 地址库。")
            rows = validated_catalogue(files[0].stream.read(MAX_UPLOAD_BYTES + 1))
        except InvoiceError as error:
            saved = db.session.get(TmsAddressBook, customer_id)
            return page("address_book", customer=customer, saved=saved, error=str(error)), 400
        # Serialize updates for the same customer, including their first upload.
        if db.session.get(Customer, customer_id, with_for_update=True) is None:
            abort(404)
        saved = db.session.get(TmsAddressBook, customer_id)
        if saved is None:
            db.session.add(TmsAddressBook(customer_id=customer_id, rows=rows))
        else:
            saved.rows = rows
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, "地址库未保存，请刷新后再试。")
        flash("客户地址库已保存。", "success")
        return redirect(url_for("tms_staff.address_book", customer_id=customer_id))
    saved = db.session.get(TmsAddressBook, customer_id)
    if request.args.get("download") == "1":
        try:
            return download_invoice_template(saved.rows if saved else None)
        except InvoiceError as error:
            return page("address_book", customer=customer, saved=saved, error=str(error)), 400
    return page("address_book", customer=customer, saved=saved)


@staff.post("/accounts/<int:account_id>/availability")
def account_availability(account_id):
    account = db.get_or_404(TmsAccount, account_id)
    state = request.form.get("state")
    if state not in {"enable", "disable"}:
        abort(400)
    account.active = state == "enable"
    db.session.commit()
    flash("客户账号状态已更新。", "success")
    return redirect(url_for("tms_staff.accounts"))


@staff.get("/")
def queue():
    return order_list(TmsOrder.query.filter(TmsOrder.status != "draft"), "tms_staff.queue", "pending")


def reviewable_order(order_id):
    return TmsOrder.query.filter(TmsOrder.status != "draft", TmsOrder.id == order_id).first_or_404()


def destination_reference(order):
    """Compare only with this customer's optional, staff-maintained reference."""
    if not inspect(db.session.connection()).has_table(TmsAddressBook.__tablename__):
        return None
    saved = db.session.get(TmsAddressBook, order.customer_id)
    if saved is None:
        return None
    code = order.invoice["address_code"].casefold()
    matches = [row for row in saved.rows if row[1].casefold() == code]
    if len(matches) != 1:
        return {"no_match": True}
    reference = address_values(matches[0])
    return {"differences": [
        (DESTINATION_FIELDS[key], order.invoice["address"].get(key, ""), value)
        for key, value in reference.items()
        if comparable_address(order.invoice["address"].get(key, "")) != comparable_address(value)
    ]}


def staff_detail(order, **context):
    agents = CarrierAgent.query.filter_by(is_active=True).order_by(CarrierAgent.name).all()
    channels = {}
    if order.status != "approved" and agents and tms_carriers.carrier_schema():
        channels = {profile.agent_id: profile.service_codes for profile in db.session.scalars(
            select(TmsAgentApi).where(TmsAgentApi.provider == "nextsls",
                                     TmsAgentApi.agent_id.in_([agent.id for agent in agents])))
                    if profile.service_codes}
    adjustments = (db.session.scalars(select(TmsChargeAdjustment).where(
        TmsChargeAdjustment.order_id == order.id).order_by(TmsChargeAdjustment.id.desc())).all()
        if order.status == "approved" and billing_available() else [])
    supplier = {}
    if order.status == "approved" and tms_carriers.carrier_schema():
        try:
            shipment, receivable = tms_carriers.approved_shipment(order)
        except nextsls.ProtocolError:
            pass
        else:
            agent = db.session.get(CarrierAgent, order.agent_id)
            profile = db.session.get(TmsAgentApi, order.agent_id)
            link = db.session.get(TmsSupplierOrder, order.id)
            snapshot = tms_carriers.latest_event(order.id, "snapshot")
            if not tms_carriers.snapshot_matches(link, shipment, snapshot, order.agent_service):
                snapshot = None
            cost = tms_carriers.latest_event(order.id, "cost")
            supplier = {
                "link": link, "snapshot": snapshot, "cost": cost, "supplier_number": shipment.shipment_id,
                "can_create": bool(agent and tms_carriers.available(profile, agent, "create")),
                "can_reconcile": bool(agent and tms_carriers.available(profile, agent, "reconcile")),
                "can_sync": bool(agent and any(tms_carriers.available(profile, agent, key)
                                 for key in tms_carriers.DETAILS)),
                "profit": tms_carriers.profit(receivable, cost, snapshot),
                "cost_status": tms_carriers.cost_status(receivable, cost, snapshot),
                "settlements": tms_carriers.SETTLEMENTS, "cost_bases": tms_carriers.COST_BASES,
                "cost_ownership": tms_carriers.COST_OWNERSHIP,
                "measurements": measurements.measurement_view(order.invoice, snapshot.details, internal=True)
                                if snapshot and measurements.has_measurements(snapshot.details) else None,
                "unit_choices": measurements.available_unit_choices(snapshot.details) if snapshot else {},
                "parcel_labels": measurements.PARCEL_FIELDS,
                "source": measurements.SOURCE,
                "history": db.session.scalars(select(TmsSupplierEvent).where(
                    TmsSupplierEvent.order_id == order.id, TmsSupplierEvent.kind.in_(("cost", "snapshot")),
                ).order_by(TmsSupplierEvent.id.desc()).limit(15)).all(),
            }
    destination = {}
    if order.status == "pending" and order.invoice.get("address_source") == "workbook":
        destination = {
            "destination_fields": DESTINATION_FIELDS, "required_destination": REQUIRED_ADDRESS,
            "destination_reference": destination_reference(order),
        }
    return order_detail(order, agents=agents, agent_channels=channels, customer=db.session.get(Customer, order.customer_id),
                        adjustments=adjustments, supplier=supplier, **destination, **context)


@staff.get("/orders/<int:order_id>")
def review(order_id):
    return staff_detail(reviewable_order(order_id))


@staff.post("/orders/<int:order_id>/supplier")
def supplier_action(order_id):
    order = reviewable_order(order_id)
    action = request.form.get("action")
    if action not in {"create", "reconcile", "retry", "sync", "cost", "measurement_review"}:
        abort(400)
    try:
        if action == "create":
            if request.form.get("confirm") != "yes":
                raise nextsls.ProtocolError("请确认本票供应商下单。")
            state = tms_carriers.send_order(order, current_user.id)
            message = ("已保存供应商运单号。" if state == "linked"
                       else "提交结果未确认。请先对账，系统不会自动重试。")
        elif action == "reconcile":
            state = tms_carriers.reconcile_order(order, current_user.id)
            message = ("已核对并保存供应商运单号。" if state == "linked"
                       else "列表未找到匹配运单。请向供应商核实后再解除重试锁。")
        elif action == "retry":
            if request.form.get("confirm") != "yes":
                raise nextsls.ProtocolError("请确认供应商已核实没有创建运单。")
            tms_carriers.unlock_retry(order, current_user.id, request.form.get("reason"))
            message = "已解除重试锁。再次下单仍须单独确认。"
        elif action == "sync":
            tms_carriers.sync_snapshot(order, current_user.id)
            message = "已保存本票可用的代理数据。"
        elif action == "measurement_review":
            tms_carriers.review_measurements(order, current_user.id, request.form)
            message = "已记录本票测量单位的核对结果。"
        else:
            tms_carriers.record_cost(order, current_user.id, request.form)
            message = "已保存人工确认的采购成本。"
    except nextsls.ProtocolError as error:
        db.session.rollback()
        return staff_detail(order, supplier_error=str(error)), 400
    except SQLAlchemyError:
        db.session.rollback()
        abort(409, "代理数据未能保存。若已提交下单，请先对账。")
    flash(message, "success")
    return redirect(url_for("tms_staff.review", order_id=order.id))


@staff.get("/orders/<int:order_id>/supplier/measurements.xlsx")
def supplier_measurements(order_id):
    order = reviewable_order(order_id)
    try:
        shipment, _ = tms_carriers.approved_shipment(order)
    except nextsls.ProtocolError:
        abort(404)
    link, snapshot = tms_carriers.verified_snapshot(order, shipment)
    if not snapshot or not measurements.has_measurements(snapshot.details):
        abort(404)
    report = tms_carriers.measurement_export(order, link, snapshot, business_date(link.synced_at))
    return send_file(report, download_name="supplier-measurements.xlsx", as_attachment=True)


@staff.post("/orders/<int:order_id>/decision")
def decide(order_id):
    order = reviewable_order(order_id)
    decision = request.form.get("decision")
    if decision not in {"approve", "reject", "draft", "adjust"}:
        abort(400)
    required_status = "approved" if decision == "adjust" else "pending"
    if order.status != required_status or request.form.get("revision", type=int) != order.revision:
        abort(409, "订单刚刚变更，请刷新后再审核。")

    if decision == "reject":
        reason = request.form.get("reason", "").strip()
        if not 1 <= len(reason) <= 500:
            return staff_detail(order, rejection_error="请填写可向客户显示的驳回原因。"), 400
        update_order(order, {"status": "rejected", "rejection_reason": reason})
        history(order, "reject")
    else:
        if not billing_available():
            abort(503, "收费功能待执行增量迁移。")
        try:
            save_pricing(order, decision)
        except ValueError as error:
            return staff_detail(order, pricing_error=str(error), posted=pricing_input()), 400
        except SQLAlchemyError:
            db.session.rollback()
            abort(409, "收费或运单保存失败，所有修改已回滚。")
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        abort(409, "审核保存失败，所有修改已回滚。")
    return redirect(url_for("tms_staff.review", order_id=order_id))


def pricing_input():
    try:
        fields = price_form(request.form)
        valid_draft(fields)
    except ValueError:
        return {}
    fields.update(agent_id=request.form.get("agent_id", type=int), agent_service=request.form.get("agent_service", "")[:100],
                  adjustment_reason=request.form.get("adjustment_reason", "")[:500])
    return fields


def reviewed_invoice(invoice, form):
    if form.get("destination_confirmed") != "yes":
        raise ValueError("请先核对并确认本票收件地址。")
    original = invoice["address"]
    code = form.get("destination_code", invoice["address_code"]).strip()
    if not IDENTIFIER.fullmatch(code):
        raise ValueError("目的地地址编码格式不正确。")
    address = {}
    for key, label in DESTINATION_FIELDS.items():
        value = form.get(f"destination_{key}", original.get(key, "")).strip()
        if key in REQUIRED_ADDRESS and not value:
            raise ValueError(f"收件地址的「{label}」不能为空。")
        if len(value) > (50 if key == "warehouse_code" else 200) or any(ord(char) < 32 for char in value):
            raise ValueError(f"收件地址的「{label}」格式或长度不正确。")
        address[key] = value
    address["country"] = address["country"].upper()
    if not re.fullmatch(r"[A-Z]{2}", address["country"]):
        raise ValueError("收件地址国家代码须为两位字母。")
    changed = code != invoice["address_code"] or address != original
    reason = form.get("destination_reason", "").strip()
    if len(reason) > 500 or (changed and not reason):
        raise ValueError("更正收件地址须填写原因，最多 500 字。")
    return dict(invoice, address_code=code, address=address,
                submitted_destination={"address_code": invoice["address_code"], "address": original},
                destination_review={"by": current_user.id, "at": datetime.now(timezone.utc).isoformat(),
                                    "changed": changed, "reason": reason})


def save_pricing(order, decision):
    form = request.form
    values = price_form(form)
    valid_draft(values)
    receivable = db.session.get(TmsReceivable, order.id)
    if decision == "adjust":
        if not receivable or receivable.shipment_id is None:
            raise ValueError("该历史订单没有已确认的收费记录。")
        reason = form.get("adjustment_reason", "").strip()
        if not 1 <= len(reason) <= 500:
            raise ValueError("请填写本次费用调整的原因。")
        details = confirm_price(values, form)
        update_order(order, {})
        db.session.add(TmsChargeAdjustment(
            order_id=order.id, previous=receivable.details, revised=details,
            reason=reason, actor_id=current_user.id,
        ))
        receivable.details, receivable.total, receivable.currency = details, Decimal(details["total"]), details["currency"]
        history(order, "adjust")
        return

    agent_id = form.get("agent_id", type=int)
    service = form.get("agent_service", "").strip()
    if len(service) > 100 or (agent_id is not None and not 0 < agent_id < 2**31):
        raise ValueError("代理或运输渠道格式不正确。")
    agent = db.session.get(CarrierAgent, agent_id, with_for_update=True) if agent_id else None
    if (agent_id or decision == "approve") and (not agent or not agent.is_active):
        raise ValueError("请选择启用的实际物流代理。")
    if decision == "approve" and not service:
        raise ValueError("请填写实际代理运输渠道。")
    if decision == "approve":
        profile = tms_carriers.agent_profile(agent.id)
        if profile and profile.provider == "nextsls" and profile.service_codes and service not in profile.service_codes:
            raise ValueError("请选择此 NextSLS 代理已配置的渠道代码。")
    details = confirm_price(values, form) if decision == "approve" else values
    if decision == "approve":
        invoice = order.invoice
        changes = {"status": "approved", "agent_id": agent.id, "agent_service": service}
        if invoice.get("address_source") == "workbook":
            invoice = reviewed_invoice(invoice, form)
            changes["invoice"] = invoice
        db.session.get(Customer, order.customer_id, with_for_update=True)
        if legacy_duplicate(order.customer_id, order.number_key):
            abort(409, "本客户已存在同一单号的运单，请先人工核对。")
        update_order(order, changes)
        shipment = Shipment(
            tracking_number=invoice["customer_number"], customer_id=order.customer_id, agent_id=agent.id,
            destination=invoice["address"]["country"], channel=invoice["service"],
            pieces=len(invoice["boxes"]), weight=float(invoice_totals(invoice)["weight"]),
            status="已创建",
        )
        db.session.add(shipment)
        db.session.flush()
    else:
        update_order(order, {})
    if receivable is None:
        receivable = TmsReceivable(order_id=order.id)
        db.session.add(receivable)
    receivable.details, receivable.agent_id, receivable.agent_service = details, agent_id, service or None
    if decision == "approve":
        receivable.shipment_id = shipment.id
        receivable.total, receivable.currency = Decimal(details["total"]), details["currency"]
        receivable.confirmed_by, receivable.confirmed_at = current_user.id, datetime.now(timezone.utc).replace(tzinfo=None)
    history(order, "approve" if decision == "approve" else "pricing_draft")


@staff.get("/orders/<int:order_id>/invoice")
def staff_download(order_id):
    return original_workbook(reviewable_order(order_id))


@staff.get("/orders/<int:order_id>/photos/<int:item_index>")
def staff_photo(order_id, item_index):
    return product_photo(reviewable_order(order_id), item_index)
