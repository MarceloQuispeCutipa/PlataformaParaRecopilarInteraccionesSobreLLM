"""Orquestación de un mensaje: registrar consulta -> generar -> registrar respuesta.

Orden garantizado (todo en el backend, en SQLite):
  TX1: sesión + mensaje de usuario (processing) + mensaje del asistente (generating) + eventos
  [llamada al LLM fuera de transacción]
  TX2: respuesta (completed/failed) + contadores + eventos + evaluación de satisfacción
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from ..analytics import satisfaction
from ..repositories import store
from ..timeutil import iso
from .errors import ApiError
from .llm import LLMError

log = logging.getLogger("tracker.chat")

QUESTION_WORDS = ("qué", "que", "cómo", "como", "por qué", "porque", "cuál", "cual", "cuándo", "cuando",
                  "dónde", "donde", "quién", "quien", "what", "how", "why", "which", "when", "where", "who")


def classify_message_type(content: str, seq: int) -> str:
    """Heurística simple y documentada: follow_up si no es el primer mensaje; query si parece pregunta."""
    if seq > 1:
        return "follow_up"
    t = content.strip().lower().lstrip("¿")
    return "query" if "?" in content or t.startswith(QUESTION_WORDS) else "instruction"


class ChatService:
    def __init__(self, db, cfg, llm, clock, sessions):
        self.db, self.cfg, self.llm, self.clock, self.sessions = db, cfg, llm, clock, sessions

    # ------------------------------------------------------------------
    def create_conversation(self, user_id: str, title: Optional[str] = None) -> dict:
        now = self.clock()
        with self.db.tx() as conn:
            store.ensure_user(conn, user_id, now)
            conv = store.create_conversation(conn, user_id, self.cfg.model_name, now, title)
            store.log_event(conn, "conversation_started", user_id, conv["conversation_id"], None, now,
                            {"model": self.cfg.model_name})
        return conv

    def _payload(self, conn, user_msg: dict, replayed: bool = False) -> dict:
        reply = store.get_reply(conn, user_msg["message_id"])
        return {"user_message": user_msg, "assistant_message": reply, "session_id": user_msg["session_id"],
                "replayed": replayed}

    # ------------------------------------------------------------------
    def send_message(self, user_id: str, conversation_id: str, content: str,
                     client_message_id: Optional[str] = None) -> dict:
        content = (content or "").strip()
        if not content:
            raise ApiError(400, "empty_message", "El mensaje no puede estar vacío.")
        if len(content) > self.cfg.max_message_chars:
            raise ApiError(400, "message_too_long", f"El mensaje supera {self.cfg.max_message_chars} caracteres.")
        if client_message_id is not None and not (isinstance(client_message_id, str) and 0 < len(client_message_id) <= 100):
            raise ApiError(400, "bad_client_message_id", "client_message_id inválido.")

        now = self.clock()
        # ---------- TX1: registrar la consulta ANTES de pedir la respuesta ----------
        with self.db.tx() as conn:
            conv = store.get_conversation(conn, conversation_id, user_id)
            if not conv:
                raise ApiError(404, "not_found", "Conversación no encontrada.")
            if client_message_id:
                existing = store.find_by_client_id(conn, conversation_id, client_message_id)
                if existing:  # reintento: no se duplica nada
                    return self._payload(conn, existing, replayed=True)
            if conv["status"] == "closed":
                raise ApiError(409, "conversation_closed", "La conversación fue finalizada. Inicia una nueva.")
            if store.conversation_has_pending(conn, conversation_id):
                raise ApiError(409, "pending_generation", "Hay una respuesta en curso en esta conversación.")

            session = self.sessions.resolve_for_message(conn, conv, now)
            sid, seq = session["session_id"], store.next_seq(conn, conversation_id)
            ev_user = store.log_event(conn, "message_sent", user_id, conversation_id, sid, now,
                                      {"seq": seq, "chars": len(content)})
            user_mid = store.insert_message(
                conn, event_id=ev_user, user_id=user_id, conversation_id=conversation_id, session_id=sid,
                parent_message_id=None, seq=seq, role="user", message_type=classify_message_type(content, seq),
                content=content, created_at=iso(now), model_name=conv["model_name"], status="processing",
                client_message_id=client_message_id)
            n_user = session["total_user_messages"] + 1
            conn.execute("UPDATE sessions SET total_user_messages=?, last_activity_at=? WHERE session_id=?",
                         (n_user, iso(now), sid))
            store.touch_conversation(conn, conversation_id, now, title=content[:60])
            th = self.cfg.message_threshold
            if th and n_user >= th and not session["message_threshold_reached"]:
                conn.execute("UPDATE sessions SET message_threshold_reached=1 WHERE session_id=?", (sid,))
                store.log_event(conn, "message_threshold_reached", user_id, conversation_id, sid, now,
                                {"threshold": th, "user_messages": n_user,
                                 "note": "regla técnica de análisis; no es prueba de finalización"})
            gen_start = self.clock()
            ev_gen = store.log_event(conn, "generation_started", user_id, conversation_id, sid, gen_start,
                                     {"user_message_id": user_mid, "model": conv["model_name"]})
            asst_mid = store.insert_message(
                conn, event_id=ev_gen, user_id=user_id, conversation_id=conversation_id, session_id=sid,
                parent_message_id=user_mid, seq=seq + 1, role="assistant", content="",
                created_at=iso(gen_start), generation_started_at=iso(gen_start), model_name=conv["model_name"],
                status="generating")
            history = store.completed_history(conn, conversation_id, self.cfg.max_history_messages)
            model_name = conv["model_name"]

        # ---------- generación (fuera de la transacción) ----------
        prompt = [{"role": "system", "content": self.cfg.system_prompt}, *history,
                  {"role": "user", "content": content}]
        result, error = None, None
        try:
            result = self.llm.chat(prompt)
        except LLMError as e:
            error = e
        except Exception as e:  # nunca se inventa una respuesta
            log.error("Error inesperado del motor: %s", type(e).__name__)
            error = LLMError("Error inesperado al generar la respuesta.", "generic")
        done = self.clock()
        ms = max(int((done - gen_start).total_seconds() * 1000), 0)

        # ---------- TX2: registrar el resultado (un reintento ante error de escritura) ----------
        for attempt in (1, 2):
            try:
                with self.db.tx() as conn:
                    self._finalize(conn, user_id, conversation_id, sid, user_mid, asst_mid, result, error, done, ms)
                    return self._payload(conn, store.get_message(conn, user_mid)) | {"llm_error": error}
            except sqlite3.Error as e:
                log.error("Error de escritura al registrar la respuesta (intento %s): %s", attempt, e)
        raise ApiError(500, "storage_error", "No se pudo guardar la respuesta. Se conservó el registro de la consulta.")

    def _finalize(self, conn, user_id, conversation_id, sid, user_mid, asst_mid, result, error, done, ms):
        if error is None:
            conn.execute(
                "UPDATE messages SET content=?, status='completed', generation_completed_at=?, response_time_ms=?,"
                " input_tokens=?, output_tokens=? WHERE message_id=?",
                (result.text, iso(done), ms, result.input_tokens, result.output_tokens, asst_mid))
            conn.execute("UPDATE messages SET status='completed' WHERE message_id=?", (user_mid,))
            conn.execute("UPDATE sessions SET total_assistant_messages=total_assistant_messages+1,"
                         " last_activity_at=? WHERE session_id=?", (iso(done), sid))
            store.log_event(conn, "generation_completed", user_id, conversation_id, sid, done,
                            {"message_id": asst_mid, "response_time_ms": ms, "input_tokens": result.input_tokens,
                             "output_tokens": result.output_tokens})
        else:
            conn.execute(
                "UPDATE messages SET status='failed', generation_completed_at=?, response_time_ms=?, error_info=?"
                " WHERE message_id=?", (iso(done), ms, f"{error.kind}: {error.user_message}"[:500], asst_mid))
            conn.execute("UPDATE messages SET status='failed', error_info=? WHERE message_id=?", (error.kind, user_mid))
            conn.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?", (iso(done), sid))
            if error.kind in ("connection", "model_missing", "timeout"):
                store.log_event(conn, "llm_communication_error", user_id, conversation_id, sid, done,
                                {"message_id": asst_mid, "kind": error.kind})
            store.log_event(conn, "generation_failed", user_id, conversation_id, sid, done,
                            {"message_id": asst_mid, "kind": error.kind, "response_time_ms": ms})
        store.touch_conversation(conn, conversation_id, done)
        satisfaction.evaluate_session(conn, sid, done)

    # ------------------------------------------------------------------
    def submit_feedback(self, user_id: str, message_id: str, rating: str, goal_completed, comment) -> dict:
        if rating not in ("useful", "not_useful"):
            raise ApiError(400, "bad_rating", "rating debe ser 'useful' o 'not_useful'.")
        if goal_completed is not None and not isinstance(goal_completed, bool):
            raise ApiError(400, "bad_goal_completed", "goal_completed debe ser booleano.")
        comment = (comment or "").strip()[:500] or None
        now = self.clock()
        with self.db.tx() as conn:
            msg = store.get_message(conn, message_id)
            if not msg or msg["user_id"] != user_id or msg["role"] != "assistant":
                raise ApiError(404, "not_found", "Respuesta no encontrada.")
            store.upsert_feedback(conn, msg, user_id, rating, goal_completed, comment, now)
            store.log_event(conn, "feedback_submitted", user_id, msg["conversation_id"], msg["session_id"], now,
                            {"message_id": message_id, "rating": rating, "goal_completed": goal_completed})
            return satisfaction.evaluate_session(conn, msg["session_id"], now)

    def log_client_event(self, user_id: str, conversation_id: Optional[str], event_type: str) -> None:
        """Señales auxiliares del navegador (cierre/ocultación de pestaña). NO finalizan sesiones."""
        if event_type not in ("page_hidden", "page_closed", "page_visible"):
            raise ApiError(400, "bad_event", "Tipo de evento no permitido.")
        now = self.clock()
        with self.db.tx() as conn:
            store.ensure_user(conn, user_id, now)
            sid = None
            if conversation_id:
                conv = store.get_conversation(conn, conversation_id, user_id)
                if not conv:
                    raise ApiError(404, "not_found", "Conversación no encontrada.")
                active = store.get_active_session(conn, conversation_id)
                sid = active["session_id"] if active else None
            store.log_event(conn, "client_" + event_type, user_id, conversation_id, sid, now,
                            {"auxiliary_signal": True, "note": "señal auxiliar; no confirma el fin de uso"})

    def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        with self.db.tx() as conn:
            if not store.get_conversation(conn, conversation_id, user_id):
                raise ApiError(404, "not_found", "Conversación no encontrada.")
            store.delete_conversation(conn, conversation_id)
