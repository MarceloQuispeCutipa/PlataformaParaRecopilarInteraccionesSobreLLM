"""Cálculo de métricas de uso a partir de SQLite (solo lectura).

Denominadores:
- "sesiones con actividad" = sesiones con al menos un mensaje de usuario.
- Los porcentajes de satisfacción usan ese denominador; nunca se cuentan como satisfactorias las
  sesiones cerradas por inactividad (quedan en abandoned_or_inactive salvo señal explícita).
- Duraciones: solo sesiones finalizadas (las activas aún no tienen duración total).
- Pausas: tiempo entre el fin de una respuesta y el siguiente mensaje del usuario, dentro de la misma sesión.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime

from ..timeutil import iso, parse_iso
from . import satisfaction


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {"n": len(values), "mean": round(statistics.fmean(values), 3), "median": round(statistics.median(values), 3),
            "min": round(min(values), 3), "max": round(max(values), 3)}


def _pct(n: int, d: int) -> dict:
    return {"count": n, "denominator": d, "percent": round(100 * n / d, 1) if d else None}


def _bucket(value: float, edges: list[tuple[float, str]], last: str) -> str:
    for limit, label in edges:
        if value < limit:
            return label
    return last


def compute_summary(conn, now: datetime) -> dict:
    scalar = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    sessions = [dict(r) for r in conn.execute("SELECT * FROM sessions")]
    messages = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY conversation_id, seq")]
    users_msgs = [m for m in messages if m["role"] == "user"]
    asst = [m for m in messages if m["role"] == "assistant"]
    active_sessions = [s for s in sessions if s["total_user_messages"] > 0]
    ended = [s for s in sessions if s["status"] == "ended"]

    dur_edges = [(60, "<1 min"), (300, "1-5 min"), (900, "5-15 min"), (3600, "15-60 min")]
    msg_edges = [(2, "1"), (3, "2"), (4, "3"), (6, "4-5"), (9, "6-8")]
    dur_dist = Counter(_bucket(s["total_duration_seconds"], dur_edges, ">60 min") for s in ended if s["total_duration_seconds"] is not None)
    msg_dist = Counter(_bucket(s["total_user_messages"], msg_edges, "9+") for s in active_sessions)

    # --- comportamiento ---
    by_status = Counter(s["satisfaction_status"] for s in active_sessions)
    den = len(active_sessions)
    clar = sum(1 for m in users_msgs if satisfaction.is_clarification_request(m["content"]))
    corr = sum(1 for m in users_msgs if satisfaction.is_correction_request(m["content"]))
    prev_by_conv: dict[str, list[str]] = defaultdict(list)
    repeats = 0
    for m in users_msgs:
        if satisfaction.is_repeat(m["content"], prev_by_conv[m["conversation_id"]]):
            repeats += 1
        prev_by_conv[m["conversation_id"]].append(m["content"])

    gaps = []
    by_conv: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        by_conv[m["conversation_id"]].append(m)
    for ms in by_conv.values():
        for a, b in zip(ms, ms[1:]):
            if a["role"] == "assistant" and b["role"] == "user" and a["session_id"] == b["session_id"] \
                    and a["generation_completed_at"]:
                gaps.append((parse_iso(b["created_at"]) - parse_iso(a["generation_completed_at"])).total_seconds())

    # --- utilización ---
    per_user = Counter(m["user_id"] for m in users_msgs)
    per_conv = Counter(m["conversation_id"] for m in messages)
    per_day = Counter(s["started_at"][:10] for s in sessions)           # día UTC
    per_hour = Counter(m["created_at"][11:13] for m in users_msgs)      # hora UTC
    resp_times = [m["response_time_ms"] for m in asst if m["status"] == "completed" and m["response_time_ms"] is not None]
    durations = [s["total_duration_seconds"] for s in ended if s["total_duration_seconds"] is not None]

    return {
        "generated_at": iso(now),
        "time_basis": "UTC",
        "general": {
            "total_users": scalar("SELECT COUNT(*) FROM users"),
            "total_conversations": scalar("SELECT COUNT(*) FROM conversations"),
            "total_sessions": len(sessions),
            "total_user_messages": len(users_msgs),
            "total_assistant_responses": sum(1 for m in asst if m["status"] == "completed"),
            "failed_or_interrupted_responses": sum(1 for m in asst if m["status"] in ("failed", "interrupted")),
            "avg_user_messages_per_session": _stats([s["total_user_messages"] for s in active_sessions])["mean"],
            "avg_assistant_responses_per_session": _stats([s["total_assistant_messages"] for s in active_sessions])["mean"],
            "session_duration_seconds": _stats(durations),
            "response_time_ms": _stats(resp_times),
            "sessions_by_duration": dict(dur_dist),
            "sessions_by_user_message_count": dict(msg_dist),
        },
        "behavior": {
            "sessions_ended_explicitly": sum(1 for s in sessions if s["end_reason"] == "explicit_user_action"),
            "sessions_ended_by_inactivity": sum(1 for s in sessions if s["end_reason"] == "inactivity_timeout"),
            "sessions_end_unknown_still_active": sum(1 for s in sessions if s["status"] == "active"),
            "sessions_by_satisfaction_status": dict(by_status),
            "sessions_with_satisfaction_signals": by_status["explicitly_satisfied"] + by_status["likely_satisfied"],
            "sessions_apparently_unresolved": by_status["unresolved"] + by_status["abandoned_or_inactive"],
            "clarification_requests": clar,
            "correction_requests": corr,
            "repeated_queries": repeats,
            "responses_rated_useful": scalar("SELECT COUNT(*) FROM feedback WHERE rating='useful'"),
            "responses_rated_not_useful": scalar("SELECT COUNT(*) FROM feedback WHERE rating='not_useful'"),
            "seconds_between_response_and_next_user_message": _stats(gaps),
        },
        "usage": {
            "user_messages_per_pseudonymous_user": _stats(list(per_user.values())),
            "sessions_per_day_utc": dict(sorted(per_day.items())),
            "messages_per_conversation": _stats(list(per_conv.values())),
            "pause_seconds_between_messages": _stats(gaps),
            "user_messages_by_hour_utc": dict(sorted(per_hour.items())),
            "pct_sessions_explicitly_satisfied": _pct(by_status["explicitly_satisfied"], den),
            "pct_sessions_estimated_satisfied": _pct(by_status["likely_satisfied"], den),
            "pct_sessions_inconclusive_or_unknown": _pct(
                by_status["unresolved"] + by_status["abandoned_or_inactive"] + by_status["unknown"], den),
            "denominator_note": "denominador = sesiones con al menos un mensaje de usuario; la inactividad no cuenta como satisfacción",
        },
        "caveats": [
            "Las categorías de satisfacción son estimaciones por reglas, no hechos confirmados.",
            "Más mensajes no implica mejor aprendizaje, mayor satisfacción ni peor desempeño.",
        ],
    }
