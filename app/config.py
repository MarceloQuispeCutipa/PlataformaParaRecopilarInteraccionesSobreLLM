"""Configuración centralizada, leída de variables de entorno / .env."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
_PLACEHOLDER_SECRETS = {"", "cambia-esto-por-una-cadena-larga-y-aleatoria", "change-me"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"La variable {name} debe ser un entero (valor: {raw!r})")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"La variable {name} debe ser numérica (valor: {raw!r})")


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    p = Path(raw) if raw else default
    return p if p.is_absolute() else BASE_DIR / p


@dataclass
class Config:
    # modelo
    ollama_url: str = "http://localhost:11434"
    model_name: str = "llama3.2:3b"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 1024
    llm_num_ctx: int = 4096
    llm_timeout_seconds: int = 300
    max_history_messages: int = 20
    system_prompt: str = "Eres un asistente útil. Responde en el idioma del usuario de forma clara y precisa."
    # almacenamiento
    data_dir: Path = BASE_DIR / "data"
    db_path: Path = BASE_DIR / "data" / "llm_interactions.db"
    export_dir: Path = BASE_DIR / "exports"
    # sesiones
    inactivity_threshold_seconds: int = 600
    session_check_interval_seconds: int = 30
    message_threshold: int = 8
    # exportación / privacidad
    export_schedule_minutes: int = 0
    export_timezone: str = "UTC"
    retention_days: int = 0
    max_message_chars: int = 8000
    # app
    app_env: str = "development"
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 5000
    secret_key: str = ""
    admin_token: str = ""
    cookie_secure: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(BASE_DIR / ".env")
        data_dir = _path("DATA_DIR", BASE_DIR / "data")
        env = os.getenv("APP_ENV", "development").lower()
        secret = os.getenv("SECRET_KEY", "")
        if secret in _PLACEHOLDER_SECRETS:
            if env == "production":
                raise RuntimeError("En producción debes definir SECRET_KEY con un valor propio.")
            secret = secrets.token_hex(32)  # desarrollo: las cookies se invalidan al reiniciar
        return cls(
            ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            model_name=os.getenv("MODEL_NAME", "llama3.2:3b"),
            llm_temperature=_float("LLM_TEMPERATURE", 0.7),
            llm_max_tokens=_int("LLM_MAX_TOKENS", 1024),
            llm_num_ctx=_int("LLM_NUM_CTX", 4096),
            llm_timeout_seconds=_int("LLM_TIMEOUT_SECONDS", 300),
            max_history_messages=_int("MAX_HISTORY_MESSAGES", 20),
            data_dir=data_dir,
            db_path=_path("DB_PATH", data_dir / "llm_interactions.db"),
            export_dir=_path("EXPORT_DIR", BASE_DIR / "exports"),
            inactivity_threshold_seconds=_int("INACTIVITY_THRESHOLD_SECONDS", 600),
            session_check_interval_seconds=_int("SESSION_CHECK_INTERVAL_SECONDS", 30),
            message_threshold=_int("MESSAGE_THRESHOLD", 8),
            export_schedule_minutes=_int("EXPORT_SCHEDULE_MINUTES", 0),
            export_timezone=os.getenv("EXPORT_TIMEZONE", "UTC"),
            retention_days=_int("RETENTION_DAYS", 0),
            app_env=env,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            host=os.getenv("HOST", "127.0.0.1"),
            port=_int("PORT", 5000),
            secret_key=secret,
            admin_token=os.getenv("ADMIN_TOKEN", ""),
            cookie_secure=os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes"),
        )
