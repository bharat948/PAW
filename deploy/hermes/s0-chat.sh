#!/usr/bin/env bash
# Talk to the S0 Hermes container from a terminal and watch what it does.
#   deploy/hermes/s0-chat.sh /path/to/s0.env "your message"
# Shows tool calls and the final answer. This is the raw runtime, NOT PAW chat (no PAW identity, memory or approvals).
set -euo pipefail
ENVF="${1:?usage: s0-chat.sh /path/to/s0.env \"message\"}"; MSG="${2:?message required}"
KEY=$(grep '^API_SERVER_KEY=' "$ENVF" | cut -d= -f2-); URL="${PAW_HERMES_URL:-http://127.0.0.1:8642}"
H="Authorization: Bearer $KEY"
BODY=$(python3 -c 'import json,sys;print(json.dumps({"input":sys.argv[1]}))' "$MSG")
ID=$(curl -fsS -H "$H" -H 'Content-Type: application/json' -d "$BODY" "$URL/v1/runs" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')
echo "run $ID"
curl -fsSN -H "$H" "$URL/v1/runs/$ID/events" | grep --line-buffered '^data:' | python3 -c '
import sys, json
for line in sys.stdin:
    d = json.loads(line[5:]); e = d["event"]
    if e == "message.delta": sys.stdout.write(d.get("delta", "")); sys.stdout.flush()
    elif e.startswith("tool."): print("\n[%s] %s %s" % (e, d.get("tool"), d.get("preview", "")))
    elif e == "approval.request": print("\n[approval requested: the run will wait; stop it or deny]")
    elif e.startswith("run."): print("\n[%s]" % e)'
curl -fsS -H "$H" "$URL/v1/runs/$ID" | python3 -c 'import sys,json;d=json.load(sys.stdin);u=d.get("usage",{});print("status:",d["status"],"| tokens in/out:",u.get("input_tokens"),"/",u.get("output_tokens"))'
