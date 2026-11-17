"""Administrator-only, transactional NextSLS account import from the customer's workbook."""
import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from hmac import compare_digest
from io import BytesIO
import json
import os
from time import time
from types import SimpleNamespace
from uuid import uuid4

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from openpyxl import load_workbook
from sqlalchemy import inspect, select, text
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from models import CarrierAgent, db
from tms_models import TmsAgentApi, TmsAgentImport
import nextsls
import tms_carriers

MAX_BYTES = 2 * 1024 * 1024
CAPABILITIES = {"create", "reconcile", "tracking", "measurement", "parcels", "charges"}
operations = Blueprint("tms_agent_operations", __name__, url_prefix="/tms/operations/agents")


@operations.before_request
def authorize_operation():
    # A temporary deployment credential grants only account import and read-only verification.
    # Only its SHA-256 digest is stored in Render. It cannot create shipments or log in as a user.
    digest = current_app.config.get("TMS_AGENT_IMPORT_KEY_SHA256", "")
    expiry = current_app.config.get("TMS_AGENT_IMPORT_KEY_EXPIRES", 0)
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if (os.getenv("RENDER") != "true" or current_app.config.get("TMS_NEXTSLS_CREATE_ENABLED")
            or not current_app.config.get("TMS_ENABLED") or not digest or time() >= expiry
            or not compare_digest(sha256(supplied.encode()).hexdigest(), digest)):
        abort(404)
    if (current_app.config.get("TMS_AGENT_IMPORT_SCOPE") == "preflight"
            and request.endpoint == "tms_agent_operations.import_operation"):
        abort(404)


@operations.after_request
def protect_operation_response(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@operations.post("/import")
def import_operation():
    digest = current_app.config["TMS_AGENT_IMPORT_KEY_SHA256"]
    existing = db.session.scalar(select(TmsAgentImport).where(TmsAgentImport.source_digest == "operation:" + digest))
    if existing:
        return jsonify(batch=existing.id, result=existing.result)
    if request.content_length is None or request.content_length > MAX_BYTES + 65536:
        abort(413)
    upload = request.files.get("accounts")
    raw = upload.read(MAX_BYTES + 1) if upload else b""
    try:
        batch = apply_accounts(parse_accounts(raw), 0, "operation:" + digest)
    except nextsls.ProtocolError as error:
        return jsonify(error=str(error)), 400
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify(error="代理配置未保存，本批已回滚。"), 409
    return jsonify(batch=batch.id, result=batch.result)


@operations.get("")
def verify_operation():
    agents = list(db.session.scalars(select(CarrierAgent).order_by(CarrierAgent.id)))
    profiles = {p.agent_id: p for p in db.session.scalars(select(TmsAgentApi))}
    from models import Shipment
    from sqlalchemy import func
    counts = dict(db.session.execute(select(Shipment.agent_id, func.count()).group_by(Shipment.agent_id)).all())
    return render_template("agent.html", agents=agents, profiles=profiles, shipment_counts=counts,
                           providers=tms_carriers.PROVIDERS, capabilities=nextsls.CAPABILITIES,
                           integration_ready=True)


@operations.route("/channels", methods=["GET", "POST"])
def verify_channels_operation():
    from tms_models import TmsOrder
    from tms_portal import assignment_channel_state
    order = db.session.scalar(select(TmsOrder).order_by(TmsOrder.id))
    fixture_text = request.values.get("preflight_invoice")
    if fixture_text:
        try:
            invoice = json.loads(fixture_text)
            if invoice["customer_number"] != "XR-PREFLIGHT-DO-NOT-SHIP":
                raise ValueError()
            order = SimpleNamespace(invoice=invoice, agent_id=None, agent_service=None)
        except (ValueError, KeyError, TypeError):
            abort(400)
    agent_id = request.values.get("agent_id", type=int)
    agent = db.session.get(CarrierAgent, agent_id) if agent_id else None
    if not order or not agent or not agent.is_active:
        return jsonify(error="请选择启用的实际物流供应商。"), 400
    return jsonify(assignment_channel_state(order, agent, db.session.get(TmsAgentApi, agent.id)))


@operations.get("/review")
def verify_review_operation():
    from tms_models import TmsOrder
    from tms_portal import assignment_channel_state
    order = db.session.scalar(select(TmsOrder).order_by(TmsOrder.id))
    agents = list(db.session.scalars(select(CarrierAgent).where(CarrierAgent.is_active.is_(True))))
    profiles = {p.agent_id: p for p in db.session.scalars(select(TmsAgentApi))}
    assignment = {a.id: assignment_channel_state(order, a, profiles.get(a.id)) for a in agents}
    return render_template("tms/agent_import_review.html", order=order, invoice=order.invoice, agents=agents,
                           assignment=assignment, channel_url=url_for("tms_agent_operations.verify_channels_operation"))


@operations.post("/preflight")
def preflight_operation():
    """Build payloads from explicitly synthetic data without reserving an order or sending HTTP."""
    from copy import deepcopy
    from collections import Counter
    import tms_supplier_files
    import tms_supplier_images
    body = request.get_json(silent=True)
    invoice = body.get("invoice", body) if isinstance(body, dict) else None
    deliveries = body.get("deliveries") if isinstance(body, dict) else None
    if not isinstance(invoice, dict) or invoice.get("customer_number") != "XR-PREFLIGHT-DO-NOT-SHIP":
        abort(400)
    store = tms_supplier_files.configured_store()
    results = []
    for profile in db.session.scalars(select(TmsAgentApi).where(TmsAgentApi.provider == "nextsls")):
        if body.get("agent_id") and body["agent_id"] != profile.agent_id:
            continue
        agent = db.session.get(CarrierAgent, profile.agent_id)
        catalogue = tms_carriers.channel_catalogue(profile)
        passed, failures, blocked = [], Counter(), {}
        sample = None
        actual_delivery, delivery_error = None, None
        if deliveries is not None and agent.is_active:
            try:
                actual_delivery = deliveries[str(profile.agent_id)]
                if set(actual_delivery) != {"packing", *(str(i) for i in range(len(invoice["declarations"])))}:
                    raise ValueError()
                for slot, url in actual_delivery.items():
                    tms_supplier_files.preview_reference(url, profile.agent_id, invoice, slot)
            except Exception:
                delivery_error = "预检图片或装箱单与当前供应商、模拟资料不匹配。"
        for code in catalogue:
            if body.get("channel_code") and body["channel_code"] != code:
                continue
            if deliveries is not None and not agent.is_active:
                blocked[code] = "代理保持停用，本轮不执行附件预检"
                failures[blocked[code]] += 1
                continue
            rules = tms_carriers.channel_rules(profile, code)
            if rules is None:
                failures["供应商未提供完整渠道规则"] += 1
                blocked[code] = "供应商未提供完整渠道规则"
                continue
            encoder = tms_supplier_images.photos_encoder(profile, code)
            image_fields = {"photo_url"} if "photo_url" in rules["require_declaration_list"] else set()
            if encoder and "photos" in rules["require_declaration_list"]:
                image_fields.add("photos")
            try:
                nextsls.create_payload(deepcopy(invoice), code, "XR-PREFLIGHT-NOT-SENT", rules,
                    profile.sender, preflight=True, image_fields=image_fields,
                    packing_host=store.host if store else "", photos_protocol_confirmed=bool(encoder))
                # Explicitly synthetic links exercise construction only. They are never published or sent.
                fixture_host = store.host if store else "preflight.example.com"
                if delivery_error:
                    raise nextsls.ProtocolError(delivery_error)
                actual = actual_delivery
                payload = nextsls.create_payload(deepcopy(invoice), code, "XR-PREFLIGHT-NOT-SENT", rules,
                    profile.sender, image_fields=image_fields, packing_host=fixture_host,
                    photos_protocol_confirmed=bool(encoder),
                    publish_image_fields=(lambda: [{key: actual[str(i)] if actual else f"https://{fixture_host}/preflight-not-published/photo-{i}.png"
                                                   for key in image_fields} for i in range(len(invoice["declarations"]))])
                                          if image_fields else None,
                    publish_packing=lambda: actual["packing"] if actual else f"https://{fixture_host}/preflight-not-published/packing.xlsx")
            except nextsls.ProtocolError as error:
                failures[str(error)] += 1
                blocked[code] = str(error)
                continue
            passed.append(code)
            if sample is None:
                sample = payload
        results.append({"agent_id": profile.agent_id, "name": agent.name, "active": agent.is_active,
                        "channels": len(catalogue), "passed": passed, "failures": dict(failures),
                        "sample": sample, "blocked": blocked, "transport_calls": 0,
                        "files_published": deliveries is not None and bool(sample)})
    return jsonify(results=results)


@operations.route("/files", methods=["POST", "DELETE"])
def preflight_files_operation():
    import re
    from PIL import Image
    import tms_supplier_files as files
    from tms_models import TmsSupplierPreviewFile
    body = request.get_json(silent=True) or {}
    batch_id = body.get("batch_id", "")
    if not isinstance(batch_id, str) or not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        abort(400)
    if request.method == "DELETE":
        count = db.session.execute(delete(TmsSupplierPreviewFile).where(
            TmsSupplierPreviewFile.batch_id == batch_id)).rowcount
        db.session.commit()
        return jsonify(deleted=count)
    if request.content_length is None or request.content_length > MAX_BYTES:
        abort(413)
    invoice = body.get("invoice", {})
    if invoice.get("customer_number") != "XR-PREFLIGHT-DO-NOT-SHIP":
        abort(400)
    store = files.configured_store()
    if not store:
        return jsonify(error="HTTPS 文件发布服务未配置。"), 400
    pictures = body.get("pictures", [])
    if len(pictures) != len(invoice.get("declarations", [])) or not 0 < len(pictures) <= 100:
        abort(400)
    uploads = []
    try:
        for index, photo in enumerate(pictures):
            raw = base64.b64decode(photo["data"], validate=True)
            with Image.open(BytesIO(raw)) as decoded:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg"}[decoded.format]
                decoded.verify()
            uploads.append((str(index), mime, raw))
        draft_order = SimpleNamespace(invoice=invoice, agent_id=0, agent_service="PREFLIGHT")
        uploads.append(("packing", files.PACKING_MIME, tms_carriers.packing_draft(draft_order).getvalue()))
    except Exception:
        return jsonify(error="模拟图片或装箱资料无效，未发布文件。"), 400
    results = []
    for agent in db.session.scalars(select(CarrierAgent).join(TmsAgentApi).where(
            CarrierAgent.is_active.is_(True), TmsAgentApi.provider == "nextsls")):
        manifest = files.stage_preview_files(store, batch_id, agent.id, invoice, uploads)
        results.append({"agent_id": agent.id, "name": agent.name, "files": manifest})
    db.session.commit()
    return jsonify(batch_id=batch_id, results=results)


def parse_accounts(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise nextsls.ProtocolError("请选择不超过 2 MB 的代理接入 XLSX 文件。")
    try:
        book = load_workbook(BytesIO(raw), read_only=True, data_only=True)
        sheet = book["代理填写"]
        rows = list(sheet.iter_rows(min_row=6, max_row=105, max_col=8, values_only=True))
        book.close()
    except Exception:
        raise nextsls.ProtocolError("无法读取代理填写工作表。") from None
    accounts, origins = [], set()
    for number, row in enumerate(rows, 6):
        _, name, candidate, token, choice, _, connector, _ = row
        if not name or choice in {"不接入", "跳过"}:
            continue
        name = nextsls.supplied_text(name, 200)
        token = token.strip() if isinstance(token, str) else ""
        if not 20 <= len(token) <= 255 or any(ord(char) <= 32 for char in token):
            raise nextsls.ProtocolError(f"第 {number} 行缺少有效 App Token。")
        origin = nextsls.base_url(candidate.strip() if isinstance(candidate, str) else "", "nextsls")
        if connector:
            try:
                decoded = json.loads(base64.b64decode(str(connector).strip(), validate=True))
                code_origin = nextsls.base_url("https://" + decoded["app_code"] + ".nextsls.com", "nextsls")
                if decoded["token"] != token:
                    raise ValueError()
            except Exception:
                raise nextsls.ProtocolError(f"第 {number} 行对接码与 App Token 不一致或格式无效。") from None
            # The signed-in operator supplied both fields. The connector carries the actual tenant code.
            origin = code_origin
        if not origin or origin in origins:
            raise nextsls.ProtocolError(f"第 {number} 行的租户域名缺失或重复。")
        origins.add(origin)
        accounts.append({"name": name, "origin": origin, "token": token})
    if not accounts or len(accounts) > 50:
        raise nextsls.ProtocolError("每批须包含 1 至 50 家不同租户。")
    return accounts


def load_account(account):
    # Read-only verification is independent of both real-create and reconciliation switches.
    agent = SimpleNamespace(app_token=account["token"], is_active=True, supports_api=True, api_url="")
    profile = SimpleNamespace(provider="nextsls", enabled=["create"], base_url=account["origin"])
    catalogue, rules = tms_carriers.read_channel_catalogue(profile, agent)
    if not catalogue:
        raise nextsls.ProtocolError(f"{account['name']} 没有返回可用渠道，本批未写入。")
    return account | {"catalogue": catalogue, "rules": rules}


def capture(agent, profile):
    return {"agent": {column.name: getattr(agent, column.name) for column in CarrierAgent.__table__.columns
                      if column.name not in {"created_at", "updated_at"}},
            "profile": ({column.name: getattr(profile, column.name) for column in TmsAgentApi.__table__.columns
                        if column.name not in {"created_at", "updated_at"}} if profile else None)}


def apply_accounts(accounts, actor_id, source_digest):
    if current_app.config.get("TMS_NEXTSLS_CREATE_ENABLED"):
        raise nextsls.ProtocolError("请先关闭站点真实创建总开关，再导入代理配置。")
    # Resolve remote reads before taking configuration locks. No create endpoint is called here.
    app = current_app._get_current_object()
    def fetch(account):
        with app.app_context():
            return load_account(account)
    with ThreadPoolExecutor(max_workers=6) as pool:
        verified = list(pool.map(fetch, accounts))
    try:
        if db.engine.dialect.name == "postgresql":
            db.session.execute(text("SELECT pg_advisory_xact_lock(651360349)"))
        if source_digest.startswith("operation:"):
            prior = db.session.scalar(select(TmsAgentImport).where(TmsAgentImport.source_digest == source_digest))
            if prior:
                db.session.commit()
                return prior
        agents = list(db.session.scalars(select(CarrierAgent).with_for_update()))
        profiles = {p.agent_id: p for p in db.session.scalars(select(TmsAgentApi).with_for_update())}
        previous, result, used = [], [], set()
        for account in verified:
            origin = account["origin"]
            matches = [a for a in agents if (profiles.get(a.id) and profiles[a.id].base_url == origin)
                       or (nextsls.tenant_host(a.api_url) == nextsls.tenant_host(origin))
                       or a.name.casefold() == account["name"].casefold()]
            if len(matches) > 1:
                raise nextsls.ProtocolError(f"{account['name']} 匹配到多个账号，本批已回滚。")
            agent = matches[0] if matches else None
            created = agent is None
            if created:
                name = "锦瞬达 jsd" if account["name"] == "jsd" else account["name"]
                agent = CarrierAgent(name=name, is_active=True, supports_api=True)
                db.session.add(agent)
                db.session.flush()
                agents.append(agent)
            if agent.id in used:
                raise nextsls.ProtocolError("不同租户匹配到同一代理，本批已回滚。")
            used.add(agent.id)
            profile = profiles.get(agent.id)
            if (profile and (profile.provider != "nextsls" or profile.base_url != origin)
                    or not nextsls.legacy_tenant_matches(agent.api_url, origin)):
                raise nextsls.ProtocolError(f"{agent.name} 的既有接口与租户不符，本批已回滚。")
            previous.append({"id": agent.id, "created": created,
                             "config": None if created else capture(agent, profile)})
            if profile is None:
                profile = TmsAgentApi(agent_id=agent.id, provider="nextsls", base_url=origin,
                                      enabled=[], service_codes=[], sender={}, verified={})
                db.session.add(profile)
                profiles[agent.id] = profile
            if agent.app_token and agent.app_token != account["token"]:
                profile.verified = {key: value for key, value in profile.verified.items()
                                    if key == tms_carriers.CHANNEL_MAPPINGS}
            agent.app_token = account["token"]
            agent.supports_api = True
            profile.enabled = sorted(set(profile.enabled) | CAPABILITIES)
            tms_carriers.store_channel_catalogue(profile, account["catalogue"], account["rules"])
            result.append({"id": agent.id, "name": agent.name, "origin": origin, "created": created,
                           "active": agent.is_active, "channels": len(account["catalogue"]),
                           "complete_rules": len(account["rules"]), "token_configured": bool(agent.app_token)})
        batch = TmsAgentImport(id=uuid4().hex, actor_id=actor_id, source_digest=source_digest,
                               previous=previous, result=result)
        db.session.add(batch)
        db.session.commit()
        return batch
    except Exception:
        db.session.rollback()
        raise


def init_app(app):
    app.config["TMS_AGENT_IMPORT_SCOPE"] = os.getenv("TMS_AGENT_IMPORT_SCOPE", "import")
    app.config["TMS_AGENT_IMPORT_KEY_SHA256"] = os.getenv("TMS_AGENT_IMPORT_KEY_SHA256", "")
    expiry = os.getenv("TMS_AGENT_IMPORT_KEY_EXPIRES", "0")
    app.config["TMS_AGENT_IMPORT_KEY_EXPIRES"] = int(expiry) if expiry.isdigit() else 0
    app.register_blueprint(operations)
    from write_security import csrf
    csrf.exempt(operations)
    if os.getenv("RENDER") == "true" and app.config.get("TMS_ENABLED") and not app.config.get("ISOLATED_MODE"):
        with app.app_context():
            with db.engine.begin() as connection:
                if inspect(connection).has_table(TmsAgentApi.__tablename__):
                    TmsAgentImport.__table__.create(connection, checkfirst=True)
            db.engine.dispose()


def register_routes(staff):
    @staff.route("/agents/import", methods=["GET", "POST"])
    def import_agents():
        if request.method == "POST":
            if request.content_length is None or request.content_length > MAX_BYTES + 65536:
                abort(413)
            if request.form.get("confirmed") != "yes":
                abort(400, "请确认批量配置供应商。")
            upload = request.files.get("accounts")
            raw = upload.read(MAX_BYTES + 1) if upload else b""
            try:
                batch = apply_accounts(parse_accounts(raw), current_user.id, sha256(raw).hexdigest())
            except nextsls.ProtocolError as error:
                return render_template("tms/agent_import.html", error=str(error)), 400
            except SQLAlchemyError:
                db.session.rollback()
                return render_template("tms/agent_import.html", error="代理配置未保存，本批已回滚。"), 409
            flash("代理配置和独立渠道清单已保存。停用状态已保留。", "success")
            return redirect(url_for("tms_staff.import_agents", batch=batch.id))
        batch = db.session.get(TmsAgentImport, request.args.get("batch")) if request.args.get("batch") else None
        return render_template("tms/agent_import.html", result=batch.result if batch else None)
