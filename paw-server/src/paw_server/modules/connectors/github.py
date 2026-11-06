"""GitHub App transport. Only this module handles installation tokens and the App key.

The configured account is an operator-approved binding. Installation IDs and repository
permissions are resolved from GitHub, never trusted from redirect parameters or model text.
"""

import base64
import binascii
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

ACCOUNT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
READ_PERMISSIONS = {"contents": "read", "pull_requests": "read", "checks": "read", "issues": "read"}


class GitHubError(Exception):
    def __init__(self, code: str, retry_after: int | None = None) -> None:
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


def repository_name(value: str) -> str:
    parts = value.split("/")
    if len(parts) != 2 or not ACCOUNT.fullmatch(parts[0]) or not REPOSITORY.fullmatch(parts[1]):
        raise GitHubError("invalid_repository")
    return value.lower()


class GitHubProvider:
    def __init__(
        self, app_id: int, account: str, key_file: str, http: httpx.Client | None = None
    ) -> None:
        if app_id < 1 or not ACCOUNT.fullmatch(account):
            raise GitHubError("not_configured")
        self.app_id, self.account, self.key_file = app_id, account, key_file
        self.http = http or httpx.Client(
            base_url="https://api.github.com", timeout=15, follow_redirects=False
        )
        self._tokens: dict[tuple[int, int | None, bool], tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _jwt(self) -> str:
        try:
            return jwt.encode(
                {
                    "iat": int(time.time()) - 30,
                    "exp": int(time.time()) + 480,
                    "iss": str(self.app_id),
                },
                Path(self.key_file).read_bytes(),
                algorithm="RS256",
            )
        except Exception:
            raise GitHubError("key_unavailable") from None

    def _request(
        self, method: str, path: str, token: str, payload: dict[str, Any] | None = None
    ) -> Any:
        try:
            with self.http.stream(
                method,
                path,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise GitHubError("response_too_large")
        except httpx.HTTPError:
            raise GitHubError("unavailable") from None
        if response.status_code in (403, 429):
            if (
                response.status_code == 429
                or response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
            ):
                wait = response.headers.get("retry-after", "")
                raise GitHubError("rate_limited", min(int(wait), 3600) if wait.isdigit() else None)
            raise GitHubError("forbidden")
        if response.status_code in (401, 404):
            self.clear_tokens()
            raise GitHubError("revoked" if response.status_code == 401 else "not_found")
        if response.status_code >= 500:
            raise GitHubError("unavailable")
        if response.status_code not in (200, 201, 204):
            raise GitHubError("rejected")
        if response.status_code == 204:
            return {}
        try:
            return json.loads(body)
        except ValueError:
            raise GitHubError("invalid_response") from None

    def clear_tokens(self) -> None:
        with self._lock:
            self._tokens.clear()

    def installation(self) -> dict[str, Any]:
        try:
            result = self._request("GET", f"/users/{self.account}/installation", self._jwt())
        except GitHubError as exc:
            if exc.code == "not_found":
                raise GitHubError("not_installed") from None
            raise
        if (
            not isinstance(result, dict)
            or result.get("app_id") != self.app_id
            or not isinstance(result.get("account"), dict)
            or str(result["account"].get("login", "")).lower() != self.account.lower()
            or result.get("suspended_at") is not None
            or type(result.get("id")) is not int
        ):
            raise GitHubError("installation_mismatch")
        return result

    def token(self, installation: int, repo_id: int | None = None, *, write: bool = False) -> str:
        cache_key = (installation, repo_id, write)
        with self._lock:
            cached = self._tokens.get(cache_key)
            if cached and cached[1] > time.time() + 90:
                return cached[0]
        permissions = {**READ_PERMISSIONS, "metadata": "read"}
        if write:
            permissions["issues"] = "write"
        payload: dict[str, Any] = {"permissions": permissions}
        if repo_id is not None:
            payload["repository_ids"] = [repo_id]
        result = self._request(
            "POST", f"/app/installations/{installation}/access_tokens", self._jwt(), payload
        )
        try:
            from datetime import datetime

            token = result["token"]
            expires = datetime.fromisoformat(
                result["expires_at"].replace("Z", "+00:00")
            ).timestamp()
            if not isinstance(token, str) or not token or expires <= time.time() + 90:
                raise ValueError
        except (TypeError, KeyError, ValueError):
            raise GitHubError("invalid_response") from None
        with self._lock:
            self._tokens[cache_key] = (token, expires)
        return token

    def repositories(self, installation: int) -> list[dict[str, Any]]:
        token, out = self.token(installation), []
        for page in range(1, 11):
            response = self._request(
                "GET", f"/installation/repositories?per_page=100&page={page}", token
            )
            rows = response.get("repositories") if isinstance(response, dict) else None
            if not isinstance(rows, list):
                raise GitHubError("invalid_response")
            for row in rows:
                if not isinstance(row, dict) or type(row.get("id")) is not int:
                    raise GitHubError("invalid_response")
                name = repository_name(str(row.get("full_name", "")))
                out.append(
                    {
                        "id": row["id"],
                        "full_name": name,
                        "private": bool(row.get("private")),
                        "html_url": row.get("html_url"),
                        "default_branch": row.get("default_branch"),
                    }
                )
            if len(rows) < 100:
                return out
        raise GitHubError("too_many_repositories")

    def read(
        self, installation: int, repo: dict[str, Any], tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        root = "/repos/" + repository_name(repo["full_name"])
        token = self.token(installation, repo["id"])
        limit = int(args.get("limit", 20))
        if tool == "github_get_file":
            path = str(args["path"])
            if (
                path.startswith("/")
                or any(part in ("..", ".", "") for part in path.split("/"))
                or "\\" in path
            ):
                raise GitHubError("invalid_path")
            ref = str(args.get("ref") or repo.get("default_branch") or "HEAD")
            data = self._request(
                "GET",
                root + "/contents/" + quote(path, safe="/") + "?ref=" + quote(ref, safe=""),
                token,
            )
            if (
                not isinstance(data, dict)
                or data.get("type") != "file"
                or data.get("encoding") != "base64"
                or int(data.get("size", 0)) > 128 * 1024
            ):
                raise GitHubError("unsupported_file")
            try:
                raw = base64.b64decode("".join(str(data.get("content", "")).split()), validate=True)
                if len(raw) > 128 * 1024:
                    raise ValueError
                text = raw.decode("utf-8")
            except (UnicodeError, ValueError, binascii.Error):
                raise GitHubError("unsupported_file") from None
            return {
                "repository": repo["full_name"],
                "path": path,
                "ref": ref,
                "sha": data.get("sha"),
                "text": text,
                "url": data.get("html_url"),
            }
        if tool == "github_get_pr":
            data = self._request("GET", root + f"/pulls/{int(args['number'])}", token)
            return {"repository": repo["full_name"], "pull_request": _pull(data)}
        if tool == "github_get_checks":
            ref = quote(str(args["ref"]), safe="")
            data = self._request("GET", root + f"/commits/{ref}/check-runs?per_page={limit}", token)
            return {
                "repository": repo["full_name"],
                "checks": [
                    {k: r.get(k) for k in ("id", "name", "status", "conclusion", "html_url")}
                    for r in data.get("check_runs", [])
                ],
            }
        path = "/pulls" if tool == "github_list_prs" else "/issues"
        data = self._request(
            "GET", root + path + f"?state={args.get('state', 'open')}&per_page={limit}", token
        )
        if not isinstance(data, list):
            raise GitHubError("invalid_response")
        if tool == "github_list_prs":
            return {"repository": repo["full_name"], "pull_requests": [_pull(r) for r in data]}
        return {
            "repository": repo["full_name"],
            "issues": [
                {k: r.get(k) for k in ("number", "title", "body", "state", "html_url")}
                for r in data
                if "pull_request" not in r
            ],
        }

    def create_issue(
        self, installation: int, repo: dict[str, Any], args: dict[str, Any], operation: str
    ) -> dict[str, Any]:
        root = "/repos/" + repository_name(repo["full_name"])
        data = self._request(
            "POST",
            root + "/issues",
            self.token(installation, repo["id"], write=True),
            {
                "title": args["title"],
                "body": args["body"] + "\n\n<!-- paw-op:" + operation + " -->",
            },
        )
        return _issue(data)

    def find_issue(
        self, installation: int, repo: dict[str, Any], operation: str
    ) -> dict[str, Any] | None:
        root = "/repos/" + repository_name(repo["full_name"])
        marker = "<!-- paw-op:" + operation + " -->"
        token = self.token(installation, repo["id"])
        for page in range(1, 11):
            rows = self._request(
                "GET",
                root + f"/issues?state=all&sort=created&direction=desc&per_page=100&page={page}",
                token,
            )
            if not isinstance(rows, list):
                raise GitHubError("invalid_response")
            for row in rows:
                if (
                    isinstance(row, dict)
                    and "pull_request" not in row
                    and marker in str(row.get("body") or "")
                ):
                    return _issue(row)
            if len(rows) < 100:
                return None
        raise GitHubError("reconciliation_incomplete")


def _issue(data: Any) -> dict[str, Any]:
    if (
        not isinstance(data, dict)
        or type(data.get("number")) is not int
        or not isinstance(data.get("html_url"), str)
    ):
        raise GitHubError("invalid_response")
    return {"number": data["number"], "url": data["html_url"]}


def _pull(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            k: row.get(k)
            for k in ("number", "title", "body", "state", "draft", "html_url", "merged")
        },
        "head_sha": (row.get("head") or {}).get("sha"),
        "base": (row.get("base") or {}).get("ref"),
    }
