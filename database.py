"""SQLite storage for users, chat conversations and messages.

Uses Python's built-in sqlite3 module. Each call opens a short-lived
connection, which keeps things thread-safe under Flask's threaded server
(including the streaming generator, which runs after the request returns).
"""

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

SCHEMA_VERSION = 3

PLANS = ("free", "pro")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT NOT NULL,
    created_at     INTEGER NOT NULL,         -- epoch milliseconds
    plan           TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro')),
    messages_used  INTEGER NOT NULL DEFAULT 0  -- lifetime count of messages sent
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL DEFAULT '',
    status           TEXT CHECK (status IN ('done', 'error', 'stopped')),  -- assistant only
    error            TEXT,
    stop_reason      TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    latency_ms       INTEGER,
    created_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, id);
"""

_db_path: str | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def init(path: str) -> None:
    """Create the database file and tables, and upgrade older databases."""
    global _db_path
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    _db_path = path
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Version 1 databases had no owner on conversations.
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(conversations)")]
        if "user_id" not in columns:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id, updated_at DESC)"
        )
        # Version 2 databases had no plan or message counter. Existing accounts
        # start on the free plan with a count of zero.
        user_columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
        if "plan" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro'))"
            )
        if "messages_used" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN messages_used INTEGER NOT NULL DEFAULT 0")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def connect():
    if _db_path is None:
        raise RuntimeError("database.init() must be called before using the database.")
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> None:
    """Raise if the database can't be opened (used by the health check)."""
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def create_user(email: str, password_hash: str) -> dict | None:
    """Create an account. Returns None if the email is already registered.

    The first account ever created also takes over any conversations saved
    before accounts existed, so they aren't lost.
    """
    with connect() as conn:
        is_first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        try:
            cursor = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, password_hash, _now_ms()),
            )
        except sqlite3.IntegrityError:
            return None
        user_id = cursor.lastrowid
        if is_first:
            conn.execute("UPDATE conversations SET user_id = ? WHERE user_id IS NULL", (user_id,))
    return {"id": user_id, "email": email}


def get_user_by_email(email: str) -> dict | None:
    """Return the user including their password hash (for signing in), or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, email, plan, messages_used FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def consume_message(user_id: int, free_limit: int) -> bool:
    """Count one message against the account, if its plan allows it.

    Free accounts can send `free_limit` messages in total; Pro accounts are
    unlimited. One atomic UPDATE checks and counts together, so two requests
    arriving at once can't both take the last free message.
    """
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE users SET messages_used = messages_used + 1 "
            "WHERE id = ? AND (plan = 'pro' OR messages_used < ?)",
            (user_id, free_limit),
        )
    return cursor.rowcount == 1


def refund_message(user_id: int) -> None:
    """Give back a message that never produced a reply (for example, an AWS error)."""
    with connect() as conn:
        conn.execute(
            "UPDATE users SET messages_used = MAX(messages_used - 1, 0) WHERE id = ?", (user_id,)
        )


def set_plan(email: str, plan: str) -> bool:
    """Move an account to the 'free' or 'pro' plan. Returns False if the email is unknown."""
    if plan not in PLANS:
        raise ValueError(f"plan must be one of {PLANS}")
    with connect() as conn:
        cursor = conn.execute("UPDATE users SET plan = ? WHERE email = ?", (plan, email.strip().lower()))
    return cursor.rowcount == 1


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

def _conversation(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _message(row: sqlite3.Row) -> dict:
    usage = None
    if row["input_tokens"] is not None or row["output_tokens"] is not None:
        usage = {
            "inputTokens": row["input_tokens"],
            "outputTokens": row["output_tokens"],
            "latencyMs": row["latency_ms"],
        }
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "status": row["status"],
        "error": row["error"],
        "stopReason": row["stop_reason"],
        "usage": usage,
        "createdAt": row["created_at"],
    }


# --------------------------------------------------------------------------- #
# Conversations (always scoped to their owner)
# --------------------------------------------------------------------------- #

def list_conversations(user_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_conversation(r) for r in rows]


def create_conversation(user_id: int, title: str) -> dict:
    now = _now_ms()
    conversation_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, user_id, title, now, now),
        )
    return {"id": conversation_id, "title": title, "createdAt": now, "updatedAt": now}


def get_conversation(user_id: int, conversation_id: str) -> dict | None:
    """Return the user's conversation with all of its messages, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if row is None:
            return None
        messages = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ).fetchall()
    conversation = _conversation(row)
    conversation["messages"] = [_message(m) for m in messages]
    return conversation


def conversation_exists(user_id: int, conversation_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
        ).fetchone()
    return row is not None


def delete_conversation(user_id: int, conversation_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
        )
    return cursor.rowcount > 0


# --------------------------------------------------------------------------- #
# Messages (callers must check conversation ownership first)
# --------------------------------------------------------------------------- #

def add_message(
    conversation_id: str,
    role: str,
    content: str,
    *,
    status: str | None = None,
    error: str | None = None,
    stop_reason: str | None = None,
    usage: dict | None = None,
) -> int | None:
    """Append a message. Returns its ID, or None if the conversation was deleted."""
    usage = usage or {}
    now = _now_ms()
    with connect() as conn:
        # INSERT ... SELECT ... WHERE EXISTS: a reply that finishes after its
        # conversation was deleted is silently dropped instead of failing.
        cursor = conn.execute(
            """
            INSERT INTO messages (conversation_id, role, content, status, error, stop_reason,
                                  input_tokens, output_tokens, latency_ms, created_at)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE EXISTS (SELECT 1 FROM conversations WHERE id = ?)
            """,
            (
                conversation_id, role, content, status, error, stop_reason,
                usage.get("inputTokens"), usage.get("outputTokens"), usage.get("latencyMs"),
                now, conversation_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return cursor.lastrowid


def last_message(conversation_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    return _message(row) if row else None


def delete_trailing_assistant(conversation_id: str) -> None:
    """Remove the last message if it is an assistant reply (used by Retry)."""
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM messages
            WHERE id = (SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1)
              AND role = 'assistant'
            """,
            (conversation_id,),
        )


def history(conversation_id: str) -> list[dict]:
    """Messages in order, as plain dicts with role, content and status."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content, status FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return [dict(r) for r in rows]
