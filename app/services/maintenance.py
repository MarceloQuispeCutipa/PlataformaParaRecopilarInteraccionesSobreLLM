"""Hilo de mantenimiento en el servidor: cierre por inactividad, retención y exportación programada.
Se ejecuta independientemente de que haya pestañas del navegador abiertas."""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from ..timeutil import iso, parse_iso
from . import exporter

log = logging.getLogger("tracker.maintenance")


class Maintenance:
    def __init__(self, db, cfg, clock, sessions):
        self.db, self.cfg, self.clock, self.sessions = db, cfg, clock, sessions
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="maintenance", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.cfg.session_check_interval_seconds):
            self.run_once()

    def run_once(self):
        try:
            n = self.sessions.sweep_inactive(self.clock())
            if n:
                log.info("Sesiones cerradas por inactividad: %s", n)
        except Exception as e:
            log.error("Fallo en el barrido de inactividad: %s", e)
        try:
            self.purge_retention()
        except Exception as e:
            log.error("Fallo al aplicar la retención: %s", e)
        try:
            self.scheduled_export()
        except Exception as e:
            log.error("Fallo en la exportación programada: %s", e)

    def purge_retention(self) -> int:
        if self.cfg.retention_days <= 0:
            return 0
        cutoff = iso(self.clock() - timedelta(days=self.cfg.retention_days))
        with self.db.tx() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE last_activity_at < ? AND conversation_id NOT IN"
                               " (SELECT conversation_id FROM sessions WHERE status='active')", (cutoff,))
            if cur.rowcount:
                log.info("Retención: %s conversaciones eliminadas (>%s días)", cur.rowcount, self.cfg.retention_days)
            return cur.rowcount

    def scheduled_export(self) -> bool:
        """Solo si el administrador configuró EXPORT_SCHEDULE_MINUTES > 0."""
        minutes = self.cfg.export_schedule_minutes
        if minutes <= 0:
            return False
        last = exporter.last_exports(self.db, 1)
        if last and last[0]["status"] == "ok" and self.clock() - parse_iso(last[0]["started_at"]) < timedelta(minutes=minutes):
            return False
        exporter.export_to_dir(self.db, self.cfg, self.clock, requested_by="scheduler")
        return True
