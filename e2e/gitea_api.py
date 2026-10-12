"""Small, dependency-free Gitea API helper used by the e2e harness.

This is intentionally independent of the application's own client so the test
verifies the real HTTP behaviour rather than sharing code with the code under
test.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


# The e2e helpers create and delete data, so they must only ever talk to the
# disposable local instance. This guard refuses anything else outright.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Api:
    def __init__(self, url: str, token: str | None = None, *, insecure: bool = True) -> None:
        host = urllib.parse.urlparse(url).hostname
        if host not in LOCAL_HOSTS:
            raise RuntimeError(
                f"refusing to use {url!r}: the e2e helpers only ever talk to a "
                "local throwaway Gitea, never a real server"
            )
        self.base = url.rstrip("/") + "/api/v1"
        self.token = token
        context = ssl.create_default_context()
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )

    def request(self, method: str, path: str, body: Any | None = None) -> Any:
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                message = json.loads(raw).get("message", raw)
            except ValueError:
                message = raw
            raise ApiError(exc.code, message) from exc

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self.request("POST", path, body)

    def put(self, path: str, body: Any = None) -> Any:
        return self.request("PUT", path, body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)


def b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")
