"""Small JSON HTTP transport shared by provider adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HttpError(RuntimeError):
    """A remote API returned an error or an invalid response."""

    status: int | None
    message: str

    def __str__(self) -> str:
        prefix = f"HTTP {self.status}" if self.status is not None else "HTTP"
        return f"{prefix}: {self.message}"


class JsonHttpTransport:
    """Perform bounded JSON requests using only the Python standard library."""

    def __init__(self, *, timeout_seconds: float = 30) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Return a decoded JSON response, or ``None`` for an empty body."""

        encoded = None
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode()
        request = urllib.request.Request(
            url,
            data=encoded,
            headers=dict(headers or {}),
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                response_body = response.read()
        except urllib.error.HTTPError as error:
            response_body = error.read()
            raise HttpError(
                error.code,
                _error_message(response_body, str(error.reason)),
            ) from error
        except urllib.error.URLError as error:
            raise HttpError(None, str(error.reason)) from error

        if not response_body:
            return None
        try:
            return json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpError(None, "remote API returned invalid JSON") from error


def _error_message(response_body: bytes, fallback: str) -> str:
    try:
        payload = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    if isinstance(payload, Mapping) and isinstance(payload.get("message"), str):
        return payload["message"]
    return fallback
