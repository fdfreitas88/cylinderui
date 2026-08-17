# CylinderUI for Llama.cpp — install & benchmark scripts

Turn-key, multi-platform scripts to install, run and benchmark the CylinderUI
local LLM console. See **[INSTALL.md](../docs/INSTALL.md)** for the full guide.

## Files

| File | Purpose |
|------|---------|
| `install.sh` | macOS/Linux installer: detect OS/arch/accelerator, Python venv, build/fetch llama.cpp (Metal/CUDA/CPU), llama-swap, initial model, generate configs, start services. **Non-destructive**: detects & reuses an existing llama.cpp/llama-swap/models and never overwrites user configs. Flags: `--cpu --no-model --dev --yes --force-llama --force-swap --model URL --restart`. |
| `install.ps1` | Windows/PowerShell equivalent (pre-compiled llama.cpp release). Also `-Action run|stop|status`. Same non-destructive detection/reuse. Flags: `-Cpu -NoModel -Dev -Yes -ForceLlama -ForceSwap -Model URL -Restart`. |
| `scripts/lib.sh` | Shared bash helpers: detection, paths, `.env`, network-confirm gate. |
| `scripts/run.sh` / `stop.sh` / `status.sh` | Service lifecycle (PIDs in `run/`, logs in `logs/`). |
| `scripts/bench.sh` / `bench.ps1` | Cross-platform `llama-bench` wrapper (profiles `rapido`/`medio`/`detalhado`, `--json`). |
| `Makefile` / `justfile` | Targets: `install run stop status update bench clean`. |
| `requirements.txt` | Agent (FastAPI) deps — example, adjustable. |
| `.env.example` / `router-config.example.json` | Config templates, placeholders only. |
| `../docs/INSTALL.md` | Prerequisites, ports, start/stop, models, benchmark, troubleshooting. |

## Quick start

```bash
# macOS / Linux
./install.sh
# Windows
pwsh -File .\install.ps1
# then open http://localhost:8088
```

## Benchmark

```bash
scripts/bench.sh <MODEL.gguf> [rapido|medio|detalhado] [--json]
```

Detects platform + vendor and offloads correctly: Metal/CUDA use `-ngl 99`, CPU
uses `-ngl 0 -t <cores>`. Prints **pp** (prompt tok/s) and **tg** (generation
tok/s).

## Notes

- The **agent** (`native-agent-v2`) is **not in this repo yet**; router + UI work
  without it, but Visões / Model Store / Benchmark UI / RAG need it. Drop it in
  `agent/` or `native-agent-v2/` and re-run.
- **No secrets** are committed; every network download is confirmed at runtime.
- **Respects an existing install**: llama.cpp / llama-swap / models already on
  your machine are detected and reused, and existing configs (`.env`,
  `router-config.json`, `vendor/llama-swap.yaml`) are never overwritten. See
  *Installing over an existing environment* in [INSTALL.md](../docs/INSTALL.md).
