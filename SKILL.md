---
name: graphskill
description: >
  Query a project's code knowledge graph instead of grepping/reading whole
  files. Use when locating symbols, tracing callers/callees, finding
  dependencies, pulling a single function/class body, listing files in a
  directory, checking inheritance, or auditing cross-layer imports — on any
  indexed repo. Trigger on "where is X", "what calls X", "what does X depend
  on", "show me X", "what implements X", "what imports what", or whenever
  about to grep/glob/Read a large codebase.
---

# graphskill — code graph navigation

This project is indexed into an embedded graph (KuzuDB) of symbols
(functions, classes, methods) and edges (CALLS, IMPORTS, INHERITS, USES, CONTAINS).
Querying the graph returns *exactly* the symbol or relationship you need —
far cheaper than grep + reading whole files.

## ALWAYS use the graph FIRST. NEVER do these things first:

| ❌ Do NOT | ✅ Use instead |
|-----------|---------------|
| Read many files to orient on a new repo | `repo_map()` |
| `grep` for a concept when you don't know the name | `search_semantic("what it does")` |
| `grep -r "ClassName"` to find a symbol | `search_symbols("ClassName")` |
| Read a whole file to see what's in it | `symbols_in_file(path)` |
| `grep` for a comment or docstring | `search_docs(query)` |
| `ls` or `find` to list files | `list_files(dir_prefix?)` |
| Read multiple files to understand a module | `module_overview(dir_prefix)` |
| `grep` for subclasses of an interface | `inheritors(name)` |
| Read a file just to see one function | `read_symbol_body(ref)` |
| Multiple callers/callees/uses queries for related symbols | `subgraph(names)` |
| Read files to find cross-layer imports | `architecture_violations(from, not_to)` |

## Decision tree

```
Need to understand code?
├── New/unfamiliar repo? → repo_map() FIRST (ranked overview, ~2k tokens)
├── Know what code does but not its name? → search_semantic("intent phrase")
├── Know the name? → search_symbols(name)
├── Know the file, want its structure? → symbols_in_file(path)
├── Want one function's body? → read_symbol_body(ref)
├── Want N bodies at once? → batch_read_symbol_bodies(refs)
├── Who calls X? → callers(name)
├── What does X call? → callees(name)
├── What implements interface X? → inheritors(name)
├── What does class X extend? → inherited_from(name)
├── What does class X depend on (type-level)? → uses(name)
├── What depends on class X? → used_by(name)
├── Context around several related symbols? → subgraph(names)
├── List files in a directory? → list_files(dir_prefix)
├── What's in a module/directory? → module_overview(dir_prefix)
├── Most-called symbols (entry points)? → hot_symbols(n, edge)
├── Cross-layer dependency violations? → architecture_violations(from, not_to)
├── Shortest call chain A→B? → path(src_name, dst_name)
├── File imports? → imports(path) / dependents(path)
└── Orientation (total counts)? → overview()
```

## Tool reference

### Discovery
| Tool | Returns |
|------|---------|
| `repo_map(dir_prefix?, budget_tokens?)` | Top symbols by PageRank, trimmed to a token budget. **Read first on an unfamiliar repo.** |
| `search_semantic(query, limit?, compact?)` | Symbols ranked by meaning (embedding similarity). Use for intent queries where the name is unknown. |
| `search_symbols(query, kind?, limit?, offset?, compact?, visibility?)` | Symbols by name substring → id, kind, location, signature, visibility, modifiers |
| `search_docs(query, limit?, offset?, compact?)` | Symbols by docstring/comment substring |
| `get_symbol(ref)` | Signature, doc, kind, location for one symbol |
| `list_files(dir_prefix?)` | All indexed file paths, optional prefix filter |
| `module_overview(dir_prefix?)` | Symbol counts grouped by top-level directory |
| `hot_symbols(n?, edge?)` | Most-referenced symbols (god nodes). edge: CALLS/USES/INHERITS |

### Reading source
| Tool | Returns |
|------|---------|
| `read_symbol_body(ref)` | Exact source of ONE symbol — not the whole file |
| `batch_read_symbol_bodies(refs)` | Multiple bodies in one call |
| `symbols_in_file(path, compact?)` | All symbols in a file with signatures |

### Graph traversal
| Tool | Returns |
|------|---------|
| `callers(name, depth?, compact?)` | Symbols that call `name`. depth=1 includes confidence. |
| `callees(name, depth?, compact?)` | Symbols called by `name`. depth=1 includes confidence. |
| `uses(name, compact?)` | Types a class depends on (type-hints/new/static). With confidence. |
| `used_by(name, compact?)` | Types that depend on a class. With confidence. |
| `inheritors(name, depth?, compact?)` | Classes that inherit from / implement `name` |
| `inherited_from(name, depth?, compact?)` | Base classes that `name` extends |
| `imports(path)` / `dependents(path)` | File-level import edges |
| `path(src_name, dst_name)` | Shortest CALLS chain, list of names |
| `subgraph(names, depth?)` | callers+callees+uses+used_by for N symbols at once |
| `architecture_violations(from_prefix, not_to_prefix, edge?)` | Cross-layer edges. edge: IMPORTS/CALLS/USES/INHERITS |

## Token-saving tips

- Use `compact=True` on any tool that supports it when you don't need full JSON — returns tab-separated strings, ~50% fewer tokens.
- Use `offset` to paginate large result sets — if a query returns exactly `limit` rows, there may be more.
- `batch_read_symbol_bodies` for multiple bodies > N separate `read_symbol_body` calls.
- `subgraph` for context around 2+ related symbols > separate callers/callees/uses queries.
- `symbols_in_file` to understand a file's structure > reading the file.

## Confidence on edges

`callers`, `callees`, `uses`, `used_by` include a `confidence` field:
- `EXTRACTED` — unambiguous, trust it
- `INFERRED` — resolved via an import, likely correct
- `AMBIGUOUS` — multiple candidates; treat as a hint, verify with `read_symbol_body` before acting

## Keeping the graph fresh

The server auto-re-indexes on source file changes (1.5 s debounce). If graph
seems stale after a branch switch or large rewrite:

```
graphskill index .
```

## Limits

- Resolution is name-based (no full type/scope analysis). Dynamic dispatch and
  same-named symbols across files may be imprecise — flagged `AMBIGUOUS`.
- `uses`/`used_by` edges exist for PHP, Python, and TypeScript. Other languages
  only have CALLS/IMPORTS/INHERITS.
- External/stdlib calls are not nodes in the graph.
