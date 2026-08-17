# -*- coding: utf-8 -*-
"""
model_store.py — Model Store do CylinderUI

Busca modelos GGUF (Hugging Face + ModelScope best-effort), gerencia uma fila
de downloads em background com progresso/pausa/cancelamento/retomada, instala
modelos no llama-swap.yaml (só append/remoção de BLOCOS DE TEXTO marcados —
NUNCA parseia nem reescreve o arquivo inteiro) e mantém o estado (visibilidade,
padrão, ordem, ativo, uso, benchmark/tuning) em data/models.json.

Este módulo é independente de app/tools.py: não importa nada de lá e não é
importado por ele — mesma convenção de app/fs_tools.py. A integração
acontece só em app/stream.py (record_usage, ver PATCH-MODELSTORE-V1) e
app/main.py (endpoints /api/store/* e /api/models*).

Sem dependências novas: só stdlib (json, os, re, subprocess, threading,
queue, time, uuid, urllib). NADA de pip novo (sem "requests", sem "yaml").
"""
from __future__ import annotations

import glob as _glob
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:  # quando vive dentro de app/ (produção), reaproveita os caminhos reais
    from .config import DATA_DIR as _CFG_DATA_DIR  # type: ignore
except Exception:
    _CFG_DATA_DIR = None
try:
    from .config import LOG_DIR as _CFG_LOG_DIR  # type: ignore
except Exception:
    _CFG_LOG_DIR = None

# ---------------------------------------------------------------------------
# Onda V2 (Model Store dinâmico por VISÃO): visions.py é a fonte da lista de
# interfaces (ver _interfaces() abaixo, substitui a tupla fixa INTERFACES).
# Import relativo em produção (model_store.py e visions.py convivem em
# app/); fallback por caminho de arquivo p/ rodar isolado (staging/testes),
# igual ao padrão já usado por visions.py p/ importar .config. Recarrega
# sempre do zero em vez de reaproveitar sys.modules -- DATA_DIR pode mudar
# entre execuções (ex. um tmp_path novo por teste do pytest), e um módulo
# cacheado da rodada anterior leria o visions.json errado.
# ---------------------------------------------------------------------------
try:  # produção: model_store.py vive dentro do pacote app/, junto de visions.py
    from . import visions as _visions  # type: ignore
except Exception:
    import importlib.util as _ilu
    import sys as _sys
    _VISIONS_DEP_NAME = "_model_store_visions_dep"
    _sys.modules.pop(_VISIONS_DEP_NAME, None)
    _visions_spec = _ilu.spec_from_file_location(
        _VISIONS_DEP_NAME, Path(__file__).resolve().parent / "visions.py"
    )
    _visions = _ilu.module_from_spec(_visions_spec)  # type: ignore[arg-type]
    _sys.modules[_VISIONS_DEP_NAME] = _visions
    _visions_spec.loader.exec_module(_visions)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Caminhos (com fallback autocontido para rodar fora do pacote app/, ex. testes)
# ---------------------------------------------------------------------------
DATA_DIR = Path(_CFG_DATA_DIR) if _CFG_DATA_DIR else Path(
    os.getenv("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
).resolve()
LOG_DIR = Path(_CFG_LOG_DIR) if _CFG_LOG_DIR else Path(
    os.getenv("LOG_DIR", str(Path.home() / "local-ai/logs"))
).resolve()

MODELS_STATE_FILE = DATA_DIR / "models.json"
MODELS_DIR = Path(os.getenv("MODELS_DIR", str(Path.home() / "local-ai/models"))).resolve()
LLAMA_SWAP_YAML = Path(os.getenv("LLAMA_SWAP_YAML", str(Path.home() / "local-ai/configs/llama-swap.yaml")))
LLAMA_SWAP_RUNNERS_DIR = Path(os.getenv("LLAMA_SWAP_RUNNERS_DIR", str(Path.home() / "local-ai/llama-swap-runners")))
LLAMA_SERVER_BIN = Path(os.getenv(
    "LLAMA_SERVER_BIN", str(Path.home() / "local-ai/llama.cpp/build/bin/llama-server")
))

for _p in (DATA_DIR,):
    try:
        _p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def _interfaces() -> tuple[str, ...]:
    """Onda V2: lista DINÂMICA de vision-ids (substitui a antiga tupla fixa
    INTERFACES = ("cylinderui","cyber","god")). Nunca cacheada -- visões podem
    ser criadas/deletadas em runtime (ver visions.create_vision/delete_vision
    em backend/visions.py), então cada chamador relê visions.get_vision_ids()
    na hora, nunca no import do módulo. Fallback defensivo para as 3 legadas
    se visions.json estiver vazio/corrompido (visions.get_vision_ids() nunca
    lança -- ver _load_state lá -- mas nunca deixamos a loja sem NENHUMA
    interface, o que quebraria vis/def/exec de todo mundo)."""
    try:
        ids = list(_visions.get_vision_ids())
    except Exception:
        ids = []
    return tuple(ids) if ids else ("cylinderui", "cyber", "god")


# Contrato de API (§6 do context-pack) usa códigos curtos "C"/"CC"/"GOD" nas
# rotas /api/models; o estado interno usa os ids por extenso das visões
# (vis{cylinderui,cyber,...}). normalize_interface() faz a ponte -- e agora
# (Onda V2) aceita QUALQUER vision-id existente, não só as 3 legadas.
_INTERFACE_CODE_MAP = {"C": "cylinderui", "CC": "cyber", "GOD": "god"}

RAM_BUDGET_RATIO = 0.85
_FALLBACK_RAM_BYTES = 64 * 1024 ** 3

SEARCH_CACHE_TTL = 600  # 10 min
DEFAULT_SEARCH_LIMIT = 30
HF_API_BASE = "https://huggingface.co/api/models"
MS_API_BASE = "https://modelscope.cn/api/v1/models"
CHUNK_SIZE = 256 * 1024

_STATE_LOCK = threading.RLock()
_DL_LOCK = threading.RLock()
_DOWNLOADS: dict[str, dict[str, Any]] = {}
_DL_QUEUE: "queue.Queue[str]" = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False
_SEARCH_CACHE: dict[tuple, dict] = {}

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")

# ---------------------------------------------------------------------------
# Modelos pré-existentes (fora da Model Store): enumerate_runtime_models() lê
# (read-only) o llama-swap.yaml real e devolve TODOS os modelos declarados
# lá -- não só os ~instalados pela loja. Parse textual (sem pyyaml -- este
# módulo não ganha deps novas, ver docstring do arquivo); cobre o formato
# real observado: um mapa `"id": {name, description, cmd, ttl}` de 2 espaços
# de indentação sob a chave de topo `models:`, terminado por outra chave de
# topo (ex. `routing:`) ou fim do arquivo.
# ---------------------------------------------------------------------------
_RUNTIME_YAML_ENTRY_RE = re.compile(
    r'^  "(?P<id>[^"]+)":[ \t]*\n(?P<body>(?:(?:[ \t]{4}.*)?\n)*)',
    re.MULTILINE,
)
_YAML_NAME_RE = re.compile(r'name:\s*"([^"]*)"')
_YAML_CMD_DIRECT_MODEL_RE = re.compile(r'(?:-m|--model)\s+"([^"]+\.gguf)"')
_RUNNER_MODEL_STATIC_RE = re.compile(r'^\s*MODEL="([^"$]+\.gguf)"\s*$', re.MULTILINE)
_RUNNER_MODEL_DIR_RE = re.compile(r'^\s*MODEL_DIR="([^"]+)"\s*$', re.MULTILINE)
_RUNNER_MODEL_GLOB_RE = re.compile(r'ls\s+"\$MODEL_DIR"/([^"]+\.gguf)')
_QUANT_RE = re.compile(r'(Q\d(?:_[A-Z0-9]+)*|MXFP4|BF16|FP16|F16)', re.IGNORECASE)


def _extract_yaml_models_section(text: str) -> str:
    """Recorta só o corpo da chave de topo `models:` (até a próxima chave de
    topo, ex. `routing:`, ou fim do arquivo). Não toca no resto do yaml."""
    m = re.search(r'^models:[ \t]*\n', text, re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    m2 = re.search(r'^\S', text[start:], re.MULTILINE)
    end = start + m2.start() if m2 else len(text)
    return text[start:end]


def _resolve_runner_model_file(runner_path: Path) -> str | None:
    """Lê um runner (`run-<id>.sh`) e resolve o caminho real do .gguf que ele
    carrega -- ou um `MODEL="/caminho/fixo.gguf"` estático, ou um
    `MODEL_DIR="..."` + `ls "$MODEL_DIR"/<padrão>.gguf | head -1` (padrão
    real usado pela maioria dos runners pré-existentes). Nunca escreve nada;
    só leitura best-effort (arquivo ausente/malformado -> None, sem erro)."""
    try:
        text = runner_path.read_text(encoding="utf-8")
    except Exception:
        return None

    static_m = _RUNNER_MODEL_STATIC_RE.search(text)
    if static_m:
        return static_m.group(1)

    dir_m = _RUNNER_MODEL_DIR_RE.search(text)
    if not dir_m:
        return None
    model_dir = dir_m.group(1)
    for glob_m in _RUNNER_MODEL_GLOB_RE.finditer(text):
        pattern = glob_m.group(1)
        try:
            matches = sorted(_glob.glob(str(Path(model_dir) / pattern)))
        except Exception:
            matches = []
        if matches:
            return matches[0]
    return None


def _resolve_runtime_model_file(model_id: str, cmd_text: str) -> str | None:
    """Extrai o caminho do .gguf de um modelo declarado no yaml: primeiro
    tenta um `-m`/`--model` direto na linha `cmd:` (formato futuro possível),
    senão cai no runner convencional `run-<id>.sh` (formato real de hoje --
    ver llama-swap-runners/)."""
    direct_m = _YAML_CMD_DIRECT_MODEL_RE.search(cmd_text)
    if direct_m:
        return direct_m.group(1)
    runner_path = LLAMA_SWAP_RUNNERS_DIR / f"run-{model_id}.sh"
    return _resolve_runner_model_file(runner_path)


def _infer_quant(file_path: str | None) -> str:
    if not file_path:
        return "—"
    m = _QUANT_RE.search(Path(file_path).name)
    return m.group(1).upper() if m else "—"


def enumerate_runtime_models() -> list[dict[str, Any]]:
    """Enumera (read-only, nunca escreve) os modelos declarados no
    llama-swap.yaml real -- inclui os ~27 pré-existentes (fora da Model
    Store) e também os que a loja já instalou (bloco `# BEGIN MODELSTORE:id`
    ou não; aqui não filtra por isso, list_models() decide `store_managed`).
    Cada item: {id, name, file, quant, size_gb, source:"runtime"}."""
    try:
        text = LLAMA_SWAP_YAML.read_text(encoding="utf-8")
    except Exception:
        return []

    section = _extract_yaml_models_section(text)
    if not section:
        return []

    out: list[dict[str, Any]] = []
    for m in _RUNTIME_YAML_ENTRY_RE.finditer(section):
        model_id = m.group("id")
        body = m.group("body")
        name_m = _YAML_NAME_RE.search(body)
        name = name_m.group(1) if name_m else model_id

        file_path = _resolve_runtime_model_file(model_id, body)
        quant = _infer_quant(file_path)
        size_gb: float | None = None
        if file_path:
            try:
                size_gb = round(os.path.getsize(file_path) / (1024 ** 3), 2)
            except Exception:
                size_gb = None

        out.append({
            "id": model_id,
            "name": name,
            "file": file_path or "",
            "quant": quant,
            "size_gb": size_gb,
            "source": "runtime",
        })
    return out


# ---------------------------------------------------------------------------
# Utilidades comuns
# ---------------------------------------------------------------------------
def _normalize_source(source: str | None) -> str | None:
    if source in (None, "", "both"):
        return None
    return {"ms": "modelscope"}.get(source, source)


def normalize_interface(name: str) -> str:
    """Aceita os códigos curtos legados ("C"/"CC"/"GOD"), os ids
    cylinderui/cyber/god por extenso, OU qualquer vision-id dinâmico criado
    pelo usuário (Onda V2) -- valida contra a lista ATUAL de visões
    (_interfaces(), relida a cada chamada). Levanta ValueError se `name` não
    é alias conhecido nem vision-id existente."""
    key = _INTERFACE_CODE_MAP.get(name, name)
    if not key or key not in _interfaces():
        raise ValueError(f"interface desconhecida: {name!r}")
    return key


def _backup_file(path: Path) -> None:
    """Backup best-effort .bak-AAAAMMDD-HHMMSS antes de escrever (mesmo padrão de fs_tools._backup)."""
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            data = path.read_bytes()
            path.with_name(path.name + f".bak-{stamp}").write_bytes(data)
        except Exception:
            pass


def _slugify(repo_id: str) -> str:
    base = repo_id.split("/")[-1].lower()
    slug = _SLUG_RE.sub("-", base).strip("-")
    return (slug or "model")[:60]


def _friendly_name(repo_id: str) -> str:
    base = repo_id.split("/")[-1]
    return base.replace("-", " ").replace("_", " ").strip() or repo_id


def _validate_filename(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\\" in name or not _SAFE_FILENAME_RE.match(name):
        raise ValueError(f"nome de arquivo inválido: {name!r}")


def _validate_repo_id(repo_id: str) -> None:
    if not repo_id or not _SAFE_REPO_ID_RE.match(repo_id):
        raise ValueError(f"repo_id inválido: {repo_id!r}")


def _is_store_managed(model_id: str, stored_record: dict[str, Any] | None, yaml_text: str | None = None) -> bool:
    """`store_managed` = true se o modelo tem um bloco marcado
    `# BEGIN MODELSTORE:<id>` no llama-swap.yaml (instalado pela loja) OU se
    o registro em models.json tem `repo_id` (só a loja grava isso, ver
    _finalize_install) -- cobre o caso raro do bloco ter sido removido do
    yaml manualmente depois. Único ponto de verdade usado tanto por
    list_models() (exibição) quanto por uninstall_model() (bloqueio)."""
    if yaml_text is None:
        try:
            yaml_text = LLAMA_SWAP_YAML.read_text(encoding="utf-8")
        except Exception:
            yaml_text = ""
    if f"# BEGIN MODELSTORE:{model_id}" in yaml_text:
        return True
    if stored_record and stored_record.get("repo_id"):
        return True
    return False


# ---------------------------------------------------------------------------
# Estado (data/models.json)
# ---------------------------------------------------------------------------
def _empty_bool_map() -> dict[str, bool]:
    return {k: False for k in _interfaces()}


def _reconcile_interfaces(state: dict[str, Any]) -> bool:
    """Migração idempotente (Onda V2): garante que `vis`/`def` de CADA
    modelo, e o dict `state["exec"]` (config de execução por interface),
    tenham exatamente as chaves das visões ATUAIS (_interfaces() ==
    visions.get_vision_ids(), com fallback pras 3 legadas). Visão nova ganha
    chave default (False para vis/def; sem entrada em exec -- get_exec já
    preenche default sob demanda); visão deletada perde a chave, SEM tocar
    nas outras (os valores de cylinderui/cyber/god são preservados sempre que
    essas 3 ainda existirem como visão -- é só o caso comum de "vision-id
    atual"). Não lança nunca (defensivo, mesmo padrão do resto do módulo).
    Devolve True se algo mudou, pra quem chama decidir se vale persistir."""
    current = _interfaces()
    changed = False
    for m in (state.get("models") or {}).values():
        if not isinstance(m, dict):
            continue
        for key in ("vis", "def"):
            old = m.get(key) if isinstance(m.get(key), dict) else {}
            new = {iface: bool(old.get(iface, False)) for iface in current}
            if old != new:
                m[key] = new
                changed = True
    exec_state = state.get("exec")
    if isinstance(exec_state, dict):
        for stale_key in [k for k in exec_state if k not in current]:
            exec_state.pop(stale_key, None)
            changed = True
    return changed


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(MODELS_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {"models": {}, "exec": {}}
        data.setdefault("models", {})
        data.setdefault("exec", {})  # PATCH-MODELEXEC-V1 (Onda 4)
    except Exception:
        data = {"models": {}, "exec": {}}
    # Onda V2: reconciliação LAZY -- disparada em toda leitura de estado (não
    # só list_models(): também get_model, os setters, get_usage, prune, ...),
    # porque todos passam por _load_state(). Self-heal automático quando uma
    # visão é criada/deletada em visions.json, sem precisar acoplar
    # visions.py <-> model_store.py (import circular). Ver on_visions_changed()
    # abaixo para quem quiser forçar a reconciliação+persistência na hora.
    if _reconcile_interfaces(data):
        _save_state(data)
    return data


def _save_state(state: dict[str, Any]) -> None:
    MODELS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODELS_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


def on_visions_changed() -> dict[str, Any]:
    """Hook opcional (Onda V2) para quem cria/deleta/reordena visões (ex. um
    futuro endpoint em main.py que chama visions.create_vision/delete_vision)
    forçar a reconciliação + persistência IMEDIATA de data/models.json, em
    vez de esperar a próxima leitura preguiçosa. NÃO é obrigatório chamar
    isso -- _load_state() já reconcilia e salva sozinho em toda chamada
    (list_models(), get_model(), qualquer setter, ...); este hook só existe
    pra quem quiser garantir sincronia no disco no instante da mudança, sem
    esperar o próximo acesso. visions.py não importa nada daqui (nem
    precisa saber que este hook existe) -- zero risco de import circular."""
    with _STATE_LOCK:
        state = _load_state()  # já reconcilia e salva se algo mudou
    return {"ok": True, "interfaces": list(_interfaces())}


# ---------------------------------------------------------------------------
# Onda 4 (R2): size_gb real por modelo, lido do .gguf em disco (os.stat),
# com cache simples por model_id chaveado em (mtime, size) -- evita um
# stat() a cada listagem quando nada mudou. Nunca inventa tamanho: se o
# arquivo nao existir/nao puder ser lido, size_gb fica ausente (None), e o
# frontend (Onda 3f) ja trata isso como "modo sem dado" honesto.
# ---------------------------------------------------------------------------
_SIZE_GB_CACHE: dict[str, tuple[float, float]] = {}  # model_id -> (mtime, size_gb)


def _file_size_gb(model_id: str, file_path: str) -> float | None:
    if not file_path:
        return None
    try:
        st = os.stat(file_path)
    except Exception:
        return None
    cached = _SIZE_GB_CACHE.get(model_id)
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    size_gb = st.st_size / (1024 ** 3)
    _SIZE_GB_CACHE[model_id] = (st.st_mtime, size_gb)
    return size_gb


def _with_size_gb(m: dict[str, Any]) -> dict[str, Any]:
    out = dict(m)
    sg = _file_size_gb(out.get("id", ""), out.get("file", ""))
    if sg is not None:
        out["size_gb"] = round(sg, 2)
    return out


def _seed_from_runtime(model_id: str, runtime: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Registro-default (lazy) para um id que existe no llama-swap.yaml
    (`enumerate_runtime_models`) mas ainda não tem entrada em models.json --
    vis/def todos False, ordem no fim, ativo False, uso zerado, bench/tuned
    vazios (§3 do pedido). Não persiste nada sozinho -- quem chama decide."""
    rt = runtime
    if rt is None:
        rt = next((r for r in enumerate_runtime_models() if r["id"] == model_id), None)
    if not rt:
        return None
    return {
        "id": model_id,
        "name": rt.get("name") or model_id,
        "file": rt.get("file") or "",
        "quant": rt.get("quant") or "—",
        "source": "runtime",
        "vis": _empty_bool_map(),
        "def": _empty_bool_map(),
        "order": 10_000,
        "active": False,
        "usage": {"calls": 0, "last": None},
        "bench": {},
        "tuned": {},
    }


def _ensure_model_record(state: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    """Garante um registro em state["models"][model_id] -- se já existir,
    devolve; senão, tenta semear a partir do runtime (yaml) e INSERE em
    state (quem chama ainda precisa _save_state(state), não é feito aqui).
    Só é usado pelos setters (mutação real do usuário); list_models() nunca
    chama isso (mescla em memória, sem gravar -- ver docstring de list_models
    e §3 do pedido: "sem gravar à força"). Devolve None se o id não existe
    nem no estado nem no runtime (id realmente desconhecido)."""
    m = state["models"].get(model_id)
    if m:
        return m
    seed = _seed_from_runtime(model_id)
    if not seed:
        return None
    state["models"][model_id] = seed
    return seed


def get_model(model_id: str) -> dict[str, Any] | None:
    with _STATE_LOCK:
        m = _load_state()["models"].get(model_id)
    if m:
        return _with_size_gb(m)
    seed = _seed_from_runtime(model_id)
    return _with_size_gb(seed) if seed else None


def list_models() -> list[dict[str, Any]]:
    """Lista TODOS os modelos que existem de verdade no servidor: mescla os
    ~27 (ou quantos houver) declarados no llama-swap.yaml
    (`enumerate_runtime_models`, fonte de identidade/arquivo/quant) com o
    estado guardado em data/models.json (fonte de vis/def/order/active/
    usage/bench/tuned -- e de name/file/source quando o registro foi criado
    pela loja). Nunca escreve nada (leitura pura); modelos runtime sem
    registro ainda ganham defaults "lazy" em memória (ver _seed_from_runtime)
    -- só viram registro de verdade em models.json quando o usuário mexe em
    algo (ver os setters, que chamam _ensure_model_record)."""
    try:
        yaml_text = LLAMA_SWAP_YAML.read_text(encoding="utf-8")
    except Exception:
        yaml_text = ""

    with _STATE_LOCK:
        stored = {mid: dict(m) for mid, m in _load_state()["models"].items()}

    merged: dict[str, dict[str, Any]] = {}
    for i, rt in enumerate(enumerate_runtime_models()):
        mid = rt["id"]
        merged[mid] = {
            "id": mid,
            "name": rt.get("name") or mid,
            "file": rt.get("file") or "",
            "quant": rt.get("quant") or "—",
            "source": "runtime",
            "store_managed": _is_store_managed(mid, None, yaml_text),
            "vis": _empty_bool_map(),
            "def": _empty_bool_map(),
            "order": 10_000 + i,  # entram no fim, na ordem do yaml, até o usuário reordenar
            "active": False,
            "usage": {"calls": 0, "last": None},
            "bench": {},
            "tuned": {},
        }

    for mid, sm in stored.items():
        base = merged.get(mid)
        entry = dict(base) if base else {"source": "store"}
        entry.update(sm)  # models.json manda no que definir (vis/def/order/active/usage/bench/tuned/name/file/...)
        entry["id"] = mid
        entry["store_managed"] = _is_store_managed(mid, sm, yaml_text)
        merged[mid] = entry

    models = [_with_size_gb(m) for m in merged.values()]
    models.sort(key=lambda m: (m.get("order", 0), m.get("id", "")))
    return models


def reorder_models(order: list[str]) -> dict[str, Any]:
    with _STATE_LOCK:
        state = _load_state()
        for mid in order:  # seeda ids runtime (yaml) ainda não persistidos, se existirem
            if mid not in state["models"]:
                _ensure_model_record(state, mid)
        known = list(state["models"].keys())
        seq = [mid for mid in order if mid in state["models"]]
        rest = [mid for mid in known if mid not in seq]
        final = seq + rest
        for i, mid in enumerate(final):
            state["models"][mid]["order"] = i
        _save_state(state)
    return {"ok": True, "order": final}


def set_order_position(model_id: str, position: int) -> dict[str, Any]:
    # usa list_models() (mescla runtime+store) pra incluir modelos runtime
    # ainda não persistidos na lista de ids reordenável (§6 do pedido: ▲▼
    # também funciona neles) -- reorder_models() seeda o que precisar.
    all_ids = [m["id"] for m in list_models()]
    if model_id not in all_ids:
        return {"ok": False, "error": "modelo não encontrado"}
    all_ids.remove(model_id)
    position = max(0, min(int(position), len(all_ids)))
    all_ids.insert(position, model_id)
    return reorder_models(all_ids)


def set_visibility(model_id: str, interface: str, visible: bool) -> dict[str, Any]:
    iface = normalize_interface(interface)
    with _STATE_LOCK:
        state = _load_state()
        m = _ensure_model_record(state, model_id)
        if not m:
            return {"ok": False, "error": "modelo não encontrado"}
        m.setdefault("vis", _empty_bool_map())[iface] = bool(visible)
        _save_state(state)
    return {"ok": True}


def set_visible_interfaces(model_id: str, interfaces: list[str]) -> dict[str, Any]:
    wanted = {normalize_interface(x) for x in (interfaces or [])}
    for iface in _interfaces():
        res = set_visibility(model_id, iface, iface in wanted)
        if not res.get("ok"):
            return res
    return {"ok": True}


def set_default(model_id: str, interface: str, is_default: bool) -> dict[str, Any]:
    """No máximo 1 modelo padrão por interface — ativar aqui desativa qualquer outro."""
    iface = normalize_interface(interface)
    with _STATE_LOCK:
        state = _load_state()
        m = _ensure_model_record(state, model_id)
        if not m:
            return {"ok": False, "error": "modelo não encontrado"}
        if is_default:
            for other_id, other in state["models"].items():
                if other_id != model_id:
                    other.setdefault("def", _empty_bool_map())[iface] = False
            m.setdefault("def", _empty_bool_map())[iface] = True
            m.setdefault("vis", _empty_bool_map())[iface] = True  # padrão implica visível
        else:
            m.setdefault("def", _empty_bool_map())[iface] = False
        _save_state(state)
    return {"ok": True}


def set_default_interfaces(model_id: str, interfaces: list[str]) -> dict[str, Any]:
    wanted = {normalize_interface(x) for x in (interfaces or [])}
    for iface in _interfaces():
        res = set_default(model_id, iface, iface in wanted)
        if not res.get("ok"):
            return res
    return {"ok": True}


def _total_ram_bytes() -> int:
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip())
    except Exception:
        pass
    return _FALLBACK_RAM_BYTES


def _model_footprint_bytes(m: dict[str, Any]) -> int:
    try:
        return Path(m.get("file", "")).stat().st_size
    except Exception:
        return int(m.get("size_q4_estimate_bytes") or 0)


def set_active(model_id: str, active: bool) -> dict[str, Any]:
    """Ativar respeita orçamento de RAM: soma do que já está ativo + este modelo
    não pode passar de RAM_BUDGET_RATIO da RAM total (sysctl hw.memsize, senão 64GB)."""
    with _STATE_LOCK:
        state = _load_state()
        m = _ensure_model_record(state, model_id)
        if not m:
            return {"ok": False, "error": "modelo não encontrado"}
        if not active:
            m["active"] = False
            _save_state(state)
            return {"ok": True}
        total = _model_footprint_bytes(m)
        for other_id, other in state["models"].items():
            if other_id != model_id and other.get("active"):
                total += _model_footprint_bytes(other)
        budget = _total_ram_bytes() * RAM_BUDGET_RATIO
        if total > budget:
            return {
                "ok": False,
                "error": "orçamento de RAM excedido",
                "estimated_bytes": total,
                "budget_bytes": budget,
            }
        m["active"] = True
        _save_state(state)
    return {"ok": True}


def record_usage(model_id: str) -> None:
    """Exportado para app/stream.py chamar a cada uso real do modelo (complete/stream_tokens)."""
    with _STATE_LOCK:
        state = _load_state()
        m = _ensure_model_record(state, model_id)
        if not m:
            return
        usage = m.setdefault("usage", {"calls": 0, "last": None})
        usage["calls"] = usage.get("calls", 0) + 1
        usage["last"] = time.time()
        _save_state(state)


def get_usage() -> dict[str, Any]:
    with _STATE_LOCK:
        state = _load_state()
    return {mid: m.get("usage", {"calls": 0, "last": None}) for mid, m in state["models"].items()}


# ---------------------------------------------------------------------------
# Modos de execucao por interface (Onda 4): single/dual/agent, guardado em
# data/models.json (state["exec"][cylinderui|cyber|god]). app/stream.py le esse
# estado (get_exec) no comeco de orchestrate() -- ver backend/model_exec.py
# (orquestracao dual/router, testavel isolado) e patch-stream-exec.py.txt
# (pontos exatos de insercao em app/stream.py, estendendo o patch-stream-usage
# ja escrito pela Onda 3, nunca o substituindo). Este modulo (model_store.py)
# so guarda/valida o estado -- nao sabe nada sobre llama-swap/orquestracao.
# ---------------------------------------------------------------------------
EXEC_MODES = ("single", "dual", "agent")
EXEC_ROLES = ("second", "review", "draft", "router")
_EXEC_TOOL_KEYS = ("files", "rag", "web")


def _default_exec() -> dict[str, Any]:
    return {"mode": "single", "main": None, "aux": None, "role": "second",
            "tools": {k: False for k in _EXEC_TOOL_KEYS}}


def _fill_exec_defaults(rec: dict[str, Any] | None) -> dict[str, Any]:
    out = _default_exec()
    if rec:
        for k in ("mode", "main", "aux", "role"):
            if rec.get(k) is not None:
                out[k] = rec[k]
        if isinstance(rec.get("tools"), dict):
            for k, v in rec["tools"].items():
                if k in out["tools"]:
                    out["tools"][k] = bool(v)
    return out


def get_exec(interface: str) -> dict[str, Any]:
    """Estado de execucao (mode/main/aux/role/tools) de UMA interface
    (aceita codigo curto "C"/"CC"/"GOD" ou nome interno cylinderui/cyber/god).
    Sempre devolve um registro completo (preenchido com defaults) -- nunca
    None, mesmo se a interface nunca foi configurada."""
    iface = normalize_interface(interface)
    with _STATE_LOCK:
        state = _load_state()
        rec = state["exec"].get(iface)
    return _fill_exec_defaults(rec)


def get_exec_all() -> dict[str, Any]:
    """Estado de execucao de TODAS as interfaces (visões) atuais, chaveado
    pelo vision-id -- mesmo vocabulario usado por list_models() em vis/def,
    para nao introduzir um terceiro dialeto de nomes de interface. Onda V2:
    a lista de interfaces é dinâmica (_interfaces()), não mais fixa nas 3
    legadas."""
    with _STATE_LOCK:
        state = _load_state()
        recs = {iface: state["exec"].get(iface) for iface in _interfaces()}
    return {iface: _fill_exec_defaults(rec) for iface, rec in recs.items()}


def set_exec(
    interface: str,
    mode: str | None = None,
    main: str | None = None,
    aux: str | None = None,
    role: str | None = None,
    tools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atualiza (merge parcial) o estado de execucao de uma interface.

    Convencao: campos nao passados (None) mantem o valor atual. Para LIMPAR
    main/aux explicitamente, passe string vazia "" (o seletor "— escolher —"
    do mockup manda "" quando desmarcado) -- vira None no estado.

    Validacao: mode/role precisam estar nos conjuntos conhecidos; main/aux
    (quando nao vazios) precisam ser ids de modelos instalados; aux nao pode
    ser igual a main. tools aceita so as chaves conhecidas (files/rag/web),
    o resto e ignorado silenciosamente (defensivo, igual ao resto do modulo)."""
    iface = normalize_interface(interface)
    with _STATE_LOCK:
        state = _load_state()
        cur = _fill_exec_defaults(state["exec"].get(iface))

        if mode is not None:
            if mode not in EXEC_MODES:
                return {"ok": False, "error": f"modo inválido: {mode!r} (use {EXEC_MODES})"}
            cur["mode"] = mode

        if main is not None:
            main_id = main or None
            if main_id and not _ensure_model_record(state, main_id):
                return {"ok": False, "error": f"modelo principal não encontrado: {main_id!r}"}
            cur["main"] = main_id

        if aux is not None:
            aux_id = aux or None
            if aux_id and not _ensure_model_record(state, aux_id):
                return {"ok": False, "error": f"modelo auxiliar não encontrado: {aux_id!r}"}
            cur["aux"] = aux_id

        if role is not None:
            if role not in EXEC_ROLES:
                return {"ok": False, "error": f"papel inválido: {role!r} (use {EXEC_ROLES})"}
            cur["role"] = role

        if tools is not None:
            if not isinstance(tools, dict):
                return {"ok": False, "error": "tools inválido (esperado objeto)"}
            for k, v in tools.items():
                if k in cur["tools"]:
                    cur["tools"][k] = bool(v)

        if cur["main"] and cur["aux"] and cur["main"] == cur["aux"]:
            return {"ok": False, "error": "modelo auxiliar precisa ser diferente do principal"}

        state["exec"][iface] = cur
        _save_state(state)
    return {"ok": True, "exec": cur}


# ---------------------------------------------------------------------------
# Benchmark / tuning (Onda 3): setters de estado usados por model_bench.py.
# Este módulo (model_store.py) não sabe nada sobre llama-bench -- só guarda
# o que model_bench.py manda, igual ao resto do estado.
# ---------------------------------------------------------------------------
def set_bench(model_id: str, bench: dict[str, Any]) -> dict[str, Any]:
    """bench={pp,tg,lat,threads,date,prof,ext?} -- ver model_bench.py."""
    with _STATE_LOCK:
        state = _load_state()
        m = _ensure_model_record(state, model_id)
        if not m:
            return {"ok": False, "error": "modelo não encontrado"}
        m["bench"] = bench
        _save_state(state)
    return {"ok": True}


def set_tuned(model_id: str, tuned: dict[str, Any]) -> dict[str, Any]:
    """tuned={gain,threads,batch,...} -- ver model_bench.py::optimize_cpu."""
    with _STATE_LOCK:
        state = _load_state()
        m = _ensure_model_record(state, model_id)
        if not m:
            return {"ok": False, "error": "modelo não encontrado"}
        m["tuned"] = tuned
        _save_state(state)
    return {"ok": True}


def apply_cpu_tuning(model_id: str, threads: int, batch: int, mlock: bool = False) -> dict[str, Any]:
    """Atualiza SÓ o bloco marcado `# BEGIN/END MODELSTORE:<id>` no
    llama-swap.yaml, prefixando a linha `cmd:` com variáveis de ambiente
    LLAMA_TUNE_THREADS/LLAMA_TUNE_BATCH/LLAMA_TUNE_MLOCK lidas pelo runner
    (`run-<id>.sh`, ver _RUNNER_TEMPLATE). Nunca reescreve o arquivo inteiro;
    se o modelo não tiver bloco marcado (instalado antes do Model Store, fora
    do escopo de blocos marcados desta feature), retorna ok=False com nota --
    quem chamar deve gravar o tuning no estado mesmo assim (set_tuned)."""
    if not LLAMA_SWAP_YAML.exists():
        return {"ok": False, "error": f"llama-swap.yaml não encontrado: {LLAMA_SWAP_YAML}"}
    text = LLAMA_SWAP_YAML.read_text(encoding="utf-8")
    begin = f"# BEGIN MODELSTORE:{model_id}"
    end = f"# END MODELSTORE:{model_id}"
    start = text.find(begin)
    if start == -1:
        return {
            "ok": False,
            "error": "sem bloco marcado no yaml para este modelo (instalado fora do Model "
                     "Store) -- tuning gravado só no estado, yaml não foi tocado",
        }
    end_idx = text.find(end, start)
    if end_idx == -1:
        return {"ok": False, "error": "marcador de fim não encontrado (BEGIN sem END correspondente)"}
    line_end = text.find("\n", end_idx)
    line_end = line_end + 1 if line_end != -1 else len(text)

    block = text[start:line_end]
    cmd_re = re.compile(r'(?:LLAMA_TUNE_\w+=\S+\s+)*(/bin/bash "[^"]*run-' + re.escape(model_id) + r'\.sh"\s*\$\{PORT\})')
    if not cmd_re.search(block):
        return {"ok": False, "error": "linha cmd não encontrada no bloco marcado (formato inesperado)"}
    env_prefix = (
        f'LLAMA_TUNE_THREADS={int(threads)} LLAMA_TUNE_BATCH={int(batch)} '
        f'LLAMA_TUNE_MLOCK={"1" if mlock else "0"} '
    )
    new_block = cmd_re.sub(lambda mo: env_prefix + mo.group(1), block, count=1)

    _backup_file(LLAMA_SWAP_YAML)
    new_text = text[:start] + new_block + text[line_end:]
    LLAMA_SWAP_YAML.write_text(new_text, encoding="utf-8")
    return {"ok": True}


def prune(ids: list[str] | None = None) -> dict[str, Any]:
    """Remove modelos instalados. Sem `ids`: política automática — remove só
    modelos inativos, sem padrão em nenhuma interface e sem visibilidade em
    nenhuma interface (órfãos reais). Nunca remove um padrão, mesmo se pedido
    explicitamente em `ids` (bloqueado, igual ao uninstall)."""
    with _STATE_LOCK:
        state = _load_state()
        if ids is None:
            target_ids = [
                mid for mid, m in state["models"].items()
                if not m.get("active")
                and not any((m.get("def") or {}).values())
                and not any((m.get("vis") or {}).values())
            ]
        else:
            target_ids = list(ids)

    removed: list[str] = []
    skipped: list[dict[str, str]] = []
    for mid in target_ids:
        m = get_model(mid)
        if not m:
            skipped.append({"id": mid, "reason": "não encontrado"})
            continue
        if any((m.get("def") or {}).values()):
            skipped.append({"id": mid, "reason": "é padrão em alguma interface"})
            continue
        res = uninstall_model(mid)
        if res.get("ok"):
            removed.append(mid)
        else:
            skipped.append({"id": mid, "reason": res.get("error", "erro desconhecido")})
    return {"removed": removed, "skipped": skipped}


# ---------------------------------------------------------------------------
# Busca (Hugging Face + ModelScope best-effort), cache 10 min
# ---------------------------------------------------------------------------
def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "cylinderui-model-store/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _estimate_q4_bytes(text: str) -> int | None:
    m = _SIZE_RE.search(text or "")
    if not m:
        return None
    try:
        params_b = float(m.group(1))
    except Exception:
        return None
    return int(params_b * 1_000_000_000 * 0.6)  # heurística ~0.6 byte/parâmetro p/ Q4_K_M


def _infer_category(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("code", "coder", "coding")):
        return "code"
    if any(k in t for k in ("embed", "embedding")):
        return "embedding"
    if any(k in t for k in ("vision", "-vl", "multimodal")):
        return "vision"
    if any(k in t for k in ("uncensor", "abliterat", "jail")):
        return "uncensored"
    if any(k in t for k in ("security", "cyber", "malware", "pentest", "exploit", "redteam")):
        return "security"
    if any(k in t for k in ("chat", "instruct")):
        return "chat"
    return "general"


def _normalize_hf(item: dict) -> dict[str, Any]:
    mid = item.get("id") or item.get("modelId") or ""
    author = mid.split("/")[0] if "/" in mid else (item.get("author") or "")
    name = mid.split("/")[-1] if "/" in mid else mid
    tags = item.get("tags") or []
    siblings = item.get("siblings") or []
    gguf_files = [
        s.get("rfilename") for s in siblings
        if isinstance(s, dict) and str(s.get("rfilename", "")).lower().endswith(".gguf")
    ]
    blob = " ".join([mid] + [str(t) for t in tags])
    return {
        "id": mid, "name": name, "author": author, "source": "hf",
        "downloads": item.get("downloads", 0) or 0, "likes": item.get("likes", 0) or 0,
        "size_q4_estimate_bytes": _estimate_q4_bytes(blob),
        "tags": tags, "category": _infer_category(blob),
        "files": gguf_files,
    }


def _search_hf(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> tuple[list[dict], bool]:
    try:
        params = {"search": query, "filter": "gguf", "limit": str(limit), "full": "true"}
        url = HF_API_BASE + "?" + urllib.parse.urlencode(params)
        data = _http_get_json(url)
        if not isinstance(data, list):
            return [], False
        return [_normalize_hf(it) for it in data], True
    except Exception:
        return [], False


def _normalize_ms(item: dict) -> dict[str, Any]:
    path = item.get("Path") or item.get("Organization") or ""
    name = item.get("Name") or item.get("id") or ""
    mid = f"{path}/{name}" if path and name else (name or item.get("id", ""))
    blob = " ".join(str(v) for v in item.values() if isinstance(v, (str, int, float)))
    return {
        "id": mid, "name": name or mid, "author": path,
        "source": "modelscope",
        "downloads": item.get("Downloads", 0) or 0,
        "likes": item.get("Star", 0) or item.get("Likes", 0) or 0,
        "size_q4_estimate_bytes": _estimate_q4_bytes(blob),
        "tags": item.get("Tags") or [], "category": _infer_category(blob),
        "files": [],
    }


def _search_ms(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> tuple[list[dict], bool]:
    try:
        params = {"PageSize": str(limit), "PageNumber": "1", "Name": query}
        url = MS_API_BASE + "?" + urllib.parse.urlencode(params)
        data = _http_get_json(url)
        items = (((data or {}).get("Data") or {}).get("Models")) or []
        if not isinstance(items, list):
            return [], False
        return [_normalize_ms(it) for it in items], True
    except Exception:
        return [], False


def search_models(query: str = "", source: str | None = None) -> dict[str, Any]:
    norm_source = _normalize_source(source)
    key = (query or "", norm_source or "both")
    now = time.time()
    cached = _SEARCH_CACHE.get(key)
    if cached and (now - cached["ts"]) < SEARCH_CACHE_TTL:
        out = dict(cached["result"])
        out["cached"] = True
        return out

    hf_results, hf_ok = ([], True)
    ms_results, ms_ok = ([], True)
    if norm_source in (None, "hf"):
        hf_results, hf_ok = _search_hf(query)
    if norm_source in (None, "modelscope"):
        ms_results, ms_ok = _search_ms(query)

    out = {
        "query": query, "source": norm_source or "both",
        "hf_ok": hf_ok, "ms_ok": ms_ok,
        "results": hf_results + ms_results, "cached": False,
    }
    _SEARCH_CACHE[key] = {"ts": now, "result": out}
    return out


# ---------------------------------------------------------------------------
# Fila de downloads (thread única em background, 1 download simultâneo)
# ---------------------------------------------------------------------------
def _build_download_url(repo_id: str, file: str, source: str) -> str:
    if source == "hf":
        return f"https://huggingface.co/{repo_id}/resolve/main/{urllib.parse.quote(file)}"
    if source == "modelscope":
        return (
            f"https://modelscope.cn/api/v1/models/{repo_id}/repo?"
            f"Revision=master&FilePath={urllib.parse.quote(file)}"
        )
    raise ValueError(f"fonte inválida: {source!r}")


def _active_download_exists(exclude_id: str | None = None) -> dict | None:
    for d in _DOWNLOADS.values():
        if d["id"] != exclude_id and d["status"] in ("queued", "downloading", "pausing"):
            return d
    return None


def start_download(repo_id: str, file: str, source: str = "hf", auto_install: bool = True) -> dict[str, Any]:
    source = _normalize_source(source) or "hf"
    _validate_repo_id(repo_id)
    _validate_filename(file)
    if source not in ("hf", "modelscope"):
        raise ValueError(f"fonte inválida: {source!r}")

    with _DL_LOCK:
        existing = _active_download_exists()
        if existing:
            return {"ok": False, "error": "já existe um download em andamento (limite: 1 simultâneo)",
                     "download_id": existing["id"]}
        dl_id = uuid.uuid4().hex[:12]
        slug = _slugify(repo_id)
        dest = MODELS_DIR / slug / file
        rec = {
            "id": dl_id, "repo_id": repo_id, "file": file, "source": source,
            "url": _build_download_url(repo_id, file, source),
            "dest": str(dest), "slug": slug,
            "bytes": 0, "total": 0, "pct": 0.0,
            "status": "queued", "error": None, "auto_install": bool(auto_install),
            "created": time.time(), "updated": time.time(),
        }
        _DOWNLOADS[dl_id] = rec

    _DL_QUEUE.put(dl_id)
    _ensure_worker()
    return {"ok": True, "download": dict(rec)}


def install_model(repo_id: str, file: str, source: str = "hf") -> dict[str, Any]:
    """Endpoint /api/store/install: enfileira o download; ao concluir, o
    worker instala automaticamente (bloco no llama-swap.yaml + estado)."""
    return start_download(repo_id, file, source=source, auto_install=True)


def list_downloads() -> list[dict[str, Any]]:
    with _DL_LOCK:
        return sorted((dict(d) for d in _DOWNLOADS.values()), key=lambda d: d["created"])


def get_download(download_id: str) -> dict[str, Any] | None:
    with _DL_LOCK:
        d = _DOWNLOADS.get(download_id)
        return dict(d) if d else None


def pause_download(download_id: str) -> dict[str, Any]:
    with _DL_LOCK:
        rec = _DOWNLOADS.get(download_id)
        if not rec:
            return {"ok": False, "error": "download não encontrado"}
        if rec["status"] != "downloading":
            return {"ok": False, "error": "download não está em andamento"}
        rec["status"] = "pausing"
        rec["updated"] = time.time()
    return {"ok": True}


def resume_download(download_id: str) -> dict[str, Any]:
    with _DL_LOCK:
        rec = _DOWNLOADS.get(download_id)
        if not rec:
            return {"ok": False, "error": "download não encontrado"}
        if rec["status"] != "paused":
            return {"ok": False, "error": "download não está pausado"}
        if _active_download_exists(exclude_id=download_id):
            return {"ok": False, "error": "já existe um download em andamento (limite: 1 simultâneo)"}
        rec["status"] = "queued"
        rec["updated"] = time.time()
    _DL_QUEUE.put(download_id)
    _ensure_worker()
    return {"ok": True}


def cancel_download(download_id: str) -> dict[str, Any]:
    with _DL_LOCK:
        rec = _DOWNLOADS.get(download_id)
        if not rec:
            return {"ok": False, "error": "download não encontrado"}
        rec["status"] = "canceled"
        rec["updated"] = time.time()
        dest = rec["dest"]
    tmp = Path(dest).with_suffix(Path(dest).suffix + ".part")
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass
    return {"ok": True}


def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            t = threading.Thread(target=_worker_loop, name="modelstore-downloader", daemon=True)
            t.start()
            _WORKER_STARTED = True


def _worker_loop() -> None:
    while True:
        dl_id = _DL_QUEUE.get()
        with _DL_LOCK:
            rec = _DOWNLOADS.get(dl_id)
            status = rec["status"] if rec else None
        if not rec or status not in ("queued",):
            continue
        _run_download(dl_id)


def _run_download(dl_id: str) -> None:
    with _DL_LOCK:
        rec = _DOWNLOADS.get(dl_id)
        if not rec or rec["status"] != "queued":
            return
        rec["status"] = "downloading"
        rec["updated"] = time.time()
        dest = Path(rec["dest"])
        url = rec["url"]

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        with _DL_LOCK:
            rec["status"] = "error"
            rec["error"] = f"falha ao criar diretório de destino: {e}"
        return

    resume_from = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "cylinderui-model-store/1.0"})
    if resume_from:
        req.add_header("Range", f"bytes={resume_from}-")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status_code = getattr(resp, "status", 200)
            length_hdr = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            total = int(length_hdr) if length_hdr else 0
            if resume_from and status_code == 206:
                total += resume_from
            elif resume_from and status_code != 206:
                resume_from = 0  # servidor ignorou Range: recomeça do zero

            with _DL_LOCK:
                rec["total"] = total or rec.get("total", 0)

            mode = "ab" if resume_from else "wb"
            downloaded = resume_from
            with open(tmp, mode) as f:
                while True:
                    with _DL_LOCK:
                        cur_status = rec["status"]
                    if cur_status == "pausing":
                        with _DL_LOCK:
                            rec["status"] = "paused"
                            rec["updated"] = time.time()
                        return
                    if cur_status == "canceled":
                        return
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    with _DL_LOCK:
                        rec["bytes"] = downloaded
                        if not rec["total"]:
                            rec["total"] = downloaded
                        rec["pct"] = round(downloaded / rec["total"] * 100, 1) if rec["total"] else 0.0
                        rec["updated"] = time.time()

        tmp.replace(dest)
        with _DL_LOCK:
            rec["status"] = "done"
            rec["bytes"] = downloaded
            rec["total"] = rec["total"] or downloaded
            rec["pct"] = 100.0
            rec["updated"] = time.time()
            auto_install = rec.get("auto_install")
        if auto_install:
            _finalize_install(dl_id)
    except Exception as e:
        with _DL_LOCK:
            rec["status"] = "error"
            rec["error"] = str(e)
            rec["updated"] = time.time()


# ---------------------------------------------------------------------------
# Instalar / desinstalar (llama-swap.yaml por bloco marcado + estado)
# ---------------------------------------------------------------------------
_RUNNER_TEMPLATE = """#!/bin/bash
# Instalado via Model Store (CylinderUI) - __REPO_ID__
set -u
PORT="${1:-${PORT:-}}"
[ -z "$PORT" ] && { echo "PORT was not provided" >&2; exit 1; }
SERVER="__SERVER__"
MODEL="__MODEL__"
LOG="__LOG__"
[ -f "$MODEL" ] || { echo "GGUF ausente em $MODEL" >&2; exit 1; }
HELP="$("$SERVER" --help 2>&1 || true)"
supports(){ printf '%s\\n' "$HELP" | grep -F -- "$1" >/dev/null 2>&1; }
# LLAMA_TUNE_* (opcionais): gravados pelo Otimizar CPU (Onda 3, model_bench.py)
# como prefixo de env var na linha cmd: do bloco marcado deste modelo no
# llama-swap.yaml (ver model_store.apply_cpu_tuning). Sem tuning aplicado,
# caem nos defaults de sempre (8 threads / batch 512 / sem mlock).
THREADS="${LLAMA_TUNE_THREADS:-8}"
BATCH="${LLAMA_TUNE_BATCH:-512}"
MLOCK="${LLAMA_TUNE_MLOCK:-0}"
ARGS=( --model "$MODEL" --alias "__MODEL_ID__" --host 127.0.0.1 --port "$PORT" )
add_arg(){ flag="$1"; value="$2"; supports "$flag" && ARGS+=("$flag" "$value"); }
add_switch(){ flag="$1"; supports "$flag" && ARGS+=("$flag"); }
add_arg --ctx-size "8192"
add_arg --threads "$THREADS"
add_arg --threads-batch "$THREADS"
add_arg --batch-size "$BATCH"
add_arg --ubatch-size "$BATCH"
add_arg --parallel 1
add_arg --n-gpu-layers 0
add_arg --cache-type-k q8_0
add_arg --cache-type-v q8_0
add_arg --flash-attn "on"
add_switch --jinja
add_switch --cont-batching
[ "$MLOCK" = "1" ] && add_switch --mlock
{ echo; echo "=== $(date) port=$PORT model=$MODEL"; } >> "$LOG"
exec "$SERVER" "${ARGS[@]}" >> "$LOG" 2>&1
"""


def _write_runner_script(model_id: str, model_path: str, repo_id: str = "") -> Path:
    LLAMA_SWAP_RUNNERS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = str(LOG_DIR / f"upstream-{model_id}.log")
    content = (
        _RUNNER_TEMPLATE
        .replace("__REPO_ID__", repo_id)
        .replace("__SERVER__", str(LLAMA_SERVER_BIN))
        .replace("__MODEL__", model_path)
        .replace("__LOG__", log_path)
        .replace("__MODEL_ID__", model_id)
    )
    path = LLAMA_SWAP_RUNNERS_DIR / f"run-{model_id}.sh"
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o755)
    except Exception:
        pass
    return path


def _yaml_block_text(model_id: str, friendly_name: str, description: str) -> str:
    begin = f"# BEGIN MODELSTORE:{model_id}"
    end = f"# END MODELSTORE:{model_id}"
    return (
        f"  {begin}\n"
        f'  "{model_id}":\n'
        f'    name: "{friendly_name}"\n'
        f'    description: "{description}"\n'
        f"    cmd: >-\n"
        f'      /bin/bash "{LLAMA_SWAP_RUNNERS_DIR}/run-{model_id}.sh" ${{PORT}}\n'
        f"    ttl: 1800\n"
        f"  {end}\n\n"
    )


def _install_yaml_block(model_id: str, friendly_name: str, description: str = "Instalado via Model Store") -> dict[str, Any]:
    """Só append de um bloco marcado — nunca parseia/reescreve o yaml inteiro.
    Insere logo antes de um eventual `routing:` de topo (senão, no fim do
    arquivo), pois models: precisa continuar acima de routing: no YAML real."""
    if not LLAMA_SWAP_YAML.exists():
        return {"ok": False, "error": f"llama-swap.yaml não encontrado: {LLAMA_SWAP_YAML}"}
    text = LLAMA_SWAP_YAML.read_text(encoding="utf-8")
    begin = f"# BEGIN MODELSTORE:{model_id}"
    if begin in text:
        return {"ok": True, "already": True}

    block = _yaml_block_text(model_id, friendly_name, description)
    anchor = "\nrouting:"
    idx = text.find(anchor)
    if idx == -1:
        new_text = text.rstrip("\n") + "\n\n" + block
    else:
        new_text = text[: idx + 1] + block + text[idx + 1:]

    _backup_file(LLAMA_SWAP_YAML)
    LLAMA_SWAP_YAML.write_text(new_text, encoding="utf-8")
    return {"ok": True}


def _remove_yaml_block(model_id: str) -> dict[str, Any]:
    if not LLAMA_SWAP_YAML.exists():
        return {"ok": True, "removed": False}
    text = LLAMA_SWAP_YAML.read_text(encoding="utf-8")
    begin = f"# BEGIN MODELSTORE:{model_id}"
    end = f"# END MODELSTORE:{model_id}"
    start = text.find(begin)
    if start == -1:
        return {"ok": True, "removed": False}
    line_start = text.rfind("\n", 0, start) + 1
    end_idx = text.find(end, start)
    if end_idx == -1:
        return {"ok": False, "error": "marcador de fim não encontrado (BEGIN sem END correspondente)"}
    line_end = text.find("\n", end_idx)
    line_end = line_end + 1 if line_end != -1 else len(text)
    if text[line_end:line_end + 1] == "\n":  # engole 1 linha em branco extra deixada pelo install
        line_end += 1

    _backup_file(LLAMA_SWAP_YAML)
    new_text = text[:line_start] + text[line_end:]
    LLAMA_SWAP_YAML.write_text(new_text, encoding="utf-8")
    return {"ok": True, "removed": True}


def _finalize_install(dl_id: str) -> dict[str, Any]:
    rec = get_download(dl_id)
    if not rec:
        return {"ok": False, "error": "download não encontrado"}
    model_id = rec["slug"]
    friendly = _friendly_name(rec["repo_id"])
    _write_runner_script(model_id, rec["dest"], repo_id=rec["repo_id"])
    yaml_res = _install_yaml_block(model_id, friendly, description=f"Instalado via Model Store — {rec['repo_id']}")

    with _STATE_LOCK:
        state = _load_state()
        order = max([m.get("order", -1) for m in state["models"].values()], default=-1) + 1
        state["models"][model_id] = {
            "id": model_id, "name": friendly, "repo_id": rec["repo_id"], "source": rec["source"],
            "file": rec["dest"],
            "vis": _empty_bool_map(), "def": _empty_bool_map(),
            "order": order, "active": False,
            "usage": {"calls": 0, "last": None},
            "bench": {}, "tuned": {},
            "installed_at": time.time(),
        }
        _save_state(state)
    return {"ok": True, "model_id": model_id, "yaml": yaml_res}


def uninstall_model(model_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        state = _load_state()
        m = state["models"].get(model_id)
        store_managed = _is_store_managed(model_id, m)

        if not store_managed:
            # Modelo runtime/externo (sem bloco marcado no yaml e não criado
            # pela loja) -- mesmo que já tenha um registro lazy em models.json
            # (por ex. o usuário ativou/tornou padrão). Nunca editamos o
            # llama-swap.yaml compartilhado por segurança (§4 do pedido).
            known = bool(m) or (_seed_from_runtime(model_id) is not None)
            if not known:
                return {"ok": False, "error": "modelo não encontrado"}
            return {
                "ok": False,
                "error": "Modelo instalado fora da Model Store — para removê-lo, edite o llama-swap.yaml manualmente",
                "external": True,
            }

        if not m:
            return {"ok": False, "error": "modelo não encontrado"}
        if any((m.get("def") or {}).values()):
            return {"ok": False, "error": "modelo é padrão em alguma interface; remova o padrão antes de desinstalar"}
        file_path = m.get("file", "")

    yaml_res = _remove_yaml_block(model_id)
    try:
        f = Path(file_path)
        if f.exists():
            f.unlink()
        try:
            if f.parent.exists() and f.parent != MODELS_DIR and not any(f.parent.iterdir()):
                f.parent.rmdir()
        except Exception:
            pass
    except Exception as e:
        return {"ok": False, "error": f"falha ao remover arquivo do modelo: {e}"}

    runner = LLAMA_SWAP_RUNNERS_DIR / f"run-{model_id}.sh"
    try:
        if runner.exists():
            runner.unlink()
    except Exception:
        pass

    with _STATE_LOCK:
        state = _load_state()
        state["models"].pop(model_id, None)
        _save_state(state)
    return {"ok": True, "yaml": yaml_res}
