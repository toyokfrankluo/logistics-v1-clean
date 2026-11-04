"""Request-level permissions plus standard Flask-WTF CSRF protection."""
from flask import abort, request
from flask_login import current_user
from flask_wtf.csrf import CSRFProtect, CSRFError

csrf = CSRFProtect()

ADMIN_ENDPOINTS = {
    "agents", "edit_agent", "delete_agent", "bank_accounts",
    "add_shipment", "edit_shipment", "delete_shipment", "import_shipments",
    "sync_to_supabase", "sync_tracking_details", "refresh_tracking",
    "refresh_single_tracking", "refresh_full_history", "auto_sync_single",
    "auto_sync_all_safe", "auto_sync_all", "auto_sync_recent",
}
# These POSTs only query/export data or change the caller's login session.
NON_ADMIN_POSTS = {
    "login", "logout", "track", "track_17track", "finance", "invoice",
    "public_track_page", "public_track_json",
}


def init_write_security(app, login_manager):
    @app.before_request
    def authorize_operation():
        if request.endpoint is None:
            return None
        name = request.endpoint.rsplit(".", 1)[-1]
        needs_admin = name in ADMIN_ENDPOINTS or (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and name not in NON_ADMIN_POSTS
        )
        if needs_admin:
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if not current_user.is_admin:
                abort(403)

    # Permission checks run first; CSRF remains enabled even in TESTING mode.
    csrf.init_app(app)

    @app.errorhandler(CSRFError)
    def csrf_failure(error):
        return "请求验证失败，请刷新页面后重试。", 400
