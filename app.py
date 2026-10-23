import base64
import csv
import io
import json
import math
import os
import secrets
import sqlite3
import tempfile
import threading
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from PIL import Image, ImageOps, UnidentifiedImageError
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, Response, g, jsonify, redirect, render_template, request, send_file, session, url_for
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image as ReportImage, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix


ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("BT_INVO_DATABASE", ROOT / "bt_invo.sqlite3"))
INSTANCE_DIR = Path(os.environ.get("BT_INVO_INSTANCE_DIR", ROOT / "instance"))
CATEGORIES = ("CHOCOLATE", "CAKE", "FLOWERS", "COFFE", "CLEANING", "GENERAL")
UNITS = (("kg", 5, "kg", 1, "kg"), ("gram", 0.5, "gram", 1, "gram"), ("pieces", 1, "pieces", 1, "pieces"), ("box", 6, "pieces", 6, "pieces"))
CURRENCIES = ("INR", "LKR", "USD", "AED", "BHD", "EUR", "GBP", "AUD", "CAD")
ROLES = ("master", "admin", "staff")

app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=2 * 1024 * 1024)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.appdata"]
DRIVE_BACKUP_NAME = "BT INVO backup.sqlite3"
drive_sync_wake = threading.Event()
drive_sync_worker_guard = threading.Lock()
drive_sync_upload_guard = threading.Lock()
drive_sync_worker = None


def connect_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    db = connect_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('master', 'admin', 'staff')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            address TEXT NOT NULL DEFAULT '',
            telephone TEXT NOT NULL DEFAULT '',
            mobile TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS product_uom (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            default_pack_size REAL NOT NULL,
            pack_unit TEXT NOT NULL,
            default_pack_count INTEGER NOT NULL DEFAULT 1,
            base_unit TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            supplier_item_code TEXT NOT NULL DEFAULT '',
            uom TEXT NOT NULL REFERENCES product_uom(name),
            pack_size REAL NOT NULL,
            category TEXT NOT NULL REFERENCES categories(name),
            supplier_id INTEGER REFERENCES suppliers(id) ON DELETE RESTRICT,
            default_price REAL NOT NULL DEFAULT 0 CHECK (default_price >= 0),
            pack_count INTEGER NOT NULL DEFAULT 1,
            units_per_pack REAL NOT NULL DEFAULT 1,
            base_unit TEXT NOT NULL DEFAULT '',
            reorder_level REAL NOT NULL DEFAULT 5,
            default_price_currency TEXT NOT NULL DEFAULT 'INR',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
            movement_type TEXT NOT NULL CHECK (movement_type IN ('stock_in', 'stock_out')),
            quantity REAL NOT NULL CHECK (quantity > 0),
            unit_price REAL NOT NULL DEFAULT 0 CHECK (unit_price >= 0),
            total_amount REAL NOT NULL DEFAULT 0,
            base_quantity REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'INR',
            supplier_id INTEGER REFERENCES suppliers(id) ON DELETE RESTRICT,
            purchase_order_id INTEGER REFERENCES purchase_orders(id) ON DELETE RESTRICT,
            invoice_number TEXT NOT NULL DEFAULT '',
            movement_date TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
            quantity REAL NOT NULL CHECK (quantity > 0),
            received_quantity REAL NOT NULL DEFAULT 0,
            pack_size REAL NOT NULL DEFAULT 1,
            pack_count INTEGER NOT NULL DEFAULT 1,
            units_per_pack REAL NOT NULL DEFAULT 1,
            base_unit TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            supplier_id INTEGER REFERENCES suppliers(id) ON DELETE RESTRICT,
            order_date TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS stock_adjustments (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
            quantity_delta REAL NOT NULL CHECK (quantity_delta != 0),
            base_quantity_delta REAL NOT NULL,
            reason TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            adjustment_date TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_movements_date ON stock_movements(movement_date);
        CREATE INDEX IF NOT EXISTS idx_movements_item ON stock_movements(item_id);
        CREATE INDEX IF NOT EXISTS idx_orders_date ON purchase_orders(order_date);
        """
    )
    user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "password_change_required" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN password_change_required INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE users SET password_change_required = 1")
    item_columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
    legacy_pack_migration = "units_per_pack" not in item_columns
    item_migrations = {
        "default_price": "REAL NOT NULL DEFAULT 0",
        "pack_count": "INTEGER NOT NULL DEFAULT 1",
        "units_per_pack": "REAL NOT NULL DEFAULT 1",
        "base_unit": "TEXT NOT NULL DEFAULT ''",
        "reorder_level": "REAL NOT NULL DEFAULT 5",
        "default_price_currency": "TEXT NOT NULL DEFAULT 'INR'",
    }
    for column, definition in item_migrations.items():
        if column not in item_columns:
            db.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")
    uom_columns = {row[1] for row in db.execute("PRAGMA table_info(product_uom)")}
    for column, definition in {
        "default_pack_count": "INTEGER NOT NULL DEFAULT 1",
        "base_unit": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in uom_columns:
            db.execute(f"ALTER TABLE product_uom ADD COLUMN {column} {definition}")
    movement_columns = {row[1] for row in db.execute("PRAGMA table_info(stock_movements)")}
    for column, definition in {
        "base_quantity": "REAL NOT NULL DEFAULT 0",
        "currency": "TEXT NOT NULL DEFAULT 'INR'",
        "purchase_order_id": "INTEGER REFERENCES purchase_orders(id) ON DELETE RESTRICT",
        "invoice_number": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in movement_columns:
            db.execute(f"ALTER TABLE stock_movements ADD COLUMN {column} {definition}")
    order_columns = {row[1] for row in db.execute("PRAGMA table_info(purchase_orders)")}
    legacy_order_migration = "pack_size" not in order_columns
    for column, definition in {
        "received_quantity": "REAL NOT NULL DEFAULT 0",
        "pack_size": "REAL NOT NULL DEFAULT 1",
        "pack_count": "INTEGER NOT NULL DEFAULT 1",
        "units_per_pack": "REAL NOT NULL DEFAULT 1",
        "base_unit": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'open'",
        "supplier_id": "INTEGER REFERENCES suppliers(id) ON DELETE RESTRICT",
    }.items():
        if column not in order_columns:
            db.execute(f"ALTER TABLE purchase_orders ADD COLUMN {column} {definition}")
    if legacy_order_migration:
        db.execute("UPDATE purchase_orders SET pack_size=COALESCE((SELECT pack_size FROM items WHERE id=purchase_orders.item_id), 1), pack_count=COALESCE((SELECT pack_count FROM items WHERE id=purchase_orders.item_id), 1), units_per_pack=COALESCE((SELECT units_per_pack FROM items WHERE id=purchase_orders.item_id), 1), base_unit=COALESCE((SELECT base_unit FROM items WHERE id=purchase_orders.item_id), '')")
        db.execute("UPDATE purchase_orders SET status=CASE WHEN received_quantity >= quantity THEN 'received' WHEN received_quantity > 0 THEN 'partial' ELSE 'open' END")
    seeded = db.execute("SELECT value FROM app_settings WHERE key = 'defaults_seeded'").fetchone()
    if seeded is None:
        db.executemany("INSERT OR IGNORE INTO categories(name) VALUES (?)", ((name,) for name in CATEGORIES))
        db.executemany(
            "INSERT OR IGNORE INTO product_uom(name, default_pack_size, pack_unit, default_pack_count, base_unit) VALUES (?, ?, ?, ?, ?)",
            UNITS,
        )
        db.execute("INSERT INTO app_settings(key, value) VALUES ('defaults_seeded', '1')")
    db.execute("UPDATE product_uom SET default_pack_size=6, default_pack_count=6, pack_unit='pieces', base_unit='pieces' WHERE name='box' AND default_pack_size=36 AND default_pack_count=1")
    db.execute("UPDATE product_uom SET base_unit=pack_unit WHERE base_unit=''")
    if legacy_pack_migration:
        db.execute("UPDATE items SET units_per_pack=pack_size WHERE units_per_pack=1 AND pack_size != 1")
        db.execute("UPDATE items SET pack_count=6, units_per_pack=6, base_unit='pieces' WHERE uom='box' AND pack_size=36")
    db.execute("UPDATE items SET base_unit=COALESCE((SELECT base_unit FROM product_uom WHERE name=items.uom), uom) WHERE base_unit=''")
    db.execute("UPDATE stock_movements SET base_quantity=quantity * COALESCE((SELECT pack_size FROM items WHERE id=stock_movements.item_id), 1) WHERE base_quantity=0")
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        bootstrap_username = os.environ.get("BT_INVO_MASTER_USERNAME", "").strip()
        bootstrap_password = os.environ.get("BT_INVO_MASTER_PASSWORD", "")
        if bootstrap_username and bootstrap_password:
            if len(bootstrap_username) > 160:
                raise ValueError("BT_INVO_MASTER_USERNAME must be 160 characters or fewer.")
            if len(bootstrap_password) < 12:
                raise ValueError("BT_INVO_MASTER_PASSWORD must be at least 12 characters.")
            if len(bootstrap_password) > 256:
                raise ValueError("BT_INVO_MASTER_PASSWORD must be 256 characters or fewer.")
            db.execute(
                """INSERT INTO users(username, password_hash, role, password_change_required)
                   VALUES (?, ?, 'master', 0)""",
                (bootstrap_username, generate_password_hash(bootstrap_password)),
            )
    secret = db.execute("SELECT value FROM app_settings WHERE key = 'secret_key'").fetchone()
    if secret is None:
        secret_value = secrets.token_hex(32)
        db.execute("INSERT INTO app_settings(key, value) VALUES ('secret_key', ?)", (secret_value,))
    else:
        secret_value = secret[0]
    db.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES ('currency', 'INR')")
    db.executemany(
        "INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)",
        (("drive_sync_enabled", "1"), ("drive_sync_pending", "0"), ("drive_sync_revision", "0"), ("drive_sync_needs_initial_choice", "0")),
    )
    db.commit()
    db.close()
    app.secret_key = os.environ.get("BT_INVO_SECRET_KEY", secret_value)


initialize_database()


def drive_client_secrets_path():
    return INSTANCE_DIR / "google_drive_oauth.json"


def drive_oauth_redirect_uri():
    public_url = os.environ.get("BT_INVO_PUBLIC_URL", "").strip().rstrip("/")
    if public_url:
        parsed_url = urlsplit(public_url)
        if (parsed_url.scheme not in ("http", "https") or not parsed_url.netloc
                or parsed_url.path or parsed_url.query or parsed_url.fragment
                or parsed_url.username or parsed_url.password):
            raise ValueError("BT_INVO_PUBLIC_URL must be an HTTP(S) origin without a path.")
        return f"{public_url}/api/drive/callback"
    return url_for("drive_oauth_callback", _external=True)


def drive_token_cipher():
    key_path = INSTANCE_DIR / "drive-token.key"
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path.parent.chmod(0o700)
    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        generated_key = Fernet.generate_key()
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(generated_key)
            key = generated_key
        except FileExistsError:
            key = key_path.read_bytes()
    key_path.chmod(0o600)
    try:
        return Fernet(key)
    except ValueError:
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        return Fernet(key)


def save_setting(db, key, value):
    db.execute(
        "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def setting_value(key, default=""):
    row = database().execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def drive_credentials():
    from google.oauth2.credentials import Credentials

    encrypted = setting_value("drive_credentials")
    if not encrypted:
        return None
    try:
        token_info = json.loads(drive_token_cipher().decrypt(encrypted.encode("ascii")))
        return Credentials.from_authorized_user_info(token_info, DRIVE_SCOPES)
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_drive_credentials(db, credentials):
    encrypted = drive_token_cipher().encrypt(credentials.to_json().encode("utf-8")).decode("ascii")
    save_setting(db, "drive_credentials", encrypted)


def build_drive_service():
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build

    credentials = drive_credentials()
    if credentials is None:
        raise RuntimeError("Connect a Google Drive account first.")
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        db = database()
        save_drive_credentials(db, credentials)
        db.commit()
    if not credentials.valid:
        raise RuntimeError("Google Drive authorization has expired. Connect again.")
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def find_drive_backup(service):
    result = service.files().list(
        q=f"name = '{DRIVE_BACKUP_NAME}' and trashed = false",
        spaces="appDataFolder", pageSize=10, fields="files(id,name,modifiedTime,size)",
    ).execute()
    return (result.get("files") or [None])[0]


def upload_database_snapshot():
    from googleapiclient.http import MediaFileUpload

    if not drive_sync_upload_guard.acquire(blocking=False):
        return
    snapshot_path = None
    try:
        db = database()
        if setting_value("drive_sync_enabled", "1") != "1":
            return
        if setting_value("drive_sync_needs_initial_choice") == "1":
            return
        if setting_value("drive_sync_pending") != "1":
            return
        revision = int(setting_value("drive_sync_revision", "0"))
        handle = tempfile.NamedTemporaryFile(prefix="bt-invo-sync-", suffix=".sqlite3", delete=False)
        snapshot_path = Path(handle.name)
        handle.close()
        destination = sqlite3.connect(snapshot_path)
        db.backup(destination)
        destination.close()
        snapshot = sqlite3.connect(snapshot_path)
        snapshot.execute("DELETE FROM app_settings WHERE key='drive_credentials'")
        snapshot.execute("UPDATE app_settings SET value=? WHERE key='secret_key'", (secrets.token_hex(32),))
        snapshot.commit()
        snapshot.close()

        service = build_drive_service()
        backup = find_drive_backup(service)
        media = MediaFileUpload(str(snapshot_path), mimetype="application/vnd.sqlite3", resumable=True)
        if backup:
            uploaded = service.files().update(fileId=backup["id"], media_body=media).execute()
        else:
            uploaded = service.files().create(
                body={"name": DRIVE_BACKUP_NAME, "mimeType": "application/vnd.sqlite3", "parents": ["appDataFolder"]},
                media_body=media, fields="id,name,modifiedTime",
            ).execute()

        db.execute("BEGIN IMMEDIATE")
        current_revision = int(setting_value("drive_sync_revision", "0"))
        save_setting(db, "drive_sync_file_id", uploaded.get("id", ""))
        save_setting(db, "drive_sync_last_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        save_setting(db, "drive_sync_last_error", "")
        save_setting(db, "drive_sync_pending", "1" if current_revision > revision else "0")
        db.commit()
    finally:
        if snapshot_path:
            snapshot_path.unlink(missing_ok=True)
        drive_sync_upload_guard.release()


def drive_sync_worker_loop():
    while True:
        drive_sync_wake.wait(timeout=60)
        drive_sync_wake.clear()
        try:
            with app.app_context():
                upload_database_snapshot()
        except Exception:
            try:
                with app.app_context():
                    db = database()
                    save_setting(db, "drive_sync_last_error", "Could not reach Google Drive. Local changes are safe; retrying automatically.")
                    db.commit()
            except Exception:
                app.logger.exception("Could not record Google Drive sync failure")


def ensure_drive_sync_worker():
    global drive_sync_worker
    if not drive_client_secrets_path().is_file() or not setting_value("drive_credentials"):
        return
    with drive_sync_worker_guard:
        if drive_sync_worker is None or not drive_sync_worker.is_alive():
            drive_sync_worker = threading.Thread(target=drive_sync_worker_loop, name="bt-invo-drive-sync", daemon=True)
            drive_sync_worker.start()
        drive_sync_wake.set()


@app.after_request
def queue_drive_snapshot(response):
    if request.method not in ("POST", "PUT", "PATCH", "DELETE") or not 200 <= response.status_code < 300:
        return response
    if request.path in ("/api/login", "/api/logout", "/api/drive/sync", "/api/drive/auto-sync", "/api/drive/client"):
        return response
    db = None
    try:
        db = connect_db()
        connected = db.execute("SELECT value FROM app_settings WHERE key='drive_credentials'").fetchone()
        enabled = db.execute("SELECT value FROM app_settings WHERE key='drive_sync_enabled'").fetchone()
        if connected and enabled is not None and enabled[0] == "1":
            revision_row = db.execute("SELECT value FROM app_settings WHERE key='drive_sync_revision'").fetchone()
            revision = int(revision_row[0]) + 1 if revision_row else 1
            save_setting(db, "drive_sync_revision", revision)
            save_setting(db, "drive_sync_pending", "1")
            db.commit()
            with app.app_context():
                ensure_drive_sync_worker()
        db.close()
    except Exception:
        app.logger.exception("Could not queue Google Drive snapshot")
    finally:
        if db is not None:
            db.close()
    return response


def database():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_database(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def json_error(message, status=400):
    return jsonify({"error": message}), status


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        user = database().execute(
            "SELECT username, role, active, password_change_required FROM users WHERE id = ?",
            (user_id,),
        ).fetchone() if user_id else None
        if user is None or not user["active"]:
            session.clear()
            return json_error("Please sign in to continue.", 401)
        session["username"] = user["username"]
        session["role"] = user["role"]
        session["password_change_required"] = bool(user["password_change_required"])
        if user["password_change_required"] and request.endpoint != "change_own_password":
            return json_error("Change your password before using the app.", 403)
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") not in roles:
                return json_error("Your account cannot make this change.", 403)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def payload():
    return request.get_json(silent=True) or {}


def required_text(data, key, label, limit=160):
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"{label} is required.")
    if len(value) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer.")
    return value


def positive_number(data, key, label, allow_zero=False):
    try:
        value = float(data.get(key))
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {label.lower()}.") from None
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"{label} must be {qualifier}.")
    return value


def valid_date(value, label):
    value = str(value or date.today().isoformat())
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Choose a valid {label.lower()}.") from None
    return value


def current_user_id():
    return int(session["user_id"])


def category_names(db=None):
    db = db or database()
    return [row[0] for row in db.execute("SELECT name FROM categories ORDER BY id")]


def app_setting(key, default=""):
    row = database().execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def business_profile():
    return {
        "shop_name": app_setting("business_shop_name", "BT Chocolate Shop"),
        "address": app_setting("business_address"),
        "telephone": app_setting("business_telephone"),
        "mobile": app_setting("business_mobile"),
        "has_logo": bool(app_setting("business_logo_data")),
        "logo_url": url_for("business_logo") if app_setting("business_logo_data") else "",
    }


def audit_event(db, action, entity_type, entity_id=None, details=None):
    db.execute(
        "INSERT INTO audit_log(user_id, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?)",
        (session.get("user_id"), action, entity_type, entity_id, json.dumps(details or {}, sort_keys=True)),
    )


def stock_rows(category=None, active_only=True):
    filters = []
    params = []
    if active_only:
        filters.append("i.active = 1")
    if category and category in category_names():
        filters.append("i.category = ?")
        params.append(category)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    return database().execute(
        f"""SELECT i.*, s.name AS supplier_name,
            COALESCE(m.base_on_hand, 0) + COALESCE(a.base_quantity_delta, 0) AS on_hand_base,
            (COALESCE(m.base_on_hand, 0) + COALESCE(a.base_quantity_delta, 0)) / NULLIF(i.pack_size, 0) AS on_hand,
            COALESCE((SELECT unit_price FROM stock_movements WHERE item_id = i.id AND movement_type = 'stock_in'
             ORDER BY movement_date DESC, id DESC LIMIT 1), i.default_price) AS last_price
            FROM items i LEFT JOIN suppliers s ON s.id = i.supplier_id
            LEFT JOIN (SELECT item_id,
                SUM(CASE WHEN movement_type='stock_in' THEN base_quantity ELSE -base_quantity END) AS base_on_hand
                FROM stock_movements GROUP BY item_id) m ON m.item_id = i.id
            LEFT JOIN (SELECT item_id, SUM(quantity_delta) AS quantity_delta,
                SUM(base_quantity_delta) AS base_quantity_delta FROM stock_adjustments GROUP BY item_id) a ON a.item_id=i.id
            {where} ORDER BY i.name COLLATE NOCASE""",
        params,
    ).fetchall()


def serialize(row):
    return dict(row)


def item_for_request(item_id):
    item = database().execute("SELECT * FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone()
    if item is None:
        raise ValueError("Choose an active item.")
    return item


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/login")
def login():
    data = payload()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if database().execute("SELECT COUNT(*) FROM users WHERE active = 1").fetchone()[0] == 0:
        return json_error(
            "No account is configured. Set BT_INVO_MASTER_USERNAME and BT_INVO_MASTER_PASSWORD, then restart the app.",
            503,
        )
    user = database().execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return json_error("Username or password is incorrect.", 401)
    session.clear()
    session.update(
        user_id=user["id"], username=user["username"], role=user["role"],
        password_change_required=bool(user["password_change_required"]),
    )
    return jsonify({"user": {
        "id": user["id"], "username": user["username"], "role": user["role"],
        "password_change_required": bool(user["password_change_required"]),
    }})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/session")
@login_required
def session_info():
    return jsonify({"user": {"id": session["user_id"], "username": session["username"], "role": session["role"]}})


@app.get("/api/bootstrap")
@login_required
def bootstrap():
    db = database()
    if (setting_value("drive_sync_pending") == "1"
            and setting_value("drive_sync_enabled", "1") == "1"
            and setting_value("drive_sync_needs_initial_choice") != "1"):
        ensure_drive_sync_worker()
    items = stock_rows(active_only=False)
    movements = db.execute(
        """SELECT m.*, i.name AS item_name, i.supplier_item_code, i.uom, i.pack_size, i.base_unit, i.category,
            s.name AS supplier_name, u.username FROM stock_movements m
            JOIN items i ON i.id = m.item_id LEFT JOIN suppliers s ON s.id = m.supplier_id
            JOIN users u ON u.id = m.user_id ORDER BY m.movement_date DESC, m.id DESC LIMIT 12"""
    ).fetchall()
    adjustments = db.execute(
        """SELECT a.*, i.name AS item_name, i.uom, i.base_unit, i.category, u.username
           FROM stock_adjustments a JOIN items i ON i.id=a.item_id JOIN users u ON u.id=a.user_id
           ORDER BY a.adjustment_date DESC, a.id DESC LIMIT 12"""
    ).fetchall()
    return jsonify(
        {
            "user": {"id": session["user_id"], "username": session["username"], "role": session["role"]},
            "categories": category_names(db),
            "categoryRecords": [serialize(row) for row in db.execute("SELECT * FROM categories ORDER BY id")],
            "uoms": [serialize(row) for row in db.execute("SELECT * FROM product_uom ORDER BY id")],
            "currency": app_setting("currency", "INR"),
            "businessProfile": business_profile(),
            "suppliers": [serialize(row) for row in db.execute("SELECT * FROM suppliers ORDER BY name COLLATE NOCASE")],
            "items": [serialize(row) for row in items],
            "users": [serialize(row) for row in db.execute("SELECT id, username, role, active FROM users ORDER BY username")],
            "movements": [serialize(row) for row in movements],
            "adjustments": [serialize(row) for row in adjustments],
            "orders": [serialize(row) for row in db.execute(
                    """SELECT p.*, i.name AS item_name, i.supplier_item_code, i.uom, i.pack_size, s.name AS supplier_name,
                         u.username, (p.quantity - p.received_quantity) AS remaining_quantity
                         FROM purchase_orders p JOIN items i ON i.id = p.item_id JOIN users u ON u.id = p.user_id
                         LEFT JOIN suppliers s ON s.id=p.supplier_id
                         ORDER BY CASE p.status WHEN 'open' THEN 0 WHEN 'partial' THEN 1 ELSE 2 END, p.order_date DESC, p.id DESC LIMIT 100"""
            )],
                         "audit": [serialize(row) for row in db.execute(
                          """SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id=a.user_id
                             ORDER BY a.id DESC LIMIT 100"""
                         )] if session.get("role") in ("master", "admin") else [],
        }
    )


@app.post("/api/categories")
@login_required
@roles_required("master", "admin")
def create_category():
    try:
        name = required_text(payload(), "name", "Category", 60).upper()
        db = database()
        cursor = db.execute("INSERT INTO categories(name) VALUES (?)", (name,))
        audit_event(db, "create", "category", cursor.lastrowid, {"name": name})
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.IntegrityError:
        return json_error("That category already exists.")


@app.put("/api/categories/<int:category_id>")
@login_required
@roles_required("master", "admin")
def update_category(category_id):
    try:
        name = required_text(payload(), "name", "Category", 60).upper()
        db = database()
        old = db.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
        if old is None:
            return json_error("Category not found.", 404)
        db.execute("PRAGMA defer_foreign_keys = ON")
        db.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
        db.execute("UPDATE items SET category = ? WHERE category = ?", (name, old["name"]))
        audit_event(db, "rename", "category", category_id, {"from": old["name"], "to": name})
        db.commit()
        return jsonify({"ok": True})
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.IntegrityError:
        database().rollback()
        return json_error("That category name already exists.")


@app.delete("/api/categories/<int:category_id>")
@login_required
@roles_required("master", "admin")
def delete_category(category_id):
    db = database()
    try:
        if db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] <= 1:
            return json_error("At least one category must remain.")
        cursor = db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        if not cursor.rowcount:
            return json_error("Category not found.", 404)
        audit_event(db, "delete", "category", category_id)
        db.commit()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        db.rollback()
        return json_error("Categories used by products cannot be deleted.")


@app.post("/api/units")
@login_required
@roles_required("master", "admin")
def create_unit():
    data = payload()
    try:
        name = required_text(data, "name", "Unit", 32)
        pack_unit = required_text(data, "pack_unit", "Base unit", 32)
        base_unit = required_text(data, "base_unit", "Stock unit", 32)
        default_pack_count_value = positive_number(data, "default_pack_count", "Default pack count")
        if not default_pack_count_value.is_integer():
            raise ValueError("Default pack count must be a whole number.")
        units_per_pack = positive_number(data, "default_pack_size", "Units per pack")
        db = database()
        cursor = db.execute(
            """INSERT INTO product_uom(name, default_pack_size, pack_unit, default_pack_count, base_unit)
               VALUES (?, ?, ?, ?, ?)""",
            (name, units_per_pack, pack_unit, int(default_pack_count_value), base_unit),
        )
        audit_event(db, "create", "unit", cursor.lastrowid, {"name": name, "base_unit": base_unit})
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.IntegrityError:
        return json_error("That unit already exists or its details are invalid.")


@app.delete("/api/units/<int:unit_id>")
@login_required
@roles_required("master", "admin")
def delete_unit(unit_id):
    db = database()
    try:
        if db.execute("SELECT COUNT(*) FROM product_uom").fetchone()[0] <= 1:
            return json_error("At least one product unit must remain.")
        cursor = db.execute("DELETE FROM product_uom WHERE id = ?", (unit_id,))
        if not cursor.rowcount:
            return json_error("Unit not found.", 404)
        audit_event(db, "delete", "unit", unit_id)
        db.commit()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        db.rollback()
        return json_error("Units used by products cannot be deleted.")


@app.patch("/api/settings")
@login_required
@roles_required("master", "admin")
def update_settings():
    currency = str(payload().get("currency", "")).upper()
    if currency not in CURRENCIES:
        return json_error("Choose a supported currency.")
    db = database()
    current = app_setting("currency", "INR")
    if currency != current and db.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]:
        return json_error("Currency is locked after the first stock transaction to keep historical totals consistent.")
    db.execute("INSERT INTO app_settings(key, value) VALUES ('currency', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (currency,))
    db.execute("UPDATE items SET default_price_currency = ?", (currency,))
    audit_event(db, "update", "settings", None, {"currency": currency})
    db.commit()
    return jsonify({"currency": currency})


@app.post("/api/business-profile")
@login_required
@roles_required("master", "admin")
def update_business_profile():
    shop_name = str(request.form.get("shop_name", "")).strip()
    address = str(request.form.get("address", "")).strip()
    telephone = str(request.form.get("telephone", "")).strip()
    mobile = str(request.form.get("mobile", "")).strip()
    if not shop_name or len(shop_name) > 120:
        return json_error("Shop name is required and must be 120 characters or fewer.")
    if len(address) > 300 or len(telephone) > 40 or len(mobile) > 40:
        return json_error("Address must be 300 characters or fewer; phone numbers must be 40 characters or fewer.")

    logo = request.files.get("logo")
    logo_data = None
    logo_mime = None
    if logo and logo.filename:
        raw = logo.stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return json_error("The logo image must be 1 MB or smaller.")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ("PNG", "JPEG") or image.width > 4096 or image.height > 4096:
                    return json_error("Choose a PNG or JPEG logo no larger than 4096 × 4096 pixels.")
                image_format = image.format
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image = ImageOps.exif_transpose(image)
                output = io.BytesIO()
                if image_format == "PNG":
                    image.save(output, format="PNG", optimize=True)
                    logo_mime = "image/png"
                else:
                    image.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
                    logo_mime = "image/jpeg"
                logo_data = output.getvalue()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            return json_error("The uploaded logo could not be read as a valid PNG or JPEG image.")
        if len(logo_data) > 1024 * 1024:
            return json_error("The optimized logo image must be 1 MB or smaller.")

    db = database()
    save_setting(db, "business_shop_name", shop_name)
    save_setting(db, "business_address", address)
    save_setting(db, "business_telephone", telephone)
    save_setting(db, "business_mobile", mobile)
    if logo_data is not None:
        save_setting(db, "business_logo_data", base64.b64encode(logo_data).decode("ascii"))
        save_setting(db, "business_logo_mime", logo_mime)
    elif request.form.get("remove_logo") == "1":
        db.execute("DELETE FROM app_settings WHERE key IN ('business_logo_data', 'business_logo_mime')")
    audit_event(db, "update", "business_profile", None, {"logo_updated": logo_data is not None or request.form.get("remove_logo") == "1"})
    db.commit()
    return jsonify({"businessProfile": business_profile()})


@app.get("/api/business-logo")
@login_required
def business_logo():
    encoded = app_setting("business_logo_data")
    if not encoded:
        return json_error("No business logo has been uploaded.", 404)
    image_data = base64.b64decode(encoded, validate=True)
    return send_file(io.BytesIO(image_data), mimetype=app_setting("business_logo_mime", "image/png"),
                     download_name="business-logo", as_attachment=False, max_age=300)


@app.get("/api/audit")
@login_required
@roles_required("master", "admin")
def audit_history():
    rows = database().execute(
        """SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id=a.user_id
           ORDER BY a.id DESC LIMIT 500"""
    ).fetchall()
    return jsonify({"events": [serialize(row) for row in rows]})


@app.post("/api/items/<int:item_id>/adjustments")
@login_required
@roles_required("master", "admin")
def adjust_stock(item_id):
    data = payload()
    try:
        item = item_for_request(item_id)
        delta = float(data.get("quantity_delta"))
        if not math.isfinite(delta) or delta == 0:
            raise ValueError("Adjustment quantity must be a finite non-zero number.")
        reason = required_text(data, "reason", "Adjustment reason", 40)
        if reason not in ("count correction", "damage", "opening balance", "other"):
            raise ValueError("Choose a valid adjustment reason.")
        adjustment_date = valid_date(data.get("adjustment_date"), "Adjustment date")
        db = database()
        on_hand = db.execute(
            """SELECT COALESCE(SUM(CASE WHEN movement_type='stock_in' THEN quantity ELSE -quantity END), 0)
               + COALESCE((SELECT SUM(quantity_delta) FROM stock_adjustments WHERE item_id=?), 0)
               FROM stock_movements WHERE item_id=?""",
            (item_id, item_id),
        ).fetchone()[0]
        if on_hand + delta < 0:
            raise ValueError(f"Adjustment cannot reduce stock below zero. Current balance: {on_hand:g} {item['uom']}.")
        cursor = db.execute(
            """INSERT INTO stock_adjustments(item_id, quantity_delta, base_quantity_delta, reason, note, adjustment_date, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_id, delta, delta * item["pack_size"], reason, str(data.get("note", "")).strip(), adjustment_date, current_user_id()),
        )
        audit_event(db, "adjust", "stock", cursor.lastrowid,
                    {"item_id": item_id, "quantity_delta": delta, "reason": reason, "note": str(data.get("note", "")).strip()})
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except (ValueError, TypeError) as error:
        return json_error(str(error) or "Enter a valid adjustment quantity.")


@app.post("/api/account/password")
@login_required
def change_own_password():
    data = payload()
    old_password = str(data.get("old_password", ""))
    new_password = str(data.get("new_password", ""))
    user = database().execute("SELECT password_hash FROM users WHERE id = ?", (current_user_id(),)).fetchone()
    if not check_password_hash(user["password_hash"], old_password):
        return json_error("Current password is incorrect.", 403)
    if len(new_password) < 12:
        return json_error("New password must be at least 12 characters.")
    if len(new_password) > 256:
        return json_error("New password must be 256 characters or fewer.")
    if check_password_hash(user["password_hash"], new_password):
        return json_error("Choose a new password different from your current password.")
    db = database()
    db.execute(
        "UPDATE users SET password_hash = ?, password_change_required = 0 WHERE id = ?",
        (generate_password_hash(new_password), current_user_id()),
    )
    audit_event(db, "change password", "user", current_user_id())
    db.commit()
    session["password_change_required"] = False
    return jsonify({"ok": True})


@app.get("/api/export/items.csv")
@login_required
@roles_required("master", "admin")
def export_items():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Item", "Supplier item code", "Category", "Supplier", "Unit", "Packs", "Units per pack", "Base unit", "On hand (packs)", "On hand (base)", "Reorder level", "Latest price", "Currency"])
    for item in stock_rows(active_only=False):
        writer.writerow([item["name"], item["supplier_item_code"], item["category"], item["supplier_name"], item["uom"], item["pack_count"], item["units_per_pack"], item["base_unit"], item["on_hand"], item["on_hand_base"], item["reorder_level"], item["last_price"], item["default_price_currency"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=bt-invo-items.csv"})


@app.get("/api/export/movements.csv")
@login_required
@roles_required("master", "admin")
def export_movements():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Type", "Item", "Supplier", "Invoice number", "Package quantity", "Base quantity", "Price per package", "Amount", "Currency", "Order ID", "User"])
    rows = database().execute(
        """SELECT m.*, i.name AS item_name, s.name AS supplier_name, u.username
           FROM stock_movements m JOIN items i ON i.id=m.item_id LEFT JOIN suppliers s ON s.id=m.supplier_id
           JOIN users u ON u.id=m.user_id ORDER BY m.movement_date, m.id"""
    )
    for row in rows:
        writer.writerow([row["movement_date"], row["movement_type"], row["item_name"], row["supplier_name"], row["invoice_number"], row["quantity"], row["base_quantity"], row["unit_price"], row["total_amount"], row["currency"], row["purchase_order_id"], row["username"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=bt-invo-movements.csv"})


@app.get("/api/backup")
@login_required
@roles_required("master", "admin")
def backup_database():
    handle = tempfile.NamedTemporaryFile(prefix="bt-invo-", suffix=".sqlite3", delete=False)
    backup_path = Path(handle.name)
    handle.close()
    destination = sqlite3.connect(backup_path)
    database().backup(destination)
    destination.close()
    response = send_file(backup_path, as_attachment=True, download_name=f"bt-invo-backup-{date.today().isoformat()}.sqlite3", mimetype="application/vnd.sqlite3")
    response.call_on_close(lambda: backup_path.unlink(missing_ok=True))
    return response


@app.get("/api/drive/status")
@login_required
@roles_required("master")
def drive_sync_status():
    db = database()
    get_setting = lambda key, default="": (db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone() or [default])[0]
    try:
        connected = drive_credentials() is not None
    except Exception:
        connected = False
    return jsonify({
        "configured": drive_client_secrets_path().is_file(),
        "connected": connected,
        "enabled": get_setting("drive_sync_enabled", "1") == "1",
        "pending": get_setting("drive_sync_pending") == "1",
        "needs_initial_choice": get_setting("drive_sync_needs_initial_choice") == "1",
        "backup_exists": bool(get_setting("drive_sync_file_id")),
        "last_sync": get_setting("drive_sync_last_at") or None,
        "last_error": get_setting("drive_sync_last_error") or None,
        "callback_url": drive_oauth_redirect_uri(),
    })


@app.post("/api/drive/client")
@login_required
@roles_required("master")
def upload_drive_client_secrets():
    uploaded = request.files.get("client_json")
    if uploaded is None or not uploaded.filename:
        return json_error("Choose the Google OAuth client JSON file.")
    raw = uploaded.stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        return json_error("The OAuth client file must be 64 KB or smaller.")
    try:
        client_config = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return json_error("The selected file is not valid JSON.")
    web_config = client_config.get("web")
    if not isinstance(web_config, dict) or not web_config.get("client_id") or not web_config.get("client_secret"):
        return json_error("Upload a Google OAuth client JSON for a Web application.")
    path = drive_client_secrets_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_bytes(raw)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)
    path.chmod(0o600)
    db = database()
    db.execute("DELETE FROM app_settings WHERE key='drive_credentials'")
    save_setting(db, "drive_sync_pending", "0")
    save_setting(db, "drive_sync_needs_initial_choice", "0")
    save_setting(db, "drive_sync_last_error", "")
    audit_event(db, "configure", "google_drive", None, {"oauth_client": "uploaded"})
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/drive/connect")
@login_required
@roles_required("master")
def connect_google_drive():
    from google_auth_oauthlib.flow import Flow

    client_path = drive_client_secrets_path()
    if not client_path.is_file():
        return json_error("Upload a Google OAuth Web client first.", 400)
    flow = Flow.from_client_secrets_file(str(client_path), scopes=DRIVE_SCOPES)
    flow.redirect_uri = drive_oauth_redirect_uri()
    authorization_url, state_token = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true",
    )
    session["drive_oauth_state"] = state_token
    return redirect(authorization_url)


@app.get("/api/drive/callback")
@login_required
@roles_required("master")
def drive_oauth_callback():
    from google_auth_oauthlib.flow import Flow

    state_token = session.pop("drive_oauth_state", None)
    if not state_token or request.args.get("state") != state_token:
        return json_error("Google sign-in state did not match. Try connecting again.", 400)
    if request.args.get("error"):
        return json_error("Google Drive authorization was cancelled.", 400)
    flow = Flow.from_client_secrets_file(str(drive_client_secrets_path()), scopes=DRIVE_SCOPES, state=state_token)
    flow.redirect_uri = drive_oauth_redirect_uri()
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception:
        return json_error("Google Drive authorization failed. Check the OAuth redirect URI and try again.", 400)
    db = database()
    save_drive_credentials(db, flow.credentials)
    save_setting(db, "drive_sync_enabled", "1")
    save_setting(db, "drive_sync_last_error", "")
    db.commit()
    try:
        remote = find_drive_backup(build_drive_service())
    except Exception:
        remote = None
    if remote:
        save_setting(db, "drive_sync_file_id", remote["id"])
        save_setting(db, "drive_sync_needs_initial_choice", "1")
        save_setting(db, "drive_sync_pending", "0")
    else:
        revision = int(setting_value("drive_sync_revision", "0")) + 1
        save_setting(db, "drive_sync_revision", revision)
        save_setting(db, "drive_sync_needs_initial_choice", "0")
        save_setting(db, "drive_sync_pending", "1")
    audit_event(db, "connect", "google_drive")
    db.commit()
    if not remote:
        ensure_drive_sync_worker()
    return redirect("/?drive=connected")


@app.post("/api/drive/sync")
@login_required
@roles_required("master")
def start_drive_sync():
    if not drive_credentials():
        return json_error("Connect a Google Drive account first.", 400)
    db = database()
    revision = int(setting_value("drive_sync_revision", "0")) + 1
    save_setting(db, "drive_sync_revision", revision)
    save_setting(db, "drive_sync_pending", "1")
    save_setting(db, "drive_sync_needs_initial_choice", "0")
    save_setting(db, "drive_sync_enabled", "1")
    save_setting(db, "drive_sync_last_error", "")
    audit_event(db, "sync requested", "google_drive")
    db.commit()
    ensure_drive_sync_worker()
    return jsonify({"ok": True, "queued": True})


@app.patch("/api/drive/auto-sync")
@login_required
@roles_required("master")
def set_drive_auto_sync():
    enabled = bool(payload().get("enabled"))
    if enabled and not drive_credentials():
        return json_error("Connect a Google Drive account before enabling automatic sync.")
    db = database()
    save_setting(db, "drive_sync_enabled", "1" if enabled else "0")
    audit_event(db, "automatic sync enabled" if enabled else "automatic sync paused", "google_drive")
    db.commit()
    if enabled:
        ensure_drive_sync_worker()
    return jsonify({"enabled": enabled})


@app.post("/api/drive/disconnect")
@login_required
@roles_required("master")
def disconnect_google_drive():
    db = database()
    db.execute("DELETE FROM app_settings WHERE key='drive_credentials'")
    save_setting(db, "drive_sync_enabled", "0")
    save_setting(db, "drive_sync_pending", "0")
    save_setting(db, "drive_sync_needs_initial_choice", "0")
    audit_event(db, "disconnect", "google_drive")
    db.commit()
    return jsonify({"ok": True, "drive_backup_preserved": True})


@app.get("/api/drive/backup")
@login_required
@roles_required("master")
def download_drive_backup():
    from googleapiclient.http import MediaIoBaseDownload

    file_id = setting_value("drive_sync_file_id")
    if not file_id or not drive_credentials():
        return json_error("No connected Google Drive backup was found.", 404)
    output = io.BytesIO()
    downloader = MediaIoBaseDownload(output, build_drive_service().files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    output.seek(0)
    return send_file(output, mimetype="application/vnd.sqlite3", as_attachment=True,
                     download_name=f"bt-invo-drive-backup-{date.today().isoformat()}.sqlite3")


@app.post("/api/suppliers")
@login_required
@roles_required("master", "admin")
def create_supplier():
    data = payload()
    try:
        name = required_text(data, "name", "Supplier name")
        db = database()
        cursor = db.execute(
            "INSERT INTO suppliers(name, address, telephone, mobile) VALUES (?, ?, ?, ?)",
            (name, str(data.get("address", "")).strip(), str(data.get("telephone", "")).strip(), str(data.get("mobile", "")).strip()),
        )
        audit_event(db, "create", "supplier", cursor.lastrowid, {"name": name})
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.IntegrityError:
        return json_error("A supplier with this name already exists.")


@app.put("/api/suppliers/<int:supplier_id>")
@login_required
@roles_required("master", "admin")
def update_supplier(supplier_id):
    data = payload()
    try:
        name = required_text(data, "name", "Supplier name")
        db = database()
        cursor = db.execute(
            "UPDATE suppliers SET name=?, address=?, telephone=?, mobile=? WHERE id=?",
            (name, str(data.get("address", "")).strip(), str(data.get("telephone", "")).strip(), str(data.get("mobile", "")).strip(), supplier_id),
        )
        if not cursor.rowcount:
            return json_error("Supplier not found.", 404)
        audit_event(db, "update", "supplier", supplier_id, {"name": name})
        db.commit()
        return jsonify({"ok": True})
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.IntegrityError:
        return json_error("A supplier with this name already exists.")


@app.delete("/api/suppliers/<int:supplier_id>")
@login_required
@roles_required("master", "admin")
def delete_supplier(supplier_id):
    try:
        db = database()
        cursor = db.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))
        if not cursor.rowcount:
            return json_error("Supplier not found.", 404)
        audit_event(db, "delete", "supplier", supplier_id)
        db.commit()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return json_error("This supplier is linked to items or stock history and cannot be deleted.")


@app.post("/api/items")
@login_required
@roles_required("master", "admin")
def create_item():
    data = payload()
    try:
        name = required_text(data, "name", "Item name")
        uom = required_text(data, "uom", "Unit")
        category = required_text(data, "category", "Category")
        db = database()
        unit = db.execute("SELECT * FROM product_uom WHERE name = ?", (uom,)).fetchone()
        if unit is None:
            raise ValueError("Choose one of the available units.")
        if category not in category_names(db):
            raise ValueError("Choose one of the available categories.")
        pack_count_value = positive_number(data, "pack_count", "Packs per package") if data.get("pack_count") else float(unit["default_pack_count"])
        if not pack_count_value.is_integer():
            raise ValueError("Packs per package must be a whole number.")
        pack_count = int(pack_count_value)
        if pack_count < 1:
            raise ValueError("Packs per package must be at least one.")
        units_per_pack = positive_number(data, "units_per_pack", "Units per pack") if data.get("units_per_pack") else unit["default_pack_size"]
        pack_size = pack_count * units_per_pack
        default_price = positive_number(data, "default_price", "Price per unit", allow_zero=True) if data.get("default_price") is not None else 0
        reorder_level = positive_number(data, "reorder_level", "Low-stock threshold", allow_zero=True) if data.get("reorder_level") is not None else 5
        supplier_id = data.get("supplier_id") or None
        cursor = db.execute(
            """INSERT INTO items(name, supplier_item_code, uom, pack_size, category, supplier_id, default_price,
               pack_count, units_per_pack, base_unit, reorder_level, default_price_currency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, str(data.get("supplier_item_code", "")).strip(), uom, pack_size, category, supplier_id,
             default_price, pack_count, units_per_pack, unit["base_unit"] or unit["pack_unit"], reorder_level,
             app_setting("currency", "INR")),
        )
        audit_event(db, "create", "item", cursor.lastrowid, {"name": name, "category": category, "uom": uom})
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.IntegrityError:
        return json_error("Check the supplier and item details; the item could not be saved.")


@app.put("/api/items/<int:item_id>")
@login_required
@roles_required("master", "admin")
def update_item(item_id):
    data = payload()
    try:
        name = required_text(data, "name", "Item name")
        uom = required_text(data, "uom", "Unit")
        category = required_text(data, "category", "Category")
        db = database()
        unit = db.execute("SELECT * FROM product_uom WHERE name = ?", (uom,)).fetchone()
        if unit is None or category not in category_names(db):
            raise ValueError("Choose a valid unit and category.")
        existing = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if existing is None:
            return json_error("Item not found.", 404)
        has_history = db.execute(
            """SELECT EXISTS(SELECT 1 FROM stock_movements WHERE item_id=?)
               OR EXISTS(SELECT 1 FROM stock_adjustments WHERE item_id=?)
               OR EXISTS(SELECT 1 FROM purchase_orders WHERE item_id=?)""",
            (item_id, item_id, item_id),
        ).fetchone()[0]
        next_base_unit = unit["base_unit"] or unit["pack_unit"]
        if has_history and existing["base_unit"] != next_base_unit:
            raise ValueError("An item with stock or order history cannot change its base unit. Create a new item instead.")
        pack_count_value = positive_number(data, "pack_count", "Packs per package") if data.get("pack_count") else float(existing["pack_count"] if existing["uom"] == uom else unit["default_pack_count"])
        if not pack_count_value.is_integer():
            raise ValueError("Packs per package must be a whole number.")
        pack_count = int(pack_count_value)
        if pack_count < 1:
            raise ValueError("Packs per package must be at least one.")
        units_per_pack = positive_number(data, "units_per_pack", "Units per pack") if data.get("units_per_pack") else float(existing["units_per_pack"] if existing["uom"] == uom else unit["default_pack_size"])
        pack_size = pack_count * units_per_pack
        if has_history and (existing["uom"] != uom or existing["pack_size"] != pack_size):
            raise ValueError("Package conversion cannot change after stock or order history exists. Create a new item for the new package.")
        default_price = positive_number(data, "default_price", "Price per unit", allow_zero=True) if data.get("default_price") is not None else existing["default_price"]
        reorder_level = positive_number(data, "reorder_level", "Low-stock threshold", allow_zero=True) if data.get("reorder_level") is not None else existing["reorder_level"]
        cursor = db.execute(
            """UPDATE items SET name=?, supplier_item_code=?, uom=?, pack_size=?, category=?, supplier_id=?, default_price=?,
               pack_count=?, units_per_pack=?, base_unit=?, reorder_level=?, default_price_currency=? WHERE id=?""",
            (name, str(data.get("supplier_item_code", "")).strip(), uom, pack_size, category,
             data.get("supplier_id") or None, default_price, pack_count, units_per_pack,
             unit["base_unit"] or unit["pack_unit"], reorder_level, app_setting("currency", "INR"), item_id),
        )
        if not cursor.rowcount:
            return json_error("Item not found.", 404)
        audit_event(db, "update", "item", item_id, {"name": name, "category": category, "uom": uom})
        db.commit()
        return jsonify({"ok": True})
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.IntegrityError:
        return json_error("Check the supplier and item details; the item could not be saved.")


@app.delete("/api/items/<int:item_id>")
@login_required
@roles_required("master", "admin")
def archive_item(item_id):
    db = database()
    cursor = db.execute("UPDATE items SET active = 0 WHERE id = ? AND active = 1", (item_id,))
    if not cursor.rowcount:
        return json_error("Active item not found.", 404)
    audit_event(db, "archive", "item", item_id)
    db.commit()
    return jsonify({"ok": True})


def save_movement(movement_type):
    data = payload()
    db = database()
    try:
        db.execute("BEGIN IMMEDIATE")
        item = item_for_request(int(data.get("item_id", 0)))
        quantity = positive_number(data, "quantity", "Quantity")
        movement_date = valid_date(data.get("movement_date"), "Movement date")
        supplier_id = data.get("supplier_id") or item["supplier_id"]
        purchase_order_id = data.get("purchase_order_id") or None
        invoice_number = str(data.get("invoice_number", "")).strip() if movement_type == "stock_in" else ""
        if len(invoice_number) > 100:
            raise ValueError("Invoice number must be 100 characters or fewer.")
        order = None
        if purchase_order_id and movement_type == "stock_in":
            order = database().execute(
                "SELECT * FROM purchase_orders WHERE id = ? AND item_id = ?",
                (purchase_order_id, item["id"]),
            ).fetchone()
            if order is None:
                raise ValueError("Choose a purchase order for this item.")
            if order["status"] not in ("open", "partial"):
                raise ValueError("Only open or partially received orders can receive stock.")
            remaining = float(order["quantity"]) - float(order["received_quantity"])
            if quantity > remaining:
                raise ValueError(f"Only {remaining:g} ordered packages remain to receive.")
            supplier_id = order["supplier_id"] or supplier_id
        if movement_type == "stock_in":
            price = positive_number(data, "unit_price", "Price per unit", allow_zero=True)
        else:
            price = 0
            on_hand = db.execute(
                """SELECT COALESCE(SUM(CASE WHEN movement_type='stock_in' THEN base_quantity ELSE -base_quantity END), 0)
                   + COALESCE((SELECT SUM(base_quantity_delta) FROM stock_adjustments WHERE item_id=?), 0)
                   FROM stock_movements WHERE item_id = ?""",
                (item["id"], item["id"]),
            ).fetchone()[0]
            requested_base_quantity = quantity * item["pack_size"]
            if requested_base_quantity > on_hand:
                available_packages = on_hand / item["pack_size"]
                raise ValueError(f"Only {available_packages:g} {item['uom']} packages available in stock.")
        total = quantity * price
        pack_size = order["pack_size"] if order else item["pack_size"]
        cursor = db.execute(
            """INSERT INTO stock_movements(item_id, movement_type, quantity, unit_price, total_amount,
               base_quantity, currency, supplier_id, purchase_order_id, invoice_number, movement_date, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item["id"], movement_type, quantity, price, total, quantity * pack_size,
             app_setting("currency", "INR"), supplier_id, purchase_order_id, invoice_number, movement_date, current_user_id()),
        )
        if order:
            received_quantity = float(order["received_quantity"]) + quantity
            status = "received" if received_quantity >= float(order["quantity"]) else "partial"
            db.execute(
                "UPDATE purchase_orders SET received_quantity = ?, status = ? WHERE id = ?",
                (received_quantity, status, purchase_order_id),
            )
        audit_event(db, movement_type, "stock_movement", cursor.lastrowid,
                    {"item_id": item["id"], "quantity": quantity, "base_quantity": quantity * pack_size,
                     "purchase_order_id": purchase_order_id, "invoice_number": invoice_number})
        db.commit()
        return jsonify({"id": cursor.lastrowid, "total_amount": total}), 201
    except (ValueError, TypeError) as error:
        return json_error(str(error) or "Choose a valid item.")
    except sqlite3.IntegrityError:
        return json_error("Check the item and supplier details.")


@app.post("/api/stock")
@login_required
def create_stock_receipt():
    return save_movement("stock_in")


@app.post("/api/issues")
@login_required
def create_stock_issue():
    return save_movement("stock_out")


@app.post("/api/purchase-orders")
@login_required
def create_purchase_order():
    data = payload()
    try:
        item = item_for_request(int(data.get("item_id", 0)))
        quantity = positive_number(data, "quantity", "Quantity")
        order_date = valid_date(data.get("order_date"), "Order date")
        supplier_id = data.get("supplier_id") or item["supplier_id"]
        db = database()
        cursor = db.execute(
                """INSERT INTO purchase_orders(item_id, quantity, pack_size, pack_count, units_per_pack, base_unit,
                    order_date, user_id, supplier_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                (item["id"], quantity, item["pack_size"], item["pack_count"], item["units_per_pack"], item["base_unit"],
                 order_date, current_user_id(), supplier_id),
        )
        audit_event(db, "create", "purchase_order", cursor.lastrowid, {"item_id": item["id"], "quantity": quantity})
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except (ValueError, TypeError) as error:
        return json_error(str(error) or "Choose a valid item.")


@app.patch("/api/purchase-orders/<int:order_id>")
@login_required
@roles_required("master", "admin")
def update_purchase_order(order_id):
    data = payload()
    status = str(data.get("status", ""))
    if status != "cancelled":
        return json_error("Purchase orders can only be cancelled from this action.")
    db = database()
    order = db.execute("SELECT status FROM purchase_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return json_error("Purchase order not found.", 404)
    if order["status"] not in ("open", "partial"):
        return json_error("Only open or partially received orders can be cancelled.")
    db.execute("UPDATE purchase_orders SET status = 'cancelled' WHERE id = ?", (order_id,))
    audit_event(db, "cancel", "purchase_order", order_id)
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/users")
@login_required
@roles_required("master", "admin")
def create_user():
    data = payload()
    try:
        username = required_text(data, "username", "Username")
        password = required_text(data, "password", "Password", limit=256)
        if len(password) < 12:
            raise ValueError("Password must be at least 12 characters.")
        role = required_text(data, "role", "Role")
        if role not in ROLES:
            raise ValueError("Choose a valid role.")
        if role == "master" and session.get("role") != "master":
            raise ValueError("Only the master user can create another master account.")
        db = database()
        cursor = db.execute(
            "INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), role),
        )
        audit_event(db, "create", "user", cursor.lastrowid, {"username": username, "role": role})
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.IntegrityError:
        return json_error("That username is already in use.")


@app.patch("/api/users/<int:user_id>")
@login_required
@roles_required("master", "admin")
def update_user(user_id):
    data = payload()
    db = database()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return json_error("User not found.", 404)
    if session.get("role") != "master" and user["role"] == "master":
        return json_error("Only the master user can change a master account.", 403)
    active = bool(data.get("active", user["active"]))
    if user_id == current_user_id() and not active:
        return json_error("You cannot deactivate your own account.")
    role = str(data.get("role", user["role"]))
    if role not in ROLES:
        return json_error("Choose a valid role.")
    if role == "master" and session.get("role") != "master":
        return json_error("Only the master user can assign the master role.", 403)
    if user["role"] == "master" and (not active or role != "master"):
        other_master = db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'master' AND active = 1 AND id != ?", (user_id,)
        ).fetchone()[0]
        if other_master == 0:
            return json_error("Keep at least one active master account.")
    password = str(data.get("password", ""))
    if password:
        if len(password) < 12:
            return json_error("Password must be at least 12 characters.")
        db.execute(
            "UPDATE users SET password_hash = ?, password_change_required = 0 WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
    db.execute("UPDATE users SET active = ?, role = ? WHERE id = ?", (int(active), role, user_id))
    audit_event(db, "update", "user", user_id, {"username": user["username"], "role": role, "active": active, "password_reset": bool(password)})
    db.commit()
    return jsonify({"ok": True})


def report_data(start, end, category, movement_type="ALL"):
    db = database()
    valid_categories = category_names(db)
    filters = ["m.movement_date BETWEEN ? AND ?"]
    params = [start, end]
    if category in valid_categories:
        filters.append("i.category = ?")
        params.append(category)
    if movement_type in ("stock_in", "stock_out"):
        filters.append("m.movement_type = ?")
        params.append(movement_type)
    where = " AND ".join(filters)
    movements = db.execute(
        f"""SELECT m.*, i.name AS item_name, i.supplier_item_code, i.uom, i.base_unit, i.category,
                   s.name AS supplier_name, u.username,
                   CASE WHEN m.movement_type = 'stock_out' THEN
                       m.quantity * COALESCE((
                           SELECT purchase.unit_price FROM stock_movements purchase
                           WHERE purchase.item_id = m.item_id
                             AND purchase.movement_type = 'stock_in'
                             AND (purchase.movement_date < m.movement_date
                                  OR (purchase.movement_date = m.movement_date AND purchase.id < m.id))
                           ORDER BY purchase.movement_date DESC, purchase.id DESC LIMIT 1
                       ), 0)
                   ELSE m.total_amount END AS report_amount
            FROM stock_movements m JOIN items i ON i.id=m.item_id
            LEFT JOIN suppliers s ON s.id=m.supplier_id JOIN users u ON u.id=m.user_id
            WHERE {where} ORDER BY m.movement_date DESC, m.id DESC""",
        params,
    ).fetchall()
    adjustments = db.execute(
        """SELECT a.*, i.name AS item_name, i.supplier_item_code, i.uom, i.base_unit, i.category, u.username
           FROM stock_adjustments a JOIN items i ON i.id=a.item_id JOIN users u ON u.id=a.user_id
           WHERE a.adjustment_date BETWEEN ? AND ?""" +
        (" AND i.category = ?" if category in valid_categories else "") +
        " ORDER BY a.adjustment_date DESC, a.id DESC",
        ([start, end, category] if category in valid_categories else [start, end]),
    ).fetchall()
    orders = db.execute(
        """SELECT p.*, i.name AS item_name, i.supplier_item_code, i.uom, i.category, u.username, s.name AS supplier_name,
           (p.quantity-p.received_quantity) AS remaining_quantity
           FROM purchase_orders p JOIN items i ON i.id=p.item_id JOIN users u ON u.id=p.user_id
           LEFT JOIN suppliers s ON s.id=p.supplier_id WHERE p.order_date BETWEEN ? AND ?""" +
        (" AND i.category = ?" if category in valid_categories else "") + " ORDER BY p.order_date DESC, p.id DESC",
        ([start, end, category] if category in valid_categories else [start, end]),
    ).fetchall()
    item_filter = " WHERE category = ?" if category in valid_categories else ""
    balance_items = db.execute(
        "SELECT id, name, category, uom, base_unit, pack_size FROM items" + item_filter + " ORDER BY name COLLATE NOCASE",
        ([category] if category in valid_categories else []),
    ).fetchall()
    movement_balances = db.execute(
        """SELECT item_id,
             SUM(CASE WHEN movement_date < ? THEN CASE WHEN movement_type='stock_in' THEN base_quantity ELSE -base_quantity END ELSE 0 END) AS opening,
             SUM(CASE WHEN movement_date BETWEEN ? AND ? AND movement_type='stock_in' THEN base_quantity ELSE 0 END) AS received,
             SUM(CASE WHEN movement_date BETWEEN ? AND ? AND movement_type='stock_out' THEN base_quantity ELSE 0 END) AS issued
           FROM stock_movements WHERE movement_date <= ? GROUP BY item_id""",
        (start, start, end, start, end, end),
    ).fetchall()
    adjustment_balances = db.execute(
        """SELECT item_id,
             SUM(CASE WHEN adjustment_date < ? THEN base_quantity_delta ELSE 0 END) AS opening,
             SUM(CASE WHEN adjustment_date BETWEEN ? AND ? THEN base_quantity_delta ELSE 0 END) AS period_delta
           FROM stock_adjustments WHERE adjustment_date <= ? GROUP BY item_id""",
        (start, start, end, end),
    ).fetchall()
    movement_by_item = {row["item_id"]: row for row in movement_balances}
    adjustment_by_item = {row["item_id"]: row for row in adjustment_balances}
    balances = []
    for item in balance_items:
        movement_balance = movement_by_item.get(item["id"])
        adjustment_balance = adjustment_by_item.get(item["id"])
        opening = (movement_balance["opening"] if movement_balance else 0) + (adjustment_balance["opening"] if adjustment_balance else 0)
        received = movement_balance["received"] if movement_balance else 0
        issued = movement_balance["issued"] if movement_balance else 0
        adjustment = adjustment_balance["period_delta"] if adjustment_balance else 0
        balances.append({**serialize(item), "opening_base": opening, "received_base": received,
                         "issued_base": issued, "adjustment_base": adjustment,
                         "closing_base": opening + received - issued + adjustment,
                         "closing_packs": (opening + received - issued + adjustment) / item["pack_size"]})
    receipts = [row for row in movements if row["movement_type"] == "stock_in"]
    issues = [row for row in movements if row["movement_type"] == "stock_out"]
    return {
            "from": start,
            "to": end,
            "category": category if category in valid_categories else "ALL",
            "movement_type": movement_type,
            "movements": [serialize(row) for row in movements],
            "adjustments": [serialize(row) for row in adjustments],
            "orders": [serialize(row) for row in orders],
            "balances": balances,
            "totals": {
                "received_transactions": len(receipts),
                "received_amount": sum(row["total_amount"] for row in receipts),
                "issued_transactions": len(issues),
                "purchase_orders": len(orders),
            },
        }


def report_dates():
    start = request.args.get("from", "")
    end = request.args.get("to", "")
    category = request.args.get("category", "")
    movement_type = request.args.get("movement_type", "ALL")
    start = valid_date(start, "Start date")
    end = valid_date(end, "End date")
    if start > end:
        raise ValueError("Start date must be on or before end date.")
    if movement_type not in ("ALL", "stock_in", "stock_out"):
        raise ValueError("Choose stock in, stock out, or all movements.")
    return start, end, category, movement_type


@app.get("/api/reports")
@login_required
def reports():
    try:
        return jsonify(report_data(*report_dates()))
    except ValueError as error:
        return json_error(str(error))


@app.get("/api/reports.pdf")
@login_required
def report_pdf():
    try:
        report = report_data(*report_dates())
    except ValueError as error:
        return json_error(str(error))

    db = database()
    currency = app_setting("currency", "INR")
    buffer = io.BytesIO()
    page_size = landscape(A4)
    document = SimpleDocTemplate(
        buffer, pagesize=page_size, rightMargin=12 * mm, leftMargin=12 * mm,
        topMargin=14 * mm, bottomMargin=15 * mm, title="BT INVO inventory report",
        author="BT INVO",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=17, leading=20, textColor=colors.black, alignment=TA_LEFT, spaceAfter=3))
    styles.add(ParagraphStyle(name="BusinessAddress", parent=styles["Normal"], fontSize=8, leading=10, textColor=colors.black, spaceAfter=1))
    styles.add(ParagraphStyle(name="ReportSub", parent=styles["Normal"], fontSize=8, leading=11, textColor=colors.black, spaceAfter=7))
    styles.add(ParagraphStyle(name="ReportSection", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=colors.black, spaceBefore=7, spaceAfter=4, keepWithNext=True))
    styles.add(ParagraphStyle(name="ReportCell", parent=styles["Normal"], fontSize=6.5, leading=8, textColor=colors.black, wordWrap="CJK"))
    styles.add(ParagraphStyle(name="ReportHeader", parent=styles["ReportCell"], fontName="Helvetica-Bold", textColor=colors.black))

    def cell(value, header=False):
        return Paragraph(xml_escape(str(value if value is not None else "")), styles["ReportHeader" if header else "ReportCell"])

    def report_table(headers, rows, widths):
        values = [[cell(value, True) for value in headers]]
        values.extend([[cell(value) for value in row] for row in rows])
        table = LongTable(values, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("BACKGROUND", (0, 1), (-1, -1), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return table

    totals = report["totals"]
    money_total = f"{currency} {totals['received_amount']:,.2f}"
    movement_label = {
        "ALL": "Stock in and out",
        "stock_in": "Stock in only",
        "stock_out": "Stock out only",
    }[report["movement_type"]]
    show_all_sections = report["movement_type"] == "ALL"
    shop_name = xml_escape(app_setting("business_shop_name", "BT Chocolate Shop"))
    shop_address = app_setting("business_address")
    shop_telephone = app_setting("business_telephone")
    shop_mobile = app_setting("business_mobile")
    contact_lines = [xml_escape(shop_address).replace("\n", "<br/>") if shop_address else ""]
    contact_lines.extend(
        xml_escape(label + value)
        for label, value in (("Telephone: ", shop_telephone), ("Mobile: ", shop_mobile))
        if value
    )
    business_details = [Paragraph(shop_name, styles["ReportTitle"])]
    business_details.extend(Paragraph(line, styles["BusinessAddress"]) for line in contact_lines if line)
    logo = None
    encoded_logo = app_setting("business_logo_data")
    if encoded_logo:
        logo_data = base64.b64decode(encoded_logo, validate=True)
        logo = ImageReader(io.BytesIO(logo_data))
        logo_width, logo_height = logo.getSize()
        max_logo_width, max_logo_height = 30 * mm, 22 * mm
        scale = min(max_logo_width / logo_width, max_logo_height / logo_height)
        logo = ReportImage(io.BytesIO(logo_data), width=logo_width * scale, height=logo_height * scale)
    masthead_width = page_size[0] - 24 * mm
    if logo:
        masthead = Table([[logo, business_details]],
                         colWidths=[34 * mm, masthead_width - 34 * mm], hAlign="LEFT")
    else:
        masthead = Table([[business_details]], colWidths=[masthead_width], hAlign="LEFT")
    masthead.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story = [
        masthead,
        Paragraph(f"Inventory report | {report['from']} to {report['to']} | Category: {xml_escape(report['category'])} | Movement: {movement_label}", styles["ReportSub"]),
        Paragraph(
            "Showing stock movements, corrections, purchase orders, and balances for the selected dates and category."
            if show_all_sections else f"Showing only {movement_label.lower()} for the selected dates and category.",
            styles["ReportSub"],
        ),
    ]
    if show_all_sections:
        story.append(report_table(
            ["Receipts", "Purchase spend", "Issue transactions", "Purchase orders"],
            [[totals["received_transactions"], money_total, totals["issued_transactions"], totals["purchase_orders"]]],
            [(page_size[0] - 24 * mm) / 4] * 4,
        ))
        story.append(Paragraph("Opening and closing stock by item", styles["ReportSection"]))
        balance_rows = [[row["name"], row["category"], f"{row['opening_base']:,.3f} {row['base_unit']}",
                         f"{row['received_base']:,.3f} {row['base_unit']}", f"{row['issued_base']:,.3f} {row['base_unit']}",
                         f"{row['adjustment_base']:,.3f} {row['base_unit']}", f"{row['closing_base']:,.3f} {row['base_unit']}",
                         f"{row['closing_packs']:,.3f} {row['uom']}"] for row in report["balances"]]
        story.append(report_table(
            ["Item", "Category", "Opening", "Received", "Issued", "Corrections", "Closing", "Packages"], balance_rows,
            [43 * mm, 25 * mm, 29 * mm, 29 * mm, 26 * mm, 29 * mm, 29 * mm, 27 * mm],
        ))
    elif report["movement_type"] == "stock_in":
        story.append(report_table(
            ["Receipts", "Purchase spend"],
            [[totals["received_transactions"], money_total]],
            [(page_size[0] - 24 * mm) / 2] * 2,
        ))
    else:
        estimated_issue_value = sum(row["report_amount"] for row in report["movements"])
        story.append(report_table(
            ["Issue transactions", "Estimated issue value"],
            [[totals["issued_transactions"], f"{currency} {estimated_issue_value:,.2f}"]],
            [(page_size[0] - 24 * mm) / 2] * 2,
        ))
    movement_rows = [[row["movement_date"], row["item_name"], row["category"],
                      "Stock in" if row["movement_type"] == "stock_in" else "Stock out",
                      f"{row['quantity']:,.3f} {row['uom']}", f"{row['base_quantity']:,.3f}",
                      f"{currency} {row['unit_price']:,.2f}",
                      f"{currency} {row['report_amount']:,.2f}" + (" estimated" if row["movement_type"] == "stock_out" else ""),
                      row["supplier_name"] or "-", row["username"]] for row in report["movements"]]
    movement_section = {
        "ALL": "Stock movement details",
        "stock_in": "Stock receipt details",
        "stock_out": "Stock issue details",
    }[report["movement_type"]]
    story.extend([Paragraph(movement_section, styles["ReportSection"]), report_table(
        ["Date", "Item", "Category", "Type", "Packages", "Base qty", "Price/package", "Amount", "Supplier", "User"], movement_rows,
        [21 * mm, 35 * mm, 23 * mm, 20 * mm, 25 * mm, 22 * mm, 28 * mm, 27 * mm, 34 * mm, 22 * mm],
    )])
    if show_all_sections:
        adjustment_rows = [[row["adjustment_date"], row["item_name"], f"{row['quantity_delta']:,.3f} {row['uom']}",
                            f"{row['base_quantity_delta']:,.3f} {row['base_unit']}", row["reason"], row["note"], row["username"]]
                           for row in report["adjustments"]]
        story.extend([Paragraph("Stock corrections", styles["ReportSection"]), report_table(
            ["Date", "Item", "Package change", "Base change", "Reason", "Note", "User"], adjustment_rows,
            [22 * mm, 40 * mm, 30 * mm, 32 * mm, 35 * mm, 58 * mm, 28 * mm],
        )])
        order_rows = [[row["order_date"], row["item_name"], row["supplier_name"] or "-", row["status"],
                       f"{row['quantity']:,.3f} {row['uom']}", f"{row['received_quantity']:,.3f} {row['uom']}",
                       f"{row['remaining_quantity']:,.3f} {row['uom']}", row["username"]] for row in report["orders"]]
        story.extend([Paragraph("Purchase orders", styles["ReportSection"]), report_table(
            ["Date", "Item", "Supplier", "Status", "Ordered", "Received", "Remaining", "User"], order_rows,
            [24 * mm, 42 * mm, 38 * mm, 23 * mm, 30 * mm, 30 * mm, 30 * mm, 27 * mm],
        )])

    def footer(canvas, _document):
        canvas.saveState()
        canvas.setStrokeColor(colors.black)
        canvas.line(12 * mm, 11 * mm, page_size[0] - 12 * mm, 11 * mm)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawCentredString(page_size[0] / 2, 7 * mm, f"Powered by JC007 | Page {_document.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True,
                     download_name=f"bt-invo-report-{report['from']}-to-{report['to']}.pdf")


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return json_error("Not found.", 404)
    return "Not found", 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)