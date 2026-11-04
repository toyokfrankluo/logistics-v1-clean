"""Explicit disposable local runtime. Never reads .env or a business database."""
import secrets
import socket
import sqlite3
import tempfile
from weakref import WeakKeyDictionary
from pathlib import Path

import requests
from sqlalchemy import event
from sqlalchemy.engine import Engine

_directory = None
_runtimes = WeakKeyDictionary()


def deny_network(*args, **kwargs):
    raise RuntimeError("External network disabled in isolated mode")


def configure(app):
    global _directory
    _directory = tempfile.TemporaryDirectory(prefix="logistics-v1-")
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///" + str(Path(_directory.name) / "test.db"),
        SECRET_KEY=secrets.token_hex(32),
        ISOLATED_MODE=True,
    )
    _runtimes[app] = {"uri": app.config["SQLALCHEMY_DATABASE_URI"],
                      "directory": _directory, "fixtures": {}}
    @event.listens_for(Engine, "connect")
    def enforce_sqlite_foreign_keys(connection, record):
        if isinstance(connection, sqlite3.Connection):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    # Block both HTTP and direct Python TCP connections before importing routes.
    requests.sessions.Session.request = deny_network
    requests.sessions.Session.send = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network

    @app.after_request
    def browser_network_policy(response):
        # A browser must not bypass the Python guard via legacy remote fetch URLs.
        response.headers["Content-Security-Policy"] = "connect-src 'self'; form-action 'self'"
        return response


def shadow_runtime_valid(app):
    """Check private initialization record, actual engine target and installed guards.

    Reading engine configuration does not open a database connection.
    """
    runtime = _runtimes.get(app)
    if not runtime or app.config.get('ISOLATED_MODE') is not True:
        return False
    if (app.config.get('SQLALCHEMY_DATABASE_URI') != runtime['uri']
            or not Path(runtime['directory'].name).is_dir()):
        return False
    if (requests.sessions.Session.request is not deny_network
            or requests.sessions.Session.send is not deny_network
            or socket.socket.connect is not deny_network
            or socket.socket.connect_ex is not deny_network
            or socket.create_connection is not deny_network):
        return False
    from models import db
    return str(db.engine.url) == runtime['uri']


def _fixture_identity(shipment):
    return (shipment.id, shipment.tracking_number, shipment.shipment_id,
            shipment.customer_id, shipment.agent_id)


def register_shadow_fixture(app, shipment):
    """Trusted seed/test code only. Not an HTTP endpoint or environment option."""
    if not shadow_runtime_valid(app):
        raise RuntimeError('Trusted isolated runtime required')
    _runtimes[app]['fixtures'][shipment.id] = _fixture_identity(shipment)


def is_shadow_fixture(app, shipment):
    return (shadow_runtime_valid(app)
            and _runtimes[app]['fixtures'].get(shipment.id) == _fixture_identity(shipment))
