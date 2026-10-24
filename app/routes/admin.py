"""Endpoints de administración: analítica y exportaciones. Requieren ADMIN_TOKEN."""
from __future__ import annotations

import hmac
from functools import wraps

from flask import Blueprint, Response, current_app, jsonify, request

from ..analytics import metrics
from ..services import exporter
from ..services.errors import ApiError
from .api import body, svc

bp = Blueprint("admin", __name__, url_prefix="/api")

MIMES = {"csv": "text/csv; charset=utf-8", "jsonl": "application/x-ndjson; charset=utf-8"}


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        expected = svc().cfg.admin_token
        if not expected:
            raise ApiError(403, "admin_disabled", "Administración deshabilitada: define ADMIN_TOKEN en .env.")
        supplied = request.headers.get("X-Admin-Token", "")
        if not supplied and request.headers.get("Authorization", "").startswith("Bearer "):
            supplied = request.headers["Authorization"][7:]
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise ApiError(401, "unauthorized", "Token de administración inválido.")
        return fn(*a, **kw)
    return wrapper


def _filters(src) -> dict:
    return {k: src.get(k) for k in ("date_from", "date_to", "conversation_id", "session_id") if src.get(k)}


@bp.get("/analytics/summary")
@admin_required
def summary():
    with svc().db.connection() as conn:
        return jsonify(metrics.compute_summary(conn, svc().clock()))


@bp.get("/admin/overview")
@admin_required
def overview():
    with svc().db.connection() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("users", "conversations", "sessions", "messages", "interaction_events", "feedback")}
    exports = exporter.last_exports(svc().db, 5)
    last_ok = next((e for e in exports if e["status"] == "ok"), None)
    return jsonify({"counts": counts, "last_export": last_ok, "recent_exports": exports,
                    "export_dir": str(svc().cfg.export_dir), "db_path": str(svc().cfg.db_path),
                    "available_files": list(exporter.FILES)})


@bp.get("/export/<filename>")
@admin_required
def download(filename):
    data = exporter.snapshot_render(svc().db, filename, _filters(request.args), request.args.get("tz", svc().cfg.export_timezone))
    fmt = exporter.FILES[filename][1]
    return Response(data, mimetype=MIMES[fmt].split(";")[0], headers={
        "Content-Type": MIMES[fmt], "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@bp.post("/admin/export")
@admin_required
def export_to_disk():
    b = body()
    try:
        res = exporter.export_to_dir(svc().db, svc().cfg, svc().clock, "admin", b.get("files"), _filters(b), b.get("tz"))
    except exporter.ExportError as e:
        raise ApiError(500, "export_failed", str(e))
    return jsonify(res)
