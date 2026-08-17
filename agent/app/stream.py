# -*- coding: utf-8 -*-
"""
Orquestração em streaming (SSE) para o Console Local (CylinderUI).

Reaproveita as ferramentas existentes (app/tools.py) e o Router para
classificar o modelo; a GERAÇÃO final é transmitida (stream=True) direto do
llama-swap, dando streaming real de tokens.

`orchestrate(cfg)` é um GERADOR que produz tuplas (evento, dados):
  ("status", {"stage": "web_search" | "reading_knowledge" | "searching_logs"
                       | "fetch_url" | "thinking" | "generating"})
  ("tool",   {"name": str, "ok": bool})
  ("token",  {"delta": str})
  ("usage",  {"prompt_tokens": int, "completion_tokens": int, "context_window": int})
  ("done",   {"finish_reason": "stop"})
  ("error",  {"message": str})

Os switches vêm de `cfg`:
  model, messages[list], system_prompt, temperature, max_tokens,
  agent_mode(bool), web_search("off"|"auto"|"always"), knowledge(bool), logs(bool)
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterator

import httpx

from .config import ROUTER_URL, LLAMA_SWAP_URL, DEFAULT_MODEL, BASE_DIR
from .tools import SCHEMAS, FUNCS, should_search, web_search
from .fs_tools import FS_SCHEMAS, FS_FUNCS  # PATCH-DIRTOOLS-V1
from .model_store import record_usage  # PATCH-MODELUSAGE-V1
from .model_store import get_exec  # PATCH-MODELEXEC-V1
from .model_exec import run_dual  # PATCH-MODELEXEC-V1

SYSTEM_DEFAULT = (BASE_DIR / "system_prompt.md").read_text(encoding="utf-8")

# Router raiz (sem o /v1) para chamar /router/classify.
ROUTER_ROOT = ROUTER_URL[:-3] if ROUTER_URL.endswith("/v1") else ROUTER_URL
_HEADERS = {"Authorization": "Bearer local", "Content-Type": "application/json"}

# PATCH-DIRTOOLS-V1: pool combinado para lookup de execução das tools.
# tools.py continua intocado; a mescla acontece só aqui.
ALL_FUNCS = {**FUNCS, **FS_FUNCS}

# ferramentas -> estágio exibido na UI
_STAGE = {
    "web_search": "web_search",
    "fetch_url": "fetch_url",
    "list_knowledge": "reading_knowledge",
    "read_knowledge": "reading_knowledge",
    "search_logs": "searching_logs",
    "list_directory": "listing_directory",     # PATCH-DIRTOOLS-V1
    "read_file": "reading_file",               # PATCH-DIRTOOLS-V1
    "write_file": "writing_file",              # PATCH-DIRTOOLS-V1
    "edit_file": "editing_file",               # PATCH-DIRTOOLS-V1
}


def classify_model(model: str | None, messages: list[dict[str, Any]]) -> str:
    """Se model for vazio/'auto', pede ao Router o modelo mais adequado."""
    if model and model.strip().lower() not in ("", "auto"):
        return model
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(
                f"{ROUTER_ROOT}/router/classify",
                headers=_HEADERS,
                json={"model": "auto", "messages": messages},
            )
            r.raise_for_status()
            return r.json().get("selected_model") or DEFAULT_MODEL or (model or "")
    except Exception:
        return DEFAULT_MODEL or (model or "")


def gated_schemas(knowledge: bool, logs: bool, web: str, files: bool = False) -> list[dict[str, Any]]:
    """Retorna apenas as ferramentas habilitadas pelos switches."""
    out = []
    for s in SCHEMAS + FS_SCHEMAS:  # PATCH-DIRTOOLS-V1
        name = s["function"]["name"]
        if name in ("list_knowledge", "read_knowledge") and not knowledge:
            continue
        if name == "search_logs" and not logs:
            continue
        if name in ("web_search", "fetch_url") and web == "off":
            continue
        if name in ("list_directory", "read_file", "write_file", "edit_file") and not files:  # PATCH-DIRTOOLS-V1
            continue
        out.append(s)
    return out


def _payload(model, messages, temperature, max_tokens, tools=None, stream=False):
    p: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
        "cache_prompt": True,
    }
    if max_tokens:
        p["max_tokens"] = int(max_tokens)
    if tools:
        p["tools"] = tools
        p["tool_choice"] = "auto"
    if stream:
        p["stream_options"] = {"include_usage": True}
    return p


def complete(model, messages, temperature, max_tokens, tools=None) -> dict[str, Any]:
    """Chamada NÃO-streaming ao llama-swap (usada no laço de ferramentas)."""
    with httpx.Client(timeout=300) as c:
        r = c.post(
            f"{LLAMA_SWAP_URL}/v1/chat/completions",
            headers=_HEADERS,
            json=_payload(model, messages, temperature, max_tokens, tools=tools),
        )
        r.raise_for_status()
        record_usage(model)  # PATCH-MODELUSAGE-V1
        return r.json()


def stream_tokens(model, messages, temperature, max_tokens) -> Iterator[tuple[str, Any]]:
    """Gera ('token',{'delta'}) e no fim ('usage',{...}) lendo o SSE do llama-swap."""
    with httpx.Client(timeout=600) as c:
        with c.stream(
            "POST",
            f"{LLAMA_SWAP_URL}/v1/chat/completions",
            headers=_HEADERS,
            json=_payload(model, messages, temperature, max_tokens, stream=True),
        ) as r:
            r.raise_for_status()
            record_usage(model)  # PATCH-MODELUSAGE-V1
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    j = json.loads(data)
                except Exception:
                    continue
                usage = j.get("usage")
                if usage:
                    yield ("usage", {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "context_window": usage.get("total_tokens", 0),
                    })
                choices = j.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield ("token", {"delta": delta})


def _chunk(text: str, size: int = 28) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i:i + size]


# Sentinelas do formato "harmony" (gpt-oss) que às vezes vazam para o content
# quando o modelo tenta uma nova tool-call no canal de texto em vez de tool_calls.
_HARMONY_CUT = (
    "<|channel|>", "<|start|>", "<|end|>", "<|constrain|>", "<|message|>",
    "to=functions.", "【assistant", "assistant to=", "commentary<|",
)


def _clean_final(text: str) -> str:
    """Corta o content na 1ª sentinela de tool-call e remove tokens <|...|>."""
    if not text:
        return text
    cut = len(text)
    for mark in _HARMONY_CUT:
        i = text.find(mark)
        if i != -1:
            cut = min(cut, i)
    text = text[:cut]
    text = re.sub(r"<\|[^|>]*\|>", "", text)  # remove qualquer token especial restante
    return text.rstrip()



# ------------------------------------------------------------ PATCH-TOOLLOOP-V1
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "4"))


def _tool_loop(model, msgs, temperature, mtok, tools, max_iters=None):
    """Real tool-calling loop.

    Yields the same ("status"/"tool", payload) events as the rest of the module,
    and finally yields ("__final__", text) with the assistant's answer.

    Every failure mode falls through to returning whatever text we have, so a
    model that emits a malformed tool call degrades to the pre-patch behaviour
    instead of raising.
    """
    if max_iters is None:
        max_iters = MAX_TOOL_ITERATIONS
    work = list(msgs)
    text = ""
    for _ in range(max(1, int(max_iters))):
        try:
            r = complete(model, work, temperature, mtok, tools=tools or None)
        except Exception as e:
            yield ("tool", {"name": "model", "ok": False, "error": str(e)})
            break

        try:
            msg = (r["choices"][0]["message"]) or {}
        except Exception:
            break

        content = (msg.get("content") or "").strip()
        if content:
            text = content

        calls = msg.get("tool_calls") or []
        if not calls:
            break

        work.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": calls,
        })

        for call in calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            yield ("status", {"stage": _STAGE.get(name, "thinking")})
            try:
                raw = fn.get("arguments") or "{}"
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except Exception:
                args = {}
            func = ALL_FUNCS.get(name)  # PATCH-DIRTOOLS-V1: inclui list_directory/read_file/write_file/edit_file
            if func is None:
                result = {"error": f"unknown tool: {name}"}
                yield ("tool", {"name": name or "?", "ok": False, "error": "unknown tool"})
            else:
                try:
                    result = func(**args)
                    yield ("tool", {"name": name, "ok": True})
                except Exception as e:
                    result = {"error": str(e)}
                    yield ("tool", {"name": name, "ok": False, "error": str(e)})
            try:
                payload = json.dumps(result, ensure_ascii=False)[:12000]
            except Exception:
                payload = str(result)[:12000]
            work.append({
                "role": "tool",
                "tool_call_id": call.get("id") or name,
                "name": name,
                "content": payload,
            })
    yield ("__final__", _clean_final(text))
# -------------------------------------------------------- end PATCH-TOOLLOOP-V1


def orchestrate(cfg: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    messages = list(cfg.get("messages") or [])

    # ---- PATCH-MODELEXEC-V1: resolve o modo de execucao da interface ativa ----
    # cfg["interface"] vem do frontend (patch-modelstore.html, Onda 4C,
    # monkey-patch de chatPayload() -- "C"/"CC"/"GOD", default "cylinderui"
    # se o campo nao vier, ex. cliente antigo/teste manual).
    execcfg = None
    try:
        execcfg = get_exec(cfg.get("interface") or "cylinderui")
    except Exception:
        execcfg = None  # interface desconhecida ou get_exec indisponivel -> modo single de sempre
    exec_mode = (execcfg or {}).get("mode") or "single"
    if execcfg and exec_mode == "agent" and execcfg.get("main"):
        cfg = dict(cfg)
        cfg["agent_mode"] = True
        cfg["model"] = execcfg["main"]
        tools_cfg = execcfg.get("tools") or {}
        if "files" in tools_cfg: cfg["files"] = bool(tools_cfg["files"])
        if "rag" in tools_cfg: cfg["knowledge"] = bool(tools_cfg["rag"])
        if "web" in tools_cfg: cfg["web_search"] = "auto" if tools_cfg["web"] else "off"
    # ---- end resolucao do modo agent ----

    model = classify_model(cfg.get("model"), messages)
    temperature = float(cfg.get("temperature", 0.2))
    max_tokens = cfg.get("max_tokens")
    web = (cfg.get("web_search") or "auto").lower()
    knowledge = bool(cfg.get("knowledge"))
    logs = bool(cfg.get("logs"))
    files = bool(cfg.get("files"))  # PATCH-DIRTOOLS-V1
    agent = bool(cfg.get("agent_mode"))
    system_prompt = (cfg.get("system_prompt") or "").strip() or SYSTEM_DEFAULT

    msgs: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}] + messages

    # ---- PATCH-MODELEXEC-V1: modo "dual" (dois modelos, mesma interface) ----
    if execcfg and exec_mode == "dual" and execcfg.get("main"):
        if execcfg.get("aux") and execcfg["aux"] != execcfg["main"]:
            try:
                yield from run_dual(
                    role=execcfg.get("role") or "second",
                    main_id=execcfg["main"], aux_id=execcfg["aux"],
                    msgs=msgs, temperature=temperature, max_tokens=max_tokens,
                    complete_fn=complete, chunk_fn=_chunk,
                )
                return
            except Exception:
                pass  # fallback abaixo: single com o main, sem quebrar o chat
        model = execcfg["main"]
    # ---- end PATCH-MODELEXEC-V1 (dual) ----

    # ---- Agent Mode OFF: chat puro, streaming direto ----
    if not agent:
        yield ("status", {"stage": "generating"})
        try:
            yield from stream_tokens(model, msgs, temperature, max_tokens)
        except Exception as e:
            yield ("error", {"message": str(e)})
        yield ("done", {"finish_reason": "stop"})
        return

    # ---- Agent Mode ON (multi-agente sequencial condicional) ----
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    verify = bool(cfg.get("verify", True))

    ctx_blocks = []
    if web != "off":
        yield ("status", {"stage": "web_search"})
        try:
            sr = web_search(last_user, 8)
            snips = [f'[{i}] {x["title"]} - {x["url"]}\n{x["snippet"]}'
                     for i, x in enumerate(sr["results"], 1)]
            ctx_blocks.append("RESULTADOS DA WEB:\n" + "\n".join(snips))
            yield ("tool", {"name": "web_search", "ok": True})
        except Exception as e:
            yield ("tool", {"name": "web_search", "ok": False, "error": str(e)})

    if knowledge:
        try:
            kn = FUNCS["list_knowledge"](path="", recursive=True, limit=60)
            ents = [e["path"] for e in kn.get("entries", []) if not e.get("directory")]
            if ents:
                ctx_blocks.append("ARQUIVOS DE CONHECIMENTO:\n" + "\n".join(ents[:60]))
        except Exception:
            pass

    ctx = "\n\n".join(ctx_blocks)[:8000] or "(sem resultados de busca)"
    mtok = max(int(max_tokens or 0), 1200)

    # --- AGENTE 1: Pesquisador (rascunho) --- PATCH-TOOLLOOP-V1
    # Agora com laco de ferramentas real: alem do contexto pre-carregado acima,
    # o Pesquisador pode chamar fetch_url / read_knowledge / search_logs /
    # web_search por conta propria, respeitando os switches do console.
    yield ("status", {"stage": "thinking"})
    draft = ""
    tools = gated_schemas(knowledge, logs, web, files)  # PATCH-DIRTOOLS-V1
    a1_msgs = [
        {"role": "system", "content":
            "Voce e um pesquisador. Responda a pergunta de forma objetiva. "
            "Use as ferramentas disponiveis quando precisar de dados que nao estao "
            "no contexto abaixo. Cite as URLs. Se nao houver informacao suficiente, "
            "diga isso explicitamente em vez de inventar.\n\n" + ctx},
        {"role": "user", "content": last_user},
    ]
    try:
        for ev, data in _tool_loop(model, a1_msgs, temperature, mtok, tools):
            if ev == "__final__":
                draft = (data or "").strip()
            else:
                yield (ev, data)
    except Exception as e:
        yield ("error", {"message": str(e)})

    # --- AGENTE 2: Verificador (so se cfg.verify) ---
    yield ("status", {"stage": "generating"})
    ans = ""
    if verify:
        ver_sys = (
            "Voce e um verificador rigoroso. Recebe FONTES e um RASCUNHO de resposta. "
            "Confira cada afirmacao do rascunho contra as FONTES: corrija datas, numeros e nomes errados; "
            "REMOVA qualquer afirmacao sem suporte nas fontes. Se a resposta correta for que nao ha "
            "informacao (ex.: algo que nao existe ou nao foi lancado), diga isso claramente em vez de inventar. "
            "Devolva APENAS a resposta final, em portugues, correta, concisa e com as URLs relevantes.\n\n"
            "FONTES:\n" + ctx + "\n\nRASCUNHO:\n" + (draft or "(vazio)")
        )
        try:
            v = complete(model, [
                {"role": "system", "content": ver_sys},
                {"role": "user", "content": last_user},
            ], temperature, mtok, tools=None)
            ans = ((v["choices"][0]["message"] or {}).get("content") or "").strip()
        except Exception as e:
            yield ("error", {"message": str(e)})
        if not ans:
            ans = draft
    else:
        ans = draft

    if not ans:
        ans = "Nao consegui redigir a resposta com os resultados obtidos."
    for piece in _chunk(ans):
        yield ("token", {"delta": piece})
    yield ("done", {"finish_reason": "stop"})
