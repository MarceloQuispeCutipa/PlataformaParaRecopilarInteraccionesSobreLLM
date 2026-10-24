"""Estimación de satisfacción basada en SEÑALES OBSERVABLES y reglas transparentes.

No afirma conocer la intención del usuario: devuelve una estimación con método, evidencia y
confianza. La ausencia de mensajes nunca se interpreta como satisfacción.
Categorías: explicitly_satisfied | likely_satisfied | unresolved | abandoned_or_inactive | unknown
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

from ..timeutil import iso


def norm(text: str) -> str:
    t = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _compile(patterns):
    return [re.compile(p) for p in patterns]


CLARIFY = _compile([
    r"\b(puedes|podrias|podria) (explicar|aclarar|detallar|ampliar|reformular|repetir)",
    r"\bexplicame\b", r"\baclara\w*", r"\bmas (detalle|detalles|informacion|ejemplos?)\b",
    r"\bno (lo )?(entiendo|entendi|comprendo)\b", r"\bno me (queda|quedo) claro\b",
    r"\bque quieres decir\b", r"\bamplia\w*", r"\bdetalla\w*", r"\bsigo sin (entender|comprender)\b",
])
CORRECTION = _compile([
    r"\bno es (correct|asi|eso|lo que)", r"\bincorrect", r"\bequivocad", r"\besta mal\b",
    r"\bcorrig\w*", r"\beso no\b", r"\bno funciona\b", r"\bno me sirv", r"\btienes un error\b",
    r"\bte equivocas\b", r"\botra vez\b", r"\bde nuevo\b",
])
STRONG = _compile([
    r"\bperfecto\b", r"\bahora (lo )?(entiendo|comprendo)\b", r"\bya (lo )?(entendi|entiendo|comprendi|comprendo)\b",
    r"\bme (sirvio|ayudo|funciono)\b", r"\b(era|es) (justo |exactamente )?lo que (buscaba|necesitaba|queria)\b",
    r"\bya tengo (la informacion|lo que necesito|todo lo que necesito)\b", r"\bqueda (todo )?claro\b",
    r"\btodo claro\b", r"\bproblema resuelto\b", r"\bthat (helps|helped|works|worked)\b", r"\bgot it\b",
    r"\bmakes sense now\b", r"\bexcelente\b",
])
WEAK = _compile([
    r"\bgracias\b", r"\bthanks?\b", r"\bthank you\b", r"\bgenial\b", r"\bok(ay)?\b", r"\blisto\b",
    r"\bvale\b", r"\bentendido\b",
])


def _first_match(patterns, text) -> Optional[str]:
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(0)
    return None


def is_clarification_request(text: str) -> bool:
    return _first_match(CLARIFY, norm(text)) is not None


def is_correction_request(text: str) -> bool:
    return _first_match(CORRECTION, norm(text)) is not None


def classify_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """('negative'|'strong'|'weak'|None, frase detectada). Las señales negativas tienen prioridad."""
    t = norm(text)
    for label, pats in (("negative", CLARIFY + CORRECTION), ("strong", STRONG), ("weak", WEAK)):
        m = _first_match(pats, t)
        if m:
            return label, m
    return None, None


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a).strip(), norm(b).strip()).ratio()


def is_repeat(text: str, previous: list[str], threshold: float = 0.85) -> bool:
    return any(similarity(text, p) >= threshold for p in previous)


def _decide(session: dict, msgs: list[dict], earlier_user_texts: list[str], feedback: Optional[dict]):
    """Devuelve (status, method, confidence, evidence_dict)."""
    # 1) Señal directa del usuario (botón de utilidad / objetivo completado)
    if feedback:
        if feedback["rating"] == "useful":
            return ("explicitly_satisfied", "user_feedback", 0.95,
                    {"rating": "useful", "goal_completed": feedback["goal_completed"]})
        return ("unresolved", "user_feedback", 0.9, {"rating": "not_useful"})

    users = [m for m in msgs if m["role"] == "user"]
    assistants = [m for m in msgs if m["role"] == "assistant"]
    last_user = users[-1] if users else None

    # 2) Fallos de generación
    if assistants and assistants[-1]["status"] in ("failed", "interrupted"):
        return ("unresolved", "rules:generation_failure", 0.9,
                {"message_id": assistants[-1]["message_id"], "status": assistants[-1]["status"]})

    # 3) Frases del último mensaje del usuario
    if last_user:
        label, matched = classify_text(last_user["content"])
        ev = {"message_id": last_user["message_id"], "matched_phrase": matched}
        if label == "negative":
            return "unresolved", "rules:negative_phrase", 0.75, ev
        if is_repeat(last_user["content"], earlier_user_texts):
            return ("unresolved", "rules:repeated_question", 0.7,
                    {"message_id": last_user["message_id"]})
        if label == "strong":
            return "explicitly_satisfied", "rules:explicit_phrase", 0.85, ev
        if label == "weak":
            return "likely_satisfied", "rules:weak_phrase", 0.5, ev

    # 4) Sin señales en el contenido: depende de cómo terminó (si terminó)
    if session["status"] == "ended":
        if session["end_reason"] == "explicit_user_action":
            return ("likely_satisfied", "rules:explicit_end", 0.4,
                    {"end_reason": "explicit_user_action", "note": "sin señales negativas; el cierre explícito no confirma satisfacción"})
        return ("abandoned_or_inactive", "rules:inactivity", 0.5,
                {"end_reason": session["end_reason"], "note": "la inactividad no implica satisfacción"})
    return "unknown", "rules:no_signal", None, {"note": "datos insuficientes"}


def evaluate_session(conn, session_id: str, now: datetime) -> dict:
    session = dict(conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())
    msgs = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY seq", (session_id,))]
    users = [m for m in msgs if m["role"] == "user"]
    earlier: list[str] = []
    if users:
        earlier = [r["content"] for r in conn.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND role='user' AND seq<?",
            (session["conversation_id"], users[-1]["seq"]))]
    fb = conn.execute("SELECT rating, goal_completed FROM feedback WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                      (session_id,)).fetchone()
    status, method, conf, evidence = _decide(session, msgs, earlier, dict(fb) if fb else None)
    conn.execute(
        "UPDATE sessions SET satisfaction_status=?, satisfaction_method=?, satisfaction_confidence=?,"
        " satisfaction_evidence=?, satisfaction_evaluated_at=? WHERE session_id=?",
        (status, method, conf, json.dumps(evidence, ensure_ascii=False), iso(now), session_id))
    return {"status": status, "method": method, "confidence": conf, "evidence": evidence}
