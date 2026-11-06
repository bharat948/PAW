"""GitHub tool contracts. Repository scope is enforced in the policy and again at the connector."""

from typing import Any

from paw_server.modules.connectors.github import GitHubError
from paw_server.modules.connectors.github_service import GitHubService
from paw_server.modules.gateway.registry import ToolSpec
from paw_server.modules.gateway.tools import ToolError

REPO = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$",
    "maxLength": 140,
}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}
STATE = {"type": "string", "enum": ["open", "closed", "all"], "default": "open"}


def github_specs(service: GitHubService | None = None) -> list[ToolSpec]:
    contracts: list[tuple[str, str, dict[str, Any], list[str]]] = [
        (
            "github_list_repos",
            "List the connected GitHub repositories this actor may read.",
            {},
            [],
        ),
        (
            "github_get_file",
            "Read a text file at a GitHub repository ref, with its source SHA.",
            {
                "repository": REPO,
                "path": {"type": "string", "minLength": 1, "maxLength": 500},
                "ref": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            ["repository", "path"],
        ),
        (
            "github_list_prs",
            "List pull requests in an allowed repository.",
            {"repository": REPO, "state": STATE, "limit": LIMIT},
            ["repository"],
        ),
        (
            "github_get_pr",
            "Read a pull request and its head SHA.",
            {"repository": REPO, "number": {"type": "integer", "minimum": 1}},
            ["repository", "number"],
        ),
        (
            "github_get_checks",
            "Read check runs for a commit or ref.",
            {
                "repository": REPO,
                "ref": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": LIMIT,
            },
            ["repository", "ref"],
        ),
        (
            "github_list_issues",
            "List issues in an allowed repository.",
            {"repository": REPO, "state": STATE, "limit": LIMIT},
            ["repository"],
        ),
    ]
    specs = []
    for name, description, properties, required in contracts:

        def handle(ctx: Any, args: dict[str, Any], tool: str = name) -> dict[str, Any]:
            if service is None:
                raise ToolError("GitHub is not configured.")
            try:
                return service.read(
                    ctx.requester, tool, args, scopes=getattr(ctx, "resource_scopes", None)
                )
            except GitHubError as exc:
                raise ToolError(
                    {
                        "out_of_scope": "Repository access is not allowed.",
                        "rate_limited": "GitHub rate limit reached. Try later.",
                        "revoked": "GitHub access was revoked. Reconnect it.",
                        "not_connected": "GitHub is not connected.",
                        "needs_reauth": "Reconnect GitHub.",
                        "not_found": "The GitHub resource was not found.",
                    }.get(exc.code, "GitHub could not complete this request.")
                ) from None

        specs.append(
            ToolSpec(
                name=name,
                description=description,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                provider="github",
                handler=handle,
            )
        )
    specs.append(
        ToolSpec(
            name="github_create_issue",
            description="Request owner approval to create an issue in the configured sandbox repository. Does not create an issue immediately.",
            input_schema={
                "type": "object",
                "properties": {
                    "repository": REPO,
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "body": {"type": "string", "maxLength": 10000},
                },
                "required": ["repository", "title", "body"],
                "additionalProperties": False,
            },
            provider="github",
            side_effect_class="external_write",
            approval_required=True,
        )
    )
    return specs
