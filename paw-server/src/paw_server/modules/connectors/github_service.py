"""Owner-scoped GitHub connection. Every read revalidates installation and selected repositories."""

from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.connectors import store
from paw_server.modules.connectors.github import GitHubError, GitHubProvider, repository_name

PROVIDER = "github"
READ_TOOLS = (
    "github_list_repos",
    "github_get_file",
    "github_list_prs",
    "github_get_pr",
    "github_get_checks",
    "github_list_issues",
)


class GitHubService:
    def __init__(
        self, pool: ConnectionPool[Any], settings: Settings, provider: GitHubProvider
    ) -> None:
        self.pool, self.settings, self.provider = pool, settings, provider

    def connect(self, owner: str, expected_installation: int | None = None) -> dict[str, Any]:
        installation = self.provider.installation()
        if expected_installation is not None and installation["id"] != expected_installation:
            raise GitHubError("installation_mismatch")
        repos = self.provider.repositories(installation["id"])
        if not repos:
            raise GitHubError("no_repositories")
        with self.pool.connection() as conn:
            connection_id = store.save_connection(
                conn,
                self.settings.connector_key,
                owner=owner,
                provider=PROVIDER,
                email=self.provider.account,
                scopes=[r["full_name"] for r in repos],
                secrets={
                    "installation_id": installation["id"],
                    "app_id": self.provider.app_id,
                    "repositories": repos,
                },
            )
        return {"connected": True, "connection_id": str(connection_id), "repositories": repos}

    def _connection(self, owner: str) -> tuple[Any, dict[str, Any]]:
        with self.pool.connection() as conn:
            live = store.get_live(conn, owner, PROVIDER)
            data = store.load_secrets(conn, self.settings.connector_key, live.id) if live else None
        if live is None:
            raise GitHubError("not_connected")
        if live.status != "active" or data is None:
            raise GitHubError("needs_reauth")
        if (
            data.get("app_id") != self.provider.app_id
            or live.account_email.lower() != self.provider.account.lower()
        ):
            raise GitHubError("needs_reauth")
        return live, data

    def _current(self, owner: str) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
        live, data = self._connection(owner)
        try:
            installation = self.provider.installation()
            if installation["id"] != data.get("installation_id"):
                raise GitHubError("installation_mismatch")
            current = self.provider.repositories(installation["id"])
        except GitHubError as exc:
            if exc.code in ("not_installed", "revoked", "installation_mismatch"):
                self.disconnect(owner)
                raise GitHubError("revoked") from None
            raise
        saved = {(r["id"], r["full_name"]) for r in data.get("repositories", [])}
        permitted = [r for r in current if (r["id"], r["full_name"]) in saved]
        return live, data, permitted

    def read(
        self, owner: str, tool: str, args: dict[str, Any], *, scopes: list[str] | None = None
    ) -> dict[str, Any]:
        if tool not in READ_TOOLS:
            raise GitHubError("unknown_tool")
        live, data, repos = self._current(owner)
        if scopes is not None:
            allowed = {repository_name(s) for s in scopes}
            repos = [r for r in repos if r["full_name"] in allowed]
        if tool == "github_list_repos":
            result = {"repositories": repos}
        else:
            name = repository_name(str(args.get("repository", "")))
            repo = next((r for r in repos if r["full_name"] == name), None)
            if repo is None:
                raise GitHubError("out_of_scope")
            result = self.provider.read(data["installation_id"], repo, tool, args)
        # Disconnect during a provider call fences result delivery as well as the next call.
        with self.pool.connection() as conn:
            current = store.get_live(conn, owner, PROVIDER)
        if not current or current.id != live.id or current.status != "active":
            raise GitHubError("revoked")
        return result

    def connection_for_write(
        self, owner: str, connection_id: UUID, repository: str
    ) -> tuple[int, dict[str, Any]]:
        live, data, repos = self._current(owner)
        name = repository_name(repository)
        if name != self.settings.github_sandbox_repository.lower() or live.id != connection_id:
            raise GitHubError("out_of_scope")
        repo = next((r for r in repos if r["full_name"] == name), None)
        if repo is None:
            raise GitHubError("out_of_scope")
        return data["installation_id"], repo

    def disconnect(self, owner: str) -> None:
        self.provider.clear_tokens()
        with self.pool.connection() as conn:
            live = store.get_live(conn, owner, PROVIDER)
            if live:
                store.set_status(conn, live.id, "revoked")


def build_github(pool: ConnectionPool[Any], settings: Settings) -> GitHubService | None:
    if not (
        settings.github_app_id
        and settings.github_account
        and settings.github_app_slug
        and settings.github_private_key_file
        and settings.connector_key
    ):
        return None
    return GitHubService(
        pool,
        settings,
        GitHubProvider(
            settings.github_app_id, settings.github_account, settings.github_private_key_file
        ),
    )
