#!/usr/bin/env bash
# Writes runtime secrets into HERMES_HOME/.env (mode 600) from environment variables, then execs the command.
# Nothing secret exists in the image. The API listener key is required; the model key depends on the provider.
set -euo pipefail
: "${API_SERVER_KEY:?API_SERVER_KEY is required (>= 32 random chars)}"
[ "${#API_SERVER_KEY}" -ge 32 ] || { echo "API_SERVER_KEY must be at least 32 characters" >&2; exit 1; }

mkdir -p "$HERMES_HOME"
umask 077
{
  echo "API_SERVER_ENABLED=true"
  echo "API_SERVER_KEY=${API_SERVER_KEY}"
  echo "API_SERVER_HOST=${API_SERVER_HOST:-0.0.0.0}"   # reachable only on the private network / loopback publish
  echo "API_SERVER_PORT=${API_SERVER_PORT:-8642}"
  [ -z "${OPENAI_API_KEY:-}" ] || echo "OPENAI_API_KEY=${OPENAI_API_KEY}"
  [ -z "${OPENROUTER_API_KEY:-}" ] || echo "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"
} > "$HERMES_HOME/.env"

# Optional one-time config applied with the documented CLI, e.g. HERMES_CONFIG_SET="model=gpt-4o-mini;k=v"
if [ -n "${HERMES_CONFIG_SET:-}" ]; then
  IFS=';' read -ra pairs <<< "$HERMES_CONFIG_SET"
  for kv in "${pairs[@]}"; do hermes config set "${kv%%=*}" "${kv#*=}"; done
fi
# MCP server (S0 stub now, the PAW gateway in 002D). Written between markers so every start replaces it.
#   PAW_MCP_URL, PAW_MCP_TOKEN (static bearer), PAW_MCP_TRUST (full|untrusted, default full), PAW_MCP_INCLUDE (csv)
sed -i '/^# >>> paw-mcp$/,/^# <<< paw-mcp$/d' "$HERMES_HOME/config.yaml"
if [ -n "${PAW_MCP_URL:-}" ]; then
  : "${PAW_MCP_TOKEN:?PAW_MCP_TOKEN is required with PAW_MCP_URL}"
  {
    echo "# >>> paw-mcp"
    echo "mcp_servers:"
    echo "  paw:"
    echo "    url: \"${PAW_MCP_URL}\""
    echo "    headers:"
    echo "      Authorization: \"Bearer ${PAW_MCP_TOKEN}\""
    echo "    trust: ${PAW_MCP_TRUST:-full}"
    echo "    timeout: ${PAW_MCP_TIMEOUT:-60}"
    echo "    connect_timeout: ${PAW_MCP_CONNECT_TIMEOUT:-10}"
    echo "    tools:"
    [ -z "${PAW_MCP_INCLUDE:-}" ] || echo "      include: [${PAW_MCP_INCLUDE}]"
    echo "      resources: false   # drops list_resources / read_resource (not removed by include)"
    echo "      prompts: false     # drops list_prompts / get_prompt"
    echo "    sampling:"
    echo "      enabled: false     # the server may not make Hermes run model calls (default is on)"
    echo "    elicitation:"
    echo "      enabled: false     # the server may not pause a run to ask for input (default is on)"
    echo "# <<< paw-mcp"
  } >> "$HERMES_HOME/config.yaml"
fi

# PAW guard plugin (backup only; see plugins/paw-guard). PAW_HERMES_GUARD=1 installs and enables it.
if [ "${PAW_HERMES_GUARD:-1}" = "1" ]; then
  mkdir -p "$HERMES_HOME/plugins"
  cp -r /opt/paw-plugins/paw-guard "$HERMES_HOME/plugins/"
  hermes plugins enable paw-guard
fi

# Lockdown (spec D205/D208, verified in S0 on Hermes c7da41d). Hermes ships with ~14 native toolsets ON for the API
# platform (terminal, file, code_execution, browser, web, memory, session_search, delegation, cron ...) and its own
# memory feature persists facts across runs. PAW must be the only authority, so turn them all off at every start.
# Opt out only for experiments: PAW_HERMES_LOCKDOWN=0. The real compiler is 002C; this is the S0 stand-in.
if [ "${PAW_HERMES_LOCKDOWN:-1}" = "1" ]; then
  hermes config set memory.memory_enabled false
  hermes config set memory.user_profile_enabled false
  names=$(hermes tools list --platform api_server 2>&1 | grep "✓ enabled" | awk '{print $3}' | tr '\n' ' ')
  # shellcheck disable=SC2086
  [ -z "$names" ] || hermes tools disable --platform api_server $names
  left=$(hermes tools list --platform api_server 2>&1 | grep -c "✓ enabled" || true)
  [ "$left" = "0" ] || { echo "lockdown failed: $left native toolsets still enabled" >&2; exit 1; }
fi
exec "$@"
