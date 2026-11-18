"""Customer ordering and administrator review."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
from secrets import token_hex
from unicodedata import normalize
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import click
from flask import (
    Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from flask_login import current_user
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import func, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from models import CarrierAgent, Customer, ManualTrack, Shipment, db
from tms_invoice import (
    IDENTIFIER, REQUIRED_ADDRESS, InvoiceError, MAX_PRODUCT_LINES, MAX_UPLOAD_BYTES, SERVICES,
    UPLOAD_LIMIT_TEXT, address_placeholders, address_values,
    blank_invoice, comparable_address, parse_invoice, validated_catalogue,
)
from tms_models import (
    RECEIVABLE_CHECK, RECEIVABLE_CHECK_NAME, TmsAccount, TmsAddressBook, TmsAudit, TmsOrder,
    TmsReceivable, TmsChargeAdjustment,
)
from tms_billing import (
    CURRENCIES, FIXED, SETTLEMENT_SOURCES, UNITS, billing_available, confirm_sales_terms, price_form,
    settle_charge, valid_draft,
)
from tms_xlsx_images import PhotoError, read_photo
import nextsls
import tms_carriers
import tms_measurements as measurements
from tms_models import TmsAgentApi, TmsSupplierEvent, TmsSupplierFile, TmsSupplierOrder
import tms_supplier_files
import tms_agent_import
import tms_create_control
import tms_create_batch

client = Blueprint("tms_client", __name__, url_prefix="/tms/customer")
staff = Blueprint("tms_staff", __name__, url_prefix="/tms/admin")
tms_agent_import.register_routes(staff)
tms_create_control.register_routes(staff)
tms_create_batch.register_routes(staff)
SESSION_KEY = "xingrui_customer"
CUSTOMER_COOKIE = "xingrui_customer_session"
CLIENT_POSTS = frozenset(f"tms_client.{name}" for name in ("sign_in", "sign_out", "upload", "confirm", "delete_order"))
SESSION_TTL = timedelta(hours=8)
DUMMY_HASH = generate_password_hash(token_hex(32))
STATUS_LABELS = {"draft": "待确认", "pending": "待审核", "approved": "审核通过", "rejected": "审核驳回"}
AUDIT_LABELS = {
    "upload": "上传发票", "replace": "更正发票", "confirm": "提交审核",
    "acceptance_copy": "经授权复用发票准备独立接口验收单（未提交供应商）",
    "approve": "审核通过", "reject": "审核驳回", "pricing_draft": "保存收费草稿", "adjust": "调整销售条款",
    "settle": "确认最终应收",
    "supplier_channel": "调整未提交的供应商或渠道",
    "supplier_prep": "保存下单补充资料",
    "supplier_address": "更正下单收件地址",
    "supplier_grant": "授权本票真实下单",
    "supplier_revoke": "撤销本票真实下单授权",
    "order_deleted": "删除订单",
    "order_restored": "恢复订单",
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
SUPPLIER_ADDRESS_CORRECTIONS = ("mobile", "street2", "street3")


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
        TMS_NEXTSLS_REQUIRE_ORDER_GRANT=not app.config.get("ISOLATED_MODE"),
        TMS_NEXTSLS_SAMPLE_CREATE_APPROVALS=os.getenv("TMS_NEXTSLS_SAMPLE_CREATE_APPROVALS", ""),
        TMS_NEXTSLS_PACKING_ATTACHMENT_HOST=os.getenv("TMS_NEXTSLS_PACKING_ATTACHMENT_HOST", "").strip().lower(),
    )
    for suffix in ("SCOPE", "BUCKET", "REGION", "URL_TTL_SECONDS", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY",
                   "SESSION_TOKEN"):
        key = "TMS_NEXTSLS_IMAGE_" + suffix
        app.config[key] = os.getenv(key, "").strip()
    if app.config["TMS_ENABLED"] and not app.config.get("ISOLATED_MODE"):
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.register_blueprint(client)
    app.register_blueprint(staff)
    tms_supplier_files.init_app(app)
    tms_agent_import.init_app(app)
    tms_create_control.init_app(app)
    tms_create_batch.init_app(app)

    @app.before_request
    def check_tms_availability():
        if request.blueprint not in {client.name, staff.name}:
            return
        if not app.config["TMS_ENABLED"]:
            abort(404)
        if request.endpoint in {"tms_client.upload", "tms_staff.address_book"} and request.method == "POST":
            if request.content_length is None or request.content_length > MAX_UPLOAD_BYTES + 64 * 1024:
                abort(413, UPLOAD_LIMIT_TEXT)

    @app.after_request
    def protect_tms_responses(response):
        if request.blueprint in {client.name, staff.name}:
            response.headers["Cache-Control"] = "private, no-store"
            response.headers.setdefault("Referrer-Policy", "same-origin")
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

    @app.cli.command("tms-upgrade-billing")
    @click.option("--confirm", is_flag=True, help="Enable approved pricing without a final total.")
    def upgrade_billing(confirm):
        if not confirm:
            raise click.UsageError("请先核对数据库目标并备份，再明确使用 --confirm。")
        with db.engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                raise click.ClickException("此约束迁移仅适用于 PostgreSQL。")
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            connection.execute(text("SELECT pg_advisory_xact_lock(651360348)"))
            constraints = {item["name"] for item in inspect(connection).get_check_constraints("tms_receivable")}
            old = "ck_tms_receivable_confirmation"
            if RECEIVABLE_CHECK_NAME not in constraints:
                if old not in constraints:
                    raise click.ClickException("未找到预期的旧计费约束，迁移已停止。")
                connection.execute(text(
                    f"ALTER TABLE tms_receivable ADD CONSTRAINT {RECEIVABLE_CHECK_NAME} "
                    f"CHECK ({RECEIVABLE_CHECK}) NOT VALID"
                ))
            connection.execute(text(f"ALTER TABLE tms_receivable VALIDATE CONSTRAINT {RECEIVABLE_CHECK_NAME}"))
            if old in constraints:
                connection.execute(text(f"ALTER TABLE tms_receivable DROP CONSTRAINT {old}"))
        click.echo("TMS 分阶段计费约束已就绪。")


def create_tms_schema():
    with db.engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(651360348)"))
        for model in (TmsAccount, TmsAddressBook, TmsOrder, TmsAudit, TmsReceivable, TmsChargeAdjustment,
                      TmsAgentApi, TmsSupplierOrder, TmsSupplierEvent, TmsSupplierFile):
            model.__table__.create(connection, checkfirst=True)
        tms_agent_import.TmsAgentImport.__table__.create(connection, checkfirst=True)
        tms_supplier_files.TmsSupplierPreviewFile.__table__.create(connection, checkfirst=True)
        tms_create_control.TmsCreateControl.__table__.create(connection, checkfirst=True)
        if current_app.config["TMS_BATCH_ENABLED"]:
            for table in tms_create_batch.TABLES:
                table.create(connection, checkfirst=True)


@client.before_request
def customer_required():
    if request.endpoint == "tms_client.sign_in":
        return
    cookie = request.cookies.get(CUSTOMER_COOKIE)
    identity = session.get(SESSION_KEY) if cookie is None else None
    if cookie is not None:
        try:
            identity = customer_session_serializer().loads(cookie, max_age=int(SESSION_TTL.total_seconds()))
        except BadData:
            pass
    if isinstance(identity, dict) and identity.get("expiry", 0) > datetime.now().timestamp():
        account = db.session.get(TmsAccount, identity["account_id"])
        if account and account.active and account.customer_id == identity["customer_id"]:
            g.tms_account = account
            return
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
        status_labels=STATUS_LABELS, upload_limit=UPLOAD_LIMIT_TEXT, max_product_lines=MAX_PRODUCT_LINES, **context,
    )


def customer_session_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="xingrui.customer.session.v1")


def customer_cookie_options():
    return dict(path=client.url_prefix, httponly=True, samesite="Lax",
                secure=current_app.config["SESSION_COOKIE_SECURE"])


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
    session.pop(SESSION_KEY, None)
    identity = {
        "account_id": account.id, "customer_id": account.customer_id,
        "expiry": (datetime.now() + SESSION_TTL).timestamp(),
    }
    response = redirect(url_for("tms_client.orders"))
    response.set_cookie(CUSTOMER_COOKIE, customer_session_serializer().dumps(identity),
                        **customer_cookie_options())
    return response


@client.post("/logout")
def sign_out():
    session.pop(SESSION_KEY, None)
    response = redirect(url_for("tms_client.sign_in"))
    response.delete_cookie(CUSTOMER_COOKIE, **customer_cookie_options())
    return response


def archive_marker():
    return TmsOrder.invoice["order_archive"]["at"].as_string()


def active_orders():
    return TmsOrder.query.filter(archive_marker().is_(None))


def owned_orders():
    return active_orders().filter_by(customer_id=g.tms_account.customer_id)


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


def order_deletion_options(orders, *, by_staff):
    approved = {order.id: order for order in orders if order.status == "approved"}
    options = {order.id: {"allowed": True} for order in orders}
    if not approved:
        return options
    shipments = {}
    if billing_available():
        shipments = {charge.order_id: (charge, shipment) for charge, shipment in db.session.execute(
            select(TmsReceivable, Shipment).join(Shipment, Shipment.id == TmsReceivable.shipment_id)
            .where(TmsReceivable.order_id.in_(approved))
        )}
    links = {}
    if tms_carriers.carrier_schema():
        links = {link.order_id: link for link in db.session.scalars(
            select(TmsSupplierOrder).where(TmsSupplierOrder.order_id.in_(approved))
        )}
    for order_id, order in approved.items():
        charge, shipment = shipments.get(order_id, (None, None))
        link = links.get(order_id)
        if (not shipment or shipment.customer_id != order.customer_id
                or shipment.tracking_number != order.invoice["customer_number"]
                or shipment.agent_id != order.agent_id or charge.agent_id != order.agent_id):
            options[order_id] = {"reason": "订单与运单或账目关联需由星睿核对后才能删除。"}
        elif link and link.state not in {"new", "linked"}:
            options[order_id] = {"reason": ("请先在供应商下单中对账，确认结果后再删除。" if by_staff
                                           else "代理提交结果尚待确认，请联系星睿核对后再处理。")}
        elif (shipment.shipment_id or shipment.third_party_tracking_number
              or shipment.status not in (None, "", "已创建", "待提交代理")
              or (link and (link.state == "linked" or link.supplier_number))):
            options[order_id] = ({"allowed": True, "linked": True} if by_staff else
                                 {"reason": "已生成运单。请联系星睿管理员申请取消，并等待承运商确认。"})
    return options


def change_archive(order, *, by_staff, restore=False):
    db.session.refresh(order, with_for_update=True)
    if request.form.get("revision", type=int) != order.revision:
        abort(409, "订单刚刚变更，请刷新后再试。")
    invoice = dict(order.invoice)
    archived = invoice.get("order_archive")
    if bool(archived) != restore:
        abort(409, "订单的删除状态已经变更，请刷新列表。")
    record = {"actor_type": "staff" if by_staff else "customer",
              "actor_id": current_user.id if by_staff else g.tms_account.id,
              "at": datetime.now(timezone.utc).isoformat()}
    if restore:
        invoice.pop("order_archive")
        record.update(action="restore", original_status=archived["original_status"])
    else:
        if order.status == "approved" and billing_available():
            charge = db.session.get(TmsReceivable, order.id)
            if charge and charge.shipment_id:
                db.session.get(Shipment, charge.shipment_id, with_for_update=True)
        option = order_deletion_options([order], by_staff=by_staff)[order.id]
        if not option.get("allowed"):
            abort(409, option["reason"])
        reason = request.form.get("reason", "").strip()
        if not reason or len(reason) > 500:
            abort(400, "请填写 1–500 字的删除原因。")
        record.update(action="delete", reason=reason, original_status=order.status)
        invoice["order_archive"] = record
    invoice.pop(tms_carriers.CREATE_GRANT, None)
    invoice["order_archive_history"] = [*invoice.get("order_archive_history", []), record]
    update_order(order, {"invoice": invoice})
    history(order, "order_restored" if restore else "order_deleted")
    db.session.commit()
    flash("订单已恢复。" if restore else "订单已移至已删除订单。", "success")


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
                if endpoint != "tms_staff.queue" or key != "draft"}
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
    archived = endpoint == "tms_staff.deleted_orders"
    options = {} if archived else order_deletion_options(pagination.items, by_staff=request.blueprint == staff.name)
    return page("orders", pagination=pagination, filters=filters, filter_args=args, counts=counts,
                archived=archived, deletion_options=options,
                archive_date=lambda value: business_date(datetime.fromisoformat(value)),
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


def customer_catalogue(customer_id, *, allow_empty=False):
    address_book = db.session.get(TmsAddressBook, customer_id)
    if address_book is not None:
        return address_book.rows
    config = current_app.config
    local_catalogue = config.get("TMS_LOCAL_CATALOGUE")
    if config.get("ISOLATED_MODE") and local_catalogue and local_catalogue[0] == customer_id:
        return read_configured_workbook(local_catalogue[1])
    directory = config.get("TMS_ADDRESS_BOOK_DIR")
    if not directory:
        if allow_empty:
            return None
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
        return page("upload", error=UPLOAD_LIMIT_TEXT, replacement=order), 400
    customer_id = g.tms_account.customer_id
    try:
        invoice = parse_invoice(raw)
    except InvoiceError as error:
        return page("upload", error=str(error), replacement=order), 400
    key = invoice["customer_number"].upper()
    digest = sha256(raw).hexdigest()
    candidates = TmsOrder.query.filter_by(customer_id=customer_id).filter(
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
    deletion = order_deletion_options([order], by_staff=request.blueprint == staff.name)[order.id]
    return page(template, order=order, deletion=deletion, totals=invoice_totals(order.invoice), shipment=shipment, tracks=tracks,
                business_date=business_date, events=events, receivable=receivable, customer_measurements=customer_view,
                units=UNITS, settlement_sources=SETTLEMENT_SOURCES, currencies=CURRENCIES, fixed=FIXED, **context)


@client.get("/template")
def template_file():
    extended = request.args.get("extended") == "1"
    try:
        return download_invoice_template(customer_catalogue(g.tms_account.customer_id, allow_empty=extended),
                                         extended=extended)
    except InvoiceError as error:
        return page("upload", error=str(error)), 400


def download_invoice_template(catalogue, *, extended=False):
    layout_path = current_app.config.get("TMS_INVOICE_TEMPLATE_PATH")
    layout = read_configured_workbook(layout_path) if layout_path else None
    result = BytesIO()
    workbook = blank_invoice(catalogue, layout=layout, extended=extended, product_details=True, amazon_po=True)
    try:
        workbook.save(result)
    finally:
        workbook.close()
    result.seek(0)
    filename = "xingrui-invoice-extended.xlsx" if extended else "xingrui-invoice-template.xlsx"
    return send_file(result, as_attachment=True, download_name=filename,
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


@client.post("/orders/<int:order_id>/delete")
def delete_order(order_id):
    change_archive(customer_order(order_id), by_staff=False)
    return redirect(url_for("tms_client.orders"))


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
    try:
        with ZipFile(BytesIO(order.workbook)) as archive:
            mimetype, data = read_photo(archive, part)
    except (BadZipFile, PhotoError, RuntimeError):
        abort(404)
    response = send_file(BytesIO(data), mimetype=mimetype, download_name="product-photo")
    response.cache_control.private = True
    response.cache_control.no_store = True
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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
    return order_list(active_orders().filter(TmsOrder.status != "draft"), "tms_staff.queue", "pending")


@staff.get("/orders/deleted")
def deleted_orders():
    return order_list(TmsOrder.query.filter(archive_marker().is_not(None)), "tms_staff.deleted_orders", "all")


@staff.post("/orders/<int:order_id>/delete")
def staff_delete_order(order_id):
    change_archive(reviewable_order(order_id), by_staff=True)
    return redirect(url_for("tms_staff.queue", status="all"))


@staff.post("/orders/<int:order_id>/restore")
def restore_order(order_id):
    order = TmsOrder.query.filter(TmsOrder.id == order_id, archive_marker().is_not(None)).first_or_404()
    change_archive(order, by_staff=True, restore=True)
    return redirect(url_for("tms_staff.deleted_orders"))


def reviewable_order(order_id):
    return active_orders().filter(TmsOrder.status != "draft", TmsOrder.id == order_id).first_or_404()


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


def assignment_channel_state(order, agent, profile):
    managed = tms_carriers.uses_nextsls(profile, agent)
    catalogue, recommended, binding = {}, None, ""
    error = tms_carriers.channel_setup_error(profile, agent) if managed else None
    if managed and not error:
        catalogue = tms_carriers.channel_catalogue(profile)
        mapping = tms_carriers.channel_mappings(profile).get(order.invoice["service"])
        recommended = mapping["code"] if mapping else None
        binding = f"{agent.id}:{profile.base_url}"
        if not catalogue:
            error = ("渠道清单已过期或该账号没有返回渠道，请在代理设置刷新。"
                     if tms_carriers.CHANNEL_CATALOGUE in profile.verified
                     else "此供应商尚未读取渠道，请在代理设置刷新。")
    requirements = {}
    for code in catalogue:
        rules = tms_carriers.channel_rules(profile, code)
        requirements[code] = ({"declarations": nextsls.declaration_requirements(rules),
                              "unknown_rules":rules.get("unknown_rules", []),
                              "packing": nextsls.required_flag(rules, "packing_list_required"),
                              "sender": rules["from_address"],
                              "photos_confirmed": bool(tms_carriers.images.photos_encoder(profile, code)),
                              "missing": nextsls.missing_supplements(order.invoice,
                                  tms_carriers.order_supplement(order), rules,
                                  use_invoice_po=tms_carriers.invoice_po_allowed(profile, code)),
                              "missing_taxes": nextsls.missing_taxes(order.invoice, rules)}
                             if rules is not None else None)
    return {"managed": managed, "catalogue": catalogue, "recommended": recommended, "binding": binding,
            "error": error, "requirements": requirements}


@staff.post("/orders/<int:order_id>/supplier-channels")
def supplier_channels(order_id):
    order = reviewable_order(order_id)
    agent_id = request.form.get("agent_id", type=int)
    agent = db.session.get(CarrierAgent, agent_id) if agent_id and 0 < agent_id < 2**31 else None
    if not agent or not agent.is_active or order.status not in {"pending", "approved"}:
        return jsonify(error="请选择启用的实际物流供应商。"), 400
    profile = tms_carriers.agent_profile(agent.id)
    error = tms_carriers.refresh_expired_channel_catalogue(profile, agent)
    state = assignment_channel_state(order, agent, profile)
    if error:
        state["error"] = f"渠道自动刷新失败：{error}"
    if state["managed"]:
        state["settings"] = url_for("views.edit_agent", agent_id=agent.id, _anchor="channel-mapping")
    return jsonify(state)


def supplier_assignment_from_form(order, form, *, channel_field="agent_service"):
    agent_id = form.get("agent_id", type=int)
    code = form.get(channel_field, "").strip()
    if not agent_id or not 0 < agent_id < 2**31 or len(code) > 100 or any(ord(c) < 32 for c in code):
        raise ValueError("供应商或运输渠道格式不正确。")
    agent = db.session.get(CarrierAgent, agent_id, with_for_update=True, populate_existing=True)
    if not agent or not agent.is_active:
        raise ValueError("请选择启用的实际物流代理。")
    if not code:
        raise ValueError("请选择或填写实际代理运输渠道。")
    profile = (db.session.get(TmsAgentApi, agent_id, with_for_update=True, populate_existing=True)
               if tms_carriers.carrier_schema() else None)
    state = assignment_channel_state(order, agent, profile)
    if state["managed"]:
        if state["error"]:
            raise ValueError(state["error"])
        if code not in state["catalogue"] or form.get("supplier_binding") != state["binding"]:
            raise ValueError("请选择此 NextSLS 代理当前已核验的渠道。供应商或租户已变化时，请重新选择。")
        return agent, profile, code, state["catalogue"][code]
    if form.get("supplier_binding"):
        raise ValueError("供应商接口类型已变化，请重新选择。")
    return agent, None, code, None


def can_change_assignment(shipment, link):
    if shipment.shipment_id or shipment.third_party_tracking_number or (link and (
            link.state != "new" or link.attempt_at or link.supplier_number or link.tenant_origin
            or link.synced_at or tms_carriers.latest_event(link.order_id, "attempt"))):
        return False
    return shipment.status == "待提交代理"


def staff_detail(order, **context):
    agents = CarrierAgent.query.filter_by(is_active=True).order_by(CarrierAgent.name).all()
    profiles = ({p.agent_id: p for p in db.session.scalars(select(TmsAgentApi)
                 .where(TmsAgentApi.agent_id.in_([a.id for a in agents])))}
                if agents and tms_carriers.carrier_schema() else {})
    assignment = {agent.id: assignment_channel_state(order, agent, profiles.get(agent.id)) for agent in agents}
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
            can_create = not shipment.shipment_id and (not link or link.state == "new")
            unsubmitted = can_create and (not link or not link.attempt_at)
            refresh_error = None
            if (unsubmitted and request.method == "GET" and profile
                    and order.invoice.get("supplier_tenant_origin", profile.base_url) == profile.base_url):
                refresh_error = tms_carriers.refresh_expired_channel_catalogue(profile, agent)
            preparation = tms_carriers.supplier_preparation(order, profile, agent) if can_create else {}
            if refresh_error:
                preparation["blockers"].append(f"渠道自动刷新失败：{refresh_error}")
            supplier = {
                "link": link, "snapshot": snapshot, "cost": cost, "supplier_number": shipment.shipment_id,
                "last_attempt": (tms_carriers.latest_event(order.id, "attempt")
                                 if link and link.state == "uncertain" else None),
                "agent_name": agent.name if agent else "—",
                "agent_active": bool(agent and agent.is_active),
                "preparation": preparation, "agent_id": agent.id if agent else None,
                "create_grant_required": (current_app.config["TMS_NEXTSLS_REQUIRE_ORDER_GRANT"]
                                          or order.invoice.get("workbook_test_sample")),
                "create_grant": tms_carriers.current_create_grant(order, profile, preparation.get("sample_approval")),
                "site_create_enabled": tms_create_control.enabled(),
                "blocker_groups": tms_carriers.supplier_blocker_groups(preparation.get("blockers", [])),
                "address_corrections": supplier_address_corrections(order)
                if unsubmitted and profile and profile.provider == "nextsls" else [],
                "supplement_fields": nextsls.SUPPLEMENTAL_DECLARATIONS,
                "can_change_assignment": can_change_assignment(shipment, link),
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
            "destination_placeholders": [
                DESTINATION_FIELDS[key] for key in address_placeholders({
                    key: request.form.get(f"destination_{key}", order.invoice["address"].get(key, ""))
                    for key in DESTINATION_FIELDS
                })
            ],
        }
    return order_detail(order, agents=agents, assignment=assignment,
                        assigned_agent=db.session.get(CarrierAgent, order.agent_id) if order.agent_id else None,
                        customer=db.session.get(Customer, order.customer_id),
                        adjustments=adjustments, supplier=supplier, **destination, **context)


@staff.get("/orders/<int:order_id>")
def review(order_id):
    return staff_detail(reviewable_order(order_id))


@staff.post("/orders/<int:order_id>/supplier")
def supplier_action(order_id):
    order = reviewable_order(order_id)
    action = request.form.get("action")
    category = "success"
    if action not in {"create", "channel", "prepare", "address", "reconcile", "retry", "sync", "query", "cost",
                      "measurement_review", "grant_create", "revoke_create"}:
        abort(400)
    try:
        if action == "create":
            if request.form.get("confirm") != "yes":
                raise nextsls.ProtocolError("请确认本票供应商下单。")
            if request.form.get("revision", type=int) != order.revision:
                raise nextsls.ProtocolError("订单已经更新，请重新核对本票下单资料。")
            state = tms_carriers.send_order(order, current_user.id)
            message = ("已保存供应商运单号。" if state == "linked"
                       else "未取得代理运单号。请先对账，系统不会自动重试。")
            if state != "linked":
                category = "warning"
        elif action == "query":
            number, channel_matches, reference_matches = tms_carriers.query_linked_shipment(order)
            message = f"供应商已查到运单 {number}。"
            if not channel_matches:
                message += "供应商未返回渠道。"
            if not reference_matches:
                message += "供应商客户参考号未与星睿记录匹配，请人工核对。"
            if not channel_matches or not reference_matches:
                category = "warning"
        elif action in {"grant_create", "revoke_create"}:
            message = change_create_grant(order, revoke=action == "revoke_create")
        elif action == "channel":
            change_supplier_assignment(order)
            message = "已保存新供应商与渠道。旧授权已清除，请按新渠道核对下单资料。"
        elif action == "prepare":
            save_supplier_supplement(order, request.form)
            message = "已保存下单补充资料。"
        elif action == "address":
            correct_supplier_address(order, request.form)
            message = "已保存收件地址更正记录。"
        elif action == "reconcile":
            state = tms_carriers.reconcile_order(order, current_user.id)
            message = ("已核对并保存供应商运单号。" if state == "linked"
                       else "列表未找到匹配运单。请向供应商核实后再解除重试锁。")
            if state != "linked":
                category = "warning"
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
    flash(message, category)
    return redirect(url_for("tms_staff.review", order_id=order.id))


def change_create_grant(order, *, revoke):
    db.session.refresh(order, with_for_update=True)
    if request.form.get("revision", type=int) != order.revision or request.form.get("confirm") != "yes":
        raise nextsls.ProtocolError("请刷新页面并明确确认本票下单授权。")
    shipment, _ = tms_carriers.approved_shipment(order)
    link = db.session.get(TmsSupplierOrder, order.id)
    if shipment.shipment_id or (link and (link.state != "new"
                                          or (order.invoice.get("workbook_test_sample") and link.attempt_at))):
        raise nextsls.ProtocolError("本票已有供应商单号或提交记录。")
    invoice = dict(order.invoice)
    if revoke:
        invoice.pop(tms_carriers.CREATE_GRANT, None)
    else:
        agent = db.session.get(CarrierAgent, order.agent_id)
        profile = db.session.get(TmsAgentApi, order.agent_id)
        preparation = tms_carriers.supplier_preparation(order, profile, agent)
        blockers = preparation["blockers"]
        if blockers:
            raise nextsls.ProtocolError("；".join(blockers) + "。")
        invoice = tms_carriers.make_create_grant(order, profile, current_user.id, preparation=preparation,
            unknown_confirmed=request.form.get("unknown_rules_confirmed") == "yes",
            unknown_basis=request.form.get("unknown_rules_basis", ""))
    update_order(order, {"invoice": invoice})
    history(order, "supplier_revoke" if revoke else "supplier_grant")
    db.session.commit()
    return "已撤销本票下单授权。" if revoke else "已授权本票下单一次，30 分钟内有效。"


def supplier_address_corrections(order):
    pending = nextsls.unverified_destination_fields(order.invoice)
    return [(key, DESTINATION_FIELDS[key]) for key in SUPPLIER_ADDRESS_CORRECTIONS if key in pending]


def correct_supplier_address(order, form):
    db.session.refresh(order, with_for_update=True)
    if form.get("revision", type=int) != order.revision:
        raise nextsls.ProtocolError("订单已更新，请刷新后核对收件地址。")
    shipment, _ = tms_carriers.approved_shipment(order)
    link = db.session.get(TmsSupplierOrder, order.id)
    profile = tms_carriers.agent_profile(order.agent_id)
    if (shipment.shipment_id or (link and (link.state != "new" or link.attempt_at))
            or not profile or profile.provider != "nextsls"):
        raise nextsls.ProtocolError("仅可更正尚未提交的 NextSLS 订单。")
    fields = supplier_address_corrections(order)
    if not fields or form.get("address_confirmed") != "yes":
        raise nextsls.ProtocolError("请按真实收件资料核对后确认更正。")
    reason = nextsls.supplied_text(form.get("address_reason"), 500)
    original = order.invoice["address"]
    changes = {}
    for key, label in fields:
        values = form.getlist(f"address_{key}")
        if len(values) != 1:
            raise nextsls.ProtocolError(f"请核对「{label}」。")
        value = values[0].strip()
        if len(value) > 200 or value == "73" or any(ord(char) < 32 for char in value):
            raise nextsls.ProtocolError(f"请填写真实的「{label}」，确认无此资料时可留空。")
        changes[key] = value
    previous_review = order.invoice.get("destination_review", {})
    review = dict(previous_review)
    review.update(by=current_user.id, at=datetime.now(timezone.utc).isoformat(), changed=True, reason=reason)
    record = {"by": review["by"], "at": review["at"], "reason": reason,
              "before": {key: original[key] for key in changes}, "after": changes,
              "previous_review": previous_review}
    invoice = dict(order.invoice, address=original | changes, destination_review=review,
                   supplier_address_reviews=[*order.invoice.get("supplier_address_reviews", []), record])
    invoice.setdefault("submitted_destination", {"address_code": invoice["address_code"], "address": original})
    update_order(order, {"invoice": invoice})
    history(order, "supplier_address")
    db.session.commit()


def change_supplier_assignment(order):
    db.session.refresh(order, with_for_update=True)
    if request.form.get("revision", type=int) != order.revision:
        raise nextsls.ProtocolError("订单已更新，请刷新后核对供应商和渠道。")
    shipment, receivable = tms_carriers.approved_shipment(order)
    db.session.refresh(shipment, with_for_update=True)
    link = db.session.get(TmsSupplierOrder, order.id, with_for_update=True)
    if not can_change_assignment(shipment, link):
        raise nextsls.ProtocolError("已有供应商提交、轨迹或运单记录，不能直接调整供应商或渠道。")
    reason = request.form.get("assignment_reason", "").strip()
    if not 1 <= len(reason) <= 500 or request.form.get("assignment_confirmed") != "yes":
        raise nextsls.ProtocolError("请核对收件地址、商品申报与费用，并填写调整原因。")
    try:
        agent, profile, code, channel_name = supplier_assignment_from_form(order, request.form)
    except ValueError as error:
        raise nextsls.ProtocolError(str(error)) from None
    original_agent = db.session.get(CarrierAgent, order.agent_id)
    old_origin = order.invoice.get("supplier_tenant_origin")
    new_origin = profile.base_url if profile else None
    if (agent.id, new_origin, code) == (order.agent_id, old_origin, order.agent_service):
        raise nextsls.ProtocolError("请选择不同的供应商或渠道。")
    invoice = dict(order.invoice)
    review = {"from": order.agent_service, "to": code, "to_name": channel_name,
              "from_name": order.invoice.get("supplier_channel", {}).get("name"), "reason": reason,
              "from_agent_id": order.agent_id, "to_agent_id": agent.id,
              "from_agent": original_agent.name if original_agent else "", "to_agent": agent.name,
              "from_origin": old_origin, "to_origin": new_origin,
              "by": current_user.id, "at": datetime.now(timezone.utc).isoformat()}
    if (agent.id, new_origin) != (order.agent_id, old_origin):
        for key in ("amazon_po_confirmation", "amazon_po_line_confirmations"):
            if key in invoice:
                review[key] = invoice.pop(key)
    invoice["supplier_channel_reviews"] = [*invoice.get("supplier_channel_reviews", []), review]
    invoice.pop(tms_carriers.CREATE_GRANT, None)
    invoice.pop("supplier_tenant_origin", None)
    invoice.pop("supplier_channel", None)
    if profile:
        invoice.update(supplier_tenant_origin=new_origin, supplier_channel={"code": code, "name": channel_name})
    declarations = tms_carriers.order_supplement(order).get("declarations")
    if declarations:
        invoice["nextsls_supplement"] = {"agent_id": agent.id, "service": code, "declarations": declarations}
    else:
        invoice.pop("nextsls_supplement", None)
    if link:
        invoice["supplier_event_floor"] = db.session.scalar(select(func.max(TmsSupplierEvent.id)).where(
            TmsSupplierEvent.order_id == order.id)) or 0
        link.agent_id = agent.id
        link.client_reference = "XR-" + uuid4().hex
    update_order(order, {"agent_id": agent.id, "agent_service": code, "invoice": invoice})
    shipment.agent_id = agent.id
    receivable.agent_id, receivable.agent_service = agent.id, code
    if receivable.final_total is not None:
        receivable.details = dict(receivable.details, billing_state="review")
    history(order, "supplier_channel")
    db.session.commit()


def save_supplier_supplement(order, form):
    db.session.refresh(order, with_for_update=True)
    if form.get("revision", type=int) != order.revision:
        raise nextsls.ProtocolError("订单已更新，请刷新后再填写资料。")
    shipment, _ = tms_carriers.approved_shipment(order)
    link = db.session.get(TmsSupplierOrder, order.id)
    profile = tms_carriers.agent_profile(order.agent_id)
    if (shipment.shipment_id or (link and (link.state != "new" or link.attempt_at)) or
            not profile or profile.provider != "nextsls"):
        raise nextsls.ProtocolError("仅可补充尚未提交的 NextSLS 订单。")
    if not nextsls.CODE.fullmatch(order.agent_service or ""):
        raise nextsls.ProtocolError("请先在订单中选择实际 NextSLS 渠道。")
    if order.invoice.get("supplier_tenant_origin", profile.base_url) != profile.base_url:
        raise nextsls.ProtocolError("代理租户已与审核记录不符，请核对原代理账号。")
    if {"packing_name", "packing_url", "remove_packing"}.intersection(form):
        raise nextsls.ProtocolError("装箱单由系统生成，请刷新页面后补充申报资料。")
    rules = tms_carriers.channel_rules(profile, order.agent_service)
    if rules is None:
        rules = tms_carriers.BRAND_ONLY_RULES
    prior = tms_carriers.order_supplement(order)
    missing = nextsls.supplement_inputs(order.invoice, prior, rules,
                                         use_invoice_po=tms_carriers.invoice_po_allowed(profile, order.agent_service))
    allowed = {f"supplement_{index}_{field}" for index, fields in missing.items() for field in fields}
    taxes = dict(order.invoice.get("nextsls_tax", {}))
    missing_tax = nextsls.tax_inputs(order.invoice, rules) if "vat_number_required" in rules else []
    allowed_tax = {"tax_" + field for field in missing_tax}
    if any(key.startswith("tax_") and (key not in allowed_tax or len(form.getlist(key)) != 1) for key in form):
        raise nextsls.ProtocolError("渠道税号要求已变化，请刷新页面后补充。")
    for field in missing_tax:
        raw = form.get("tax_" + field, "").strip()
        if raw:
            taxes[field] = nextsls.tax_value(raw)
    allowed.update(f"supplement_{index}_no_model" for index, fields in missing.items() if "model" in fields)
    if any(key.startswith("supplement_") and (key not in allowed or len(form.getlist(key)) != 1) for key in form):
        raise nextsls.ProtocolError("渠道所需资料已变化，请刷新页面后补充。")
    if "confirm_amazon_d4" in form:
        raise nextsls.ProtocolError("FBA / 客户单号不能作为 Amazon 参考号。请刷新页面。")
    candidates = {i: order.invoice["declarations"][i]["amazon_po_candidate"]
                  for i, fields in missing.items()
                  if "amazon_ref_id" in fields and order.invoice["declarations"][i].get("amazon_po_candidate")}
    line_confirmations = dict(order.invoice.get("amazon_po_line_confirmations", {}))
    line_prefixes = ("confirm_amazon_po_line_", "amazon_po_line_reason_")
    line_fields = {f"{prefix}{i}" for i in candidates for prefix in line_prefixes}
    if any(key.startswith(line_prefixes) and (key not in line_fields or len(form.getlist(key)) != 1)
           for key in form):
        raise nextsls.ProtocolError("待核实的申报行已变化，请刷新页面。")
    for index, candidate in candidates.items():
        check = form.getlist(f"confirm_amazon_po_line_{index}")
        reason = form.get(f"amazon_po_line_reason_{index}", "").strip()
        if not check and not reason:
            continue
        if check != ["yes"] or form.get(f"supplement_{index}_amazon_ref_id", "").strip():
            raise nextsls.ProtocolError("请单独核实该申报行的 PO，或填写已核实的参考号。")
        line_confirmations[str(index)] = {
            "value": nextsls.supplemental_value(order.invoice, "amazon_ref_id", candidate, confirmed_po=True),
            "reason": nextsls.supplied_text(reason, 200), "by": current_user.id,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    confirmation = None
    if "confirm_amazon_po" in form or form.get("amazon_po_reason", "").strip():
        if (nextsls.has_line_amazon_po(order.invoice) or form.getlist("confirm_amazon_po") != ["yes"]
                or not any("amazon_ref_id" in fields for fields in missing.values())
                or any(form.get(f"supplement_{i}_amazon_ref_id", "").strip() for i in missing)):
            raise nextsls.ProtocolError("请单独确认 Amazon PO，或填写已核实的 Amazon 参考号。")
        reason = nextsls.supplied_text(form.get("amazon_po_reason"), 200)
        po = nextsls.supplemental_value(order.invoice, "amazon_ref_id",
                                        order.invoice["metadata"]["po_number"], confirmed_po=True)
        confirmation = {"value": po, "by": current_user.id, "reason": reason,
                        "at": datetime.now(timezone.utc).isoformat()}
    rows = dict(prior.get("declarations", {}))
    for index, fields in missing.items():
        values = dict(rows.get(str(index), {}))
        for field in fields:
            raw = form.get(f"supplement_{index}_{field}", "").strip()
            if field == "model":
                no_model = form.get(f"supplement_{index}_no_model") == "yes"
                if (no_model and raw) or raw == "无":
                    raise nextsls.ProtocolError("商品没有型号时，请仅勾选「无型号」。")
                raw = "无" if no_model else raw
            if raw:
                values[field] = nextsls.supplemental_value(order.invoice, field, raw)
        if values:
            rows[str(index)] = values
    confirmed_lines_changed = line_confirmations != order.invoice.get("amazon_po_line_confirmations", {})
    if (rows == prior.get("declarations", {}) and not confirmation and not confirmed_lines_changed
            and taxes == order.invoice.get("nextsls_tax", {})):
        raise nextsls.ProtocolError("请填写至少一项当前缺少的申报资料。")
    supplement = {**prior, "agent_id": order.agent_id, "service": order.agent_service, "declarations": rows}
    invoice = dict(order.invoice, nextsls_supplement=supplement, nextsls_tax=taxes)
    if confirmation:
        invoice["amazon_po_confirmation"] = confirmation
    if confirmed_lines_changed:
        invoice["amazon_po_line_confirmations"] = line_confirmations
    update_order(order, {"invoice": invoice})
    history(order, "supplier_prep")
    db.session.commit()


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


@staff.get("/orders/<int:order_id>/supplier/packing-draft.xlsx")
def supplier_packing_draft(order_id):
    order = reviewable_order(order_id)
    try:
        tms_carriers.approved_shipment(order)
    except nextsls.ProtocolError:
        abort(404)
    return send_file(tms_carriers.packing_draft(order), download_name="packing-draft.xlsx", as_attachment=True)


@staff.post("/orders/<int:order_id>/supplier/files-check")
def supplier_files_check(order_id):
    order = reviewable_order(order_id)
    db.session.refresh(order, with_for_update=True)
    if request.form.get("revision", type=int) != order.revision:
        return jsonify(error="订单已更新，请刷新页面后重新验收。"), 409
    try:
        tms_carriers.approved_shipment(order)
        profile = tms_carriers.agent_profile(order.agent_id)
        if not profile or order.invoice.get("supplier_tenant_origin", profile.base_url) != profile.base_url:
            raise nextsls.ProtocolError("订单与已审核的代理账号不一致。")
        manifest, draft = tms_carriers.preview_supplier_files(order, profile)
        db.session.commit()
    except nextsls.ProtocolError as error:
        db.session.rollback()
        return jsonify(error=str(error)), 400
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify(error="文件服务暂时无法保存验收文件，请稍后重试。"), 503
    if request.form.get("download") == "yes":
        response = send_file(BytesIO(json.dumps(draft, ensure_ascii=False, indent=2).encode()),
            download_name=f"nextsls-order-{order.id}-preview.json", as_attachment=True, mimetype="application/json")
    else:
        response = jsonify(files=manifest, draft=draft)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@staff.post("/orders/<int:order_id>/decision")
def decide(order_id):
    order = reviewable_order(order_id)
    decision = request.form.get("decision")
    if decision not in {"approve", "reject", "draft", "adjust", "settle"}:
        abort(400)
    required_status = "approved" if decision in {"adjust", "settle"} else "pending"
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
            db.session.rollback()
            if decision == "settle":
                return staff_detail(order, settlement_error=str(error)), 400
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
    suspicious = address_placeholders(address)
    if {"mobile", "phone"}.intersection(suspicious):
        raise ValueError("联系电话或手机含历史模板占位值，请更正或留空。")
    if suspicious and form.get("destination_verify_73") != "yes":
        raise ValueError("收件地址含疑似历史模板占位值，请更正，或另行确认后填写核对依据。")
    if len(reason) > 500 or ((changed or suspicious) and not reason):
        raise ValueError("更正收件地址须填写原因，最多 500 字。")
    return dict(invoice, address_code=code, address=address,
                submitted_destination={"address_code": invoice["address_code"], "address": original},
                destination_review={"by": current_user.id, "at": datetime.now(timezone.utc).isoformat(),
                                    "changed": changed, "reason": reason,
                                    "verified_optional": {key: address[key] for key in suspicious}})


def save_pricing(order, decision):
    form = request.form
    if decision == "settle":
        save_settlement(order, form)
        return
    values = price_form(form)
    valid_draft(values)
    receivable = db.session.get(TmsReceivable, order.id)
    if decision == "adjust":
        if not receivable or receivable.shipment_id is None:
            raise ValueError("该历史订单没有已确认的收费记录。")
        reason = form.get("adjustment_reason", "").strip()
        if not 1 <= len(reason) <= 500:
            raise ValueError("请填写本次费用调整的原因。")
        details = confirm_sales_terms(values, form)
        details.update(sales_by=current_user.id, sales_at=datetime.now(timezone.utc).isoformat())
        if receivable.total is not None:
            details["billing_state"] = "review"
            details["previous_amount_currency"] = receivable.details.get("previous_amount_currency", receivable.currency)
        update_order(order, {})
        db.session.add(TmsChargeAdjustment(
            order_id=order.id, previous=receivable.details, revised=details,
            reason=reason, actor_id=current_user.id,
        ))
        receivable.details, receivable.currency = details, details["currency"]
        history(order, "adjust")
        return

    agent_id = form.get("agent_id", type=int)
    service = form.get("agent_service", "").strip()
    if len(service) > 100 or (agent_id is not None and not 0 < agent_id < 2**31):
        raise ValueError("代理或运输渠道格式不正确。")
    agent = db.session.get(CarrierAgent, agent_id, with_for_update=True) if agent_id else None
    if (agent_id or decision == "approve") and (not agent or not agent.is_active):
        raise ValueError("请选择启用的实际物流代理。")
    if decision == "approve":
        agent, profile, service, channel_name = supplier_assignment_from_form(order, form)
    details = confirm_sales_terms(values, form) if decision == "approve" else values
    if decision == "approve":
        invoice = order.invoice
        changes = {"status": "approved", "agent_id": agent.id, "agent_service": service}
        if invoice.get("address_source") == "workbook":
            invoice = reviewed_invoice(invoice, form)
            changes["invoice"] = invoice
        if channel_name:
            invoice = dict(invoice, supplier_channel={"code": service, "name": channel_name},
                           supplier_tenant_origin=profile.base_url)
            changes["invoice"] = invoice
        db.session.get(Customer, order.customer_id, with_for_update=True)
        if legacy_duplicate(order.customer_id, order.number_key):
            abort(409, "本客户已存在同一单号的运单，请先人工核对。")
        update_order(order, changes)
        shipment = Shipment(
            tracking_number=invoice["customer_number"], customer_id=order.customer_id, agent_id=agent.id,
            destination=invoice["address"]["country"], channel=invoice["service"],
            pieces=len(invoice["boxes"]), weight=float(invoice_totals(invoice)["weight"]),
            status="待提交代理",
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
        receivable.currency = details["currency"]
        details.update(sales_by=current_user.id, sales_at=datetime.now(timezone.utc).isoformat())
    history(order, "approve" if decision == "approve" else "pricing_draft")


def save_settlement(order, form):
    update_order(order, {})  # Serialize against other settlements and supplier measurement updates.
    receivable = db.session.get(TmsReceivable, order.id, populate_existing=True)
    if not receivable or not receivable.shipment_id:
        raise ValueError("未找到与运单关联的销售条款。")
    if form.get("settlement_confirmed") != "yes":
        raise ValueError("请明确确认最终应收。")
    source = form.get("settlement_source")
    note = form.get("settlement_note", "").strip()
    if source not in SETTLEMENT_SOURCES or not 1 <= len(note) <= 200:
        raise ValueError("请选择计费依据，并填写已核实的来源或凭证。")
    old = receivable.details
    unit = old["unit"]
    shipment = db.session.get(Shipment, receivable.shipment_id)
    if (not shipment or shipment.customer_id != order.customer_id
            or shipment.tracking_number != order.invoice["customer_number"]
            or shipment.agent_id != order.agent_id or receivable.agent_id != order.agent_id):
        raise ValueError("订单、运单和代理关联不完整，请先人工核对。")
    if form.get("settlement_unit") != unit:
        raise ValueError("计费数量单位必须与审核销售条款一致。")
    quantity = form.get("settlement_quantity", "").strip()
    if source == "snapshot":
        _, snapshot = tms_carriers.verified_snapshot(order, shipment)
        if not snapshot or form.get("settlement_snapshot") != str(snapshot.id):
            raise ValueError("没有当前代理测量，或数据刚刚更新，请刷新后复核。")
        details = snapshot.details
        expected = {"kg": "kg", "cbm": "m³"}.get(unit)
        confirmed = details.get("measurement_review", {}).get("units", {}).get("chargeable_weight")
        quantity = details.get("summary", {}).get("chargeable_weight")
        if not details.get("identity_verified") or not expected or confirmed != expected or quantity is None:
            raise ValueError("代理计费量尚未核对，或其单位与审核渠道的计费方式不同。")
    revised = settle_charge(old, quantity, source, note)
    revised.pop("previous_amount_currency", None)
    if source == "snapshot":
        revised["billing_snapshot_id"] = snapshot.id
    else:
        revised.pop("billing_snapshot_id", None)
    revised.update(settled_by=current_user.id, settled_at=datetime.now(timezone.utc).isoformat())
    db.session.add(TmsChargeAdjustment(order_id=order.id, previous=old, revised=revised,
                                       reason=note, actor_id=current_user.id))
    receivable.details = revised
    receivable.total = Decimal(revised["total"])
    receivable.confirmed_by = current_user.id
    receivable.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    history(order, "settle")


@staff.get("/orders/<int:order_id>/invoice")
def staff_download(order_id):
    return original_workbook(reviewable_order(order_id))


@staff.get("/orders/<int:order_id>/photos/<int:item_index>")
def staff_photo(order_id, item_index):
    return product_photo(reviewable_order(order_id), item_index)
