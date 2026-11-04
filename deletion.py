"""Atomic local deletion. No remote synchronization or schema changes."""
from models import db, Shipment, ManualTrack


def delete_records(records):
    """One transaction for the whole selection, including ORM-owned tracks."""
    try:
        for record in records:
            if not isinstance(record, (Shipment, ManualTrack)):
                raise ValueError("Reference records must be retained")
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
