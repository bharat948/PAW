"""Is spending enforcement actually able to enforce? Names the failing checks and nothing else: no values, no keys.

Enforcement is the model proxy (reserve, forward, settle) plus what it depends on: the database, an active profile
with usable budgets, a rate card and a token counter for every model that can be called, and the provider key.
Readiness fails closed: if any of it is missing, runs must not be reported as ready to spend money.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.costs import service as costs
from paw_server.modules.costs import tokens
from paw_server.modules.costs.pricing import CostError
from paw_server.modules.identity import service as identity

log = logging.getLogger("paw.costs")


def enforcement_problems(pool: ConnectionPool[Any], settings: Settings) -> list[str]:
    """Empty when enforcement is ready. Never raises."""
    if not settings.model_proxy_token:
        return ["model_proxy_not_configured"]
    problems: list[str] = []
    if not settings.model_proxy_upstream_key:
        problems.append("upstream_key")
    try:
        with pool.connection() as conn:
            active = identity.get_active(conn, identity.ADMIN)
            models = {settings.agent_summary_model}
            if active is None:
                problems.append("profile")
            else:
                models.add(active.compiled.config_fragment["model"])
                try:
                    budgets = active.compiled.policy["budgets"]
                    if any(Decimal(str(budgets[k])) <= 0 for k in ("per_run_usd", "daily_usd")):
                        raise InvalidOperation
                except (KeyError, TypeError, ArithmeticError):
                    problems.append("budget")
            for model in sorted(models):
                try:
                    costs.rate_for(conn, model)
                except CostError:
                    problems.append("pricing")
                    break
            conn.commit()
        for model in sorted(models):
            try:
                tokens.encoding_for(model)
            except Exception:  # CostError, or the encoding file could not be loaded
                problems.append("tokenizer")
                break
    except Exception as exc:
        log.error("enforcement readiness check failed", extra={"error": type(exc).__name__})
        problems.append("database")
    return problems


def gateway_enforcement(settings: Settings) -> list[str]:
    """Ask the running gateway (which hosts the model proxy) whether it is enforcing. Unreachable is a problem."""
    host = "127.0.0.1" if settings.gateway_host in ("0.0.0.0", "") else settings.gateway_host  # noqa: S104
    try:
        r = httpx.get(f"http://{host}:{settings.gateway_port}/_ready", timeout=2.0)
    except httpx.HTTPError:
        return ["proxy_unreachable"]
    if r.status_code == 200:
        return []
    try:
        found = r.json().get("problems")
        names = [str(p)[:40] for p in found][:10] if isinstance(found, list) else []
    except ValueError:
        names = []
    return names or ["proxy_not_ready"]
