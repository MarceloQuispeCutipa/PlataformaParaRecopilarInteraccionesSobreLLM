"""Fábrica de la aplicación Flask. Ejecutar:  python -m app.main"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

from flask import Flask, jsonify, render_template
from werkzeug.exceptions import HTTPException

from .config import Config
from .db import Database
from .routes import admin, api
from .services.chat import ChatService
from .services.errors import ApiError
from .services.llm import OllamaClient
from .services.maintenance import Maintenance
from .services.sessions import SessionManager
from .timeutil import utc_now


def create_app(cfg: Optional[Config] = None, llm=None, clock=None, start_background: bool = True) -> Flask:
    cfg = cfg or Config.from_env()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    clock = clock or utc_now
    app = Flask(__name__)
    app.config.update(SECRET_KEY=cfg.secret_key, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=cfg.cookie_secure, PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 365,
                      JSON_AS_ASCII=False)

    db = Database(cfg.db_path)
    db.init_schema()
    llm = llm or OllamaClient(cfg.ollama_url, cfg.model_name, cfg.llm_temperature, cfg.llm_max_tokens,
                              cfg.llm_num_ctx, cfg.llm_timeout_seconds)
    sessions = SessionManager(db, cfg)
    chat = ChatService(db, cfg, llm, clock, sessions)
    maintenance = Maintenance(db, cfg, clock, sessions)
    app.extensions["tracker"] = SimpleNamespace(cfg=cfg, db=db, llm=llm, clock=clock, sessions=sessions,
                                                chat=chat, maintenance=maintenance)

    recovered = sessions.recover_interrupted(clock())
    if recovered:
        logging.getLogger("tracker").warning("%s solicitudes pendientes marcadas como 'interrupted' tras reinicio", recovered)

    app.register_blueprint(api.bp)
    app.register_blueprint(admin.bp)

    @app.errorhandler(ApiError)
    def _api_error(e: ApiError):
        return jsonify({"error": {"code": e.code, "message": e.message}, **e.extra}), e.status

    @app.errorhandler(404)
    def _404(_):
        return jsonify({"error": {"code": "not_found", "message": "Recurso no encontrado."}}), 404

    @app.errorhandler(Exception)
    def _500(e):
        if isinstance(e, HTTPException):
            return jsonify({"error": {"code": e.name.lower().replace(" ", "_"), "message": e.description}}), e.code
        logging.getLogger("tracker").exception("Error no controlado: %s", type(e).__name__)  # sin contenido de mensajes
        return jsonify({"error": {"code": "internal_error", "message": "Error interno del servidor."}}), 500

    @app.get("/")
    def index():
        return render_template("index.html", model=cfg.model_name)

    @app.get("/admin")
    def admin_page():
        return render_template("admin.html")

    if start_background:
        maintenance.start()
    return app


if __name__ == "__main__":
    _cfg = Config.from_env()
    # use_reloader=False: el hilo de mantenimiento y la recuperación al arranque requieren un solo proceso
    create_app(_cfg).run(host=_cfg.host, port=_cfg.port, debug=False, use_reloader=False, threaded=True)
