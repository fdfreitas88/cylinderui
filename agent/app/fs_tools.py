# -*- coding: utf-8 -*-
"""
fs_tools.py — Diretórios & Arquivos (CylinderUI)

Permite ao agente listar, ler, criar e editar arquivos SOMENTE dentro de
diretórios que o usuário adicionou explicitamente pela interface (ícone de
pasta "Diretórios" no topo do console, ao lado do RAG). O modelo NUNCA pode
adicionar ou remover diretórios sozinho -- isso só acontece via
/api/directories (main.py), chamado pela UI (ação humana).

Todo caminho recebido do modelo é resolvido com Path.resolve() (sem seguir
".." nem escapar via symlink) e checado contra a lista de diretórios
permitidos, persistida em DATA_DIR/allowed_dirs.json. Nomes/extensões
sensíveis são bloqueados mesmo dentro de um diretório permitido — defesa em
profundidade, caso o usuário adicione sem querer uma pasta que contenha
.env, chaves SSH, etc.

Este módulo é independente de app/tools.py: não importa nada de lá e não é
importado por ele. A integração acontece só em app/stream.py (mescla
FS_SCHEMAS/FS_FUNCS com SCHEMAS/FUNCS) e app/main.py (endpoints
/api/directories). Isso evita qualquer risco de quebrar as ferramentas
existentes (web_search, knowledge, logs).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR

ALLOWED_DIRS_FILE = DATA_DIR / "allowed_dirs.json"
UPLOADS_DIR = DATA_DIR / "uploaded_dirs"  # PATCH-DIRUPLOAD-V1

MAX_READ_CHARS = 300_000     # ~300k caracteres por leitura (evita estourar o contexto)
MAX_LIST_ENTRIES = 500

# PATCH-DIRUPLOAD-V1: limites do upload de pasta local (via navegador, ver
# /api/directories/upload/* em main.py). Isso é uma COPIA para o servidor,
# nao uma conexao ao vivo -- edicoes feitas pelo agente ficam so na copia.
MAX_UPLOAD_FILE_BYTES = 25 * 1024 * 1024        # 25MB por arquivo
MAX_UPLOAD_TOTAL_BYTES = 300 * 1024 * 1024      # 300MB por sessao de upload
MAX_UPLOAD_FILES = 3000                          # arquivos por sessao de upload
_SKIP_UPLOAD_PARTS = {"node_modules", "__pycache__", "venv", ".venv"}

# Bloqueados mesmo dentro de um diretório permitido (defesa em profundidade).
_BLOCKED_NAMES = {
    ".env", ".git", ".ssh", ".aws", ".kube", ".localai-secrets",
    "id_rsa", "id_ed25519", "known_hosts", "credentials", "credentials.json",
}
_BLOCKED_SUFFIXES = {
    ".pem", ".key", ".crt", ".p12", ".pfx", ".kubeconfig", ".tfstate", ".db",
}
_BLOCKED_SUBSTR = ("secret", "senha", "password", "token")


def _load_allowed() -> list[str]:
    try:
        return json.loads(ALLOWED_DIRS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_allowed(dirs: list[str]) -> None:
    ALLOWED_DIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_DIRS_FILE.write_text(
        json.dumps(sorted(set(dirs)), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def list_allowed_directories() -> list[str]:
    return _load_allowed()


def add_allowed_directory(path: str) -> dict[str, Any]:
    """Ação humana (UI) apenas -- NUNCA exposta como tool ao modelo."""
    try:
        p = Path(path).expanduser().resolve()
    except Exception as e:
        return {"ok": False, "error": f"Caminho inválido: {e}"}
    if not p.exists() or not p.is_dir():
        return {"ok": False, "error": f"Não é um diretório existente: {p}"}
    if str(p) in ("/", str(Path.home())):
        return {"ok": False, "error": "Diretório grande demais para ser permitido (raiz ou home inteira)."}
    dirs = _load_allowed()
    if str(p) not in dirs:
        dirs.append(str(p))
        _save_allowed(dirs)
    return {"ok": True, "directories": _load_allowed()}


def remove_allowed_directory(path: str) -> dict[str, Any]:
    """Ação humana (UI) apenas."""
    try:
        p = str(Path(path).expanduser().resolve())
    except Exception:
        p = path
    dirs = [d for d in _load_allowed() if d != p]
    _save_allowed(dirs)
    return {"ok": True, "directories": dirs}


def browse_directories(path: str = "") -> dict[str, Any]:
    """PATCH-DIRBROWSE-V1. Lista subpastas de um caminho para o seletor da UI
    (o usuário navega e escolhe qual diretório adicionar). Ação humana
    apenas -- NÃO é gated pela allowlist (é assim que a allowlist é
    construída) e NUNCA é exposta como tool ao modelo. Mostra só nomes de
    pastas (nada de conteúdo de arquivo); pastas ocultas e nomes sensíveis
    ficam de fora da listagem por padrão."""
    try:
        base = Path(path).expanduser().resolve() if path else Path.home()
    except Exception:
        base = Path.home()
    if not base.exists() or not base.is_dir():
        base = Path.home()
    entries: list[dict[str, str]] = []
    try:
        for child in sorted(base.iterdir(), key=lambda c: c.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                _check_blocked(child)
            except ValueError:
                continue
            if not os.access(child, os.R_OK | os.X_OK):
                continue
            entries.append({"name": child.name, "path": str(child)})
    except PermissionError:
        pass
    parent = str(base.parent) if base != base.parent else None
    return {"path": str(base), "parent": parent, "entries": entries}


# PATCH-DIRUPLOAD-V1 ------------------------------------------------------
_UPLOAD_STATS: dict[str, dict[str, int]] = {}  # base_path -> {"bytes":n,"files":n}


def _sanitize_folder_name(name: str) -> str:
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._")
    return (safe or "pasta")[:80]


def start_upload_session(folder_name: str) -> dict[str, Any]:
    """Ação humana (UI) apenas. Cria um destino novo em UPLOADS_DIR para
    receber uma cópia de uma pasta escolhida no navegador do usuário, e já
    registra esse destino como diretório permitido (para não exigir um
    segundo clique de "adicionar" depois do upload)."""
    safe = _sanitize_folder_name(folder_name)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    base = UPLOADS_DIR / safe
    n = 2
    while base.exists():
        base = UPLOADS_DIR / f"{safe}-{n}"
        n += 1
    base.mkdir(parents=True)
    _UPLOAD_STATS[str(base)] = {"bytes": 0, "files": 0}
    add_allowed_directory(str(base))
    return {"ok": True, "path": str(base)}


def save_uploaded_file(base_path: str, rel_path: str, data: bytes) -> dict[str, Any]:
    """Ação humana (UI) apenas -- recebe um arquivo do upload de pasta e
    grava dentro de `base_path`, que precisa estar sob UPLOADS_DIR (nunca
    escreve fora dali por essa via). Pastas/arquivos ocultos e nomes
    sensíveis são pulados silenciosamente (skipped=True), não é erro fatal
    para o restante do upload."""
    try:
        base = Path(base_path).resolve()
        base.relative_to(UPLOADS_DIR.resolve())
    except Exception:
        return {"ok": False, "error": "destino de upload inválido"}
    if not base.is_dir():
        return {"ok": False, "error": "sessão de upload não encontrada"}

    parts = [p for p in (rel_path or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts or any(p.startswith(".") or p in _SKIP_UPLOAD_PARTS for p in parts):
        return {"ok": False, "skipped": True, "error": "ignorado (oculto ou pasta de sistema)"}

    stats = _UPLOAD_STATS.setdefault(str(base), {"bytes": 0, "files": 0})
    if stats["files"] >= MAX_UPLOAD_FILES or stats["bytes"] + len(data) > MAX_UPLOAD_TOTAL_BYTES:
        return {"ok": False, "skipped": True, "error": "limite de upload da sessão atingido (arquivos/tamanho total)"}
    if len(data) > MAX_UPLOAD_FILE_BYTES:
        return {"ok": False, "skipped": True, "error": "arquivo excede 25MB"}

    target = base.joinpath(*parts)
    try:
        target.relative_to(base)
        _check_blocked(target)
    except ValueError as e:
        return {"ok": False, "skipped": True, "error": str(e)}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    stats["bytes"] += len(data)
    stats["files"] += 1
    return {"ok": True, "path": str(target), "size": len(data)}
# ---------------------------------------------------------- end PATCH-DIRUPLOAD-V1


def _check_blocked(target: Path) -> None:
    name = target.name.lower()
    if name in _BLOCKED_NAMES:
        raise ValueError(f"Bloqueado por segurança: {target.name}")
    if target.suffix.lower() in _BLOCKED_SUFFIXES:
        raise ValueError(f"Tipo de arquivo bloqueado por segurança: {target.suffix}")
    low = str(target).lower()
    if any(s in low for s in _BLOCKED_SUBSTR):
        raise ValueError(f"Caminho bloqueado por segurança (padrão sensível): {target}")


def _resolve_within_allowed(path: str) -> Path:
    target = Path(path).expanduser().resolve()
    for root in _load_allowed():
        try:
            root_p = Path(root).resolve()
        except Exception:
            continue
        try:
            target.relative_to(root_p)
        except ValueError:
            continue
        _check_blocked(target)
        return target
    raise ValueError(f"Caminho fora dos diretórios permitidos: {path}")


def list_directory(path: str) -> dict[str, Any]:
    target = _resolve_within_allowed(path)
    if not target.is_dir():
        return {"error": f"Não é um diretório: {path}"}
    entries = []
    for child in sorted(target.iterdir()):
        try:
            _check_blocked(child)
        except ValueError:
            continue  # oculta entradas sensíveis também na listagem
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "size": child.stat().st_size if child.is_file() else None,
        })
        if len(entries) >= MAX_LIST_ENTRIES:
            break
    return {"path": str(target), "entries": entries}


def read_file(path: str, max_chars: int = MAX_READ_CHARS) -> dict[str, Any]:
    target = _resolve_within_allowed(path)
    if not target.is_file():
        return {"error": f"Não é um arquivo: {path}"}
    data = target.read_text(encoding="utf-8", errors="replace")
    max_chars = max(1000, min(int(max_chars), MAX_READ_CHARS))
    return {
        "path": str(target),
        "content": data[:max_chars],
        "truncated": len(data) > max_chars,
        "size": len(data),
    }


def _backup(target: Path) -> None:
    if target.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(target, target.with_name(target.name + f".bak-{stamp}"))
        except Exception:
            pass  # backup é best-effort; nunca bloqueia a escrita


def write_file(path: str, content: str) -> dict[str, Any]:
    target = _resolve_within_allowed(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _backup(target)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "ok": True, "bytes": len(content.encode("utf-8"))}


def edit_file(path: str, old_text: str, new_text: str) -> dict[str, Any]:
    target = _resolve_within_allowed(path)
    if not target.is_file():
        return {"error": f"Não é um arquivo: {path}"}
    original = target.read_text(encoding="utf-8", errors="replace")
    count = original.count(old_text)
    if count == 0:
        return {"error": "old_text não encontrado no arquivo."}
    if count > 1:
        return {"error": f"old_text não é único ({count} ocorrências); forneça mais contexto para identificar o trecho exato."}
    _backup(target)
    target.write_text(original.replace(old_text, new_text, 1), encoding="utf-8")
    return {"path": str(target), "ok": True}


FS_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_directory",
        "description": "Lista arquivos e subpastas dentro de um diretório que o usuário adicionou explicitamente à lista de diretórios permitidos.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Caminho absoluto do diretório a listar."},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Lê o conteúdo de texto de um arquivo dentro de um diretório permitido.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Caminho absoluto do arquivo a ler."},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Cria ou sobrescreve um arquivo de texto dentro de um diretório permitido. Faz backup automático (.bak-<timestamp>) do conteúdo anterior antes de sobrescrever.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Substitui um trecho único de texto (old_text) por outro (new_text) em um arquivo existente dentro de um diretório permitido. Falha se old_text não for único no arquivo.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
        }, "required": ["path", "old_text", "new_text"]},
    }},
]

FS_FUNCS = {
    "list_directory": list_directory,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
}
