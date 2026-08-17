# Installing CylinderUI for Llama.cpp

Turn-key, multi-platform install of the CylinderUI local LLM console.

> **Note on the AGENT.** Today only the **router** (`router/`) is published in
> this repo. The **agent** (`native-agent-v2`, FastAPI on port 3000) is **not in
> the repo yet**. The installer and UI work without it, but the following
> features need the agent: **Visões**, **Model Store**, **Benchmark UI**, and
> **RAG**. When the agent is available, drop it into a folder named `agent/` or
> `native-agent-v2/` at the repo root and re-run the installer — it is detected
> automatically. The standalone `scripts/bench.sh` / `scripts/bench.ps1` run
> **without** the agent.

---

## Architecture & ports

```
browser → router:8088 → agent:3000 → llama-swap:8080 → llama.cpp
```

| Component   | Port | What it is                                                        |
|-------------|------|-------------------------------------------------------------------|
| router      | 8088 | `prompt-router` (Python stdlib). Serves the UI, proxies `/api/*`. **The URL you open.** |
| agent       | 3000 | `native-agent-v2` (FastAPI). Chat, `/api/visions`, `/api/store`, `/api/models`, `/api/bench`, RAG. Loopback only. |
| llama-swap  | 8080 | Model swapper; fronts inference.                                  |
| llama.cpp   |  —   | Provides `llama-server` and `llama-bench`.                         |

---

## Prerequisites

- **macOS**: [Homebrew](https://brew.sh) (for Python), Xcode command line tools
  (`xcode-select --install`) for building llama.cpp with Metal. `cmake`, `git`.
- **Linux**: `python3` 3.11+ (`apt`/`dnf`), `git`, `cmake`, a compiler
  (`build-essential`). For GPU: NVIDIA driver + CUDA toolkit (`nvidia-smi` must work).
- **Windows**: [PowerShell 7+](https://github.com/PowerShell/PowerShell),
  Python 3.11+ (`winget install Python.Python.3.12`). GPU: NVIDIA driver so
  `nvidia-smi` works. No compiler needed — the installer pulls a pre-compiled
  llama.cpp release.

All installers **print every network download and ask for confirmation** before
running it (pass `--yes` / `-Yes` to auto-approve in CI). No secrets are ever
written; configs are generated from `.example` files with placeholders only.

---

## What the installer does

1. Detects **OS** (`uname`), **arch** (arm64/x86_64), and **accelerator**:
   - macOS Apple Silicon → **Metal** (`cmake -DGGML_METAL=ON`)
   - macOS Intel → **CPU** (Accelerate)
   - Linux + `nvidia-smi` → **CUDA** (`cmake -DGGML_CUDA=ON`), else **CPU**
   - Windows + `nvidia-smi` → **CUDA** release, else **CPU** release
2. Ensures **Python 3.11+** (brew / apt / dnf / winget — never a blind `sudo`).
3. Creates **`.venv`** and installs `requirements.txt` (agent deps).
4. Builds/downloads **llama.cpp** with the right acceleration — **unless you
   already have it**, in which case it is detected and reused (see below).
5. Downloads **llama-swap** release binary — **reused if already present**.
6. Downloads **1 small GGUF model** (default: Qwen2.5-0.5B-Instruct Q4; change
   via `MODEL_URL` in `.env`) — **skipped if the models dir already has `.gguf`**.
7. **`.env`** and **`router-config.json`**: created from the `.example` files
   **only if missing** — an existing config is **preserved intact**.
8. Starts **router + agent (if present) + llama-swap** in the background and
   prints **http://localhost:8088**.

Idempotent and **non-destructive** — re-run to update; it never overwrites an
existing llama.cpp, llama-swap, model or config. Set `CYL_FORCE_REBUILD=1` (or
`make update`) to rebuild the bundled llama.cpp / refresh binaries.

---

## Quick start

### macOS / Linux
```bash
cd cylinderui-scripts
./install.sh                 # full install + start (asks before each download)
# flags: --cpu  --no-model  --dev  --yes
#        --force-llama  --force-swap  --model URL  --restart
```

### Windows
```powershell
cd cylinderui-scripts
pwsh -File .\install.ps1      # -Cpu  -NoModel  -Dev  -Yes
#                              -ForceLlama  -ForceSwap  -Model URL  -Restart
```

### With make / just
```bash
make install       # or: just install
make status
make bench PROFILE=medio
```

---

## Start / stop / status

| Action | macOS/Linux                | Windows                                   | make / just    |
|--------|----------------------------|-------------------------------------------|----------------|
| start  | `scripts/run.sh`           | `pwsh -File install.ps1 -Action run`      | `make run`     |
| stop   | `scripts/stop.sh`          | `pwsh -File install.ps1 -Action stop`     | `make stop`    |
| status | `scripts/status.sh`        | `pwsh -File install.ps1 -Action status`   | `make status`  |

PIDs live in `run/`, logs in `logs/` (`router.log`, `agent.log`, `llama-swap.log`).

---

## Changing / downloading models

- Edit `MODEL_URL` and `MODEL_FILE` in `.env`, then re-run the installer (or just
  `cyl_fetch` the file into `models/`).
- Any `.gguf` placed in `models/` can be served; add it to `vendor/llama-swap.yaml`
  under `models:` so llama-swap can spawn it.
- The default model is a small ~0.5B instruct model so the first run is fast.
  Point `MODEL_URL` at any Hugging Face GGUF you prefer.

---

## Benchmark

The benchmark runs `llama-bench` with the **right command line per
platform/vendor** and prints **pp** (prompt/prefill tok/s) and **tg**
(generation tok/s).

```bash
# macOS / Linux
scripts/bench.sh <MODEL.gguf> [rapido|medio|detalhado] [--json] [-t N]

# Windows
pwsh -File scripts\bench.ps1 <MODEL.gguf> [rapido|medio|detalhado] [-Json] [-Threads N]
```

If `<MODEL.gguf>` is omitted, it uses `models/$MODEL_FILE`. If `llama-bench` is
not on `PATH`, it is found under `vendor/llama.cpp/build/bin` (or pass
`--llama-bench PATH` / `-LlamaBench`).

**Profiles** (mirror the agent's `model_bench.py`):

| Profile     | What it does                                              |
|-------------|----------------------------------------------------------|
| `rapido`    | Current config: `-p 512 -n 128` (one run).               |
| `medio`     | Thread sweep `-t 4,8,12,…` up to core count (CPU tuning). |
| `detalhado` | Thread sweep × prompt-length sweep (`-p 512,2048`).       |

**Command lines the wrapper builds** (offload flag `-ngl` differs by target):

| Target                       | Command                                                        |
|------------------------------|----------------------------------------------------------------|
| Apple Silicon (macOS, Metal) | `llama-bench -m M -ngl 99 -p 512 -n 128`                        |
| macOS Intel (CPU)            | `llama-bench -m M -ngl 0 -t <ncpu> -p 512 -n 128`              |
| Linux NVIDIA (CUDA)          | `llama-bench -m M -ngl 99 -p 512 -n 128`                        |
| Linux CPU                    | `llama-bench -m M -ngl 0 -t <nproc> -p 512 -n 128`            |
| Windows CUDA                 | `llama-bench.exe -m M -ngl 99 -p 512 -n 128`                    |
| Windows CPU                  | `llama-bench.exe -m M -ngl 0 -t <NUMBER_OF_PROCESSORS> -p 512 -n 128` |

Threads auto-detect via `nproc` / `sysctl -n hw.ncpu` / `NUMBER_OF_PROCESSORS`.
Add `--json` / `-Json` to emit machine-readable output for the agent's
`/api/bench`.

Examples:
```bash
scripts/bench.sh models/qwen2.5-0.5b-instruct-q4_k_m.gguf rapido
scripts/bench.sh models/mymodel.gguf medio
scripts/bench.sh models/mymodel.gguf detalhado --json
```

---

## Installing over an existing environment

**Already have llama.cpp, llama-swap, models or a config? Nothing of yours gets
touched.** The installer is idempotent and non-destructive: it **detects and
reuses** what you already have and only creates, downloads or compiles what is
missing. You can safely run it on top of a hand-built setup.

**What is detected & reused**

| Component | How it is detected (first match wins) |
|-----------|----------------------------------------|
| **llama.cpp** | `llama-server` **and** `llama-bench` on `PATH`; or env `LLAMA_CPP_BIN` / `LLAMA_CPP_DIR`; or common paths (`/usr/local/bin`, `/opt/homebrew/bin`, `~/llama.cpp/build/bin`, `./llama.cpp/build/bin`; Windows: `%USERPROFILE%\llama.cpp\build\bin`, `%LOCALAPPDATA%\...`). If found → **"llama.cpp detectado em &lt;path&gt; — usando o existente"** and the build is skipped. |
| **llama-swap** | binary on `PATH`; or env `LLAMA_SWAP_BIN`; or common paths (`/usr/local/bin`, `~/.local/bin`, `%USERPROFILE%`). If found → reused, download skipped. |
| **models** | env `MODELS_DIR`, or the `models/` folder already containing any `.gguf`. If present → the initial-model download is **skipped** ("modelos já presentes …"). Your models are never deleted. |

**What is preserved (per config file)**

| File | Behavior |
|------|----------|
| `.env` | Created from `.env.example` **only if missing**. If it exists → **kept intact**; the installer just points you at `.env.example` to compare for new options. Never overwritten. |
| `router-config.json` | Same: seeded from `router-config.example.json` only when absent; an existing file is preserved untouched. |
| `vendor/llama-swap.yaml` (llama-swap config) | Generated by `run.sh` **only when it does not exist**. If you already have your own — with your models and tuning — it is **never rewritten**. Add entries by editing it yourself. |

> The preservation strategy is **preserve-in-place** for every config: existing
> files are left exactly as they are (no in-place key merge, no silent edits). If
> a future version needs a new key, it will make a timestamped
> `<file>.bak-YYYYMMDD-HHMMSS` backup before touching anything and tell you.

**Environment-variable overrides** — point the installer at your existing tools:
`LLAMA_CPP_DIR` / `LLAMA_CPP_BIN`, `LLAMA_SWAP_BIN`, `MODELS_DIR`.

**Override flags** (opt in to reinstall/replace only when you actually want it):

| Flag (bash / PowerShell) | Effect |
|--------------------------|--------|
| `--force-llama` / `-ForceLlama` | Reinstall/rebuild llama.cpp even if one is detected. |
| `--force-swap` / `-ForceSwap` | Reinstall llama-swap even if one is detected. |
| `--model URL` / `-Model URL` | Add a specific GGUF even when models already exist (never deletes). |
| `--restart` / `-Restart` | Stop running services before starting (otherwise a busy port is reused, not killed). |

**Ports/services** — if `8088` / `3000` / `8080` is already serving, the
installer does **not** kill it blindly: it warns and lets `run.sh` reuse the
running service. Use `--restart` / `-Restart` to stop-then-start instead.

---

## Troubleshooting

- **Port already in use (8088/3000/8080).** `scripts/status.sh` shows which
  ports are listening. Stop the conflicting process, or change the ports in
  `.env` (`PROMPT_ROUTER_PORT`, `AGENT_PORT`, `LLAMA_SWAP_PORT`) and re-run.
  `run.sh` refuses to start a service whose port is already taken.
- **GPU not detected.** Ensure `nvidia-smi` runs (Linux/Windows). Force CPU with
  `--cpu` / `-Cpu`. On macOS, Metal is used automatically on Apple Silicon; Intel
  Macs are CPU-only.
- **Model won't load.** Confirm the `.gguf` exists in `models/`, is referenced in
  `vendor/llama-swap.yaml`, and that `llama-server` was built (check
  `vendor/llama.cpp/build/bin`). Check `logs/llama-swap.log`.
- **llama.cpp build fails (macOS).** Run `xcode-select --install`; ensure `cmake`
  is installed (`brew install cmake`).
- **Windows release asset not found.** llama.cpp Windows zip names change per
  build (e.g. `llama-bXXXX-bin-win-cuda-x64.zip`). Pick the matching asset at the
  releases page and set its URL, or drop `llama-server.exe` + `llama-bench.exe`
  into `vendor/llama.cpp`.
- **Agent features missing (Visões / Model Store / Benchmark UI / RAG).** The
  agent is not in the repo yet — see the note at the top. The router/UI still work.
- **Downloads blocked / offline.** Every download is confirmed at runtime; decline
  and place files manually (`models/`, `vendor/`). Nothing is fetched silently.
