"""Gitea REST API client built on :mod:`httpx`.

Only the endpoints needed by the CLI are implemented. Authentication uses an
access token sent as ``Authorization: token <token>`` which Gitea accepts for
both the API and git-over-HTTP. ``httpx`` handles connection pooling, TLS,
redirects and (optional) proxy support from the environment.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import httpx

from . import __version__
from .errors import ConfigError, GiteaError


def _quote(value: str) -> str:
    return quote(str(value), safe="")


class GiteaClient:
    """Small synchronous Gitea API client."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        insecure: bool = False,
        no_proxy: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ConfigError("gitea url is required (--gitea-url or GITEA_URL)")

        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/api/v1"):
            self.base_url = self.base_url[: -len("/api/v1")]
        self.api_base = f"{self.base_url}/api/v1"
        self.token = token
        self.timeout = timeout
        self.insecure = insecure
        self.no_proxy = no_proxy

        headers = {
            "Accept": "application/json",
            "User-Agent": f"gitea-ocr-review/{__version__}",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout,
            verify=not insecure,
            # ``trust_env=False`` ignores HTTP(S)_PROXY / ALL_PROXY / NO_PROXY.
            trust_env=not no_proxy,
            follow_redirects=False,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GiteaClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any | None = None,
    ) -> Any:
        url = f"{self.api_base}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None} or None
        method = method.upper()

        try:
            response = self._client.request(method, url, params=clean, json=body)
        except httpx.HTTPError as exc:
            raise GiteaError(
                f"cannot reach Gitea at {self.base_url}: {exc}",
                url=url,
                hint="verify --gitea-url and your network/proxy settings",
            ) from exc

        if response.status_code >= 400:
            message = self._error_message(response.content) or response.reason_phrase
            if response.status_code == 401:
                hint = "check that the access token is valid and has repository write scope"
            elif response.status_code == 403:
                hint = "the token's user may lack write access to this repository"
            elif response.status_code == 404:
                hint = "check the repository name, pull request number and token scopes"
            else:
                hint = None
            raise GiteaError(
                f"Gitea API returned HTTP {response.status_code} "
                f"for {method} {path}: {message}",
                status=response.status_code,
                url=str(response.request.url),
                hint=hint,
            )

        if response.status_code >= 300:
            location = response.headers.get("location", "")
            raise GiteaError(
                f"Gitea API returned HTTP {response.status_code} for {method} {path}"
                + (f" -> {location}" if location else ""),
                status=response.status_code,
                url=str(response.request.url),
                hint=(
                    "the instance may live under a sub-path "
                    "(e.g. https://host/git) or redirect to a canonical URL"
                ),
            )

        if not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise GiteaError(
                f"Gitea returned a non-JSON response for {method} {path}: {exc}",
                url=str(response.request.url),
            ) from exc

    @staticmethod
    def _error_message(raw: bytes) -> str | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            text = raw.decode("utf-8", "replace").strip()
            return text[:300] or None
        if isinstance(payload, dict):
            message = payload.get("message")
            if message:
                return str(message)
            errors = payload.get("errors")
            if errors:
                return str(errors)
            return json.dumps(payload)[:300]
        return str(payload)[:300]

    # -- endpoints ---------------------------------------------------------

    def get_authenticated_user(self) -> dict[str, Any]:
        return self._request("GET", "/user") or {}

    def list_my_teams(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Teams the authenticated user belongs to (``GET /user/teams``)."""

        teams: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self._request(
                "GET", "/user/teams", params={"limit": limit, "page": page}
            )
            batch = list(result or [])
            teams.extend(batch)
            if len(batch) < limit:
                return teams
            page += 1

    def list_pulls(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        limit: int = 50,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"/repos/{_quote(owner)}/{_quote(repo)}/pulls",
            params={"state": state, "limit": limit, "page": page},
        )
        return list(result or [])

    def get_pull(self, owner: str, repo: str, index: int) -> dict[str, Any]:
        return self._request(
            "GET", f"/repos/{_quote(owner)}/{_quote(repo)}/pulls/{int(index)}"
        ) or {}

    def list_org_repos(
        self, org: str, *, limit: int = 50, page: int = 1
    ) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"/orgs/{_quote(org)}/repos",
            params={"limit": limit, "page": page},
        )
        return list(result or [])

    def list_user_repos(
        self, user: str, *, limit: int = 50, page: int = 1
    ) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"/users/{_quote(user)}/repos",
            params={"limit": limit, "page": page},
        )
        return list(result or [])

    def list_reviews(
        self, owner: str, repo: str, index: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Every review of a pull request, following pagination.

        Pending ``REQUEST_REVIEW`` rows are the newest ones, so stopping at the
        first page could hide a request and bypass a review.
        """

        reviews: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self._request(
                "GET",
                f"/repos/{_quote(owner)}/{_quote(repo)}/pulls/{int(index)}/reviews",
                params={"limit": limit, "page": page},
            )
            batch = list(result or [])
            reviews.extend(batch)
            if len(batch) < limit:
                return reviews
            page += 1

    def list_issue_comments(
        self, owner: str, repo: str, index: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Conversation comments on a pull request (``/issues/{n}/comments``)."""

        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self._request(
                "GET",
                f"/repos/{_quote(owner)}/{_quote(repo)}/issues/{int(index)}/comments",
                params={"limit": limit, "page": page},
            )
            batch = list(result or [])
            comments.extend(batch)
            if len(batch) < limit:
                return comments
            page += 1

    def list_review_comments(
        self, owner: str, repo: str, index: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Line comments left by pull-request reviews.

        GitHub serves them all at ``/pulls/{n}/comments``; Gitea returns 404
        there (verified on 1.26), so fall back to walking every review and its
        per-review comment list instead.
        """

        comments: list[dict[str, Any]] = []
        try:
            page = 1
            while True:
                result = self._request(
                    "GET",
                    f"/repos/{_quote(owner)}/{_quote(repo)}/pulls/{int(index)}/comments",
                    params={"limit": limit, "page": page},
                )
                batch = list(result or [])
                comments.extend(batch)
                if len(batch) < limit:
                    return comments
                page += 1
        except GiteaError as exc:
            if exc.status != 404:
                raise

        for review in self.list_reviews(owner, repo, index, limit=limit):
            review_id = review.get("id")
            if not review_id:
                continue
            page = 1
            while True:
                result = self._request(
                    "GET",
                    f"/repos/{_quote(owner)}/{_quote(repo)}/pulls/{int(index)}"
                    f"/reviews/{int(review_id)}/comments",
                    params={"limit": limit, "page": page},
                )
                batch = list(result or [])
                comments.extend(batch)
                if len(batch) < limit:
                    break
                page += 1
        return comments

    def search_review_requested(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Pull requests whose review is requested from the authenticated user.

        A single call across every repository, with Gitea itself expanding team
        requests to the team's members. This replaces one ``/reviews`` lookup
        per pull request.
        """

        issues: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self._request(
                "GET",
                "/repos/issues/search",
                params={
                    "state": "open",
                    "type": "pulls",
                    "review_requested": "true",
                    "limit": limit,
                    "page": page,
                },
            )
            batch = list(result or [])
            issues.extend(batch)
            if len(batch) < limit:
                return issues
            page += 1

    def create_review(
        self,
        owner: str,
        repo: str,
        index: int,
        *,
        event: str,
        body: str,
        comments: Iterable[Mapping[str, Any]] | None = None,
        commit_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"event": event}
        if body:
            payload["body"] = body
        if comments:
            payload["comments"] = list(comments)
        if commit_id:
            payload["commit_id"] = commit_id
        return self._request(
            "POST",
            f"/repos/{_quote(owner)}/{_quote(repo)}/pulls/{int(index)}/reviews",
            body=payload,
        ) or {}
