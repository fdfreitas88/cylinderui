#!/usr/bin/env bash
# =============================================================================
# CylinderUI for Llama.cpp - turn-key installer (macOS + Linux)
# -----------------------------------------------------------------------------
# What it does (idempotent & NON-DESTRUCTIVE -- re-run to update):
#   1. Detect OS (uname) + arch (arm64/x86_64) + accelerator (Metal/CUDA/CPU)
#   2. Ensure Python 3.11+ (brew on macOS / apt|dnf on Linux; never blind sudo)
#   3. Create .venv and install requirements (agent deps)
#   4. Get llama.cpp with the RIGHT acceleration -- but if the user ALREADY has
#      llama.cpp (PATH / env / common paths) it is DETECTED and REUSED, not rebuilt
#   5. Get llama-swap -- likewise REUSED if already present
#   6. Download 1 small initial GGUF model -- SKIPPED if the models dir already
#      has .gguf files (existing models are never touched)
#   7. .env / router-config.json: created from .example only if MISSING; an
#      existing user config is PRESERVED intact, never overwritten
#   8. Start services (router + agent-if-present + llama-swap) in background --
#      a port already in use is reused, not killed (see --restart)
#
# RESPECTS an existing install: never overwrites llama.cpp, llama-swap, models
# or user configs. Only creates/downloads/compiles what is missing.
#
# SECURITY: no secrets are written. Every network download is printed and
# confirmed at runtime (unless --yes). Package installs are never run under a
# blind sudo -- if privileges are missing the script tells you the command.
#
# Flags: --cpu (force CPU)  --no-model (skip model download)  --dev (do not
#        auto-start services)  --yes (auto-approve network ops)
#        --force-llama (reinstall llama.cpp even if detected)
#        --force-swap  (reinstall llama-swap even if detected)
#        --model URL   (add a specific GGUF even if models already exist)
#        --restart     (stop running services before starting)  --help
#
# Env overrides (reuse an existing install): LLAMA_CPP_DIR / LLAMA_CPP_BIN,
#        LLAMA_SWAP_BIN, MODELS_DIR.
# =============================================================================
set -euo pipefail

# shellcheck source=scripts/lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/lib.sh"

# ---- flags ------------------------------------------------------------------
CYL_FORCE_CPU=0; DO_MODEL=1; DEV_MODE=0; CYL_ASSUME_YES=0
CYL_FORCE_LLAMA=0; CYL_FORCE_SWAP=0; CYL_RESTART=0; CYL_MODEL_URL_OVERRIDE=""
usage() { sed -n '2,40p' "$0"; exit 0; }
while [ $# -gt 0 ]; do
  case "$1" in
    --cpu)          CYL_FORCE_CPU=1 ;;
    --no-model)     DO_MODEL=0 ;;
    --dev)          DEV_MODE=1 ;;
    --yes|-y)       CYL_ASSUME_YES=1 ;;
    --force-llama)  CYL_FORCE_LLAMA=1 ;;
    --force-swap)   CYL_FORCE_SWAP=1 ;;
    --restart)      CYL_RESTART=1 ;;
    --model)        shift; CYL_MODEL_URL_OVERRIDE="${1:-}" ;;
    --model=*)      CYL_MODEL_URL_OVERRIDE="${1#*=}" ;;
    -h|--help)      usage ;;
    *) die "unknown flag: $1 (see --help)" ;;
  esac
  shift
done
export CYL_FORCE_CPU CYL_ASSUME_YES CYL_FORCE_LLAMA CYL_FORCE_SWAP

cyl_init_paths
cyl_load_env
cyl_detect

log "OS=$CYL_OS  arch=$CYL_ARCH  accelerator=$CYL_ACCEL  root=$CYL_ROOT"
[ "$CYL_OS" = "unknown" ] && die "unsupported OS ($(uname -s)); use install.ps1 on Windows"

# =============================================================================
# 2. Python 3.11+
# =============================================================================
cyl_python() {
  local py=""
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      local v; v="$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
      case "$v" in 3.1[1-9]|3.[2-9][0-9]) py="$c"; break ;; esac
    fi
  done
  if [ -z "$py" ]; then
    warn "Python 3.11+ not found."
    if [ "$CYL_OS" = "macos" ]; then
      if command -v brew >/dev/null 2>&1; then
        cyl_confirm "brew install python@3.12" && brew install python@3.12 || true
      else
        die "Install Homebrew (https://brew.sh) then: brew install python@3.12"
      fi
    else
      if command -v apt-get >/dev/null 2>&1; then
        warn "Run: sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip"
      elif command -v dnf >/dev/null 2>&1; then
        warn "Run: sudo dnf install -y python3 python3-virtualenv python3-pip"
      fi
      die "install Python 3.11+ with your package manager, then re-run"
    fi
    # re-probe
    for c in python3.12 python3.11 python3; do command -v "$c" >/dev/null 2>&1 && { py="$c"; break; }; done
  fi
  [ -n "$py" ] || die "still no suitable Python; aborting"
  echo "$py"
}
PY="$(cyl_python)"
ok "python: $PY ($("$PY" --version 2>&1))"

# =============================================================================
# 3. virtualenv + requirements
# =============================================================================
if [ ! -d "$VENV_DIR" ]; then
  log "creating venv at $VENV_DIR"
  "$PY" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --quiet --upgrade pip
if [ -f "$PKG_DIR/requirements.txt" ]; then
  if cyl_confirm "pip install -r requirements.txt (agent deps)"; then
    python -m pip install -r "$PKG_DIR/requirements.txt" || warn "pip install had errors (ok if running router-only)"
  fi
fi
ok "venv ready"

# =============================================================================
# 4. llama.cpp with the right acceleration
# =============================================================================
install_llamacpp() {
  # Respect an existing install: detect llama.cpp on PATH / env / common paths.
  if [ "$CYL_FORCE_LLAMA" != "1" ]; then
    local found; found="$(cyl_find_llamacpp || true)"
    if [ -n "$found" ]; then
      ok "llama.cpp detectado em $found — usando o existente (--force-llama para reinstalar)"
      export CYL_LLAMACPP_BIN="$found"
      return 0
    fi
  fi
  local have_bin=""
  [ -x "$LLAMACPP_DIR/build/bin/llama-bench" ] && have_bin=1
  if [ -n "$have_bin" ] && [ "${CYL_FORCE_REBUILD:-0}" != "1" ] && [ "$CYL_FORCE_LLAMA" != "1" ]; then
    ok "llama.cpp already built ($LLAMACPP_DIR/build/bin) -- skipping (CYL_FORCE_REBUILD=1 to rebuild)"
    return 0
  fi
  command -v cmake >/dev/null 2>&1 || warn "cmake not found -- needed to build llama.cpp"
  command -v git   >/dev/null 2>&1 || die  "git required to fetch llama.cpp"

  if [ ! -d "$LLAMACPP_DIR/.git" ]; then
    cyl_confirm "git clone https://github.com/ggml-org/llama.cpp into vendor/" || { warn "skipping llama.cpp"; return 0; }
    git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMACPP_DIR"
  else
    cyl_confirm "git -C llama.cpp pull (update)" && git -C "$LLAMACPP_DIR" pull --ff-only || true
  fi

  local cmflags="-DGGML_NATIVE=ON"
  case "$CYL_ACCEL" in
    metal) cmflags="-DGGML_METAL=ON" ;;                 # Apple Silicon
    cuda)  cmflags="-DGGML_CUDA=ON" ;;                  # Linux NVIDIA
    cpu)
      if [ "$CYL_OS" = "macos" ]; then cmflags="-DGGML_METAL=OFF -DGGML_ACCELERATE=ON"  # Intel mac: Accelerate
      else cmflags="-DGGML_NATIVE=ON"; fi              # Linux CPU (OpenBLAS optional)
      ;;
  esac
  log "building llama.cpp ($CYL_ACCEL): cmake $cmflags"
  cmake -S "$LLAMACPP_DIR" -B "$LLAMACPP_DIR/build" $cmflags -DCMAKE_BUILD_TYPE=Release
  cmake --build "$LLAMACPP_DIR/build" --config Release -j --target llama-server llama-bench
  ok "llama.cpp built -> $LLAMACPP_DIR/build/bin"
}
install_llamacpp

# =============================================================================
# 5. llama-swap release binary
# =============================================================================
install_llamaswap() {
  local dest="$VENDOR_DIR/llama-swap"
  # Respect an existing install: detect llama-swap on PATH / env / common paths.
  if [ "$CYL_FORCE_SWAP" != "1" ]; then
    local found; found="$(cyl_find_llamaswap || true)"
    if [ -n "$found" ]; then
      ok "llama-swap detectado em $found — usando o existente (--force-swap para reinstalar)"
      export CYL_LLAMASWAP_BIN="$found"
      return 0
    fi
  fi
  if [ -x "$dest" ] && [ "${CYL_FORCE_REBUILD:-0}" != "1" ] && [ "$CYL_FORCE_SWAP" != "1" ]; then
    ok "llama-swap already present ($dest)"; return 0
  fi
  # Map platform -> release asset suffix (adjust if upstream naming changes).
  local os_tag arch_tag
  case "$CYL_OS"   in macos) os_tag="darwin" ;; linux) os_tag="linux" ;; esac
  case "$CYL_ARCH" in arm64) arch_tag="arm64" ;; x86_64) arch_tag="amd64" ;; *) arch_tag="$CYL_ARCH" ;; esac
  local base="https://github.com/mostlygeek/llama-swap/releases/latest/download"
  local asset="llama-swap_${os_tag}_${arch_tag}.tar.gz"
  log "llama-swap asset (verify at https://github.com/mostlygeek/llama-swap/releases): $asset"
  if cyl_confirm "download llama-swap release: $base/$asset"; then
    local tmp; tmp="$(mktemp -d)"
    if cyl_fetch "$base/$asset" "$tmp/$asset"; then
      tar -xzf "$tmp/$asset" -C "$tmp" || warn "extract failed -- check asset name at releases page"
      local found; found="$(find "$tmp" -type f -name 'llama-swap*' ! -name '*.tar.gz' | head -1)"
      if [ -n "$found" ]; then install -m 0755 "$found" "$dest"; ok "llama-swap -> $dest"; else warn "binary not found in archive; place it manually at $dest"; fi
    else
      warn "download failed; grab it manually from the releases page and place at $dest"
    fi
    rm -rf "$tmp"
  else
    warn "llama-swap skipped; model swapping/inference will not work until you place it at $dest"
  fi
}
install_llamaswap

# =============================================================================
# 6. initial GGUF model
# =============================================================================
if [ "$DO_MODEL" = "1" ]; then
  # Explicit --model URL always adds (never deletes) a specific GGUF.
  if [ -n "$CYL_MODEL_URL_OVERRIDE" ]; then
    MODEL_URL="$CYL_MODEL_URL_OVERRIDE"; MODEL_FILE="$(basename "$CYL_MODEL_URL_OVERRIDE")"
  fi
  present="$(cyl_models_present || true)"
  if [ -n "$present" ] && [ -z "$CYL_MODEL_URL_OVERRIDE" ]; then
    ok "modelos já presentes em $present — pulando download (use --model URL para adicionar)"
  else
    target="$MODELS_DIR/$MODEL_FILE"
    if [ -f "$target" ]; then
      ok "model already present: $target"
    else
      log "initial model: $MODEL_FILE"
      log "source URL (change via MODEL_URL in .env or --model): $MODEL_URL"
      if cyl_confirm "download GGUF model (~hundreds of MB): $MODEL_URL"; then
        cyl_fetch "$MODEL_URL" "$target" && ok "model -> $target" || warn "model download failed; retry or set MODEL_URL"
      fi
    fi
  fi
else
  warn "--no-model: skipping model download (place a .gguf in $MODELS_DIR yourself)"
fi

# =============================================================================
# 7. .env / router-config.json -- create if missing, PRESERVE if it exists
# -----------------------------------------------------------------------------
# Strategy per file: existing user config is kept intact (never overwritten);
# only missing files are seeded from the .example. cyl_preserve_config points
# you at the .example to compare for new options. The llama-swap.yaml is handled
# the same way by run.sh (generated only when absent -- your tuning is untouched).
# =============================================================================
cyl_preserve_config "$PKG_DIR/.env" "$PKG_DIR/.env.example"
if [ -n "$ROUTER_DIR" ] && [ -d "$ROUTER_DIR" ]; then
  cyl_preserve_config "$ROUTER_DIR/router-config.json" "$PKG_DIR/router-config.example.json"
else
  warn "router/ folder not found at $ROUTER_DIR -- copy the published router there"
fi

# =============================================================================
# 8. start services
# =============================================================================
if [ "$DEV_MODE" = "1" ]; then
  ok "install complete (--dev: services NOT started). Start with: scripts/run.sh"
else
  if [ "$CYL_RESTART" = "1" ]; then
    log "--restart: stopping any running services first"
    "$SCRIPTS_DIR/stop.sh" || true
  else
    for pp in "$PROMPT_ROUTER_PORT" "$AGENT_PORT" "$LLAMA_SWAP_PORT"; do
      cyl_port_busy "$pp" && warn "porta $pp já em uso — run.sh reutiliza o serviço existente (use --restart para reiniciar)"
    done
  fi
  log "starting services..."
  "$SCRIPTS_DIR/run.sh" || warn "run.sh reported an issue; check logs in $LOG_DIR"
fi

echo
ok  "CylinderUI install finished."
log "Open:  http://localhost:${PROMPT_ROUTER_PORT}"
if [ -z "$AGENT_DIR" ]; then
  warn "AGENT (native-agent-v2) NOT found in the repo. The router + UI work, but"
  warn "Visões, Model Store, Benchmark and RAG need the agent. Drop it in a folder"
  warn "named 'agent/' or 'native-agent-v2/' at the repo root, then re-run install.sh."
fi
