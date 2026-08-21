"""Local, privacy-preserving voice transcription for Telegram gateway messages."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class VoiceTranscriptionConfigurationError(ValueError):
    pass


class VoiceTranscriptionError(RuntimeError):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class VoiceTranscriptionSettings:
    base_url: str
    request_timeout_seconds: int
    queue_timeout_seconds: int
    language: str | None
    profile: str | None
    max_audio_bytes: int


@dataclass(frozen=True)
class VoiceTranscription:
    text: str
    language: str | None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class EccoVoxClient:
    """Use EccoVox only through a loopback HTTP endpoint and a shared endpoint lock."""

    _gates: dict[str, threading.BoundedSemaphore] = {}
    _gates_lock = threading.Lock()

    def transcribe(self, path: Path, settings: VoiceTranscriptionSettings) -> VoiceTranscription:
        if not path.is_file() or path.is_symlink():
            raise VoiceTranscriptionError("invalid_audio")
        if path.stat().st_size > settings.max_audio_bytes:
            raise VoiceTranscriptionError("audio_too_large")
        gate = self._gate(settings.base_url)
        if not gate.acquire(timeout=settings.queue_timeout_seconds):
            raise VoiceTranscriptionError("queue_timeout", retryable=True)
        try:
            return self._post(path, settings)
        finally:
            gate.release()

    @classmethod
    def _gate(cls, base_url: str) -> threading.BoundedSemaphore:
        with cls._gates_lock:
            return cls._gates.setdefault(base_url, threading.BoundedSemaphore(1))

    @staticmethod
    def _post(path: Path, settings: VoiceTranscriptionSettings) -> VoiceTranscription:
        boundary = "----akuma-voice-" + secrets.token_hex(16)
        fields = {"responseFormat": "json"}
        if settings.language:
            fields["language"] = settings.language
        if settings.profile:
            fields["profile"] = settings.profile
        body = _multipart_body(boundary, fields, path)
        request = Request(
            settings.base_url + "/v1/audio/transcriptions",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
        )
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=settings.request_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            details = _error_payload(exc)
            raise VoiceTranscriptionError(str(details.get("code") or "runtime_unavailable"),
                                          bool(details.get("retryable"))) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise VoiceTranscriptionError("runtime_unavailable", retryable=True) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise VoiceTranscriptionError("empty_transcription")
        language = payload.get("language") if isinstance(payload, dict) else None
        return VoiceTranscription(text.strip(), language if isinstance(language, str) else None)


def voice_transcription_settings(config: dict[str, Any]) -> VoiceTranscriptionSettings | None:
    value = config.get("voice_transcription")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise VoiceTranscriptionConfigurationError("voice_transcription must be an object")
    if value.get("enabled") is not True:
        return None
    provider = value.get("provider", "eccovox")
    if provider != "eccovox":
        raise VoiceTranscriptionConfigurationError("voice_transcription.provider must be eccovox")
    base_url = str(value.get("base_url") or "http://127.0.0.1:8870").rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise VoiceTranscriptionConfigurationError("voice_transcription.base_url must be a plain http loopback URL")
    if (parsed.hostname or "").casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise VoiceTranscriptionConfigurationError("voice_transcription.base_url must use a loopback host")
    if not parsed.port:
        raise VoiceTranscriptionConfigurationError("voice_transcription.base_url must include a port")
    timeout = _positive_int(value.get("request_timeout_seconds", 120), "voice_transcription.request_timeout_seconds", 1, 300)
    queue_timeout = _positive_int(value.get("queue_timeout_seconds", 180), "voice_transcription.queue_timeout_seconds", 1, 900)
    maximum = _positive_int(value.get("max_audio_bytes", 10 * 1024 * 1024), "voice_transcription.max_audio_bytes", 1, 10 * 1024 * 1024)
    language = _optional_string(value.get("language", "pt-BR"), "voice_transcription.language")
    profile = _optional_string(value.get("profile", "balanced"), "voice_transcription.profile")
    return VoiceTranscriptionSettings(base_url, timeout, queue_timeout, language, profile, maximum)


def _multipart_body(boundary: str, fields: dict[str, str], path: Path) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode("utf-8"), b"\r\n"))
    chunks.extend((
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: audio/ogg\r\n\r\n",
        path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ))
    return b"".join(chunks)


def _error_payload(error: HTTPError) -> dict[str, Any]:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return detail if isinstance(detail, dict) else payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _positive_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise VoiceTranscriptionConfigurationError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise VoiceTranscriptionConfigurationError(f"{name} must be a non-empty string of at most 128 characters or null")
    return value.strip()
