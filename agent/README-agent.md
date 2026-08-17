# CylinderUI — Agent

Local AI agent backend for CylinderUI. A FastAPI service that:

- Streams chat completions (SSE) via an OpenAI-compatible router / llama-swap.
- Runs a Model Store (search, download, install GGUF models).
- Benchmarks / tunes CPU inference (`llama-bench`).
- Exposes read-only, directory-scoped file tools and optional web search.
- Manages dynamic **Visions** (isolated workspaces) — see `app/visions.py`.

## Requirements

- Python 3.10+
- An OpenAI-compatible upstream (router / llama-swap) reachable via `ROUTER_URL`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit as needed
```

State lives under `data/` (created on first run). A neutral example seed is
provided in `data/visions.json.example`.

## Run

```bash
uvicorn app.main:app --port 3000
```
