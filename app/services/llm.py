"""Cliente del motor de inferencia. Interfaz mínima (health/chat) para poder cambiar de motor
o de modelo sin tocar el resto de la aplicación. Implementación por defecto: Ollama."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests


class LLMError(Exception):
    """Error del motor con un mensaje comprensible para el usuario."""

    def __init__(self, user_message: str, kind: str = "generic"):
        super().__init__(user_message)
        self.user_message = user_message
        self.kind = kind  # connection | model_missing | timeout | empty | http | generic


@dataclass
class LLMResult:
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class OllamaClient:
    def __init__(self, base_url: str, model: str, temperature: float, max_tokens: int,
                 num_ctx: int, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.num_ctx = num_ctx
        self.timeout = timeout

    def _model_installed(self, names: list[str]) -> bool:
        return self.model in names or (":" not in self.model and f"{self.model}:latest" in names)

    def health(self) -> dict:
        info = {"model": self.model, "service_available": False, "model_installed": False, "error": None}
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=3)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            info["service_available"] = True
            info["model_installed"] = self._model_installed(names)
            if not info["model_installed"]:
                info["error"] = f"El modelo '{self.model}' no está instalado. Ejecuta: ollama pull {self.model}"
        except requests.RequestException:
            info["error"] = f"No se pudo conectar con Ollama en {self.base_url}. Inicia el servicio (ollama serve)."
        except ValueError:
            info["error"] = "Ollama devolvió una respuesta que no se pudo interpretar."
        return info

    def chat(self, messages: list[dict]) -> LLMResult:
        payload = {
            "model": self.model, "messages": messages, "stream": False,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens, "num_ctx": self.num_ctx},
        }
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        except requests.ConnectionError:
            raise LLMError(f"No se pudo conectar con Ollama en {self.base_url}. ¿Está en ejecución (ollama serve)?", "connection")
        except requests.Timeout:
            raise LLMError("El modelo tardó demasiado en responder. Prueba un modelo más pequeño o aumenta LLM_TIMEOUT_SECONDS.", "timeout")
        except requests.RequestException:
            raise LLMError("Error de comunicación con el motor de inferencia.", "connection")

        if r.status_code == 404:
            raise LLMError(f"El modelo '{self.model}' no está instalado. Ejecuta: ollama pull {self.model}", "model_missing")
        if r.status_code >= 400:
            raise LLMError(f"El motor de inferencia respondió con error HTTP {r.status_code}.", "http")
        try:
            data = r.json()
            text = (data.get("message") or {}).get("content", "")
        except ValueError:
            raise LLMError("El motor devolvió una respuesta que no se pudo interpretar.", "http")
        if not text.strip():
            raise LLMError("El modelo devolvió una respuesta vacía.", "empty")
        return LLMResult(text=text, input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"))
