"""Marcas temporales: UTC, ISO 8601 con milisegundos y zona explícita (+00:00)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_zone(value: str, tz_name: str) -> str:
    """Convierte un ISO UTC almacenado a otra zona (solo para exportación)."""
    if not value or tz_name.upper() == "UTC":
        return value
    return parse_iso(value).astimezone(ZoneInfo(tz_name)).isoformat(timespec="milliseconds")


def validate_tz(tz_name: str) -> None:
    ZoneInfo(tz_name)  # lanza ZoneInfoNotFoundError / ValueError si es inválida
