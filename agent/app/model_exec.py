# -*- coding: utf-8 -*-
"""
model_exec.py — Orquestração dos modos de execução por interface (Onda 4).

Contém só a lógica de "dado um exec state (mode/main/aux/role) e o histórico
de mensagens, o que chamar e em que ordem" -- não sabe nada de HTTP, FastAPI,
httpx nem llama-swap. As chamadas reais ao modelo (complete()/stream_tokens(),
já existentes em app/stream.py) são injetadas por parâmetro (complete_fn),
exatamente para dar para testar este módulo com stubs, sem rede nem app/*
(ver tests/test_stream_exec.py).

Descoberta da Onda 0/1 (context-pack §4): o grupo do llama-swap com os
modelos "grandes" é exclusivo -- só 1 residente por vez. Por isso "dois
modelos simultâneos" é, na prática, sequencial (o llama-swap troca ao vivo
entre as duas chamadas) -- daqui em diante chamado só de "dual". Cada role
abaixo é uma receita de 1 ou 2 chamadas sequenciais ao llama-swap.

Integração com app/stream.py::orchestrate(): ver backend/patch-stream-exec.py.txt
(bloco PATCH-MODELEXEC-V1, Onda 5) -- estende, não substitui, o patch-stream-usage
já escrito (Onda 3b), que grava record_usage(model) no ponto de chamada real.
Cada chamada de complete_fn aqui É uma chamada real ao llama-swap quando ligada
em produção, então record_usage roda uma vez por chamada (2 por request dual,
1 por request "router"), automaticamente -- este módulo não precisa saber nada
sobre contagem de uso.

Contrato de eventos: os geradores abaixo produzem as MESMAS tuplas (evento,
dados) que app/stream.py::orchestrate() já produz para o consumidor SSE
(app/main.py::_sse), então plugam no mesmo fluxo sem exigir mudança no
frontend: ("status",{"stage":str}), ("token",{"delta":str}), ("done",{...}).
Qualquer exceção propaga para quem chamou run_dual() -- o chamador
(orchestrate(), via patch-stream-exec.py.txt) é responsável pelo fallback
"cai para single com o main, sem quebrar o chat".
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterator

CompleteFn = Callable[[str, list[dict[str, Any]], float, int], dict[str, Any]]
ChunkFn = Callable[[str], Iterator[str]]

DEFAULT_ROLE = "second"

# Heurística do papel "router" (Onda 4): sinais textuais de código/tarefa
# técnica na última mensagem do usuário mandam a requisição para o modelo
# auxiliar. Best-effort, não um parser de linguagem -- propositalmente barato
# (1 regex), já que "router" precisa decidir ANTES de qualquer chamada ao
# modelo (não dá para perguntar a um LLM "isso é código?" sem gastar uma
# chamada a mais, o que anularia a vantagem de latência do roteador).
_CODE_HINT_RE = re.compile(
    r"```|`[^`]+`|\bdef\s+\w+\s*\(|\bclass\s+\w+\s*[:\(]|\bfunction\s*\w*\s*\("
    r"|=>|#include\b|\bimport\s+\w+|\bconsole\.log\s*\(|\bSELECT\b.+\bFROM\b"
    r"|\btraceback\b|\bstack ?trace\b|\bexception\b|\berror:\s*\S|;\s*$"
    r"|\b(python|javascript|typescript|regex|json|yaml|sql|bash|dockerfile)\b",
    re.IGNORECASE | re.MULTILINE,
)


def last_user_text(msgs: list[dict[str, Any]]) -> str:
    """Mesma extração usada em app/stream.py::orchestrate() para `last_user`
    (procura de trás pra frente pela última mensagem role=="user")."""
    for m in reversed(msgs or []):
        if m.get("role") == "user":
            return m.get("content", "") or ""
    return ""


def route_model(main_id: str, aux_id: str | None, last_user: str) -> str:
    """Papel "router": decide main OU aux com base num sinal barato de
    código/tarefa técnica no texto do usuário. Sem aux configurado, sempre
    main (nada para rotear)."""
    if not aux_id:
        return main_id
    if _CODE_HINT_RE.search(last_user or ""):
        return aux_id
    return main_id


def _extract_text(resp: dict[str, Any]) -> str:
    try:
        return ((resp["choices"][0]["message"] or {}).get("content") or "").strip()
    except Exception:
        return ""


def _with_context(msgs: list[dict[str, Any]], extra_system: str) -> list[dict[str, Any]]:
    """Insere uma instrução extra logo após a mensagem de sistema original
    (índice 0, se existir) -- nunca no fim da lista, para não ficar depois da
    última mensagem "user" (alguns templates de chat lidam mal com role
    "system" fora do começo da conversa)."""
    out = list(msgs)
    insert_at = 1 if out and out[0].get("role") == "system" else 0
    out.insert(insert_at, {"role": "system", "content": extra_system})
    return out


def _emit_final(text: str, chunk_fn: ChunkFn | None) -> Iterator[tuple[str, Any]]:
    if not text:
        text = "Não consegui gerar uma resposta com a combinação de modelos configurada."
    fn = chunk_fn or (lambda t: iter([t]))
    for piece in fn(text):
        yield ("token", {"delta": piece})
    yield ("done", {"finish_reason": "stop"})


_ROLE_STAGES = {
    "second": ("consulting_aux", "consolidating"),
    "review": ("thinking", "reviewing"),
    "draft": ("thinking", "generating"),
}

_ROLE_PROMPTS = {
    "second": (
        "Você recebeu uma segunda opinião de outro modelo sobre a mesma pergunta. "
        "Considere-a, mas responda com o seu próprio julgamento -- discorde dela se "
        "for o caso. Não mencione o processo (\"segunda opinião\", \"outro modelo\") "
        "na resposta final, apenas responda.\n\nSEGUNDA OPINIÃO:\n"
    ),
    "review": (
        "Você é um revisor rigoroso. Corrija erros factuais, de raciocínio ou de "
        "formatação no RASCUNHO abaixo e devolva a versão final, pronta para o "
        "usuário -- sem comentar o processo de revisão.\n\nRASCUNHO:\n"
    ),
    "draft": (
        "Outro modelo preparou um rascunho de resposta para a mesma pergunta. "
        "Finalize-o: melhore clareza, correção e formatação mantendo o conteúdo. "
        "Devolva só a resposta final, pronta para o usuário.\n\nRASCUNHO:\n"
    ),
}


def run_dual(
    role: str,
    main_id: str,
    aux_id: str | None,
    msgs: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    complete_fn: CompleteFn,
    chunk_fn: ChunkFn | None = None,
) -> Iterator[tuple[str, Any]]:
    """Orquestra o modo "dois modelos simultâneos" (dual, ver docstring do
    módulo). Gera eventos ("status"/"token"/"done") compatíveis com
    app/stream.py::orchestrate(). Propaga qualquer exceção de complete_fn --
    quem chama decide o fallback (ver patch-stream-exec.py.txt)."""
    role = role or DEFAULT_ROLE
    mtok = max(int(max_tokens or 0), 800)
    last_user = last_user_text(msgs)

    if role == "router":
        chosen = route_model(main_id, aux_id, last_user)
        yield ("status", {"stage": "generating"})
        r = complete_fn(chosen, msgs, temperature, mtok)
        yield from _emit_final(_extract_text(r), chunk_fn)
        return

    if role not in _ROLE_STAGES:
        raise ValueError(f"papel de dual desconhecido: {role!r}")
    if not aux_id:
        raise ValueError("papel exige modelo auxiliar (aux) configurado")

    stage1, stage2 = _ROLE_STAGES[role]
    prompt_prefix = _ROLE_PROMPTS[role]

    # first_model / second_model dependem do papel:
    #   second -> aux fala primeiro (opinião independente), main consolida
    #   review -> main fala primeiro (rascunho), aux revisa
    #   draft  -> aux fala primeiro (rascunho), main finaliza
    first_model = aux_id if role in ("second", "draft") else main_id
    second_model = main_id if role in ("second", "draft") else aux_id

    yield ("status", {"stage": stage1})
    first_r = complete_fn(first_model, msgs, temperature, mtok)
    first_text = _extract_text(first_r)

    yield ("status", {"stage": stage2})
    second_msgs = _with_context(msgs, prompt_prefix + (first_text or "(vazio)"))
    second_r = complete_fn(second_model, second_msgs, temperature, mtok)
    final_text = _extract_text(second_r) or first_text

    yield from _emit_final(final_text, chunk_fn)
