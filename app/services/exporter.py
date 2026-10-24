"""Exportación SQLite -> CSV / JSONL.

Reglas:
- SQLite es la fuente oficial; aquí SOLO se leen datos (nunca se modifican las tablas de interacción).
- Se ejecuta únicamente bajo petición explícita (admin) o por la tarea programada configurada.
- Nunca se llama desde el registro de mensajes.
- Los archivos se escriben de forma atómica (temporal + os.replace) en una carpeta nueva por exportación.
- CSV: UTF-8 con BOM (utf-8-sig) para que Excel muestre bien las tildes; pandas: encoding="utf-8-sig".
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from typing import Optional

from ..repositories import store
from ..timeutil import iso, parse_iso, to_zone, validate_tz
from .errors import ApiError

log = logging.getLogger("tracker.export")


class ExportError(Exception):
    pass


DATASETS = {
    "interactions": dict(table="messages", ts="created_at", order="created_at, seq", conv="conversation_id", sess="session_id"),
    "sessions": dict(table="sessions", ts="started_at", order="started_at, rowid", conv="conversation_id", sess="session_id"),
    "conversations": dict(table="conversations", ts="created_at", order="created_at, rowid", conv="conversation_id", sess=None),
    "events": dict(table="interaction_events", ts="event_timestamp", order="event_timestamp, rowid", conv="conversation_id", sess="session_id"),
    "feedback": dict(table="feedback", ts="created_at", order="created_at, rowid", conv="conversation_id", sess="session_id"),
}
FILES = {  # nombre de archivo -> (dataset, formato)
    "interactions.csv": ("interactions", "csv"), "sessions.csv": ("sessions", "csv"),
    "conversations.csv": ("conversations", "csv"), "events.csv": ("events", "csv"),
    "feedback.csv": ("feedback", "csv"), "events.jsonl": ("events", "jsonl"),
    "interactions.jsonl": ("interactions", "jsonl"),
}
DEFAULT_ALL = list(FILES)
_JSONL_ESCAPES = {0x2028: "\\u2028", 0x2029: "\\u2029", 0x85: "\\u0085"}  # separadores de línea Unicode


def _bound(value: str, is_end: bool) -> tuple[str, str]:
    try:
        dt = parse_iso(value)
    except ValueError:
        raise ApiError(400, "bad_date", f"Fecha inválida: {value!r}. Usa ISO 8601 (2026-01-31 o 2026-01-31T10:00:00Z).")
    if is_end and len(value) == 10:  # fecha sin hora: incluye todo ese día
        return iso(dt + timedelta(days=1)), "<"
    return iso(dt), "<=" if is_end else ">="


def fetch(conn, name: str, filters: Optional[dict] = None):
    spec, filters = DATASETS[name], filters or {}
    where, params = [], []
    if filters.get("date_from"):
        v, op = _bound(filters["date_from"], False)
        where.append(f"{spec['ts']} {op} ?"); params.append(v)
    if filters.get("date_to"):
        v, op = _bound(filters["date_to"], True)
        where.append(f"{spec['ts']} {op} ?"); params.append(v)
    if filters.get("conversation_id"):
        where.append(f"{spec['conv']} = ?"); params.append(filters["conversation_id"])
    if filters.get("session_id"):
        if spec["sess"]:
            where.append(f"{spec['sess']} = ?"); params.append(filters["session_id"])
        else:
            where.append("conversation_id IN (SELECT conversation_id FROM sessions WHERE session_id = ?)")
            params.append(filters["session_id"])
    sql = f"SELECT * FROM {spec['table']}" + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {spec['order']}"
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return cols, [dict(zip(cols, r)) for r in cur.fetchall()]


def _convert_times(rows, cols, tz):
    if tz.upper() == "UTC":
        return rows
    tcols = [c for c in cols if c.endswith("_at") or c == "event_timestamp"]
    for r in rows:
        for c in tcols:
            if r.get(c):
                r[c] = to_zone(r[c], tz)
    return rows


def render(conn, filename: str, filters: Optional[dict] = None, tz: str = "UTC") -> bytes:
    if filename not in FILES:
        raise ApiError(404, "unknown_export", f"Exportación desconocida: {filename}")
    try:
        validate_tz(tz)
    except Exception:
        raise ApiError(400, "bad_timezone", f"Zona horaria inválida: {tz!r}")
    dataset, fmt = FILES[filename]
    cols, rows = fetch(conn, dataset, filters)
    rows = _convert_times(rows, cols, tz)
    if fmt == "csv":
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=cols, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode("utf-8-sig")
    lines = []
    for r in rows:
        if dataset == "events":
            r = {**{k: v for k, v in r.items() if k != "metadata_json"}, "metadata": json.loads(r["metadata_json"] or "{}")}
        lines.append(json.dumps(r, ensure_ascii=False).translate(_JSONL_ESCAPES))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def snapshot_render(db, filename, filters=None, tz="UTC") -> bytes:
    with db.connection() as conn:
        conn.execute("BEGIN")
        try:
            return render(conn, filename, filters, tz)
        finally:
            conn.execute("COMMIT")


def export_to_dir(db, cfg, clock, requested_by: str, files: Optional[list[str]] = None,
                  filters: Optional[dict] = None, tz: Optional[str] = None) -> dict:
    files = files or DEFAULT_ALL
    for f in files:
        if f not in FILES:
            raise ApiError(400, "unknown_export", f"Exportación desconocida: {f}")
    tz = tz or cfg.export_timezone
    started = clock()
    export_id = store.new_id()
    out_dir = cfg.export_dir / f"export_{started.strftime('%Y%m%dT%H%M%SZ')}_{export_id[:6]}"
    written: dict[str, int] = {}
    try:
        with db.connection() as conn:               # una lectura consistente de todos los datasets
            conn.execute("BEGIN")
            try:
                blobs = {f: render(conn, f, filters, tz) for f in files}
            finally:
                conn.execute("COMMIT")
        out_dir.mkdir(parents=True, exist_ok=False)
        for name, data in blobs.items():
            tmp = out_dir / (name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, out_dir / name)
            written[name] = len(data)
        status, error = "ok", None
    except ApiError:
        raise
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)  # no deja archivos parciales; SQLite no se toca
        status, error = "failed", f"{type(e).__name__}: {e}"
        log.error("Exportación fallida: %s", error)
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO export_log (export_id,requested_by,started_at,finished_at,status,output_dir,files_json,filters_json,error)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (export_id, requested_by, iso(started), iso(clock()), status, str(out_dir) if status == "ok" else None,
                 json.dumps(written), json.dumps(filters or {}), error))
    except Exception as e:
        log.error("No se pudo registrar la exportación en export_log: %s", e)
    if status != "ok":
        raise ExportError("La exportación falló; los datos originales en SQLite siguen intactos.")
    return {"export_id": export_id, "output_dir": str(out_dir), "files": written}


def last_exports(db, limit: int = 5) -> list[dict]:
    with db.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM export_log ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))]
