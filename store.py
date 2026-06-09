"""Persistent metadata + blob store for send.

Files live on disk under DATA_DIR/blobs/<token>; metadata lives in a small
SQLite database. A drop is gone the moment it expires OR its remaining reads
hit zero, whichever comes first. A background reaper sweeps the rest.
"""

import os
import time
import secrets
import sqlite3
import threading

from werkzeug.security import generate_password_hash, check_password_hash

DATA_DIR = os.environ.get("SEND_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
BLOB_DIR = os.path.join(DATA_DIR, "blobs")
DB_PATH = os.path.join(DATA_DIR, "send.db")

os.makedirs(BLOB_DIR, exist_ok=True)

_init_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    with _init_lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drops (
                    token         TEXT PRIMARY KEY,
                    filename      TEXT NOT NULL,
                    mime          TEXT NOT NULL,
                    size          INTEGER NOT NULL,
                    created       INTEGER NOT NULL,
                    expires_at    INTEGER NOT NULL,
                    reads_left    INTEGER NOT NULL,
                    password_hash TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def _blob_path(token):
    return os.path.join(BLOB_DIR, token)


def _remove_blob(token):
    try:
        os.remove(_blob_path(token))
    except OSError:
        # Already gone, or briefly locked (Windows) — the reaper's orphan
        # sweep will retry on its next pass.
        pass


def remove_blob(token):
    """Public: delete a blob whose row is already gone (post-stream cleanup)."""
    _remove_blob(token)


def _delete_row(conn, token):
    conn.execute("DELETE FROM drops WHERE token=?", (token,))
    conn.commit()
    _remove_blob(token)


def total_bytes():
    conn = _connect()
    try:
        row = conn.execute("SELECT COALESCE(SUM(size), 0) AS total FROM drops").fetchone()
        return int(row["total"])
    finally:
        conn.close()


def create(file_storage, filename, mime, expiry_seconds, max_reads, password=None):
    """Persist an uploaded file and return its public metadata."""
    conn = _connect()
    try:
        token = secrets.token_urlsafe(9)
        # Vanishingly unlikely, but never reuse a live token.
        while conn.execute("SELECT 1 FROM drops WHERE token=?", (token,)).fetchone():
            token = secrets.token_urlsafe(9)

        path = _blob_path(token)
        try:
            file_storage.save(path)
            size = os.path.getsize(path)
        except OSError:
            # Disk full or write error mid-save — drop the partial blob and
            # let the caller surface a clean error.
            _remove_blob(token)
            raise

        now = int(time.time())
        expires_at = now + int(expiry_seconds)
        pw_hash = generate_password_hash(password) if password else None

        conn.execute(
            "INSERT INTO drops (token, filename, mime, size, created, expires_at, reads_left, password_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token, filename, mime, size, now, expires_at, int(max_reads), pw_hash),
        )
        conn.commit()

        return {
            "token": token,
            "filename": filename,
            "size": size,
            "created": now,
            "expires_at": expires_at,
            "reads_left": int(max_reads),
            "protected": pw_hash is not None,
        }
    finally:
        conn.close()


def meta(token):
    """Public metadata for a live drop, or None if it is gone. Read-only."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM drops WHERE token=?", (token,)).fetchone()
        if row is None:
            return None
        now = int(time.time())
        if row["expires_at"] <= now or row["reads_left"] <= 0:
            _delete_row(conn, token)
            return None
        return {
            "token": token,
            "filename": row["filename"],
            "size": row["size"],
            "created": row["created"],
            "expires_at": row["expires_at"],
            "reads_left": row["reads_left"],
            "protected": row["password_hash"] is not None,
        }
    finally:
        conn.close()


def consume(token, password=None):
    """Claim one read of a drop.

    Returns a dict with ``status`` of 'ok', 'not_found', or 'unauthorized'.
    On 'ok', either ``data`` (bytes, when the drop just burned) or ``path``
    (when reads remain) is populated.
    """
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM drops WHERE token=?", (token,)).fetchone()
        if row is None:
            return {"status": "not_found"}

        now = int(time.time())
        if row["expires_at"] <= now or row["reads_left"] <= 0:
            _delete_row(conn, token)
            return {"status": "not_found"}

        if row["password_hash"]:
            if not password or not check_password_hash(row["password_hash"], password):
                return {"status": "unauthorized"}

        # Atomic claim: only one concurrent request wins the last read.
        cur = conn.execute(
            "UPDATE drops SET reads_left = reads_left - 1 WHERE token=? AND reads_left > 0",
            (token,),
        )
        conn.commit()
        if cur.rowcount == 0:
            _delete_row(conn, token)
            return {"status": "not_found"}

        reads_left = row["reads_left"] - 1
        burn = reads_left <= 0
        if burn:
            # Remove the row now so the drop can never be claimed again, but
            # leave the blob on disk so it can be *streamed* to the client
            # (never read whole files into memory — worlds can be many GB).
            # The caller deletes the blob once the response finishes; if that
            # is missed (crash, dropped connection), the reaper's orphan sweep
            # cleans it up.
            conn.execute("DELETE FROM drops WHERE token=?", (token,))
            conn.commit()
        return {
            "status": "ok",
            "filename": row["filename"],
            "mime": row["mime"],
            "reads_left": reads_left,
            "burn": burn,
            "path": _blob_path(token),
        }
    finally:
        conn.close()


def burn(token):
    """Manually destroy a drop. Idempotent; never reveals whether it existed."""
    conn = _connect()
    try:
        _delete_row(conn, token)
    finally:
        conn.close()


def sweep():
    """Delete expired or exhausted drops and any orphaned blobs."""
    conn = _connect()
    try:
        now = int(time.time())
        dead = conn.execute(
            "SELECT token FROM drops WHERE expires_at <= ? OR reads_left <= 0", (now,)
        ).fetchall()
        for row in dead:
            _delete_row(conn, row["token"])

        live = {row["token"] for row in conn.execute("SELECT token FROM drops").fetchall()}
        for name in os.listdir(BLOB_DIR):
            if name not in live:
                _remove_blob(name)
    finally:
        conn.close()


def start_reaper(interval=60):
    def loop():
        while True:
            time.sleep(interval)
            try:
                sweep()
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True).start()
