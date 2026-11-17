"""Time-limited HTTPS access to individual, immutable supplier file snapshots."""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
from time import time
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4

from flask import Blueprint, Response, abort, current_app, g, request
from itsdangerous import BadData, URLSafeSerializer
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from models import db
from tms_models import TmsOrder, TmsSupplierFile, TmsSupplierPreviewFile


PACKING_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FILE_NAMES = {"image/png": "photo.png", "image/jpeg": "photo.jpg", PACKING_MIME: "packing.xlsx"}
FILE_PATH = "/tms/supplier-files"
MAX_TTL = 7 * 86400
PREVIEW_TTL = 300
files = Blueprint("tms_supplier_files", __name__, url_prefix=FILE_PATH)


@dataclass(frozen=True)
class DirectStore:
    origin: str
    ttl: int

    @property
    def host(self):
        return urlsplit(self.origin).hostname


def configured_store():
    origin = current_app.config.get("TMS_NEXTSLS_FILE_ORIGIN", "")
    ttl = current_app.config.get("TMS_NEXTSLS_FILE_TTL_SECONDS", MAX_TTL)
    if (not re.fullmatch(r"https://[a-z0-9.-]{1,253}\.[a-z]{2,}", origin)
            or type(ttl) is not int or not 3600 <= ttl <= MAX_TTL
            or not current_app.secret_key):
        return None
    return DirectStore(origin, ttl)


def init_app(app):
    render_origin = os.getenv("RENDER_EXTERNAL_URL", "") if os.getenv("RENDER") == "true" else ""
    raw_ttl = os.getenv("TMS_NEXTSLS_FILE_TTL_SECONDS", str(MAX_TTL)).strip()
    app.config.update(
        TMS_NEXTSLS_FILE_ORIGIN=os.getenv("TMS_NEXTSLS_FILE_ORIGIN", render_origin).strip().rstrip("/"),
        TMS_NEXTSLS_FILE_TTL_SECONDS=int(raw_ttl) if re.fullmatch(r"[0-9]{1,6}", raw_ttl) else 0,
    )
    app.register_blueprint(files)
    if render_origin and app.config["TMS_ENABLED"] and not app.config.get("ISOLATED_MODE"):
        # This is additive and runs only for an existing TMS database on Render.
        with app.app_context():
            if configured_store():
                try:
                    with db.engine.begin() as connection:
                        if connection.dialect.name == "postgresql":
                            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                            connection.execute(text("SELECT pg_advisory_xact_lock(651360348)"))
                        if inspect(connection).has_table(TmsOrder.__tablename__):
                            TmsSupplierFile.__table__.create(connection, checkfirst=True)
                            TmsSupplierPreviewFile.__table__.create(connection, checkfirst=True)
                            connection.execute(delete(TmsSupplierFile).where(TmsSupplierFile.expires_at <= int(time())))
                            connection.execute(delete(TmsSupplierPreviewFile).where(TmsSupplierPreviewFile.expires_at <= int(time())))
                except SQLAlchemyError:
                    app.logger.warning("TMS supplier file storage initialization failed")
                finally:
                    # Render preloads Gunicorn before forking its web workers.
                    db.engine.dispose()


def direct_store(order, profile):
    if profile.provider != "nextsls" or profile.agent_id != order.agent_id:
        return None
    store = configured_store()
    if not store:
        return None
    if "tms_supplier_file_table" not in g:
        g.tms_supplier_file_table = inspect(db.session.connection()).has_table(TmsSupplierFile.__tablename__)
    return store if g.tms_supplier_file_table else None


def signer():
    return URLSafeSerializer(current_app.secret_key, salt="xingrui-nextsls-file-v1",
                             signer_kwargs={"digest_method": sha256})


def stage_files(store, order, uploads, *, preview=False):
    """Insert snapshots in the caller's transaction. Return links only after it can commit."""
    now = int(time())
    expiry = now + (PREVIEW_TTL if preview else store.ttl)
    file_signer = signer()
    db.session.execute(delete(TmsSupplierFile).where(TmsSupplierFile.expires_at <= now))
    urls = []
    for _, _, mime, content in uploads:
        handle = uuid4().hex
        filename = FILE_NAMES[mime]
        token = file_signer.dumps([handle, expiry, filename])
        urls.append(f"{store.origin}{FILE_PATH}/{filename}?s={token}")
        db.session.add(TmsSupplierFile(id=handle, order_id=order.id, customer_id=order.customer_id,
                                      agent_id=order.agent_id, mime=mime, expires_at=expiry, content=content))
    return urls


def preview_digest(invoice):
    return sha256(json.dumps(invoice, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def stage_preview_files(store, batch_id, agent_id, invoice, uploads):
    expiry = int(time()) + 1800
    manifest = []
    for slot, mime, content in uploads:
        handle = uuid4().hex
        name = FILE_NAMES[mime]
        signature = signer().dumps(["preview", handle, expiry, name, agent_id])
        db.session.add(TmsSupplierPreviewFile(id=handle, batch_id=batch_id, agent_id=agent_id,
            source_digest=preview_digest(invoice), slot=slot, mime=mime, expires_at=expiry, content=content))
        manifest.append({"slot": slot, "mime": mime, "bytes": len(content), "sha256": sha256(content).hexdigest(),
                         "url": f"{store.origin}{FILE_PATH}/{name}?s={signature}"})
    return manifest


def preview_reference(url, agent_id, invoice, slot):
    store = configured_store()
    parsed = urlsplit(url)
    if not store or parsed.scheme + "://" + parsed.netloc != store.origin:
        raise ValueError()
    payload = signer().loads(parse_qs(parsed.query).get("s", [""])[0])
    if not isinstance(payload, list) or len(payload) != 5 or payload[0] != "preview":
        raise ValueError()
    _, handle, expiry, name, owner = payload
    if owner != agent_id or expiry <= int(time()) or parsed.path != FILE_PATH + "/" + name:
        raise ValueError()
    record = db.session.get(TmsSupplierPreviewFile, handle)
    if (not record or record.agent_id != agent_id or record.slot != slot or record.expires_at != expiry
            or record.source_digest != preview_digest(invoice) or FILE_NAMES[record.mime] != name):
        raise ValueError()
    return url


@files.get("/<filename>")
def read_file(filename):
    if not current_app.config["TMS_ENABLED"] or filename not in FILE_NAMES.values():
        abort(404)
    tokens = request.args.getlist("s")
    if len(tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", tokens[0]):
        abort(404)
    try:
        payload = signer().loads(tokens[0])
        preview_agent = None
        if isinstance(payload, list) and len(payload) == 5 and payload[0] == "preview":
            _, handle, expiry, signed_name, preview_agent = payload
            if type(preview_agent) is not int:
                abort(404)
        else:
            handle, expiry, signed_name = payload
        if (not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{32}", handle)
                or type(expiry) is not int or expiry <= int(time()) or signed_name != filename):
            abort(404)
    except (BadData, ValueError, TypeError):
        abort(404)
    try:
        if preview_agent is not None:
            record = db.session.scalar(select(TmsSupplierPreviewFile).where(
                TmsSupplierPreviewFile.id == handle, TmsSupplierPreviewFile.expires_at == expiry,
                TmsSupplierPreviewFile.agent_id == preview_agent))
        else:
            record = db.session.scalar(
                select(TmsSupplierFile).join(TmsOrder, TmsOrder.id == TmsSupplierFile.order_id)
                .where(TmsSupplierFile.id == handle, TmsSupplierFile.expires_at == expiry,
                       TmsSupplierFile.customer_id == TmsOrder.customer_id,
                       TmsSupplierFile.agent_id == TmsOrder.agent_id))
    except SQLAlchemyError:
        db.session.rollback()
        abort(404)
    if not record or FILE_NAMES.get(record.mime) != filename:
        abort(404)
    disposition = "attachment" if record.mime == PACKING_MIME else "inline"
    return Response(record.content, mimetype=record.mime,
                    headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})


@files.after_request
def protect_file_response(response):
    response.headers.update({
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
    })
    return response
