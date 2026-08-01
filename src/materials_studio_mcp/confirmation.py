from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any, Callable


DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 600


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Confirmation parameters must be finite JSON values") from exc
    return rendered.encode("utf-8")


def operation_hash(tool_name: str, parameters: dict[str, Any]) -> str:
    if not tool_name or not isinstance(parameters, dict):
        raise ValueError("tool_name and parameters object are required")
    return hashlib.sha256(_canonical_bytes({"tool": tool_name, "parameters": parameters})).hexdigest()


@dataclass
class _PendingConfirmation:
    tool_name: str
    operation_hash: str
    expires_at: int
    consumed: bool = False


class ConfirmationManager:
    """Issues short-lived opaque confirmations and consumes each exactly once."""

    def __init__(self, *, clock: Callable[[], float] = time.time, secret: bytes | None = None) -> None:
        self._clock = clock
        self._secret = secret or secrets.token_bytes(32)
        self._pending: dict[str, _PendingConfirmation] = {}
        self._lock = threading.Lock()

    def issue(self, tool_name: str, parameters: dict[str, Any], ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be an integer from 1 to {MAX_TTL_SECONDS}")
        digest = operation_hash(tool_name, parameters)
        now = int(self._clock())
        expires_at = now + ttl_seconds
        nonce = secrets.token_urlsafe(24)
        payload = {"v": 1, "nonce": nonce, "exp": expires_at, "op": digest}
        encoded = base64.urlsafe_b64encode(_canonical_bytes(payload)).rstrip(b"=").decode("ascii")
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        token = f"{encoded}.{signature}"
        with self._lock:
            self._purge_expired(now)
            self._pending[nonce] = _PendingConfirmation(tool_name, digest, expires_at)
        return {
            "confirmation_token": token,
            "tool_name": tool_name,
            "operation_hash": digest,
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            "ttl_seconds": ttl_seconds,
            "single_use": True,
        }

    def consume(self, token: str | None, tool_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if not token or not isinstance(token, str):
            raise PermissionError("A production confirmation token is required")
        try:
            encoded, supplied_signature = token.split(".", 1)
        except ValueError as exc:
            raise PermissionError("Malformed production confirmation token") from exc
        expected_signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise PermissionError("Invalid production confirmation token")
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except Exception as exc:
            raise PermissionError("Malformed production confirmation token") from exc
        now = int(self._clock())
        nonce = payload.get("nonce")
        requested_hash = operation_hash(tool_name, parameters)
        with self._lock:
            pending = self._pending.get(nonce)
            if pending is None or pending.consumed:
                raise PermissionError("Production confirmation token is unknown or already used")
            if now >= pending.expires_at or now >= int(payload.get("exp", 0)):
                self._pending.pop(nonce, None)
                raise PermissionError("Production confirmation token has expired")
            if pending.tool_name != tool_name:
                raise PermissionError("Production confirmation token is bound to a different tool")
            if pending.operation_hash != requested_hash or payload.get("op") != requested_hash:
                raise PermissionError("Production confirmation token parameters do not match")
            pending.consumed = True
        return {"confirmed": True, "tool_name": tool_name, "operation_hash": requested_hash, "consumed": True}

    def _purge_expired(self, now: int) -> None:
        for nonce in [key for key, item in self._pending.items() if now >= item.expires_at]:
            self._pending.pop(nonce, None)


confirmation_manager = ConfirmationManager()
