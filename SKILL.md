---
name: graphskill
description: >
  Query a project's code knowledge graph instead of grepping/reading whole
  files. Use when locating symbols, tracing callers/callees, finding
  dependencies, or pulling a single function/class body — on any indexed repo.
  Trigger on "where is X", "what calls X", "what does X depend on", "show me X",
  or when about to grep/glob a large codebase.
---

# graphskill — code graph navigation

This project is indexed into an embedded graph (KuzuDB) of symbols
(functions, classes, methods) and edges (CALLS, IMPORTS, INHERITS, CONTAINS).
Querying the graph returns *exactly* the symbol or relationship you need —
far cheaper than grep + reading whole files.

## Use the graph BEFORE grep / glob / Read

When you need to find or understand code, prefer the `graphskill` MCP tools:

| Need | Tool |
|------|------|
| Find a symbol by name | `search_symbols(query, kind?, limit?)` |
| Signature + docstring + location | `get_symbol(ref)` |
| The actual source of ONE function/class | `read_symbol_body(ref)` ← not Read on the file |
| Who calls X (transitive) | `callers(name, depth?)` |
| What X calls (transitive) | `callees(name, depth?)` |
| Files a file imports / its importers | `imports(path)` / `dependents(path)` |
| Shortest call chain A→B | `path(src_name, dst_name)` |
| Orientation / per-file symbol counts | `overview()` |

`ref` accepts a symbol id (`path#byte`, exact) or a name (first match).

## Keeping the graph fresh

If the graph is missing or stale, rebuild it (fast; only changed files are
re-parsed):

```
graphskill index .
```

Re-running with no changes is a no-op. After editing files in a session,
re-run `graphskill index .` before relying on the graph again.

## Confidence on edges

`CALLS` / `INHERITS` carry a `confidence`: `EXTRACTED` (unique resolution),
`INFERRED` (resolved via an import), or `AMBIGUOUS` (multiple candidates).
Treat `AMBIGUOUS` edges as hints, not ground truth — verify with
`read_symbol_body` if it matters.

## Limits

Resolution is name-based (no full type/scope analysis), so dynamic dispatch
and same-named symbols across files may be imprecise. External/stdlib calls
are not nodes. The graph is structural only — it tells you *what connects to
what*, not *why*.
