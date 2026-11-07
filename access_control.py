"""Shared access policy for every Flask-Admin view."""
from flask import abort, redirect, url_for, flash
from flask_admin import AdminIndexView
from flask_admin.actions import action
from flask_admin.contrib.sqla import ModelView
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy.exc import SQLAlchemyError

from deletion import delete_records


class AdminAccess:
    def is_accessible(self):
        return current_user.is_authenticated and current_user.is_admin

    def inaccessible_callback(self, name, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login"))
        abort(403)


class SecureAdminIndexView(AdminAccess, AdminIndexView):
    pass


class SecureModelView(AdminAccess, ModelView):
    form_base_class = FlaskForm
    # Retain reference records (users, customers, agents, bank accounts).
    can_delete = False


class OwnedRecordView(SecureModelView):
    """Shipments and manual tracks may be deleted, including atomic bulk delete."""
    can_delete = True

    def delete_model(self, model):
        try:
            delete_records([model])
            return True
        except SQLAlchemyError:
            flash("删除失败，全部修改已回滚", "error")
            return False

    @action("delete", "删除", "确定删除选中的记录及其手工轨迹吗？")
    def action_delete(self, ids):
        try:
            records = self.get_query().filter(self.model.id.in_(ids)).all()
            delete_records(records)
            flash("所选记录已全部删除", "success")
        except SQLAlchemyError:
            self.session.rollback()
            flash("删除失败，全部修改已回滚", "error")


class CarrierAdminView(SecureModelView):
    # Credentials are managed in /agents, where blank means retain existing value.
    column_exclude_list = ("app_key", "app_token", "password")
    form_excluded_columns = ("app_key", "app_token", "password")
    can_view_details = False


class ShipmentAdminView(OwnedRecordView):
    def on_model_change(self, form, model, is_created):
        if not is_created:
            from tms_billing import linked_receivable
            if linked_receivable(model.id):
                raise ValueError("已审核运单请在 TMS 维护物流资料或调整收费。")
