"""Tool names a profile may grant: exactly the gateway registry's tools (002D)."""

from paw_server.modules.gateway.builtin import builtin_registry

KNOWN_TOOLS = frozenset(builtin_registry().names())
