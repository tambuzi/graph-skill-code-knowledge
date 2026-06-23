# graphskill

Index a codebase into an embedded **graph database**, then let an AI coding
agent (Claude Code) query the graph over **MCP** instead of grepping and reading
whole files.

The goal is **token reduction**. Instead of `grep`-ing a repo and reading entire
files to find a few relevant symbols, the agent asks the graph precise questions
— *"where is `X`?"*, *"what calls `X`?"*, *"show me `X`'s body"*, *"shortest call
chain `A`→`B`"* — and gets back exactly the bytes it needs. It can also orient on
an unfamiliar repo with a PageRank-ranked **repo map**, and find code by intent
with local **semantic search** — no cloud, no API calls.

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
- [Token efficiency](#token-efficiency)
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

The MCP server watches your source files and re-indexes automatically whenever
they change (1.5 s debounce, incremental — only changed files re-parsed). No
manual `graphskill index` needed during a session.

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

The server auto-refreshes while a session is open. To rebuild outside a session
(e.g. after switching branches), run: `graphskill index /path/to/repo-a`.

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

### Discovery / orientation

| Tool | What it returns |
|------|-----------------|
| `repo_map(dir_prefix?, budget_tokens?)` | Top symbols by **PageRank**, trimmed to a token budget. Whole-repo orientation in ~1–2k tokens — **read first on an unfamiliar repo.** |
| `search_semantic(query, limit?, compact?)` | Symbols ranked by **embedding similarity** to `query`. Finds code by *intent* even when the name doesn't match. |
| `search_symbols(query, kind?, limit?, offset?, compact?, visibility?)` | Symbols whose name contains `query` → id, kind, `path:line`, signature, visibility, modifiers. **Use before grep.** |
| `search_docs(query, limit?, offset?, compact?)` | Symbols whose docstring/comment contains `query`. |
| `get_symbol(ref)` | Signature, docstring, kind, location for one symbol. |
| `list_files(dir_prefix?)` | All indexed file paths, optional directory-prefix filter. |
| `module_overview(dir_prefix?)` | Symbol counts grouped by top-level directory. Lighter than `overview()` on big repos. |
| `hot_symbols(n?, edge?)` | Most-referenced symbols by incoming edge count (god nodes). edge: CALLS/USES/INHERITS. |
| `overview()` | Per-file symbol counts plus graph totals. |

### Reading source

| Tool | What it returns |
|------|-----------------|
| `read_symbol_body(ref)` | The exact source of **one** function/class/method — not the whole file. |
| `batch_read_symbol_bodies(refs)` | Multiple symbol bodies in one call — saves round-trips. |
| `symbols_in_file(path, compact?, visibility?)` | All symbols in a file (name, kind, signature, line, visibility, modifiers). **Use instead of reading the file to see its structure.** |

### Graph traversal

| Tool | What it returns |
|------|-----------------|
| `callers(name, depth?, compact?)` | Symbols that call `name`. At depth=1 includes `confidence` + `call_line` (exact call site). |
| `callees(name, depth?, compact?)` | Symbols called by `name`. At depth=1 includes `confidence` + `call_line`. |
| `uses(name, compact?)` | Types a class depends on (type-hints, `new`, static access). Includes `confidence`. |
| `used_by(name, compact?)` | Types that depend on a class (reverse of `uses`). Includes `confidence`. |
| `inheritors(name, depth?, compact?)` | Classes/interfaces that inherit from or implement `name`. |
| `inherited_from(name, depth?, compact?)` | Base classes/interfaces that `name` extends or implements. |
| `imports(path)` / `dependents(path)` | Files a file imports / files that import it. |
| `path(src_name, dst_name)` | Shortest call chain between two symbols, or null. |
| `subgraph(names, depth?)` | callers + callees + uses + used_by for a set of symbols in one call. `depth` capped at 2. |
| `architecture_violations(from_prefix, not_to_prefix, edge?)` | Edges crossing a layer boundary. edge: IMPORTS/CALLS/USES/INHERITS. |

`ref` accepts a symbol **id** (`path#byte`, exact) or a **name** (first match).
`depth` is clamped to 1–6 (1–2 for `subgraph`).

**Compact mode** — most read/traversal tools accept `compact=True` to return
tab-separated rows instead of JSON dicts (~50% fewer tokens). `search_symbols`
and `search_docs` accept `offset` for pagination.

**Confidence tags** — `callers`, `callees`, `uses`, and `used_by` include a
`confidence` field: `EXTRACTED` (unambiguous), `INFERRED` (via import), or
`AMBIGUOUS` (multiple candidates) — verify `AMBIGUOUS` edges before acting.

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
| `graphskill map <root> [--dir PREFIX] [--budget N]` | Print a PageRank-ranked repo map (orientation view). |
| `graphskill audit <root>` | Estimate per-turn input-token overhead of the MCP tool surface + skill. |
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
  `USES` (type-dependency) edges are extracted for **PHP, Python, and
  TypeScript**. Visibility/modifier metadata is best-effort per language.
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
- **Extraction** — symbols (with visibility/modifier metadata) + `CONTAINS`/
  `IMPORTS`/`INHERITS`/`USES` edges and call sites
  (`graphskill/indexer/extract.py`). `USES` = class→class dependencies from
  type-hints, `new`, and static access (PHP/Python/TypeScript). Compiled queries
  are cached per language.
- **Resolution** — call/inherit/import targets resolved by name with a
  `confidence` tag: `EXTRACTED` (unique), `INFERRED` (via an import), or
  `AMBIGUOUS` (multiple candidates) — `graphskill/indexer/resolve.py`. `CALLS`
  edges also carry the call-site line.
- **Ranking + embeddings** — after edges resolve, a pure-python PageRank scores
  every symbol (`graphskill/indexer/rank.py`) and a local model2vec embedder
  encodes each symbol into a sidecar `embeddings.npy`
  (`graphskill/indexer/embed.py`) — powering `repo_map` and `search_semantic`.
- **Store** — embedded KuzuDB; Cypher traversal including variable-depth
  `CALLS*1..N` and shortest paths (`graphskill/store.py`). Writes use batched
  `UNWIND` inserts.
- **Incremental** — per-file content-hash manifest + cached extracts; only
  changed files are re-parsed, and a no-change run is a no-op
  (`graphskill/manifest.py`, `graphskill/index.py`).
- **Query** — MCP server over stdio (`graphskill/mcp_server.py`); the query
  logic lives in a plain, unit-testable `GraphQueries` class.
- **Live re-index** — a `watchdog` observer runs in a daemon thread inside the
  server process. Source file changes trigger an incremental `build_index()` +
  store swap (under a lock) within ~1.5 s, with no server restart required. It
  also watches `.git/HEAD`, so **switching branches re-indexes** against the
  checked-out branch's code (the checkout's file changes coalesce into one
  rebuild via the debounce).
- **Schema versioning** — the DB records a `SCHEMA_VERSION`; if the code's schema
  has moved on, the next `index` forces a full rebuild instead of serving a
  stale-layout graph (even when no source file changed).

---

## Performance

Measured on a ~2,600-file PHP/JS/TS codebase (≈14k symbols, ≈80k edges):

| Operation | Time |
|-----------|------|
| Initial full index | ~56s (one-time) |
| Re-index after editing a few files | seconds (only changed files re-parsed) |
| Re-index with no changes | instant (hash short-circuit) |
| Auto re-index latency (file watcher) | ~1.5 s after last save |

The initial build is write-bound (inserting tens of thousands of nodes/edges);
incremental updates and queries are fast.

---

## Token efficiency

Measured on a real refactoring session against a ~2,600-file PHP/JS/TS codebase
(KnoGraph) — three consecutive architecture-audit iterations, each finding and
fixing Hexagonal Architecture / DDD violations.

Each iteration was run using graphskill MCP tools exclusively; token costs for
the equivalent grep + `Read`-whole-file approach are reconstructed from what
those operations would have consumed.

### Iteration 1 — Application layer audit

| Method | Tokens consumed | Ratio |
|--------|-----------------|-------|
| graphskill MCP | ~4,019 | 1× |
| grep + Read | ~52,660 | 13.1× |
| **Savings** | | **92.4 %** |

MCP calls used: `search_symbols`, `imports`, `read_symbol_body` (targeted).  
Grep equivalent: `grep -rn "use KnoGraph\\Infrastructure"` across all layers +
full-file `Read` for each of the ~25 flagged files.

### Iteration 2 — Systemic `CommandType` + filesystem violations

| Method | Tokens consumed | Ratio |
|--------|-----------------|-------|
| graphskill MCP | ~11,650 | 1× |
| grep + Read | ~42,500 | 3.6× |
| **Savings** | | **72.6 %** |

Savings were lower here because:
- One `CommandType` fix required reading the full 274-line file regardless of method.
- The MCP graph had not yet been refreshed after iteration 1 edits, so three
  symbol lookups fell back to grep.
- Filesystem-call violations (`tempnam`, `file_get_contents`) are faster to
  spot with grep patterns than with symbol-based search.

### Iteration 3 — Systemic `handle()` → `__invoke()` CQS fix

| Method | Tokens consumed | Ratio |
|--------|-----------------|-------|
| graphskill MCP | ~3,300 | 1× |
| grep + Read | ~15,700 | 4.8× |
| **Savings** | | **79.0 %** |

MCP calls used: `search_symbols("handle", kind=method)` to identify all 79
handler methods in one query, `read_symbol_body` on two outlier handlers
(`ClusterToTopicCommandHandler`, `ListUsersQueryHandler`) to see the no-param
design flaw, and `search_symbols` on Research handlers to confirm they were
already compliant.

Grep equivalent: grep for `public function handle(` across all files + reading
`CommandHandlerInterface`, `QueryHandlerInterface`, `CommandBus`, a sample of
10+ handler files to understand patterns, and spot-checking the Research module.

Savings were moderate rather than dramatic because iteration 3 was a single
well-defined pattern (`handle` → `__invoke`); grep is reasonably efficient on a
single known string. MCP's advantage remained in targeted body lookups and
avoiding reading files that were already correct.

### Cumulative (all three iterations)

| | Tokens |
|--|--------|
| graphskill MCP | ~18,969 |
| grep + Read | ~110,860 |
| **Net savings** | **~91,891 tokens (82.9 %)** |

### Why MCP is cheaper

- `imports(path)` returns only the import list — not the file body.
- `read_symbol_body(ref)` returns one class or function — not the whole file.
- `search_symbols(query)` returns structured name/location/signature rows —
  no grep output noise or false positives to filter.
- `symbols_in_file(path)` returns all signatures in a file without reading the
  file body — replaces opening a 300-line file just to see what methods exist.
- `batch_read_symbol_bodies(refs)` reads N bodies in one call — replaces N
  sequential tool calls each with their own framing overhead.
- `inheritors(name)` finds all implementors of an interface in one query —
  replaces grepping for class declarations across hundreds of files.
- `subgraph(names)` returns the full call/dependency neighbourhood in one call
  — replaces four separate `callers`/`callees`/`uses`/`used_by` queries.
- `confidence` on edge results tells Claude when to trust an edge vs. verify —
  prevents body reads wasted on AMBIGUOUS false edges.

The efficiency gap is largest when violations are **sparse** (few bad files in a
large codebase) or **structural** (inheritance, interfaces): grep must scan
everything; the graph answers in one hop.

### Tool overhead (the other side of the ledger)

The tool schemas + skill are sent as **input on every turn**, so they offset the
per-query output savings. `graphskill audit` measures it:

```
$ graphskill audit .
Tools:            23
Tool schemas:     ~1734 tokens / turn
SKILL.md:         ~290 tokens / turn
Total overhead:   ~2024 tokens / turn
Break-even:       ~1 graph queries/session (@ ~5000 tok/query saved)
```

Because a single graph query typically saves thousands of output tokens, the
tool surface pays for itself after roughly one query per session. Tool
docstrings are deliberately kept to one line (full guidance lives in `SKILL.md`)
to keep this overhead low.

### Orientation & intent (repo map + semantic search)

- `repo_map()` ranks symbols by **PageRank** over the call/use/inherit graph and
  returns the most important ones within a token budget — a whole-repo overview
  for ~1–2k tokens instead of reading entry-point files.
- `search_semantic()` matches by **meaning** (local model2vec embeddings, no
  cloud), so an intent query like *"parse a source file into symbols"* returns
  `parse_source` / `parse_file` even though the words don't appear in the names —
  avoiding the failed-substring-search → grep → read-file retry loop.

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
- The graph is primarily **structural**. `search_semantic` adds an embedding
  layer for intent-based lookup, but the embedder is a small static model — good
  for ranking by similarity, not for generating explanations. Per-symbol natural
  language summaries and community clustering remain possible future additions.
- Embeddings require a one-time model download (~30 MB from the HuggingFace hub)
  on the first index; after that, encoding is fully local/offline. If the
  download is unavailable, indexing still succeeds — `search_semantic` simply
  returns nothing until embeddings exist.
- `USES` edges cover PHP, Python, and TypeScript; other full-tier languages have
  `CALLS`/`IMPORTS`/`INHERITS` only.
