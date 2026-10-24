"""Gestión de sesiones: inicio, continuidad, cierre explícito y por inactividad.

Política:
- La sesión empieza con la primera interacción de una conversación (o la primera tras un cierre).
- Un nuevo mensaje continúa la sesión activa mientras no se haya superado el umbral de inactividad.
- Si el umbral se superó, la sesión anterior se cierra (inactivity_timeout) con ended_at = última
  actividad real y inactivity_detected_at = momento de la detección, y se abre una sesión nueva
  vinculada (previous_session_id).
- Nunca se cierra una sesión con una solicitud pendiente (processing/generating).
- El cierre por inactividad lo ejecuta el servidor (hilo de mantenimiento), no el navegador.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..analytics import satisfaction
from ..repositories import store
from ..timeutil import iso, parse_iso
from .errors import ApiError


class SessionManager:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg

    # --- usado dentro de la transacción de send_message ---
    def resolve_for_message(self, conn, conversation: dict, now: datetime) -> dict:
        active = store.get_active_session(conn, conversation["conversation_id"])
        if active:
            idle = (now - parse_iso(active["last_activity_at"])).total_seconds()
            if idle >= self.cfg.inactivity_threshold_seconds and not store.session_has_pending(conn, active["session_id"]):
                self.close(conn, active, "inactivity_timeout", now)
                active = None
        if active:
            return active
        previous = store.get_last_session(conn, conversation["conversation_id"])
        session = store.create_session(conn, conversation, now, previous["session_id"] if previous else None)
        store.log_event(conn, "session_started", conversation["user_id"], conversation["conversation_id"],
                        session["session_id"], now, {"previous_session_id": session["previous_session_id"]})
        if previous:
            store.log_event(conn, "session_resumed", conversation["user_id"], conversation["conversation_id"],
                            session["session_id"], now, {"previous_session_id": previous["session_id"],
                                                         "previous_end_reason": previous["end_reason"]})
        return session

    def close(self, conn, session: dict, reason: str, now: datetime) -> None:
        """Cierra una sesión activa. Explícita: ended_at = now. Inactividad: ended_at = última actividad."""
        last = parse_iso(session["last_activity_at"])
        ended_at = now if reason == "explicit_user_action" else last
        duration = max((ended_at - parse_iso(session["started_at"])).total_seconds(), 0.0)
        conn.execute(
            "UPDATE sessions SET status='ended', ended_at=?, end_reason=?, total_duration_seconds=?,"
            " inactivity_detected_at=? WHERE session_id=?",
            (iso(ended_at), reason, duration, iso(now) if reason == "inactivity_timeout" else None,
             session["session_id"]))
        if reason == "inactivity_timeout":
            store.log_event(conn, "inactivity_detected", session["user_id"], session["conversation_id"],
                            session["session_id"], now,
                            {"last_activity_at": session["last_activity_at"], "detected_at": iso(now),
                             "threshold_seconds": self.cfg.inactivity_threshold_seconds})
        fresh = store.get_session(conn, session["session_id"])
        store.log_event(conn, "session_ended", session["user_id"], session["conversation_id"],
                        session["session_id"], now,
                        {"end_reason": reason, "ended_at": fresh["ended_at"], "duration_seconds": duration,
                         "user_messages": fresh["total_user_messages"],
                         "assistant_messages": fresh["total_assistant_messages"]})
        satisfaction.evaluate_session(conn, session["session_id"], now)

    # --- cierre explícito ---
    def finish(self, user_id: str, session_id: str, close_conversation: bool, now: datetime) -> dict:
        with self.db.tx() as conn:
            session = store.get_session(conn, session_id)
            if not session or session["user_id"] != user_id:
                raise ApiError(404, "not_found", "Sesión no encontrada.")
            if store.session_has_pending(conn, session_id):
                raise ApiError(409, "pending_generation", "Hay una respuesta en curso; espera a que termine.")
            if session["status"] == "active":
                self.close(conn, session, "explicit_user_action", now)
            if close_conversation:
                conv = store.get_conversation(conn, session["conversation_id"], user_id)
                if conv["status"] != "closed":
                    store.close_conversation(conn, conv["conversation_id"], now)
                    store.log_event(conn, "conversation_closed", user_id, conv["conversation_id"], session_id, now, {})
            return store.get_session(conn, session_id)

    # --- barrido de inactividad (hilo de mantenimiento) ---
    def sweep_inactive(self, now: datetime) -> int:
        cutoff = iso(now - timedelta(seconds=self.cfg.inactivity_threshold_seconds))
        closed = 0
        with self.db.tx() as conn:
            rows = conn.execute("SELECT * FROM sessions WHERE status='active' AND last_activity_at<=?", (cutoff,)).fetchall()
            for r in rows:
                s = dict(r)
                if store.session_has_pending(conn, s["session_id"]):
                    continue  # generación en curso: no se cierra
                self.close(conn, s, "inactivity_timeout", now)
                closed += 1
        return closed

    def recover_interrupted(self, now: datetime) -> int:
        """Al arrancar: solicitudes que quedaron pendientes por una caída se marcan 'interrupted'."""
        with self.db.tx() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE status IN ('processing','generating')").fetchall()
            for r in rows:
                m = dict(r)
                conn.execute("UPDATE messages SET status='interrupted', error_info=? WHERE message_id=?",
                             ("server_restart", m["message_id"]))
                store.log_event(conn, "generation_interrupted", m["user_id"], m["conversation_id"], m["session_id"],
                                now, {"message_id": m["message_id"], "reason": "server_restart"})
            return len(rows)
