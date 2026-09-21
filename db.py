"""SQLite storage: links, users, settings and force-subscribe channels."""
import os
import secrets
import sqlite3
import threading


class Database:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS links (
                    code       TEXT PRIMARY KEY,
                    editable   INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS items (
                    code     TEXT    NOT NULL,
                    position INTEGER NOT NULL,
                    kind     TEXT    NOT NULL,
                    data     TEXT    NOT NULL,
                    caption  TEXT,
                    PRIMARY KEY (code, position)
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    banned  INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forcesub (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER UNIQUE NOT NULL,
                    title   TEXT NOT NULL,
                    link    TEXT NOT NULL,
                    mode    TEXT NOT NULL DEFAULT 'normal'
                );
                CREATE TABLE IF NOT EXISTS join_requests (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );
                """
            )
            # upgrade databases created before request mode existed
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(forcesub)")]
            if "mode" not in cols:
                self._conn.execute(
                    "ALTER TABLE forcesub ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal'"
                )
            self._conn.commit()

    # ------------------------------------------------------------------ links
    def create_link(self, items, editable=False, created_by=0):
        """Store [(kind, data, caption), ...] and return a new link code."""
        with self._lock:
            while True:
                code = "c" + secrets.token_urlsafe(8)  # 12 chars, t.me safe
                row = self._conn.execute(
                    "SELECT 1 FROM links WHERE code = ?", (code,)
                ).fetchone()
                if row is None:
                    break
            self._conn.execute(
                "INSERT INTO links (code, editable, created_by) VALUES (?, ?, ?)",
                (code, 1 if editable else 0, created_by),
            )
            self._insert_items(code, items)
            self._conn.commit()
        return code

    def replace_link(self, code, items):
        """Replace the content of an editable link. Returns False if not allowed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT editable FROM links WHERE code = ?", (code,)
            ).fetchone()
            if not row or not row[0]:
                return False
            self._conn.execute("DELETE FROM items WHERE code = ?", (code,))
            self._insert_items(code, items)
            self._conn.commit()
        return True

    def _insert_items(self, code, items):
        self._conn.executemany(
            "INSERT INTO items (code, position, kind, data, caption) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (code, i, kind, data, caption)
                for i, (kind, data, caption) in enumerate(items)
            ],
        )

    def is_editable(self, code):
        with self._lock:
            row = self._conn.execute(
                "SELECT editable FROM links WHERE code = ?", (code,)
            ).fetchone()
        return bool(row and row[0])

    def get_items(self, code):
        with self._lock:
            return self._conn.execute(
                "SELECT kind, data, caption FROM items "
                "WHERE code = ? ORDER BY position",
                (code,),
            ).fetchall()

    # ------------------------------------------------------------------ users
    def add_user(self, user_id):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
            )
            self._conn.commit()

    def remove_user(self, user_id):
        with self._lock:
            self._conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def all_user_ids(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM users WHERE banned = 0"
            ).fetchall()
        return [r[0] for r in rows]

    def count_users(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def set_banned(self, user_id, banned):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
            )
            self._conn.execute(
                "UPDATE users SET banned = ? WHERE user_id = ?",
                (1 if banned else 0, user_id),
            )
            self._conn.commit()

    def is_banned(self, user_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT banned FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(row and row[0])

    # --------------------------------------------------------------- settings
    def get_setting(self, key, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            self._conn.commit()

    # -------------------------------------------------------------- forcesub
    def add_forcesub(self, chat_id, title, link, mode="normal"):
        with self._lock:
            self._conn.execute(
                "INSERT INTO forcesub (chat_id, title, link, mode) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, "
                "link = excluded.link, mode = excluded.mode",
                (chat_id, title, link, mode),
            )
            self._conn.commit()

    def remove_forcesub(self, chat_id):
        with self._lock:
            self._conn.execute("DELETE FROM forcesub WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM join_requests WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    def clear_forcesub(self):
        with self._lock:
            self._conn.execute("DELETE FROM forcesub")
            self._conn.execute("DELETE FROM join_requests")
            self._conn.commit()

    def list_forcesub(self):
        """Return [(chat_id, title, link, mode), ...] in the order they were added."""
        with self._lock:
            return self._conn.execute(
                "SELECT chat_id, title, link, mode FROM forcesub ORDER BY id"
            ).fetchall()

    # ---------------------------------------------------------- join requests
    def add_join_request(self, chat_id, user_id):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO join_requests (chat_id, user_id) VALUES (?, ?)",
                (chat_id, user_id),
            )
            self._conn.commit()

    def has_join_request(self, chat_id, user_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM join_requests WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
        return row is not None
