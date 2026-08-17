# CylinderUI for Llama.cpp

A local, multi-platform LLM console for `llama.cpp`. Run your own models on your
own machine, reach them from anywhere on your LAN, and organize your work into
**Spaces** — each with its own theme, system prompt, hero and model set. No cloud
account required; your data stays on your machine.

## Presentation

Explore the project in the
**[interactive CylinderUI presentation](https://fdfreitas88.github.io/cylinderui/cylinderui-apresentacao.html)**,
or open its source at [docs/cylinderui-apresentacao.html](docs/cylinderui-apresentacao.html).

## Features

- **Spaces / Visions** — dynamic, per-workspace setups (theme, hero, badge,
  system prompt, model selection) created and edited entirely from the UI.
- **3 themes** — warm, cyber and high-contrast, switchable at runtime.
- **Model Store** — browse and pull GGUF models from Hugging Face and ModelScope
  directly from the console.
- **Cross-platform benchmark** — `llama-bench` wrapper with `rapido` / `medio` /
  `detalhado` profiles and JSON output, on macOS, Linux and Windows.
- **Multi-provider LAN** — point a Space at local `llama.cpp` or any
  OpenAI-compatible endpoint (Ollama, vLLM, LM Studio) on your network.
- **1 or 2 models per agent** — run a single model, or pair two for
  draft/verify style workflows.
- **PWA** — installable console shell with offline app assets.

## Quick start

One command sets up Python, builds or fetches `llama.cpp` + `llama-swap`, pulls a
small starter model (with your confirmation) and starts the services:

```bash
# macOS / Linux
./scripts/install.sh

# or, via Make
make -C scripts install
```

Windows (PowerShell):

```powershell
./scripts/install.ps1
```

Then open the router URL printed at the end (default `http://<host>:8088`).

See **[docs/INSTALL.md](docs/INSTALL.md)** for prerequisites, ports, start/stop,
model management, benchmarking and troubleshooting.

## Repository layout

```
router/     Prompt router + PWA console (index.html, router.py, manifest, icons)
agent/      FastAPI agent (chat, tools, model store/bench/exec, visions)
scripts/    Cross-platform installers, service lifecycle, benchmark wrappers
docs/       Installation/distribution guides and the interactive presentation
```

Root also ships the `scripts/` install entrypoints (`Makefile` / `justfile`),
`secret-scan.sh` (publication / pre-commit gate), and config templates
(`*.example.json`, `.env.example`) with placeholders only.

## Requirements

- Python 3.10+
- A C/C++ toolchain for building `llama.cpp` (or use a prebuilt release on
  Windows) — Metal on Apple Silicon, CUDA on NVIDIA, or CPU fallback.
- ~1 GB free disk for the starter model (larger for bigger models).

Details and platform notes are in [docs/INSTALL.md](docs/INSTALL.md).

## License

Licensed under GNU GPL v3.0 — see [LICENSE](LICENSE).
