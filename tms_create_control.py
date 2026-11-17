"""Administrator-managed site permission. Every shipment still needs its own current-version grant."""
from datetime import datetime, timezone
import os

from flask import abort, current_app, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from models import db
from tms_models import TmsCreateControl


def enabled():
    if current_app.config.get("TMS_NEXTSLS_CREATE_EMERGENCY_STOP"):
        return False
    if current_app.config.get("TMS_NEXTSLS_CREATE_MODE") != "admin":
        return bool(current_app.config.get("TMS_NEXTSLS_CREATE_ENABLED"))
    control = db.session.get(TmsCreateControl, 1, populate_existing=True)
    return bool(control and control.enabled)


def save(form, actor_id):
    if current_app.config.get("TMS_NEXTSLS_CREATE_MODE") != "admin":
        abort(409, "此站点尚未启用管理员页面控制。")
    control = db.session.get(TmsCreateControl, 1, with_for_update=True, populate_existing=True)
    if not control or form.get("revision", type=int) != control.revision:
        abort(409, "站点下单设置已变化，请刷新。")
    value = form.get("enabled")
    if value not in {"yes", "no"}:
        abort(400)
    opening = value == "yes"
    if opening and (form.get("confirmed") != "yes" or current_app.config.get("TMS_NEXTSLS_CREATE_EMERGENCY_STOP")):
        abort(400, "请明确确认，且先解除部署层紧急停止。")
    reason = form.get("reason", "").strip()
    if len(reason) > 500:
        abort(400)
    control.history = [*control.history, {"enabled":opening, "by":actor_id, "reason":reason,
        "at":datetime.now(timezone.utc).isoformat(), "previous_revision":control.revision}]
    control.enabled = opening
    control.revision += 1
    db.session.commit()


def register_routes(staff):
    @staff.route("/settings/nextsls-create", methods=["GET", "POST"])
    def create_settings():
        if request.method == "POST":
            save(request.form, current_user.id)
            return redirect(url_for("tms_staff.create_settings"))
        return render_template("tms/create_settings.html", control=db.session.get(TmsCreateControl, 1),
            site_enabled=enabled(), admin_mode=current_app.config.get("TMS_NEXTSLS_CREATE_MODE") == "admin",
            emergency=current_app.config.get("TMS_NEXTSLS_CREATE_EMERGENCY_STOP"), staff=True)


def init_app(app):
    app.config["TMS_NEXTSLS_CREATE_MODE"] = os.getenv("TMS_NEXTSLS_CREATE_MODE", "environment")
    app.config["TMS_NEXTSLS_CREATE_EMERGENCY_STOP"] = os.getenv("TMS_NEXTSLS_CREATE_EMERGENCY_STOP") == "1"
    if os.getenv("RENDER") == "true" and app.config.get("TMS_ENABLED") and not app.config.get("ISOLATED_MODE"):
        with app.app_context():
            with db.engine.begin() as connection:
                if not inspect(connection).has_table("tms_agent_api"):
                    return
                TmsCreateControl.__table__.create(connection, checkfirst=True)
            if not db.session.get(TmsCreateControl, 1):
                db.session.add(TmsCreateControl(id=1, enabled=False, revision=1, history=[]))
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
            db.engine.dispose()
