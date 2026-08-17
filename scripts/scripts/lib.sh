#!/usr/bin/env bash
# =============================================================================
# CylinderUI for Llama.cpp - shared shell helpers (sourced, not executed)
# -----------------------------------------------------------------------------
# Platform/vendor detection, path layout, logging, .env loading and network
# confirmation. Sourced by install.sh, run.sh, stop.sh, status.sh, bench.sh.
#
# SECURITY: no secrets here. Every command that reaches the network goes
# through cyl_confirm() so the user approves it at runtime.
# =============================================================================

# ---- pretty logging ---------------------------------------------------------
cyl_c_reset=$'\033[0m'; cyl_c_blue=$'\033[34m'; cyl_c_green=$'\033[32m'
cyl_c_yellow=$'\033[33m'; cyl_c_red=$'\033[31m'; cyl_c_dim=$'\033[2m'
[ -t 1 ] || { cyl_c_reset=; cyl_c_blue=; cyl_c_green=; cyl_c_yellow=; cyl_c_red=; cyl_c_dim=; }

log()   { printf '%s[cyl]%s %s\n' "$cyl_c_blue" "$cyl_c_reset" "$*"; }
ok()    { printf '%s[ok ]%s %s\n' "$cyl_c_green" "$cyl_c_reset" "$*"; }
warn()  { printf '%s[warn]%s %s\n' "$cyl_c_yellow" "$cyl_c_reset" "$*" >&2; }
err()   { printf '%s[err]%s %s\n' "$cyl_c_red" "$cyl_c_reset" "$*" >&2; }
die()   { err "$*"; exit 1; }

# ---- path layout ------------------------------------------------------------
# SCRIPTS_DIR = .../cylinderui-scripts/scripts
# CYL_ROOT    = repo root (parent of cylinderui-scripts); override with CYL_REPO_ROOT
cyl_init_paths() {
  SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  PKG_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"                 # cylinderui-scripts/
  CYL_ROOT="${CYL_REPO_ROOT:-$(cd "$PKG_DIR/.." && pwd)}"  # repo root
  VENV_DIR="$CYL_ROOT/.venv"
  VENDOR_DIR="$CYL_ROOT/vendor"          # llama.cpp + llama-swap live here
  LLAMACPP_DIR="$VENDOR_DIR/llama.cpp"
  MODELS_DIR="${MODELS_DIR:-$CYL_ROOT/models}"  # honor a user MODELS_DIR override
  RUN_DIR="$CYL_ROOT/run"                # pid files
  LOG_DIR="$CYL_ROOT/logs"
  ROUTER_DIR="$CYL_ROOT/router"          # published today
  # Agent may or may not be present; probe both accepted names.
  AGENT_DIR=""
  for cand in "$CYL_ROOT/agent" "$CYL_ROOT/native-agent-v2"; do
    [ -d "$cand" ] && { AGENT_DIR="$cand"; break; }
  done
  mkdir -p "$VENDOR_DIR" "$MODELS_DIR" "$RUN_DIR" "$LOG_DIR"
}

# ---- .env loading -----------------------------------------------------------
cyl_load_env() {
  local envf="$PKG_DIR/.env"
  if [ -f "$envf" ]; then
    set -a; . "$envf"; set +a
  fi
  # Defaults (also defined in .env.example)
  : "${PROMPT_ROUTER_PORT:=8088}"
  : "${AGENT_PORT:=3000}"
  : "${LLAMA_SWAP_PORT:=8080}"
  : "${PROMPT_ROUTER_HOST:=0.0.0.0}"
  : "${LLAMA_SWAP_URL:=http://127.0.0.1:${LLAMA_SWAP_PORT}}"
  : "${ROUTER_URL:=http://127.0.0.1:${PROMPT_ROUTER_PORT}/v1}"
  : "${AGENT_MODEL:=qwen2.5-0.5b-instruct}"
  : "${MODEL_URL:=https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf}"
  : "${MODEL_FILE:=qwen2.5-0.5b-instruct-q4_k_m.gguf}"
  export PROMPT_ROUTER_PORT AGENT_PORT LLAMA_SWAP_PORT PROMPT_ROUTER_HOST \
         LLAMA_SWAP_URL ROUTER_URL AGENT_MODEL MODEL_URL MODEL_FILE
}

# ---- platform / vendor detection -------------------------------------------
# Sets: CYL_OS (macos|linux), CYL_ARCH (arm64|x86_64), CYL_ACCEL (metal|cuda|cpu)
cyl_detect() {
  local uname_s uname_m
  uname_s="$(uname -s)"; uname_m="$(uname -m)"
  case "$uname_s" in
    Darwin) CYL_OS="macos" ;;
    Linux)  CYL_OS="linux" ;;
    *)      CYL_OS="unknown" ;;
  esac
  case "$uname_m" in
    arm64|aarch64) CYL_ARCH="arm64" ;;
    x86_64|amd64)  CYL_ARCH="x86_64" ;;
    *)             CYL_ARCH="$uname_m" ;;
  esac

  # Force-CPU override honored by callers via CYL_FORCE_CPU=1
  if [ "${CYL_FORCE_CPU:-0}" = "1" ]; then
    CYL_ACCEL="cpu"
  elif [ "$CYL_OS" = "macos" ] && [ "$CYL_ARCH" = "arm64" ]; then
    CYL_ACCEL="metal"                       # Apple Silicon -> Metal
  elif [ "$CYL_OS" = "linux" ] && command -v nvidia-smi >/dev/null 2>&1 \
       && nvidia-smi >/dev/null 2>&1; then
    CYL_ACCEL="cuda"                        # NVIDIA GPU present -> CUDA
  else
    CYL_ACCEL="cpu"                         # macOS Intel / Linux no-GPU
  fi
  export CYL_OS CYL_ARCH CYL_ACCEL
}

# ---- cpu core count (cross platform) ---------------------------------------
cyl_ncpu() {
  if command -v nproc >/dev/null 2>&1; then nproc
  elif [ "$(uname -s)" = "Darwin" ]; then sysctl -n hw.ncpu
  else echo 4; fi
}

# ---- existing-install detection (idempotent, non-destructive) ---------------
# The installer must RESPECT what the user already has. These helpers locate an
# existing llama.cpp / llama-swap / models so we can REUSE them and skip any
# download or compile. Nothing here deletes or overwrites anything.

# Find an existing llama.cpp: requires BOTH llama-server and llama-bench.
# Order: env LLAMA_CPP_BIN (dir or file) / LLAMA_CPP_DIR -> PATH -> common paths.
# Echoes the directory that holds the binaries (empty + non-zero if none).
cyl_find_llamacpp() {
  local d exe bench
  if [ -n "${LLAMA_CPP_BIN:-}" ]; then
    d="$LLAMA_CPP_BIN"; [ -f "$d" ] && d="$(dirname "$d")"
    [ -x "$d/llama-server" ] && [ -x "$d/llama-bench" ] && { echo "$d"; return 0; }
  fi
  if [ -n "${LLAMA_CPP_DIR:-}" ]; then
    for d in "$LLAMA_CPP_DIR" "$LLAMA_CPP_DIR/build/bin" "$LLAMA_CPP_DIR/bin"; do
      [ -x "$d/llama-server" ] && [ -x "$d/llama-bench" ] && { echo "$d"; return 0; }
    done
  fi
  exe="$(command -v llama-server 2>/dev/null || true)"
  bench="$(command -v llama-bench 2>/dev/null || true)"
  [ -n "$exe" ] && [ -n "$bench" ] && { dirname "$exe"; return 0; }
  for d in /usr/local/bin /opt/homebrew/bin "$HOME/llama.cpp/build/bin" \
           "$HOME/llama.cpp/bin" "./llama.cpp/build/bin" \
           "${VENDOR_DIR:-}/llama.cpp/build/bin"; do
    [ -x "$d/llama-server" ] && [ -x "$d/llama-bench" ] && { echo "$d"; return 0; }
  done
  return 1
}

# Find an existing llama-swap binary.
# Order: env LLAMA_SWAP_BIN -> PATH -> common paths. Echoes the binary path.
cyl_find_llamaswap() {
  local p c
  if [ -n "${LLAMA_SWAP_BIN:-}" ] && [ -x "$LLAMA_SWAP_BIN" ]; then echo "$LLAMA_SWAP_BIN"; return 0; fi
  p="$(command -v llama-swap 2>/dev/null || true)"
  [ -n "$p" ] && { echo "$p"; return 0; }
  for c in /usr/local/bin/llama-swap /opt/homebrew/bin/llama-swap \
           "$HOME/llama-swap" "$HOME/.local/bin/llama-swap" \
           "${VENDOR_DIR:-}/llama-swap"; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

# Detect whether a models dir already holds .gguf files. Echoes the dir if so.
cyl_models_present() {
  local d f; d="${1:-$MODELS_DIR}"
  [ -d "$d" ] || return 1
  f="$(find "$d" -maxdepth 1 -type f -name '*.gguf' 2>/dev/null | head -1)"
  [ -n "$f" ] && { echo "$d"; return 0; }
  return 1
}

# Preserve-or-create a config file. NEVER overwrites an existing user config:
#   - missing  -> copy from the .example (current behavior)
#   - existing -> keep INTACT, only point at the .example for new options
cyl_preserve_config() {  # cyl_preserve_config <target> <example>
  local target="$1" example="$2"
  if [ -f "$target" ]; then
    ok "config preservada: $target (inalterada; compare com $(basename "$example") p/ novas opções)"
    return 0
  fi
  [ -f "$example" ] && { cp "$example" "$target"; ok "criado $target (a partir de $(basename "$example"))"; }
}

# ---- network confirmation gate ---------------------------------------------
# Usage: cyl_confirm "human description" -- prints command, asks y/N.
# Auto-yes when CYL_ASSUME_YES=1 (set by --yes / CI). Returns 0 to proceed.
cyl_confirm() {
  local desc="$1"
  if [ "${CYL_ASSUME_YES:-0}" = "1" ]; then
    log "auto-approve (--yes): $desc"; return 0
  fi
  printf '%s[net]%s About to: %s\n' "$cyl_c_yellow" "$cyl_c_reset" "$desc"
  printf '      Proceed with this network operation? [y/N] '
  local ans; read -r ans || ans=""
  case "$ans" in [yY]|[yY][eE][sS]) return 0 ;; *) warn "skipped by user"; return 1 ;; esac
}

# ---- downloader (curl or wget), gated by cyl_confirm at call site -----------
cyl_fetch() {
  local url="$1" out="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --progress-bar "$url" -o "$out"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --show-progress -O "$out" "$url"
  else
    die "neither curl nor wget found; install one to download files"
  fi
}

# ---- port helper ------------------------------------------------------------
cyl_port_busy() {  # cyl_port_busy <port> -> 0 if something is listening
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":$p "
  else
    return 1
  fi
}

# ---- pid helpers ------------------------------------------------------------
cyl_pidfile() { echo "$RUN_DIR/$1.pid"; }
cyl_is_running() {
  local pf; pf="$(cyl_pidfile "$1")"
  [ -f "$pf" ] || return 1
  local pid; pid="$(cat "$pf" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
