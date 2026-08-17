#!/usr/bin/env bash
# =============================================================================
# CylinderUI - cross-platform llama-bench wrapper (macOS + Linux)
# -----------------------------------------------------------------------------
# Detects platform + accelerator (Metal / CUDA / CPU) and builds the RIGHT
# llama-bench command line, then prints prompt (pp) and generation (tg) tok/s.
# Mirrors the agent's model_bench.py profiles.
#
# Usage:
#   scripts/bench.sh <MODEL.gguf> [rapido|medio|detalhado] [--json] [-t N] [--llama-bench PATH]
#
# Profiles:
#   rapido    : current config, -p 512 -n 128                 (1 run)
#   medio     : thread sweep -t 4,8,12,... up to core count   (CPU tuning)
#   detalhado : thread sweep x prompt-length sweep (512, 2048) (deep)
#
# Command lines per platform/vendor (offload flag -ngl differs):
#   Apple Silicon (Metal): llama-bench -m M -ngl 99 -p 512 -n 128
#   macOS Intel  (CPU)   : llama-bench -m M -ngl 0  -t <ncpu> -p 512 -n 128
#   Linux NVIDIA (CUDA)  : llama-bench -m M -ngl 99 -p 512 -n 128
#   Linux CPU            : llama-bench -m M -ngl 0  -t <nproc> -p 512 -n 128
# =============================================================================
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cyl_init_paths; cyl_load_env; cyl_detect

# ---- args -------------------------------------------------------------------
MODEL=""; PROFILE="rapido"; JSON=0; THREADS=""; LLAMA_BENCH="${LLAMA_BENCH:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --json)        JSON=1 ;;
    -t)            THREADS="$2"; shift ;;
    --llama-bench) LLAMA_BENCH="$2"; shift ;;
    --cpu)         CYL_FORCE_CPU=1; cyl_detect ;;
    rapido|medio|detalhado) PROFILE="$1" ;;
    -h|--help)     sed -n '2,25p' "$0"; exit 0 ;;
    *) [ -z "$MODEL" ] && MODEL="$1" || die "unexpected arg: $1" ;;
  esac
  shift
done

# ---- locate model -----------------------------------------------------------
[ -n "$MODEL" ] || MODEL="$MODELS_DIR/$MODEL_FILE"
[ -f "$MODEL" ] || { [ -f "$MODELS_DIR/$MODEL" ] && MODEL="$MODELS_DIR/$MODEL"; }
[ -f "$MODEL" ] || die "model not found: $MODEL (pass a path or put a .gguf in $MODELS_DIR)"

# ---- locate llama-bench -----------------------------------------------------
if [ -z "$LLAMA_BENCH" ]; then
  for cand in \
    "$LLAMACPP_DIR/build/bin/llama-bench" \
    "$VENDOR_DIR/llama-bench" \
    "$(command -v llama-bench 2>/dev/null || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && { LLAMA_BENCH="$cand"; break; }
  done
fi
[ -n "$LLAMA_BENCH" ] && [ -x "$LLAMA_BENCH" ] || die "llama-bench not found; build llama.cpp (install.sh) or pass --llama-bench PATH"

# ---- accelerator -> -ngl ----------------------------------------------------
case "$CYL_ACCEL" in
  metal|cuda) NGL=99 ;;   # full offload to GPU
  *)          NGL=0  ;;   # CPU
esac
NCPU="$(cyl_ncpu)"
[ -z "$THREADS" ] && THREADS="$NCPU"

log "model=$(basename "$MODEL")  platform=$CYL_OS/$CYL_ARCH  accel=$CYL_ACCEL  ngl=$NGL  ncpu=$NCPU  profile=$PROFILE"
log "llama-bench: $LLAMA_BENCH"

# ---- thread-sweep list (powers up to core count) ----------------------------
thread_list() {
  local out="" t=4
  while [ "$t" -lt "$NCPU" ]; do out="$out,$t"; t=$((t*2)); done
  out="${out#,},$NCPU"; echo "${out#,}"
}

# ---- build the -t / -p arguments per profile --------------------------------
# On GPU (metal/cuda) thread count barely matters; keep a single -t for medio.
TARG=""; PARG="512"; NARG="128"
case "$PROFILE" in
  rapido)
    [ "$NGL" = "0" ] && TARG="-t $THREADS"
    ;;
  medio)
    if [ "$NGL" = "0" ]; then TARG="-t $(thread_list)"; else TARG="-t $THREADS"; fi
    ;;
  detalhado)
    if [ "$NGL" = "0" ]; then TARG="-t $(thread_list)"; else TARG="-t $THREADS"; fi
    PARG="512,2048"    # context/prompt-length sweep
    ;;
  *) die "unknown profile: $PROFILE" ;;
esac

# ---- assemble + run ---------------------------------------------------------
# shellcheck disable=SC2086
CMD=( "$LLAMA_BENCH" -m "$MODEL" -ngl "$NGL" $TARG -p "$PARG" -n "$NARG" )
[ "$JSON" = "1" ] && CMD+=( -o json )

echo
log "command: ${CMD[*]}"
echo
if [ "$JSON" = "1" ]; then
  "${CMD[@]}"     # raw JSON to stdout (feed to the agent /api/bench)
else
  # Human-readable llama-bench table already prints pp/tg tok/s columns.
  "${CMD[@]}"
  echo
  ok "Columns: 'pp' = prompt (prefill) tok/s, 'tg' = generation tok/s."
fi
