# DISTRIBUTION.md — Notas técnicas do frontend (`index.html`) de distribuição

Estas notas descrevem como o `index.html` de distribuição apresenta conteúdo
**neutro** por padrão, e o contrato técnico do fallback estático de modo.

> Escopo: apenas os **dados/strings default** embutidos são neutros. Toda a
> FUNCIONALIDADE (visões/themes/hero/badge/model store/CRUD de visões/
> multi-provider/etc.) permanece intacta.

---

## 1. Conteúdo default neutro

Uma instalação limpa abre mostrando **uma visão exemplo neutra "CylinderUI"**
(hero do logo, prompt genérico). Os valores default embutidos são:

- `title` / header → `"CylinderUI"`.
- `placeholder` do composer → `"Mensagem para o CylinderUI…"` (PT) /
  `"Message CylinderUI…"` (EN).
- `emptyHTML` → `"<h2>Welcome to CylinderUI</h2><p>Your local AI, your way —
  create a Space to begin.</p>"`; numa instalação limpa o hero real vem do
  visions-js (`buildDefaultCylinderHeroHTML` → logo CylinderUI).
- `defaultSys` (system prompt default) → `"You are a helpful local AI
  assistant."` (genérico).
- Hero embutido → logo CylinderUI (`.cyl-brand-mark`); o backend de
  distribuição seeda `hero.image = ""`, o que sinaliza ao front usar o logo
  padrão.

---

## 2. Fallback estático de modo (`MODES`)

O objeto `MODES` é o fallback estático usado antes/na ausência das visões
dinâmicas do backend (`/api/visions`). Na build de distribuição foi reduzido
a **uma única chave `home`** com conteúdo neutro:

- `MODES` tem 1 chave (`home`) — alvo de fallback de `applyMode`, valor
  inicial de `MODE` e `data-nav` do único item estático da nav.
- O inicializador de `MODE` resolve de forma defensiva: `MODES[s] ? s :
  "home"`. Um valor legado em `localStorage` cai em `home` sem lançar.
- `LEGACY_MODE_KEYS` (conv-js) reduzido ao baseline `home`; `getModeKeys()`
  não gera buckets extras de conversas.

**Tolerância defensiva (não é escopo de limpeza):** `msIface()`,
`BUILTIN_INTERFACE_CODE` e `BUILTIN_ID_TO_MODE` ainda aceitam as chaves de
modo herdadas como fallback; `ensureModeEntry()` cria dinamicamente qualquer
entrada que o backend mande, então nada quebra por chave ausente.

---

## 3. Identificadores técnicos preservados (contrato de backend)

Os seguintes **não** são conteúdo exibido e permanecem de propósito:

- Id de tema `"cylinderui"` (rótulo "Warm", warm default) e as chaves
  de armazenamento derivadas (`cylinderui-mode`, `cylinderui-lang`,
  `cylinderui-conversations-db`, `cylinderui-provider`, evento `cylinderui:langchange`,
  classe CSS `cyl-theme-cylinderui`).
- Códigos de interface do Model Store (`C`/`CC`/`GOD`) e os ids de visão
  herdada (`cylinderui`/`cyber`/`god`) — contrato consumido pelo backend.
- Chaves estruturais de modo do fallback (`home`, e as tolerâncias
  `cyber`/`godmode` citadas acima).

---

## 4. Verificações

- `node --check` OK em todos os blocos `<script>`.
- `</body></html>` íntegros; cada `id` de patch com 1 ocorrência.
- Teste jsdom `tests/jsdom-fallback-modes.js` cobre os dois caminhos:
  (a) `/api/visions` OK → 1 visão na sidebar dinâmica; (b) `/api/visions`
  falha → no-op total, 1 visão estática (nav Home), sem exceção. Ambos passam.
