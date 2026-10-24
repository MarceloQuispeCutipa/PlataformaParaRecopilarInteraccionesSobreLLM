"""Repositorio: todo el SQL de lectura/escritura de usuarios, conversaciones, sesiones,
mensajes, eventos y feedback. Cada función recibe una conexión (normalmente dentro de db.tx())."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from ..timeutil import iso


def new_id() -> str:
    return str(uuid.uuid4())


def _row(r) -> Optional[dict]:
    return dict(r) if r is not None else None


# ---------- usuarios ----------
def ensure_user(conn, user_id: str, now: datetime) -> None:
    conn.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user_id, iso(now)))


# ---------- eventos ----------
def log_event(conn, event_type: str, user_id: str, conversation_id: Optional[str],
              session_id: Optional[str], ts: datetime, metadata: Optional[dict] = None) -> str:
    event_id = new_id()
    conn.execute(
        "INSERT INTO interaction_events (event_id,user_id,conversation_id,session_id,event_type,event_timestamp,metadata_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (event_id, user_id, conversation_id, session_id, event_type, iso(ts),
         json.dumps(metadata or {}, ensure_ascii=False)),
    )
    return event_id


# ---------- conversaciones ----------
def create_conversation(conn, user_id: str, model: str, now: datetime, title: Optional[str] = None) -> dict:
    cid = new_id()
    conn.execute(
        "INSERT INTO conversations (conversation_id,user_id,title,created_at,last_activity_at,model_name)"
        " VALUES (?,?,?,?,?,?)", (cid, user_id, title, iso(now), iso(now), model))
    return get_conversation(conn, cid)


def get_conversation(conn, conversation_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    if user_id is None:
        return _row(conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone())
    return _row(conn.execute("SELECT * FROM conversations WHERE conversation_id=? AND user_id=?",
                             (conversation_id, user_id)).fetchone())


def list_conversations(conn, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.conversation_id) AS message_count"
        " FROM conversations c WHERE c.user_id=? ORDER BY c.last_activity_at DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def touch_conversation(conn, conversation_id: str, now: datetime, title: Optional[str] = None) -> None:
    conn.execute("UPDATE conversations SET last_activity_at=?, title=COALESCE(title, ?) WHERE conversation_id=?",
                 (iso(now), title, conversation_id))


def close_conversation(conn, conversation_id: str, now: datetime) -> None:
    conn.execute("UPDATE conversations SET status='closed', explicitly_closed_at=? WHERE conversation_id=?",
                 (iso(now), conversation_id))


def delete_conversation(conn, conversation_id: str) -> None:
    conn.execute("DELETE FROM conversations WHERE conversation_id=?", (conversation_id,))  # cascada


# ---------- sesiones ----------
def get_session(conn, session_id: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())


def get_active_session(conn, conversation_id: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM sessions WHERE conversation_id=? AND status='active'",
                             (conversation_id,)).fetchone())


def get_last_session(conn, conversation_id: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM sessions WHERE conversation_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                             (conversation_id,)).fetchone())


def create_session(conn, conversation: dict, now: datetime, previous_session_id: Optional[str]) -> dict:
    sid = new_id()
    conn.execute(
        "INSERT INTO sessions (session_id,conversation_id,user_id,previous_session_id,started_at,last_activity_at)"
        " VALUES (?,?,?,?,?,?)",
        (sid, conversation["conversation_id"], conversation["user_id"], previous_session_id, iso(now), iso(now)))
    return get_session(conn, sid)


def list_sessions(conn, user_id: Optional[str] = None) -> list[dict]:
    if user_id is None:
        rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM sessions WHERE user_id=? ORDER BY started_at DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def session_has_pending(conn, session_id: str) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE session_id=? AND status IN ('processing','generating') LIMIT 1",
                        (session_id,)).fetchone() is not None


def conversation_has_pending(conn, conversation_id: str) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE conversation_id=? AND status IN ('processing','generating') LIMIT 1",
                        (conversation_id,)).fetchone() is not None


# ---------- mensajes ----------
def next_seq(conn, conversation_id: str) -> int:
    r = conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()
    return int(r["n"])


def insert_message(conn, **f: Any) -> str:
    mid = new_id()
    cols = ["message_id"] + list(f.keys())
    conn.execute(f"INSERT INTO messages ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [mid, *f.values()])
    return mid


def get_message(conn, message_id: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone())


def find_by_client_id(conn, conversation_id: str, client_message_id: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM messages WHERE conversation_id=? AND client_message_id=?",
                             (conversation_id, client_message_id)).fetchone())


def get_reply(conn, parent_message_id: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM messages WHERE parent_message_id=?", (parent_message_id,)).fetchone())


def list_messages(conn, conversation_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY seq", (conversation_id,))]


def completed_history(conn, conversation_id: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? AND status='completed' ORDER BY seq DESC LIMIT ?",
        (conversation_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------- feedback ----------
def upsert_feedback(conn, message: dict, user_id: str, rating: str, goal_completed: Optional[bool],
                    comment: Optional[str], now: datetime) -> None:
    conn.execute(
        "INSERT INTO feedback (feedback_id,message_id,user_id,conversation_id,session_id,rating,goal_completed,comment,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(message_id,user_id) DO UPDATE SET rating=excluded.rating, goal_completed=excluded.goal_completed,"
        " comment=excluded.comment, created_at=excluded.created_at",
        (new_id(), message["message_id"], user_id, message["conversation_id"], message["session_id"], rating,
         None if goal_completed is None else int(goal_completed), comment, iso(now)))
