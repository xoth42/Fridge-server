"""Shared HTTP client for Fridge-server integration tests.

NetClient is a thin urllib-based HTTP client with Basic auth and JSON bodies,
reusable across all test suites (testui, testslack, testdata).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request


class NetClient:
    """HTTP client with Basic auth for talking to Grafana and the alert-api."""

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 20) -> None:
        self.base = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._timeout = timeout

    @classmethod
    def from_env(
        cls,
        base_url: str,
        user_var: str = "GF_ADMIN_USER",
        pass_var: str = "GF_ADMIN_PASSWORD",
        timeout: int = 20,
    ) -> "NetClient":
        """Construct from environment variables."""
        user = os.environ.get(user_var, "")
        password = os.environ.get(pass_var, "")
        return cls(base_url, user, password, timeout)

    def _req(
        self,
        method: str,
        path: str,
        payload: object = None,
        extra_headers: dict[str, str] | None = None,
    ) -> object:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers: dict[str, str] = {
            "Authorization": self._auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url=url, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {method} {path}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error {method} {path}: {exc}") from exc

    def get(self, path: str) -> object:
        return self._req("GET", path)

    def post(self, path: str, payload: object = None, extra_headers: dict[str, str] | None = None) -> object:
        return self._req("POST", path, payload, extra_headers)

    def put(self, path: str, payload: object = None, extra_headers: dict[str, str] | None = None) -> object:
        return self._req("PUT", path, payload, extra_headers)

    def patch(self, path: str, payload: object = None) -> object:
        return self._req("PATCH", path, payload)

    def delete(self, path: str) -> object:
        return self._req("DELETE", path)
