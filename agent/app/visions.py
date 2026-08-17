# -*- coding: utf-8 -*-
"""
visions.py — Visões do CylinderUI (versão de DISTRIBUIÇÃO pública)

Sistema de VISÕES dinâmicas (CRUD, estilo Spaces do Mac). Cada visão é um
"desktop" isolado: modelos permitidos + modelo padrão, modo de execução
(single/dual/agent), chats próprios (conversationKey), system prompt, tema,
hero, badge e a opção de enxergar (ou não) chats de outras visões.

Estado em data/visions.json:
    {"visions": [...], "version": 1, "meta": {"seeded": true}}

NOTA: em versões anteriores este módulo seedava várias visões de exemplo.
Nesta versão de distribuição pública o seed é NEUTRO: uma única visão
exemplo "CylinderUI" (tema warm default, prompt genérico, sem modelos — o
usuário escolhe no Model Store). Toda a LÓGICA (endpoints, CRUD, merge,
normalização) é idêntica; só os DADOS seedados mudam.

A visão exemplo (id ESTÁVEL "cylinderui") é seedada uma única vez
(_seed_if_empty, controlado por meta.seeded — depois de seedar, um delete
manual NÃO dispara novo seed). É "builtin" mas totalmente livre: pode ser
editada e apagada como qualquer outra visão.

Mesmo padrão de app/model_store.py: só stdlib (json, os, re, threading,
time, uuid), estado em JSON com load/save defensivo e lock em memória
(_STATE_LOCK). Este módulo é autocontido — não importa nada de
app/model_store.py nem é importado por ele; a integração com o Model Store
é só consumir get_vision_ids() no lugar da tupla fixa INTERFACES.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

try:  # quando vive dentro de app/ (produção), reaproveita o caminho real
    from .config import DATA_DIR as _CFG_DATA_DIR  # type: ignore
except Exception:
    _CFG_DATA_DIR = None

# ---------------------------------------------------------------------------
# Caminhos (com fallback autocontido para rodar fora do pacote app/, ex. testes)
# ---------------------------------------------------------------------------
DATA_DIR = Path(_CFG_DATA_DIR) if _CFG_DATA_DIR else Path(
    os.getenv("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
).resolve()

VISIONS_STATE_FILE = DATA_DIR / "visions.json"

for _p in (DATA_DIR,):
    try:
        _p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

_STATE_LOCK = threading.RLock()

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Mesmo vocabulário de app/model_store.py::EXEC_MODES/EXEC_ROLES — mantido
# duplicado aqui de propósito (módulo autocontido, sem import cruzado); se
# um dia divergirem, este módulo é a fonte de verdade da VISÃO, não do
# modelo em si.
EXEC_MODES = ("single", "dual", "agent")
EXEC_ROLES = ("second", "review", "draft", "router")

# Tema (string simples: "cylinderui" | "chatgpt" | "ios9"; null/ausente = warm
# default "cylinderui" no front) da visão seedada. Contrato alinhado com o
# frontend: `theme` é o ESTILO da UI; `color` (cor do ícone na sidebar) é um
# campo SEPARADO. Aplicado de forma idempotente e só quando o theme está
# vazio/null (nunca sobrescreve uma escolha do usuário) — ver _seed_if_empty.
_DEFAULT_THEMES: dict[str, str] = {
    "cylinderui": "cylinderui",
}
# Tema default de qualquer visão nova criada pelo usuário (null->cylinderui no front).
_NEW_VISION_DEFAULT_THEME = "cylinderui"

# ---------------------------------------------------------------------------
# Seed da visão exemplo NEUTRA de distribuição. Valores literais congelados
# aqui — depois do seed inicial, o "dono" dos dados passa a ser visions.json;
# este dicionário nunca é lido de novo (só na primeira inicialização do
# arquivo). Uma instalação limpa mostra UMA visão "CylinderUI" genérica:
# hero do logo, prompt genérico, sem modelos (o usuário escolhe no Model
# Store), sem nenhuma marca pessoal.
# ---------------------------------------------------------------------------
_SEED_VISIONS: list[dict[str, Any]] = [
    {
        "id": "cylinderui",
        "name": "CylinderUI",
        "order": 0,
        "builtin": True,
        "theme": "cylinderui",  # Warm / warm default
        "color": "#d97757",  # accent laranja neutro (cor do ícone na sidebar)
        "badge": "",
        "systemPrompt": "You are a helpful local AI assistant.",
        "models": [],  # vazio: o usuário escolhe os modelos no Model Store
        "defaultModel": None,
        "exec": {
            "mode": "single",
            "main": None,
            "aux": None,
            "role": "review",
            "tools": {"files": True, "rag": True, "web": False},
        },
        "conversationKey": "cylinderui-conversations",
        "showOtherVisionsChats": False,
        "hero": {
            "image": "",  # vazio => frontend usa o hero do logo CylinderUI
            "title": "CylinderUI",
            "subtitle": "Your local AI, your way — create a Space to begin.",
            "chips": [],
        },
    },
]


# Campo `hero` da visão (contrato alinhado com o frontend): objeto opcional
# {image, title, subtitle, chips[]}, todas as chaves opcionais. Ausente/vazio
# => frontend usa o fallback atual (emptyHTML). O hero é SUBSTITUÍDO inteiro
# quando enviado num update (o frontend sempre manda o objeto completo); nunca
# há merge de sub-campos.
_HERO_STR_KEYS = ("image", "title", "subtitle")


def _normalize_hero(hero_in: Any) -> dict[str, Any]:
    """Valida/normaliza o campo hero de uma visão. Aceita só um dict com as
    chaves esperadas (image/title/subtitle: string; chips: list[str]); chaves
    extras são ignoradas. None/ausente/não-dict => {} (frontend faz fallback).
    Strings vazias são preservadas (ex.: image \"\" => fallback no front)."""
    if not isinstance(hero_in, dict):
        return {}
    out: dict[str, Any] = {}
    for k in _HERO_STR_KEYS:
        if hero_in.get(k) is not None:
            out[k] = str(hero_in[k])
    chips = hero_in.get("chips")
    if isinstance(chips, (list, tuple)):
        out["chips"] = [str(c) for c in chips]
    return out


def _default_exec(default_model: str | None) -> dict[str, Any]:
    return {
        "mode": "single",
        "main": default_model,
        "aux": None,
        "role": "review",
        "tools": {"files": True, "rag": True, "web": False},
    }


def _fill_exec_defaults(rec: dict[str, Any] | None, default_model: str | None) -> dict[str, Any]:
    out = _default_exec(default_model)
    if isinstance(rec, dict):
        for k in ("mode", "main", "aux", "role"):
            if rec.get(k) is not None:
                out[k] = rec[k]
        if isinstance(rec.get("tools"), dict):
            for k, v in rec["tools"].items():
                if k in out["tools"]:
                    out["tools"][k] = bool(v)
    return out


# ---------------------------------------------------------------------------
# Estado (data/visions.json)
# ---------------------------------------------------------------------------
def _default_state() -> dict[str, Any]:
    return {"visions": [], "version": 1, "meta": {"seeded": False}}


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(VISIONS_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        data.setdefault("visions", [])
        data.setdefault("version", 1)
        data.setdefault("meta", {})
        data["meta"].setdefault("seeded", False)
        if not isinstance(data["visions"], list):
            data["visions"] = []
        return data
    except Exception:
        return _default_state()


def _save_state(state: dict[str, Any]) -> None:
    VISIONS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    VISIONS_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


def _strip_diacritics(text: str) -> str:
    """Remove acentos/diacríticos preservando a letra base (NFKD + descarte
    dos caracteres combinantes), ex.: 'Visão' -> 'Visao'. Usado só no slug
    (id); o campo `name` exibido continua com acentos normais."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _slugify(name: str) -> str:
    ascii_name = _strip_diacritics(name)
    slug = _SLUG_RE.sub("-", ascii_name.lower()).strip("-")
    return (slug or "visao")[:60]


def _unique_id(state: dict[str, Any], base: str) -> str:
    existing = {v["id"] for v in state["visions"]}
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _normalize_exec(exec_in: dict[str, Any] | None, default_model: str | None) -> dict[str, Any]:
    """Valida e preenche defaults do campo exec de uma visão. Levanta
    ValueError em modo/role desconhecidos — mesma postura de model_store."""
    out = _fill_exec_defaults(exec_in, default_model)
    if out["mode"] not in EXEC_MODES:
        raise ValueError(f"exec.mode inválido: {out['mode']!r} (use {EXEC_MODES})")
    if out["role"] not in EXEC_ROLES:
        raise ValueError(f"exec.role inválido: {out['role']!r} (use {EXEC_ROLES})")
    return out


def _build_vision_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Monta um registro de visão completo (todos os campos do schema,
    preenchidos com defaults sensatos) a partir de um dict parcial."""
    default_model = raw.get("defaultModel")
    return {
        "id": raw["id"],
        "name": raw.get("name") or raw["id"],
        "order": int(raw.get("order", 0)),
        "builtin": bool(raw.get("builtin", False)),
        # theme = ESTILO da UI (string: "cylinderui"|"chatgpt"|"ios9"; null->cylinderui no front)
        "theme": raw.get("theme", None),
        # color = cor do ícone na sidebar (string simples), campo SEPARADO do theme
        "color": raw.get("color", None),
        # badge = emblema por visão (string: data URI/URL de imagem, pode ""),
        # campo SEPARADO de theme/color; opcional, default "" (sem emblema).
        "badge": str(raw.get("badge") or ""),
        "systemPrompt": raw.get("systemPrompt", "") or "",
        "models": list(raw.get("models") or []),
        "defaultModel": default_model,
        "exec": _normalize_exec(raw.get("exec"), default_model),
        "conversationKey": raw.get("conversationKey") or f"{raw['id']}-conversations",
        "showOtherVisionsChats": bool(raw.get("showOtherVisionsChats", False)),
        # hero = objeto opcional {image,title,subtitle,chips[]}; default {} (fallback)
        "hero": _normalize_hero(raw.get("hero")),
        "createdAt": raw.get("createdAt") or time.time(),
    }


# ---------------------------------------------------------------------------
# Seed (idempotente — controlado por meta.seeded, não pela lista estar vazia)
# ---------------------------------------------------------------------------
def _seed_if_empty() -> None:
    """Cria a visão exemplo neutra na primeira inicialização de
    data/visions.json. Idempotente: uma vez que meta.seeded=true, nunca mais
    reseeda — mesmo que o usuário apague a visão manualmente depois (delete não
    reseta a flag, ver delete_vision).

    Além do seed, faz um BACKFILL idempotente do tema default (_DEFAULT_THEMES):
    se uma visão seedada existente estiver com theme vazio/null, aplica o
    default correspondente. Nunca sobrescreve um theme já escolhido pelo
    usuário."""
    with _STATE_LOCK:
        state = _load_state()
        changed = False
        if not state["meta"].get("seeded"):
            existing_ids = {v.get("id") for v in state["visions"]}
            for seed in _SEED_VISIONS:
                if seed["id"] in existing_ids:
                    continue
                state["visions"].append(_build_vision_record(dict(seed)))
            state["meta"]["seeded"] = True
            changed = True
        for v in state["visions"]:
            # normaliza legado: theme salvo como OBJETO {"color": ...} (conflação
            # theme/color de versões antigas) -> extrai a cor pro campo `color`
            # (se vazio) e zera o theme (null->cylinderui no front). theme passa a
            # ser sempre string|null, conforme o contrato do frontend.
            if isinstance(v.get("theme"), dict):
                legacy_color = v["theme"].get("color")
                if not v.get("color") and isinstance(legacy_color, str):
                    v["color"] = legacy_color
                v["theme"] = None
                changed = True
            # backfill idempotente do theme default da seedada (só se vazio/null)
            default_theme = _DEFAULT_THEMES.get(v.get("id"))
            if default_theme and not v.get("theme"):
                v["theme"] = default_theme
                changed = True
        if changed:
            _save_state(state)


# ---------------------------------------------------------------------------
# API pública (CRUD)
# ---------------------------------------------------------------------------
def list_visions() -> list[dict[str, Any]]:
    """Lista todas as visões, ordenadas por `order` (empate: `id`)."""
    _seed_if_empty()
    with _STATE_LOCK:
        state = _load_state()
    visions = list(state["visions"])
    visions.sort(key=lambda v: (v.get("order", 0), v.get("id", "")))
    return visions


def get_vision(vision_id: str) -> dict[str, Any] | None:
    _seed_if_empty()
    with _STATE_LOCK:
        state = _load_state()
    for v in state["visions"]:
        if v.get("id") == vision_id:
            return dict(v)
    return None


def get_vision_ids() -> list[str]:
    """Helper para o Model Store consumir a lista dinâmica de visões no lugar
    da tupla fixa INTERFACES — ordenada igual list_visions(), só que
    devolvendo os ids."""
    return [v["id"] for v in list_visions()]


def create_vision(name: str, **fields: Any) -> dict[str, Any]:
    """Cria uma visão nova (nunca builtin). Gera um id slug único a partir
    do nome, order = último order + 1 (a menos que 'order' seja passado
    explicitamente em fields), createdAt = agora."""
    _seed_if_empty()
    if not name or not str(name).strip():
        raise ValueError("name é obrigatório")
    fields = dict(fields)
    fields.pop("builtin", None)  # criado via API nunca é builtin
    fields.pop("id", None)  # id é sempre gerado aqui
    with _STATE_LOCK:
        state = _load_state()
        base_slug = _slugify(str(name))
        new_id = _unique_id(state, base_slug)
        if "order" not in fields or fields["order"] is None:
            max_order = max([v.get("order", -1) for v in state["visions"]], default=-1)
            fields["order"] = max_order + 1
        # theme default "cylinderui" para visões novas (null->cylinderui no front);
        # só preenche se o usuário não passou um theme explícito.
        if not fields.get("theme"):
            fields["theme"] = _NEW_VISION_DEFAULT_THEME
        raw = {"id": new_id, "name": name, "builtin": False, **fields}
        record = _build_vision_record(raw)
        state["visions"].append(record)
        _save_state(state)
    return dict(record)


def update_vision(vision_id: str, **fields: Any) -> dict[str, Any]:
    """Merge parcial dos campos passados (None = não mexe, exceto onde faz
    sentido limpar explicitamente, ver exec/theme/defaultModel). `id` e
    `builtin` nunca são alterados por aqui (builtin é permanente, mas a
    visão builtin continua 100% editável/apagável no resto)."""
    _seed_if_empty()
    fields = dict(fields)
    fields.pop("id", None)
    fields.pop("builtin", None)
    fields.pop("createdAt", None)
    with _STATE_LOCK:
        state = _load_state()
        target = None
        for v in state["visions"]:
            if v.get("id") == vision_id:
                target = v
                break
        if target is None:
            raise KeyError(f"visão não encontrada: {vision_id!r}")

        merged = dict(target)
        for key in (
            "name", "order", "theme", "color", "badge", "systemPrompt", "models",
            "defaultModel", "conversationKey", "showOtherVisionsChats",
            # hero: substitui o objeto INTEIRO quando enviado (nunca merge de
            # sub-campos); ausente/None => mantém o hero atual da visão.
            "hero",
        ):
            if key in fields and fields[key] is not None:
                merged[key] = fields[key]

        # exec: merge parcial dentro do próprio exec (não substitui o objeto inteiro)
        if "exec" in fields and fields["exec"] is not None:
            base_exec = dict(target.get("exec") or {})
            base_exec.update({k: v for k, v in fields["exec"].items() if v is not None})
            if isinstance(fields["exec"].get("tools"), dict):
                tools = dict((target.get("exec") or {}).get("tools") or {})
                tools.update(fields["exec"]["tools"])
                base_exec["tools"] = tools
            merged["exec"] = _normalize_exec(base_exec, merged.get("defaultModel"))
        else:
            merged["exec"] = _normalize_exec(merged.get("exec"), merged.get("defaultModel"))

        merged["id"] = vision_id
        merged["builtin"] = bool(target.get("builtin", False))
        merged["createdAt"] = target.get("createdAt")
        record = _build_vision_record(merged)

        idx = state["visions"].index(target)
        state["visions"][idx] = record
        _save_state(state)
    return dict(record)


def delete_vision(vision_id: str) -> dict[str, Any]:
    """Remove a visão (builtin ou não — todas são totalmente livres). Não
    apaga conversationKey/dados de conversa (isso é client-side, este
    backend não gerencia chats); não re-seeda automaticamente (meta.seeded
    já é true, permanece true — ver _seed_if_empty)."""
    _seed_if_empty()
    with _STATE_LOCK:
        state = _load_state()
        before = len(state["visions"])
        state["visions"] = [v for v in state["visions"] if v.get("id") != vision_id]
        if len(state["visions"]) == before:
            return {"ok": False, "error": f"visão não encontrada: {vision_id!r}"}
        _save_state(state)
    return {"ok": True}


def reorder_visions(ids_ordenados: list[str]) -> dict[str, Any]:
    """Aplica uma nova ordem explícita. ids não citados mantêm a ordem
    relativa entre si e vão para o fim (mesma convenção de
    model_store.reorder_models)."""
    _seed_if_empty()
    with _STATE_LOCK:
        state = _load_state()
        known = [v["id"] for v in state["visions"]]
        seq = [vid for vid in ids_ordenados if vid in known]
        rest = [vid for vid in known if vid not in seq]
        final = seq + rest
        by_id = {v["id"]: v for v in state["visions"]}
        for i, vid in enumerate(final):
            by_id[vid]["order"] = i
        _save_state(state)
    return {"ok": True, "order": final}
