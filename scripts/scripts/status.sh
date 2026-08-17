#!/usr/bin/env bash
# =============================================================================
# CylinderUI - show status of services + detected platform
# =============================================================================
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cyl_init_paths; cyl_load_env; cyl_detect

echo "Platform : $CYL_OS / $CYL_ARCH / accel=$CYL_ACCEL"
echo "Root     : $CYL_ROOT"
echo "Agent    : ${AGENT_DIR:-<not in repo>}"
echo

row() {  # row <name> <port>
  local name="$1" port="$2" state pid=""
  if cyl_is_running "$name"; then pid="$(cat "$(cyl_pidfile "$name")")"; state="UP  "; else state="DOWN"; fi
  local portinfo="free"; cyl_port_busy "$port" && portinfo="listening"
  printf "  %-11s %s  port %-5s (%s) %s\n" "$name" "$state" "$port" "$portinfo" "${pid:+pid $pid}"
}
echo "Services:"
row router     "$PROMPT_ROUTER_PORT"
row agent      "$AGENT_PORT"
row llama-swap "$LLAMA_SWAP_PORT"
echo
echo "UI: http://localhost:${PROMPT_ROUTER_PORT}"
