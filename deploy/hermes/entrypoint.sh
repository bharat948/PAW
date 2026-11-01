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
