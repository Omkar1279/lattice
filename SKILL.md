---
name: lattice-retrieval
description: Local, graph-aware code retrieval for this repo. Use Lattice tools instead of broad grep/read when the question is about code structure, cross-file relationships, or concept location.
---

# Lattice — Retrieval Routing Guide

This plugin indexes the repo into a hybrid store: BM25 + semantic vectors +
tree-sitter AST graph (imports, calls, inherits, references, defines). Use the
right tool for the question shape — wrong routing costs turns.

## Decision table

| Question shape | First call | Then |
|---|---|---|
| "Where is X defined / what is X?" | `recall(query="X", kind="code", budget_tokens=1500)` | If exact match: `recall_expand(chunk_id, mode="body")` |
| "What calls X?" / "Who depends on X?" | `recall(query="X", kind="code")` to find the symbol chunk | `recall_expand(chunk_id, mode="callers")` |
| "What does X import / depend on?" | `recall(query="X", kind="code")` | `recall_expand(chunk_id, mode="imports")` |
| "Who implements interface/protocol X?" | `recall(query="X", kind="code")` | `recall_expand(chunk_id, mode="impl")` |
| "Where is feature/concept Y handled?" (fuzzy) | `recall(query="<concept in 3-8 words>", kind="auto", budget_tokens=2500)` | Pick 1–2 chunks, then `recall_expand(mode="body")` |
| "What changed recently in area Z?" | `recall(query="Z", since="7d")` | — |
| Decisions / past discussions / notes | `recall(query="...", kind="notes")` | — |

## Hard rules

1. **Never read a file >40KB directly.** The `pre_tool_use` hook will block it.
   Use `recall` first; expand only the chunks you need.
2. **Default `budget_tokens=2500`.** Raise to 4000 only if the first call
   returned `remaining_chunks > 0` and the surfaced previews look on-topic.
3. **Walk the graph before re-querying.** If `recall` returned a relevant chunk,
   prefer `recall_expand(mode=callers|imports|dependents|impl)` over a second
   `recall` with rephrased text. Graph hops are cheap; re-recall is not.
4. **Stop after 3 unsuccessful recalls.** If three queries with varied phrasing
   surface nothing useful, the answer is probably not in the index — fall back
   to filesystem tools or ask the user.
5. **Use `path_scope`** when you already know the directory. E.g.,
   `path_scope="src/auth/"` cuts noise dramatically.
6. **Paginate, don't widen.** If `continuation_token` is present and the first
   page wasn't enough, pass it back. Don't increase `budget_tokens` past 4000.

## Anti-patterns

- ❌ `grep -r` across the repo when `recall(kind="code")` would do it
- ❌ Reading a 60KB file to find one function — use `recall(query="<function name>")`
- ❌ Calling `recall` 5 times with synonyms — call once, then `recall_expand` to navigate
- ❌ `budget_tokens=8000` on a first call "to be safe" — wastes context
- ❌ Asking the user "where is X?" before trying `recall` first

## Worked examples

**"Who calls `parseConfig`?"**
1. `recall(query="parseConfig", kind="code", budget_tokens=1000)`
2. Take the top chunk_id, call `recall_expand(chunk_id, mode="callers", budget_tokens=2000)`
3. Done — no grep, no file reads.

**"How does the auth middleware decide whether to refresh tokens?"**
1. `recall(query="auth middleware refresh token decision", kind="auto", budget_tokens=2500)`
2. Pick the most on-topic chunk, `recall_expand(chunk_id, mode="body", budget_tokens=2000)`
3. If the body references a helper, `recall_expand(mode="imports")` to follow it.

**"What changed in the indexer last week?"**
1. `recall(query="indexer", path_scope="lattice/indexer/", since="7d")`
2. Read the headings; expand only what's relevant.
