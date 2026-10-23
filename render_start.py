import os
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash


database_path = Path(os.environ["BT_INVO_DATABASE"])
source_path = Path(__file__).resolve().parent / "bt_invo.sqlite3"
first_start = not database_path.exists()

database_path.parent.mkdir(parents=True, exist_ok=True)
if first_start and source_path.is_file():
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(database_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

if first_start and database_path.exists():
    username = os.environ["BT_INVO_MASTER_USERNAME"]
    password = os.environ["BT_INVO_MASTER_PASSWORD"]
    db = sqlite3.connect(database_path)
    try:
        user_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if user_table:
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
            if "password_change_required" not in columns:
                db.execute(
                    "ALTER TABLE users ADD COLUMN password_change_required "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(
                "UPDATE users SET active=0, password_change_required=1 WHERE username != ?",
                (username,),
            )
            existing = db.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
            password_hash = generate_password_hash(password)
            if existing:
                db.execute(
                    """UPDATE users SET password_hash=?, role='master', active=1,
                       password_change_required=0 WHERE id=?""",
                    (password_hash, existing[0]),
                )
            else:
                db.execute(
                    """INSERT INTO users(username,password_hash,role,active,password_change_required)
                       VALUES (?,?,'master',1,0)""",
                    (username, password_hash),
                )
            db.execute("DELETE FROM app_settings WHERE key = 'drive_credentials'")
            db.execute(
                """UPDATE app_settings SET value='0'
                   WHERE key IN ('drive_sync_pending','drive_sync_needs_initial_choice')"""
            )
            db.execute(
                """UPDATE app_settings SET value=''
                   WHERE key IN ('drive_sync_file_id','drive_sync_last_at')"""
            )
            db.commit()
    finally:
        db.close()

os.execvp(
    "gunicorn",
    [
        "gunicorn",
        "--bind",
        f"0.0.0.0:{os.environ.get('PORT', '10000')}",
        "--workers",
        "1",
        "--threads",
        "4",
        "--timeout",
        "120",
        "app:app",
    ],
)
