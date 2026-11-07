"""Atomic local deletion. No remote synchronization or schema changes."""
from models import db, Shipment, ManualTrack
from sqlalchemy.exc import IntegrityError


def delete_records(records):
    """One transaction for the whole selection, including ORM-owned tracks."""
    try:
        for record in records:
            if not isinstance(record, (Shipment, ManualTrack)):
                raise ValueError("Reference records must be retained")
            if isinstance(record, Shipment):
                from tms_billing import linked_receivable
                if linked_receivable(record.id):
                    raise IntegrityError(None, None, ValueError("已审核运单关联了收费记录"))
            db.session.delete(record)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def deactivate_agent(agent):
    """Retain shipment associations for historical attribution."""
    try:
        agent.is_active = False
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
