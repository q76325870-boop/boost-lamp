import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time

from .auth import parse_session
from .errors import Problem


def video_key(author, post, video):
    return hashlib.sha256(json.dumps([author, post, video], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def atomic_write(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "state.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=3000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE);
          CREATE TABLE IF NOT EXISTS videos (key TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS progress (key TEXT PRIMARY KEY REFERENCES videos(key),
            position REAL NOT NULL, duration REAL, completed INTEGER NOT NULL, updated REAL NOT NULL);
        """)
        os.chmod(self.directory / "state.sqlite3", 0o600)
        keyfile = self.directory / "admin.json"
        if not keyfile.exists():
            atomic_write(keyfile, {"key": secrets.token_urlsafe(32)})
        self.admin_key = json.loads(keyfile.read_text(encoding="utf-8"))["key"]
        legacy = self.directory / "session.json"
        if legacy.exists():
            if self.meta("session") is None:
                self.write_session(json.loads(legacy.read_text(encoding="utf-8")))
            legacy.unlink()

    def read_session(self):
        try:
            value = self.meta("session")
            if value is None:
                raise Problem("AUTH_REQUIRED", 401)
            return parse_session(json.loads(value))
        except (sqlite3.Error, ValueError):
            raise Problem("SESSION_STORAGE_ERROR", 503) from None

    def write_session(self, value):
        value = json.dumps(parse_session(value))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('session', ?)", (value,))

    def meta(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def install_session(self, account_id, session):
        value = json.dumps(parse_session(session))
        changed = self.meta("account") not in (None, account_id)
        with self.db:
            if changed:
                for table in ("devices", "progress", "videos"):
                    self.db.execute("DELETE FROM " + table)
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('account', ?)", (account_id,))
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('session', ?)", (value,))
        return changed

    def new_device(self):
        token = secrets.token_urlsafe(32)
        device = secrets.token_hex(8)
        with self.db:
            self.db.execute("INSERT INTO devices VALUES (?,?)", (device, hashlib.sha256(token.encode()).hexdigest()))
        return device, token

    def device(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.execute("SELECT id FROM devices WHERE token_hash=?", (digest,)).fetchone()
        if not row:
            raise Problem("DEVICE_REQUIRED", 401)
        return row[0]

    def device_exists(self, device):
        return self.db.execute("SELECT 1 FROM devices WHERE id=?", (device,)).fetchone() is not None

    def remember_video(self, key, data):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO videos VALUES (?,?)", (key, json.dumps(data, ensure_ascii=False)))

    def save_progress(self, key, position, duration, completed):
        if not self.db.execute("SELECT 1 FROM videos WHERE key=?", (key,)).fetchone():
            raise Problem("UNKNOWN_VIDEO", 404)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO progress VALUES (?,?,?,?,?)", (key, position, duration, completed, time.time()))

    def progress(self, key):
        row = self.db.execute("SELECT position, duration, completed FROM progress WHERE key=?", (key,)).fetchone()
        return {"position_seconds": row[0], "duration_seconds": row[1], "completed": bool(row[2])} if row else {"position_seconds": 0, "duration_seconds": None, "completed": False}

    def continue_watching(self):
        rows = self.db.execute("SELECT v.data,p.position,p.duration FROM videos v JOIN progress p ON v.key=p.key WHERE p.completed=0 AND p.position>0 ORDER BY p.updated DESC LIMIT 50").fetchall()
        return [{**json.loads(row[0]), "position_seconds": row[1], "duration_seconds": row[2]} for row in rows]

    def close(self):
        self.db.close()
