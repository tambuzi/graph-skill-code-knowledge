# graphskill

Index a codebase into an embedded **graph database**, then let an AI coding
agent (Claude Code) query the graph over **MCP** instead of grepping and reading
whole files.

The goal is **token reduction**. Instead of `grep`-ing a repo and reading entire
files to find a few relevant symbols, the agent asks the graph precise questions
— *"where is `X`?"*, *"what calls `X`?"*, *"show me `X`'s body"*, *"shortest call
chain `A`→`B`"* — and gets back exactly the bytes it needs.

Inspired by [graphify](https://github.com/safishamsi/graphify); tuned to a
language-agnostic tree-sitter front end, an embedded KuzuDB store, an MCP query
interface, and structural-only (no-LLM, fully local) extraction.

---

## Table of contents

- [Requirements](#requirements)
- [Install](#install)
- [Run with Docker](#run-with-docker)
- [Quick start](#quick-start)
- [Integrating with Claude Code](#integrating-with-claude-code)
- [Multiple repositories](#multiple-repositories)
- [MCP tools reference](#mcp-tools-reference)
- [CLI reference](#cli-reference)
- [Language coverage](#language-coverage)
- [How it works](#how-it-works)
- [Performance](#performance)
- [Troubleshooting](#troubleshooting)
- [Limits](#limits)

---

## Requirements

- **Python 3.10+** (developed and tested on 3.11) — or just **Docker**
  (see [Run with Docker](#run-with-docker)).
- macOS / Linux. No external services — the graph database is embedded
  (KuzuDB), and parsing is local (tree-sitter). Nothing leaves your machine.

---

## Install

```bash
# from the graphSkill project directory
python3.11 -m venv .venv
. .venv/bin/activate
pip install .
```

This installs the `graphskill` console command into the virtualenv. Verify:

```bash
graphskill --help
```

> **Note on editable installs.** `pip install -e .` can be flaky depending on
> your setuptools/site configuration (the console script may fail to import the
> package). A regular `pip install .` always provides a working `graphskill`
> command. For development, run from source instead: `python -m graphskill ...`
> or `python -m pytest`.

---

## Run with Docker

Prefer not to manage a local Python environment? Run graphskill as a container.
The image bundles Python, tree-sitter, and KuzuDB, so the only requirement on
the host is Docker.

### Build the image

```bash
docker build -t graphskill .
```

### Index and query

Mount the project you want to index at `/workspace`:

```bash
# Build the graph (writes <project>/.graphskill on the host via the mount)
docker run --rm -v "$PWD":/workspace graphskill index /workspace

# Stats
docker run --rm -v "$PWD":/workspace graphskill stats /workspace
```

Add `--user "$(id -u):$(id -g)"` so the generated `.graphskill/` files are owned
by you instead of root:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/workspace graphskill index /workspace
```

### Serve over stdio

The MCP server talks over stdin/stdout, so run it with `-i` (interactive, no
TTY). This is the command you put in `.mcp.json` (see below):

```bash
docker run -i --rm -v /path/to/your/project:/workspace graphskill serve /workspace
```

> **Version note.** The graph file format is tied to the KuzuDB version. If you
> index on the host and serve in Docker (or vice-versa), keep the KuzuDB version
> consistent, or just build *and* serve through the same image.

---

## Quick start

```bash
# 1. Build the graph for a project (creates .graphskill/graph.kuzu inside it)
graphskill index /path/to/your/project

# 2. See what got indexed
graphskill stats /path/to/your/project

# 3. Ask the graph things directly (raw Cypher escape hatch)
graphskill query "MATCH (a:Symbol)-[:CALLS]->(b:Symbol {name:'login'}) \
  RETURN a.name, a.path" /path/to/your/project
```

Re-running `graphskill index` is cheap: only changed files are re-parsed, and a
run with no changes short-circuits instantly (content-hash manifest).

The index lives at `<project>/.graphskill/`. Add `.graphskill/` to the
project's `.gitignore`.

---

## Integrating with Claude Code

This is the main use case: expose the graph as MCP tools so Claude prefers graph
queries over `grep`/`Read`.

### One-step setup (recommended)

```bash
graphskill setup /path/to/your/project
```

This writes the project's `.mcp.json` (a project-scoped server) and
`.claude/skills/graphskill/SKILL.md`, then builds the graph. Open the project in
Claude Code and approve the server. For multiple repos, run it once per repo —
see [Multiple repositories](#multiple-repositories).

The manual steps below explain what `setup` does.

### 1. Build the index

```bash
graphskill index /path/to/your/project
```

### 2. Register the MCP server

Create `.mcp.json` at the **project root** (point `command` at the `graphskill`
binary from the venv where you installed it, and pass the project's absolute
path):

```json
{
  "mcpServers": {
    "graphskill": {
      "command": "/absolute/path/to/graphSkill/.venv/bin/graphskill",
      "args": ["serve", "/path/to/your/project"],
      "env": {}
    }
  }
}
```

**Docker variant** — register the server as a container instead of a local
binary (no venv on the host):

```json
{
  "mcpServers": {
    "graphskill": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/path/to/your/project:/workspace",
        "graphskill", "serve", "/workspace"
      ],
      "env": {}
    }
  }
}
```

### 3. Add the skill (optional but recommended)

Copy `SKILL.md` to `<project>/.claude/skills/graphskill/SKILL.md`. It tells
Claude to query the graph before reaching for `grep`/`Read` — this is what turns
the tools into actual token savings.

### 4. Activate

Open the project in Claude Code. It will prompt to approve the project-scoped
MCP server — approve it. The graphskill tools then appear natively, and you can
ask things like *"who calls `dispatch`?"* and get an answer straight from the
graph.

> After editing files during a session, re-run `graphskill index <project>` so
> the graph reflects your changes before you rely on it again.

---

## Multiple repositories

graphskill is built for many repos at once, each fully isolated:

- **Isolated storage.** Every project's graph lives in its own directory,
  `~/.graphskill/projects/<slug>/` (slug derived from the project's absolute
  path). Two repos never share or overwrite each other's graph, and the DB is
  never stored inside the repo.
- **Per-project, per-session servers.** Each repo has its own `.mcp.json`
  defining a project-scoped `graphskill serve <that-repo>` server. Claude Code
  loads only the **current** project's `.mcp.json`, so opening repo A starts
  only A's server — B's is never touched.
- **Automatic shutdown.** The server runs over **stdio as a child of the
  session**. When the session ends, the process is killed. There is no
  long-running, cross-project server to leak, and a repo's graph is never served
  in another repo's session.

Onboard each repo once:

```bash
graphskill setup /path/to/repo-a
graphskill setup /path/to/repo-b
graphskill projects          # list everything indexed, with DB locations
```

After editing a repo, refresh just that one: `graphskill index /path/to/repo-a`.

### Viewing the graph in a browser

```bash
graphskill view /path/to/repo-a      # opens Kuzu Explorer at http://localhost:8001
graphskill view --stop               # stop the viewer
```

Needs Docker. Only one project is viewable at a time — `view` stops any running
viewer first, so it always shows the repo you asked for. It serves a read-only
copy with the Kuzu-version-matched Explorer image. In the browser, run Cypher to
visualise subgraphs (return relationships, not just nodes):

```cypher
MATCH p=(a:Symbol {name:'MyClass'})-[:USES|CALLS|CONTAINS*1..2]-(b) RETURN p LIMIT 100
```

---

## MCP tools reference

| Tool | What it returns |
|------|-----------------|
| `search_symbols(query, kind?, limit?)` | Symbols whose name contains `query` → id, kind, `path:line`, signature. **Use before grep.** |
| `get_symbol(ref)` | Signature, docstring, kind, location for one symbol. |
| `read_symbol_body(ref)` | The exact source of **one** function/class/method — not the whole file. |
| `callers(name, depth?)` | Symbols that call `name` (transitive up to `depth`). |
| `callees(name, depth?)` | Symbols called by `name` (transitive up to `depth`). |
| `uses(name)` | Types a class depends on (constructor/property type-hints, `new`, static access). |
| `used_by(name)` | Types that depend on a class (reverse of `uses`). |
| `imports(path)` | Files imported by a file. |
| `dependents(path)` | Files that import a file. |
| `path(src_name, dst_name)` | Shortest call chain between two symbols (list of names), or null. |
| `overview()` | Per-file symbol counts plus graph totals. |

`ref` accepts a symbol **id** (`path#byte`, exact) or a **name** (first match).
`depth` is clamped to 1–6.

---

## CLI reference

| Command | Description |
|---------|-------------|
| `graphskill setup <root> [--force] [--no-index]` | Wire a project: write its `.mcp.json` + skill, then index. One-step onboarding. |
| `graphskill projects` | List all indexed projects and their isolated graph locations. |
| `graphskill view <root> [--port N] [--stop]` | Open this project's graph in Kuzu Explorer (browser); stops any other viewer first. Needs Docker. |
| `graphskill index <root> [--db PATH] [--full]` | Build/refresh the graph. `--full` forces a full rebuild (bypasses the extract cache). |
| `graphskill stats <root> [--db PATH]` | Print node/edge counts as JSON. |
| `graphskill query <cypher> <root> [--db PATH]` | Run raw Cypher against the graph. |
| `graphskill serve <root> [--db PATH]` | Run the MCP server over stdio (used by `.mcp.json`). |

`<root>` defaults to `.`. Each project's graph lives in an **isolated**
directory `~/.graphskill/projects/<slug>/` derived from the project's absolute
path — never inside the repo, never shared between repos. `--db` overrides it.

---

## Language coverage

Two tiers:

- **Full** — Python, JavaScript, TypeScript/TSX, Go, Rust, Java, **PHP**:
  symbols **plus** `CALLS`, `IMPORTS`, `INHERITS`, `CONTAINS` edges, via
  per-language tree-sitter queries (`graphskill/indexer/queries.py`).
- **Generic fallback** — any other tree-sitter language (C, C++, Ruby, C#,
  Swift, Kotlin, …): symbols (functions / classes / methods / structs / …) +
  `CONTAINS`, extracted heuristically by node-type keywords.

Promote a language to the full tier by adding `DEFS` / `IMPORTS` / `INHERITS` /
`CALLS` entries for it in `graphskill/indexer/queries.py`.

Files in `vendor/`, `node_modules/`, `coverage/`, `backups/`, build/cache dirs,
and dot-directories are skipped automatically.

---

## How it works

```
code ──► tree-sitter ──► symbols + edges ──► KuzuDB ──► MCP tools ──► Claude
         per-lang queries   heuristic resolve   (Cypher)   search/callers/path/…
```

- **Parsing** — `tree-sitter-language-pack` precompiled grammars; language
  detected by file extension (`graphskill/parser.py`).
- **Extraction** — symbols + `CONTAINS`/`IMPORTS`/`INHERITS`/`USES` edges and
  call sites (`graphskill/indexer/extract.py`). `USES` = class→class
  dependencies from type-hints, `new`, and static access (PHP). Compiled queries
  are cached per language.
- **Resolution** — call/inherit/import targets resolved by name with a
  `confidence` tag: `EXTRACTED` (unique), `INFERRED` (via an import), or
  `AMBIGUOUS` (multiple candidates) — `graphskill/indexer/resolve.py`.
- **Store** — embedded KuzuDB; Cypher traversal including variable-depth
  `CALLS*1..N` and shortest paths (`graphskill/store.py`). Writes use batched
  `UNWIND` inserts.
- **Incremental** — per-file content-hash manifest + cached extracts; only
  changed files are re-parsed, and a no-change run is a no-op
  (`graphskill/manifest.py`, `graphskill/index.py`).
- **Query** — MCP server over stdio (`graphskill/mcp_server.py`); the query
  logic lives in a plain, unit-testable `GraphQueries` class.

---

## Performance

Measured on a ~2,600-file PHP/JS/TS codebase (≈14k symbols, ≈80k edges):

| Operation | Time |
|-----------|------|
| Initial full index | ~56s (one-time) |
| Re-index after editing a few files | seconds (only changed files re-parsed) |
| Re-index with no changes | instant (hash short-circuit) |

The initial build is write-bound (inserting tens of thousands of nodes/edges);
incremental updates and queries are fast.

---

## Tests

```bash
pip install pytest
python -m pytest
```

---

## Troubleshooting

- **`No graph at …` when starting the server** — run `graphskill index <root>`
  first; the server needs an existing graph.
- **`graphskill: command not found`** — activate the venv, or call the binary by
  its absolute path (e.g. in `.mcp.json`).
- **Console script fails with `ModuleNotFoundError: graphskill`** — you likely
  used `pip install -e .`; reinstall with a regular `pip install .`.
- **Indexing seems stuck on a huge repo** — ensure generated/vendored
  directories are being skipped (they are, by default); a 3MB+ minified bundle
  in a non-skipped dir can be slow to parse.
- **A language isn't producing edges** — it's on the generic fallback tier (only
  symbols + `CONTAINS`). Add per-language queries to promote it.
- **Docker build fails with `error getting credentials … Keychain Error`** — a
  macOS BuildKit + credential-helper quirk while checking the (already cached)
  base image. Build with the legacy builder: `DOCKER_BUILDKIT=0 docker build -t
  graphskill .`
- **Docker `Permission denied (os error 13)` from tree-sitter** — the image sets
  `HOME=/tmp` so this is handled; if you override `HOME`, point it at a writable
  directory.

---

## Limits

- Resolution is **name-based** (no full type/scope/namespace analysis), so
  dynamic dispatch and same-named symbols across files can be imprecise — those
  edges are flagged `AMBIGUOUS`.
- Calls into vendored/stdlib code are **not** nodes in the graph.
- The graph is **structural**, not semantic: it tells you *what connects to
  what*, not *why*. (Semantic summaries and community clustering are possible
  future additions.)
