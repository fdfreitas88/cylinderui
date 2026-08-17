#!/usr/bin/env bash
# =============================================================================
# CylinderUI - stop background services (router, agent, llama-swap)
# =============================================================================
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cyl_init_paths; cyl_load_env

stop_svc() {
  local name="$1" pf; pf="$(cyl_pidfile "$name")"
  if cyl_is_running "$name"; then
    local pid; pid="$(cat "$pf")"
    log "stopping $name (pid $pid)"
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do cyl_is_running "$name" || break; sleep 1; done
    cyl_is_running "$name" && { warn "$name still alive, sending SIGKILL"; kill -9 "$pid" 2>/dev/null || true; }
    ok "$name stopped"
  else
    log "$name not running"
  fi
  rm -f "$pf"
}

# Stop in reverse start order.
stop_svc router
stop_svc agent
stop_svc llama-swap
ok "all services stopped"
