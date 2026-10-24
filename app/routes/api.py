"""Endpoints para usuarios (cookie de sesión con identificador seudónimo)."""
from __future__ import annotations

import uuid
from functools import wraps

from flask import Blueprint, current_app, jsonify, request, session

from ..repositories import store
from ..services.errors import ApiError

bp = Blueprint("api", __name__, url_prefix="/api")


def svc():
    return current_app.extensions["tracker"]


def user_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        s = svc()
        uid = session.get("uid")
        if not uid:
            uid = str(uuid.uuid4())
            session["uid"] = uid
            session.permanent = True
        with s.db.tx() as conn:
            store.ensure_user(conn, uid, s.clock())
        return fn(uid, *a, **kw)
    return wrapper


def body() -> dict:
    data = request.get_json(silent=True, force=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "El cuerpo debe ser un objeto JSON.")
    return data


@bp.get("/health")
def health():
    return jsonify({**svc().llm.health(), "inactivity_threshold_seconds": svc().cfg.inactivity_threshold_seconds})


@bp.post("/conversations")
@user_required
def create_conversation(uid):
    title = body().get("title")
    return jsonify(svc().chat.create_conversation(uid, title if isinstance(title, str) else None)), 201


@bp.get("/conversations")
@user_required
def list_conversations(uid):
    with svc().db.connection() as conn:
        return jsonify(store.list_conversations(conn, uid))


@bp.get("/conversations/<cid>")
@user_required
def get_conversation(uid, cid):
    with svc().db.connection() as conn:
        conv = store.get_conversation(conn, cid, uid)
        if not conv:
            raise ApiError(404, "not_found", "Conversación no encontrada.")
        sessions = [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE conversation_id=? ORDER BY started_at", (cid,))]
        fb = {r["message_id"]: r["rating"] for r in conn.execute("SELECT message_id, rating FROM feedback WHERE conversation_id=?", (cid,))}
        active = next((s["session_id"] for s in sessions if s["status"] == "active"), None)
        return jsonify({"conversation": conv, "messages": store.list_messages(conn, cid), "sessions": sessions,
                        "active_session_id": active, "last_session_id": sessions[-1]["session_id"] if sessions else None,
                        "feedback": fb})


@bp.delete("/conversations/<cid>")
@user_required
def delete_conversation(uid, cid):
    svc().chat.delete_conversation(uid, cid)
    return jsonify({"deleted": True})


@bp.post("/messages")
@user_required
def post_message(uid):
    b = body()
    out = svc().chat.send_message(uid, b.get("conversation_id", ""), b.get("content", ""), b.get("client_message_id"))
    err = out.pop("llm_error", None)
    if err is not None:
        out["error"] = {"code": "llm_error", "kind": err.kind, "message": err.user_message}
        return jsonify(out), 502
    return jsonify(out)


@bp.post("/sessions/<sid>/finish")
@user_required
def finish_session(uid, sid):
    b = body()
    s = svc().sessions.finish(uid, sid, bool(b.get("close_conversation", False)), svc().clock())
    return jsonify(s)


@bp.post("/messages/<mid>/feedback")
@user_required
def feedback(uid, mid):
    b = body()
    res = svc().chat.submit_feedback(uid, mid, b.get("rating"), b.get("goal_completed"), b.get("comment"))
    return jsonify({"recorded": True, "session_satisfaction": res})


@bp.get("/sessions")
@user_required
def my_sessions(uid):
    with svc().db.connection() as conn:
        return jsonify(store.list_sessions(conn, uid))


@bp.post("/client-events")
@user_required
def client_event(uid):
    b = body()
    svc().chat.log_client_event(uid, b.get("conversation_id"), b.get("type", ""))
    return "", 204
