# -*- coding: utf-8 -*-
"""
model_bench.py — Benchmark & Otimizar CPU do Model Store (CylinderUI),
Onda 3.

Roda `llama-bench` (e, se existir, `llama-perplexity`) por subprocess sobre
um `.gguf` já instalado (ver model_store.py), em 1 job assíncrono por vez
(thread em background, log em arquivo `data/bench-logs/<model_id>.log`),
persistindo status/resultado em `data/models.json` através de
`model_store.set_bench` / `model_store.set_tuned` / `model_store.apply_cpu_tuning`.

Perfis de `run_benchmark(model_id, profile)`:
  - "rapido"    : 1 medição pp512/tg128 na config atual do runner instalado.
  - "medio"     : varredura threads×batch (grade fixa), escolhe o melhor
                  combo por tg e já aplica o tweak (estado + bloco marcado
                  do yaml, via model_store).
  - "detalhado" : "medio" + tg em profundidade de contexto ~4k/32k/128k
                  (flag `-d` do llama-bench, com fallback para `-p <depth>`
                  se a build não suportar `-d`) + perplexidade amostral (só
                  se `llama-perplexity` existir no llama.cpp; senão pula com
                  nota) + checagem térmica best-effort (ver _bench_detalhado).

`optimize_cpu(model_id)` é uma ação separada e mais profunda: refaz a
varredura threads×batch e, se o binário aceitar (`--help` sondado, mesmo
padrão `supports()` usado nos runners), soma uma dimensão mlock on/off;
grava `tuned={gain,threads,batch,mlock,...}` no estado e tenta atualizar o
bloco marcado do modelo no llama-swap.yaml (soft-fail se o modelo não tiver
bloco marcado -- ex.: um dos 27 modelos instalados antes do Model Store).

Segurança: só roda sobre arquivos `.gguf` dentro do diretório de modelos
do model_store (`_validate_model_file`); threads/batch são sempre `int()`
e clampados antes de virar argv (`_clamp_threads`/`_clamp_batch`); nunca usa
`shell=True` (subprocess recebe sempre uma lista de argv).

Sem dependências novas: só stdlib (json, re, shlex, subprocess, threading,
time, uuid). Independente de app/tools.py; integra só com model_store.py
(persistência) -- mesma convenção de fs_tools.py/model_store.py.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

try:  # quando vive dentro de app/ (produção)
    from . import model_store  # type: ignore
except Exception:  # standalone (testes, staging)
    import model_store  # type: ignore


# ---------------------------------------------------------------------------
# Caminhos / binários
# ---------------------------------------------------------------------------
BENCH_LOG_DIR = model_store.DATA_DIR / "bench-logs"
LLAMA_BENCH_BIN = Path(os.getenv(
    "LLAMA_BENCH_BIN", str(Path.home() / "local-ai/llama.cpp/build/bin/llama-bench")
))
LLAMA_PERPLEXITY_BIN = Path(os.getenv(
    "LLAMA_PERPLEXITY_BIN", str(Path.home() / "local-ai/llama.cpp/build/bin/llama-perplexity")
))

for _p in (BENCH_LOG_DIR,):
    try:
        _p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Perfis / grades de varredura / timeouts
# ---------------------------------------------------------------------------
PROFILES = ("rapido", "medio", "detalhado")
_PROFILE_TIMEOUT = {"rapido": 5 * 60, "medio": 20 * 60, "detalhado": 60 * 60}
_OPTIMIZE_TIMEOUT = 20 * 60

# threads pedido explicitamente no escopo da Onda 3; batch é uma grade
# pequena o bastante para caber no orçamento de tempo do perfil "medio"
# (4 x 3 = 12 combos) -- grade fixa pequena
# (ver STATUS-onda3b.md).
_THREADS_GRID = (16, 20, 24, 28)
_BATCH_GRID = (128, 256, 512)
_MLOCK_GRID = (False, True)
_CTX_POINTS = (("4k", 4096), ("32k", 32768), ("128k", 131072))

_PPL_SAMPLE_TEXT = (
    "A raposa marrom rapida pula sobre o cao preguicoso. "
    "Este e um texto curto e repetitivo usado apenas para obter uma amostra "
    "de perplexidade (ppl amostral), nao um corpus de avaliacao real. "
) * 40


# ---------------------------------------------------------------------------
# Estado do job (1 por vez, thread em background)
# ---------------------------------------------------------------------------
_JOB_LOCK = threading.Lock()
_CURRENT_JOB: dict[str, Any] | None = None
_CANCEL_EVENT = threading.Event()


class _Canceled(Exception):
    """Sinaliza cancelamento cooperativo entre chamadas de llama-bench."""


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if k != "proc"}


def get_status() -> dict[str, Any]:
    with _JOB_LOCK:
        job = _CURRENT_JOB
    return {"job": _public_job(job) if job else None}


def get_log(tail: int = 200) -> dict[str, Any]:
    with _JOB_LOCK:
        job = _CURRENT_JOB
    if not job:
        return {"lines": [], "path": None}
    path = Path(job["log_path"])
    if not path.exists():
        return {"lines": [], "path": str(path)}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        lines = []
    n = max(1, min(int(tail or 200), 5000))
    return {"lines": lines[-n:], "path": str(path)}


def cancel_benchmark() -> dict[str, Any]:
    with _JOB_LOCK:
        job = _CURRENT_JOB
        if not job or job["status"] not in ("queued", "running"):
            return {"ok": False, "error": "nenhum benchmark em andamento"}
        _CANCEL_EVENT.set()
        proc = job.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"ok": True}


def _start_job(model_id: str, kind: str, profile: str, worker) -> dict[str, Any]:
    gguf_path = _validate_model_file(model_id)  # ValueError sobe pro chamador (main.py -> 400)
    global _CURRENT_JOB
    with _JOB_LOCK:
        if _CURRENT_JOB and _CURRENT_JOB["status"] in ("queued", "running"):
            return {
                "ok": False,
                "error": "já existe um benchmark em andamento (limite: 1 simultâneo)",
                "job": _public_job(_CURRENT_JOB),
            }
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12], "model_id": model_id, "kind": kind, "profile": profile,
            "status": "queued", "error": None, "result": None, "proc": None,
            "log_path": str(BENCH_LOG_DIR / f"{model_id}.log"),
            "created": time.time(), "started": None, "finished": None,
        }
        _CURRENT_JOB = job
        _CANCEL_EVENT.clear()
    BENCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    t = threading.Thread(target=_run_job, args=(job, gguf_path, worker), name=f"modelbench-{job['id']}", daemon=True)
    t.start()
    return {"ok": True, "job": _public_job(job)}


def _run_job(job: dict[str, Any], gguf_path: Path, worker) -> None:
    job["status"] = "running"
    job["started"] = time.time()
    log_path = Path(job["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log_f:
        log_f.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} job={job['id']} "
            f"model={job['model_id']} kind={job['kind']} profile={job['profile']} ===\n"
        )
        log_f.flush()
        try:
            result = worker(job, gguf_path, log_f)
            job["result"] = result
            job["status"] = "done"
            log_f.write("\n[concluído]\n")
        except _Canceled:
            job["status"] = "canceled"
            log_f.write("\n[cancelado]\n")
        except Exception as e:  # TimeoutError, RuntimeError, ValueError, etc.
            job["status"] = "error"
            job["error"] = str(e)
            log_f.write(f"\n[erro] {e}\n")
        finally:
            job["finished"] = time.time()
            job["proc"] = None


# ---------------------------------------------------------------------------
# Segurança / sanitização
# ---------------------------------------------------------------------------
def _validate_model_file(model_id: str) -> Path:
    m = model_store.get_model(model_id)
    if not m:
        raise ValueError("modelo não encontrado")
    raw = m.get("file") or ""
    if not raw:
        raise ValueError("modelo sem arquivo associado")
    f = Path(raw)
    try:
        f = f.resolve()
    except Exception as e:
        raise ValueError(f"caminho de modelo inválido: {e}")
    if f.suffix.lower() != ".gguf":
        raise ValueError("benchmark só roda sobre arquivos .gguf")
    models_dir = model_store.MODELS_DIR.resolve()
    try:
        f.relative_to(models_dir)
    except ValueError:
        raise ValueError("arquivo do modelo fora do diretório de modelos permitido")
    if not f.exists():
        raise ValueError(f"arquivo do modelo não encontrado: {f}")
    if not LLAMA_BENCH_BIN.exists():
        raise ValueError(f"binário llama-bench não encontrado: {LLAMA_BENCH_BIN}")
    return f


def _clamp_threads(t: Any) -> int:
    try:
        t = int(t)
    except Exception:
        t = 8
    return max(1, min(t, 128))


def _clamp_batch(b: Any) -> int:
    try:
        b = int(b)
    except Exception:
        b = 512
    return max(1, min(b, 8192))


def _read_runner_config(model_id: str) -> dict[str, int]:
    """Config "atual" para o perfil rápido: usa o último tuned{} já aplicado
    (Onda 3, optimize_cpu / varreduras "médio"/"detalhado" anteriores); se
    não houver tuning ainda, cai no default embutido no runner template
    (LLAMA_TUNE_THREADS/LLAMA_TUNE_BATCH ausentes -> 8 threads / batch 512,
    ver model_store._RUNNER_TEMPLATE). Não faz parsing do shell script do
    runner: threads/batch lá são variáveis (`$THREADS`/`$BATCH`), não mais
    literais, desde que o template passou a aceitar tuning via env var."""
    m = model_store.get_model(model_id) or {}
    tuned = m.get("tuned") or {}
    threads = tuned.get("threads") or 8
    batch = tuned.get("batch") or 512
    return {"threads": _clamp_threads(threads), "batch": _clamp_batch(batch)}


# ---------------------------------------------------------------------------
# Execução de llama-bench (subprocess) + parsing do -o json
# ---------------------------------------------------------------------------
def _base_args(gguf_path: Path, threads: int, batch: int, extra: list[str] | None = None) -> list[str]:
    args = ["-m", str(gguf_path), "-t", str(_clamp_threads(threads)), "-b", str(_clamp_batch(batch)), "-ngl", "0"]
    if extra:
        args += extra
    return args


def _run_llama_bench(job: dict[str, Any], log_f, args: list[str], timeout: float) -> list[dict]:
    if _CANCEL_EVENT.is_set():
        raise _Canceled()
    cmd = [str(LLAMA_BENCH_BIN), *args, "-o", "json"]
    log_f.write("$ " + " ".join(shlex.quote(c) for c in cmd) + "\n")
    log_f.flush()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    job["proc"] = proc
    try:
        out, err = proc.communicate(timeout=max(1.0, timeout))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise TimeoutError(f"llama-bench excedeu {timeout:.0f}s: {' '.join(args)}")
    finally:
        job["proc"] = None
    if err:
        log_f.write(err[-4000:] + "\n")
    if out:
        log_f.write(out[-8000:] + "\n")
    log_f.flush()
    if _CANCEL_EVENT.is_set():
        raise _Canceled()
    if proc.returncode != 0:
        raise RuntimeError(f"llama-bench saiu com código {proc.returncode}: {(err or '')[:300]}")
    return _parse_llama_bench_json(out)


def _parse_llama_bench_json(out: str) -> list[dict]:
    out = (out or "").strip()
    start, end = out.find("["), out.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("saída do llama-bench não contém JSON reconhecível (-o json)")
    try:
        data = json.loads(out[start:end + 1])
    except Exception as e:
        raise RuntimeError(f"JSON do llama-bench inválido: {e}")
    if not isinstance(data, list):
        raise RuntimeError("saída do llama-bench não é uma lista JSON")
    return data


def _split_pp_tg(records: list[dict]) -> dict[str, Any]:
    """Um único `-p P -n N` produz 2 linhas no -o json: uma pp-only
    (n_gen=0) e uma tg-only (n_prompt=0) -- mesmo padrão usado em
    o benchmark padrão (campo avg_ts)."""
    pp = tg = threads = batch = None
    for r in records:
        n_gen = r.get("n_gen", 0) or 0
        n_prompt = r.get("n_prompt", 0) or 0
        ts = r.get("avg_ts")
        threads = r.get("n_threads", threads)
        batch = r.get("n_batch", batch)
        if n_gen == 0 and n_prompt > 0 and isinstance(ts, (int, float)):
            pp = ts
        elif n_prompt == 0 and n_gen > 0 and isinstance(ts, (int, float)):
            tg = ts
    return {"pp": pp, "tg": tg, "threads": threads, "batch": batch}


# ---------------------------------------------------------------------------
# Perfil "rápido": 1 medição na config atual (pp512/tg128)
# ---------------------------------------------------------------------------
def _bench_rapido(job: dict[str, Any], gguf_path: Path, log_f) -> dict[str, Any]:
    cfg = _read_runner_config(job["model_id"])
    args = _base_args(gguf_path, cfg["threads"], cfg["batch"], ["-p", "512", "-n", "128"])
    records = _run_llama_bench(job, log_f, args, timeout=_PROFILE_TIMEOUT["rapido"])
    r = _split_pp_tg(records)
    return {
        "pp": r["pp"], "tg": r["tg"],
        "lat": round(1000.0 / r["tg"], 2) if r["tg"] else None,
        "threads": r["threads"] or cfg["threads"],
        "date": time.time(), "prof": "rapido",
        "ext": {"batch": r["batch"] or cfg["batch"]},
    }


# ---------------------------------------------------------------------------
# Varredura threads x batch (usada por "médio", "detalhado" e optimize_cpu)
# ---------------------------------------------------------------------------
def _sweep_threads_batch(
    job: dict[str, Any], gguf_path: Path, log_f, deadline: float,
    threads_grid=None, batch_grid=None, mlock_grid=(False,),
) -> tuple[list[dict], dict]:
    # threads_grid/batch_grid=None -> lê _THREADS_GRID/_BATCH_GRID do módulo
    # NA HORA da chamada (não como default de parâmetro, que seria fixado em
    # tempo de import e ignoraria monkeypatch nos testes).
    if threads_grid is None:
        threads_grid = _THREADS_GRID
    if batch_grid is None:
        batch_grid = _BATCH_GRID
    results: list[dict] = []
    for t in threads_grid:
        for b in batch_grid:
            for mlock in mlock_grid:
                if _CANCEL_EVENT.is_set():
                    raise _Canceled()
                remaining = deadline - time.time()
                if remaining <= 5:
                    log_f.write("[orçamento de tempo esgotado — encerrando varredura antecipadamente]\n")
                    if results:
                        best = max(results, key=lambda x: (x["tg"] or 0, x["pp"] or 0))
                        return results, best
                    raise RuntimeError("orçamento de tempo esgotado antes de qualquer resultado válido")
                extra = ["-p", "512", "-n", "128"] + (["--mlock"] if mlock else [])
                args = _base_args(gguf_path, t, b, extra)
                try:
                    records = _run_llama_bench(job, log_f, args, timeout=min(remaining, 90))
                except TimeoutError as e:
                    log_f.write(f"[pulando combo threads={t} batch={b} mlock={mlock}: {e}]\n")
                    continue
                r = _split_pp_tg(records)
                results.append({"threads": t, "batch": b, "mlock": mlock, "pp": r["pp"], "tg": r["tg"]})
    if not results:
        raise RuntimeError("varredura não produziu nenhum resultado válido")
    best = max(results, key=lambda x: (x["tg"] or 0, x["pp"] or 0))
    return results, best


def _apply_tweaks(model_id: str, threads: int, batch: int, mlock: bool = False, source: str = "bench") -> dict[str, Any]:
    """Grava tuned{} no estado e tenta refletir threads/batch/mlock no bloco
    marcado do modelo no yaml (soft-fail se o modelo não tiver bloco marcado
    -- ver model_store.apply_cpu_tuning)."""
    prev = model_store.get_model(model_id) or {}
    prev_tg = (prev.get("bench") or {}).get("tg")
    gain = None
    tuned = {
        "gain": gain, "threads": threads, "batch": batch, "mlock": mlock,
        "date": time.time(), "source": source,
    }
    model_store.set_tuned(model_id, tuned)
    yaml_res = model_store.apply_cpu_tuning(model_id, threads, batch, mlock)
    return {"tuned": tuned, "yaml": yaml_res}


# ---------------------------------------------------------------------------
# Perfil "médio": varredura threads x batch, aplica o melhor combo
# ---------------------------------------------------------------------------
def _bench_medio(job: dict[str, Any], gguf_path: Path, log_f) -> dict[str, Any]:
    deadline = job["started"] + _PROFILE_TIMEOUT["medio"]
    results, best = _sweep_threads_batch(job, gguf_path, log_f, deadline)
    tweak = _apply_tweaks(job["model_id"], best["threads"], best["batch"], source="bench-medio")
    return {
        "pp": best["pp"], "tg": best["tg"],
        "lat": round(1000.0 / best["tg"], 2) if best["tg"] else None,
        "threads": best["threads"], "date": time.time(), "prof": "medio",
        "ext": {"batch": best["batch"], "sweep": results, "tweak_applied": tweak},
    }


# ---------------------------------------------------------------------------
# Perfil "detalhado": médio + tg em profundidade de contexto + ppl + térmico
# ---------------------------------------------------------------------------
def _bench_tg_at_depth(job, gguf_path, log_f, threads, batch, depth, timeout) -> float | None:
    ctx_size = depth + 1024
    args = _base_args(gguf_path, threads, batch, ["-p", "512", "-n", "128", "-d", str(depth), "-c", str(ctx_size)])
    try:
        records = _run_llama_bench(job, log_f, args, timeout=timeout)
        r = _split_pp_tg(records)
        if r["tg"] is not None:
            return r["tg"]
        raise RuntimeError("sem linha tg no resultado com -d")
    except _Canceled:
        raise
    except Exception as e:
        log_f.write(f"[fallback sem -d para depth={depth}: {e}]\n")
        # nem toda build do llama-bench suporta -d/--n-depth; aproxima
        # medindo tg logo após processar `depth` tokens de prompt.
        try:
            args2 = _base_args(gguf_path, threads, batch, ["-p", str(depth), "-n", "128", "-c", str(ctx_size)])
            records2 = _run_llama_bench(job, log_f, args2, timeout=timeout)
            r2 = _split_pp_tg(records2)
            return r2["tg"]
        except _Canceled:
            raise
        except Exception as e2:
            log_f.write(f"[contexto {depth} pulado: {e2}]\n")
            return None


def _run_perplexity_sample(job, gguf_path, log_f, threads, timeout) -> dict[str, Any]:
    if not LLAMA_PERPLEXITY_BIN.exists():
        note = f"binário llama-perplexity não encontrado em {LLAMA_PERPLEXITY_BIN}; etapa de perplexidade pulada"
        log_f.write(f"[ppl] {note}\n")
        return {"skipped": True, "note": note}
    if timeout <= 5:
        return {"skipped": True, "note": "sem orçamento de tempo restante para perplexidade"}
    sample_path = BENCH_LOG_DIR / f"_ppl-sample-{job['id']}.txt"
    try:
        sample_path.write_text(_PPL_SAMPLE_TEXT, encoding="utf-8")
        cmd = [
            str(LLAMA_PERPLEXITY_BIN), "-m", str(gguf_path), "-f", str(sample_path),
            "-t", str(_clamp_threads(threads)), "-c", "512", "--chunks", "2",
        ]
        log_f.write("$ " + " ".join(shlex.quote(c) for c in cmd) + "\n")
        log_f.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        job["proc"] = proc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return {"skipped": True, "note": f"perplexidade excedeu timeout de {timeout:.0f}s"}
        finally:
            job["proc"] = None
        log_f.write((out or "")[-4000:] + "\n")
        m = re.search(r"Final estimate:\s*PPL\s*=\s*([\d.]+)", out or "") or re.search(r"PPL\s*=\s*([\d.]+)", out or "")
        if m:
            return {"skipped": False, "value": float(m.group(1)), "chunks": 2}
        return {"skipped": True, "note": "não foi possível extrair PPL da saída"}
    except Exception as e:
        return {"skipped": True, "note": f"erro ao rodar perplexidade: {e}"}
    finally:
        try:
            sample_path.unlink(missing_ok=True)
        except Exception:
            pass


def _bench_detalhado(job: dict[str, Any], gguf_path: Path, log_f) -> dict[str, Any]:
    started = job["started"]
    deadline = started + _PROFILE_TIMEOUT["detalhado"]
    medio_deadline = min(deadline, started + _PROFILE_TIMEOUT["medio"])
    results, best = _sweep_threads_batch(job, gguf_path, log_f, medio_deadline)
    tweak = _apply_tweaks(job["model_id"], best["threads"], best["batch"], source="bench-detalhado")

    ctx_results: dict[str, Any] = {}
    first_4k_tg = None
    t0 = time.time()
    for label, depth in _CTX_POINTS:
        if _CANCEL_EVENT.is_set():
            raise _Canceled()
        remaining = deadline - time.time()
        if remaining <= 10:
            ctx_results[label] = {"skipped": True, "note": "orçamento de tempo esgotado"}
            continue
        tg = _bench_tg_at_depth(job, gguf_path, log_f, best["threads"], best["batch"], depth, timeout=min(remaining, 600))
        ctx_results[label] = {"tg": tg}
        if label == "4k" and isinstance(tg, (int, float)):
            first_4k_tg = tg

    # Checagem térmica: powermetrics exige root (indisponível aqui) e não há
    # outro sensor confiável acessível sem privilégios. Proxy adotado:
    # duração sustentada da rodada + variação de tg em contexto 4k medida no
    # início e repetida no fim do perfil detalhado (mesma condição de teste).
    thermal: dict[str, Any] = {
        "proxy": "sustained_duration_and_tg_delta_4k",
        "note": (
            "powermetrics é root-only nesta máquina; sem acesso a sensores "
            "térmicos diretos, o proxy usado é a duração sustentada da rodada "
            "de benchmark mais a variação de tg em profundidade de contexto "
            "4k medida no início e repetida no fim do perfil detalhado -- uma "
            "queda de tg sob a mesma condição de teste é o sinal indireto de "
            "possível throttling térmico sob carga sustentada."
        ),
    }
    if first_4k_tg and (deadline - time.time()) > 10 and not _CANCEL_EVENT.is_set():
        last_tg = _bench_tg_at_depth(job, gguf_path, log_f, best["threads"], best["batch"], 4096,
                                      timeout=min(deadline - time.time(), 300))
        thermal["tg_4k_first"] = first_4k_tg
        thermal["tg_4k_last"] = last_tg
        if isinstance(last_tg, (int, float)) and first_4k_tg:
            delta_pct = round((first_4k_tg - last_tg) / first_4k_tg * 100, 1)
            thermal["delta_pct"] = delta_pct
            thermal["possible_throttling"] = delta_pct > 8.0
    thermal["sustained_s"] = round(time.time() - t0, 1)

    ppl = _run_perplexity_sample(job, gguf_path, log_f, best["threads"], timeout=max(5, deadline - time.time()))

    return {
        "pp": best["pp"], "tg": best["tg"],
        "lat": round(1000.0 / best["tg"], 2) if best["tg"] else None,
        "threads": best["threads"], "date": time.time(), "prof": "detalhado",
        "ext": {
            "batch": best["batch"], "sweep": results, "tweak_applied": tweak,
            "ctx": ctx_results, "thermal": thermal, "ppl": ppl,
        },
    }


# ---------------------------------------------------------------------------
# API pública: run_benchmark / optimize_cpu
# ---------------------------------------------------------------------------
def run_benchmark(model_id: str, profile: str = "rapido") -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"perfil inválido: {profile!r} (use {PROFILES})")

    def _worker(job, gguf_path, log_f):
        if profile == "rapido":
            result = _bench_rapido(job, gguf_path, log_f)
        elif profile == "medio":
            result = _bench_medio(job, gguf_path, log_f)
        else:
            result = _bench_detalhado(job, gguf_path, log_f)
        model_store.set_bench(model_id, result)
        return result

    return _start_job(model_id, kind="bench", profile=profile, worker=_worker)


def _probe_mlock_supported(log_f) -> bool:
    """Mesmo padrão supports() dos runners: sonda --help em vez de assumir."""
    try:
        out = subprocess.run([str(LLAMA_BENCH_BIN), "--help"], capture_output=True, text=True, timeout=10)
        supported = "--mlock" in (out.stdout or "") or "--mlock" in (out.stderr or "")
    except Exception as e:
        log_f.write(f"[mlock probe falhou: {e}; assumindo não suportado]\n")
        return False
    log_f.write(f"[mlock suportado pelo llama-bench: {supported}]\n")
    return supported


def optimize_cpu(model_id: str) -> dict[str, Any]:
    """Varredura mais profunda (threads x batch x mlock, quando suportado)
    a partir do resultado existente (bench["tg"] anterior é o baseline de
    ganho, se houver); grava tuned={gain,threads,batch,mlock,...} no estado
    e tenta refletir no bloco marcado do modelo no llama-swap.yaml."""

    def _worker(job, gguf_path, log_f):
        deadline = job["started"] + _OPTIMIZE_TIMEOUT
        m = model_store.get_model(model_id) or {}
        baseline_tg = (m.get("bench") or {}).get("tg")
        mlock_supported = _probe_mlock_supported(log_f)
        mlock_grid = _MLOCK_GRID if mlock_supported else (False,)

        results, best = _sweep_threads_batch(job, gguf_path, log_f, deadline, mlock_grid=mlock_grid)

        gain = None
        if baseline_tg and best.get("tg"):
            gain = round((best["tg"] - baseline_tg) / baseline_tg * 100, 1)
        tuned = {
            "gain": gain, "threads": best["threads"], "batch": best["batch"],
            "mlock": best.get("mlock", False), "date": time.time(), "source": "optimize_cpu",
        }
        model_store.set_tuned(model_id, tuned)
        yaml_res = model_store.apply_cpu_tuning(model_id, tuned["threads"], tuned["batch"], tuned["mlock"])
        return {"tuned": tuned, "yaml": yaml_res, "sweep": results, "mlock_supported": mlock_supported}

    return _start_job(model_id, kind="optimize", profile="otimizar", worker=_worker)
