---
name: graphskill
description: >
  Query this repo's code knowledge graph instead of grepping/reading whole
  files. Use for locating symbols, tracing callers/callees, finding class
  dependencies, or pulling a single function/class/method body. Trigger on
  "where is X", "what calls X", "what does X depend on", "show me X", or
  whenever about to grep/glob this codebase.
---

# graphskill — code graph for this project

This project is indexed into an isolated embedded graph (KuzuDB). Prefer the
`graphskill` MCP tools over grep/glob/Read:

- `search_symbols(query, kind?, limit?)` — find symbols (use before grep)
- `get_symbol(ref)` / `read_symbol_body(ref)` — signature / exact source of ONE symbol
- `callers(name, depth?)` / `callees(name, depth?)` — call graph
- `uses(name)` / `used_by(name)` — class dependency graph
- `imports(path)` / `dependents(path)` — file deps
- `path(a, b)` — shortest call chain · `overview()` — orientation

This server is project-scoped: it only runs in this project's session and
serves only this project's graph. After editing files, refresh with
`graphskill index <this-project>` (only changed files are re-parsed).
