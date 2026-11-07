"""Customer ordering and administrator review. This module performs no carrier calls."""
from datetime import datetime, timedelta
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
from secrets import token_hex

import click
from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request,
    send_file, session, url_for,
)
from flask_login import current_user, logout_user
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from models import CarrierAgent, Customer, Shipment, db
from tms_invoice import InvoiceError, MAX_UPLOAD_BYTES, blank_invoice, parse_invoice
from tms_models import TmsAccount, TmsAudit, TmsOrder

client = Blueprint("tms_client", __name__, url_prefix="/tms/customer")
staff = Blueprint("tms_staff", __name__, url_prefix="/tms/admin")
SESSION_KEY = "xingrui_customer"
CLIENT_POSTS = frozenset(f"tms_client.{name}" for name in ("sign_in", "sign_out", "upload", "confirm"))
SESSION_TTL = timedelta(hours=8)
ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9_.@+-]{2,79}\Z")
DUMMY_HASH = generate_password_hash(token_hex(32))
STATUS_LABELS = {"draft": "待确认", "pending": "待审核", "approved": "审核通过", "rejected": "审核驳回"}


def init_tms(app):
    app.config.update(
        TMS_ENABLED=app.config.get("ISOLATED_MODE") or os.getenv("TMS_ENABLED") == "1",
        TMS_ADDRESS_BOOK_DIR=os.getenv("TMS_ADDRESS_BOOK_DIR"),
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
        if request.endpoint == "tms_client.upload" and request.method == "POST":
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
        click.echo("TMS v1 表已就绪。")


def create_tms_schema():
    with db.engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(651360348)"))
        for model in (TmsAccount, TmsOrder, TmsAudit):
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
    username = request.form.get("username", "").strip().casefold()
    password = request.form.get("password", "")
    if len(password) > 128:
        return page("login", error="账号或密码不正确。"), 401
    account = TmsAccount.query.filter_by(username=username).one_or_none() if ACCOUNT_NAME.fullmatch(username) else None
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


@client.get("/")
def orders():
    query = owned_orders()
    term = request.args.get("q", "").strip()[:50]
    status = request.args.get("status", "")
    if term:
        query = query.filter(TmsOrder.number_key.contains(term.upper(), autoescape=True))
    if status in STATUS_LABELS:
        query = query.filter_by(status=status)
    pagination = query.order_by(TmsOrder.id.desc()).paginate(per_page=25)
    return page("orders", pagination=pagination, term=term, status=status)


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
    directory = current_app.config.get("TMS_ADDRESS_BOOK_DIR")
    if not directory:
        return page("upload", error="请联系星睿开通本客户的地址库。", replacement=order), 400
    customer_id = g.tms_account.customer_id
    try:
        with (Path(directory) / f"{customer_id}.xlsx").open("rb") as catalogue:
            invoice = parse_invoice(raw, catalogue.read(MAX_UPLOAD_BYTES + 1))
    except OSError:
        return page("upload", error="请联系星睿开通本客户的地址库。", replacement=order), 400
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


@client.get("/orders/<int:order_id>")
def detail(order_id):
    return page("detail", order=customer_order(order_id))


@client.get("/template")
def template_file():
    result = BytesIO()
    workbook = blank_invoice()
    workbook.save(result)
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


@staff.route("/accounts", methods=["GET", "POST"])
def accounts():
    if request.method == "GET":
        return page("accounts", customers=Customer.query.order_by(Customer.name).all())
    customer_id = request.form.get("customer_id", type=int)
    username = request.form.get("username", "").strip().casefold()
    password = request.form.get("password", "")
    if not (customer_id and db.session.get(Customer, customer_id)
            and ACCOUNT_NAME.fullmatch(username) and 12 <= len(password) <= 128):
        abort(400, "请选择已有客户、填写合法账号，并使用 12–128 个字符的密码。")
    db.session.add(TmsAccount(customer_id=customer_id, username=username,
                              password_hash=generate_password_hash(password)))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, "该客户登录账号已存在。")
    flash("客户登录账号已创建。", "success")
    return redirect(url_for("tms_staff.accounts"))


@staff.get("/")
def queue():
    status = request.args.get("status", "pending")
    if status not in {"pending", "approved", "rejected"}:
        status = "pending"
    pagination = TmsOrder.query.filter_by(status=status).order_by(TmsOrder.id.desc()).paginate(per_page=25)
    return page("orders", pagination=pagination, status=status, term="")


def reviewable_order(order_id):
    return TmsOrder.query.filter(TmsOrder.status != "draft", TmsOrder.id == order_id).first_or_404()


@staff.get("/orders/<int:order_id>")
def review(order_id):
    order = reviewable_order(order_id)
    agents = CarrierAgent.query.filter_by(is_active=True).order_by(CarrierAgent.name).all()
    return page("detail", order=order, agents=agents, customer=db.session.get(Customer, order.customer_id))


@staff.post("/orders/<int:order_id>/decision")
def decide(order_id):
    order = reviewable_order(order_id)
    if order.status != "pending" or request.form.get("revision", type=int) != order.revision:
        abort(409, "订单刚刚变更，请刷新后再审核。")
    decision = request.form.get("decision")
    if decision == "approve":
        agent = db.session.get(CarrierAgent, request.form.get("agent_id", type=int) or 0)
        service = request.form.get("agent_service", "").strip()
        if not agent or not agent.is_active or not 1 <= len(service) <= 100:
            abort(400, "请选择启用的代理，并填写代理服务。")
        if legacy_duplicate(order.customer_id, order.number_key):
            abort(409, "本客户已存在同一单号的运单，请先人工核对。")
        changes = {"status": "approved", "agent_id": agent.id, "agent_service": service}
    elif decision == "reject":
        reason = request.form.get("reason", "").strip()
        if not 1 <= len(reason) <= 500:
            abort(400, "请填写可向客户显示的驳回原因。")
        changes = {"status": "rejected", "rejection_reason": reason}
    else:
        abort(400)
    update_order(order, changes)
    history(order, decision)
    db.session.commit()
    return redirect(url_for("tms_staff.review", order_id=order_id))


@staff.get("/orders/<int:order_id>/invoice")
def staff_download(order_id):
    return original_workbook(reviewable_order(order_id))
