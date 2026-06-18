"""`graphskill` command-line entrypoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .index import build_index, default_db_path
from .registry import list_projects, project_dir
from .store import GraphStore

_SKILL_TEMPLATE = """\
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
"""


@click.group()
def main() -> None:
    """Index code into a graph and query it."""


@main.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--db", "db_path", type=click.Path(), default=None, help="Graph DB path.")
@click.option("--full", is_flag=True, help="Force full rebuild (ignore cache short-circuit).")
def index(root: str, db_path: str | None, full: bool) -> None:
    """Build or refresh the code graph for ROOT."""
    build_index(root, db_path=db_path, incremental=not full, log=click.echo)


@main.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--db", "db_path", type=click.Path(), default=None)
def stats(root: str, db_path: str | None) -> None:
    """Show node/edge counts."""
    p = Path(db_path) if db_path else default_db_path(root)
    if not p.exists():
        raise click.ClickException(f"No graph at {p}. Run `graphskill index` first.")
    store = GraphStore(p)
    click.echo(json.dumps(store.stats(), indent=2))
    store.close()


@main.command()
@click.argument("cypher")
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--db", "db_path", type=click.Path(), default=None)
def query(cypher: str, root: str, db_path: str | None) -> None:
    """Run a raw Cypher QUERY against the graph (escape hatch)."""
    p = Path(db_path) if db_path else default_db_path(root)
    if not p.exists():
        raise click.ClickException(f"No graph at {p}. Run `graphskill index` first.")
    store = GraphStore(p)
    for row in store.query(cypher):
        click.echo(json.dumps(row, default=str))
    store.close()


@main.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--db", "db_path", type=click.Path(), default=None)
@click.option("--dir", "dir_prefix", default=None, help="Limit to a directory prefix.")
@click.option("--budget", default=2000, show_default=True, help="Approx token budget.")
def map(root: str, db_path: str | None, dir_prefix: str | None, budget: int) -> None:
    """Print a PageRank-ranked repo map (orientation view) for ROOT."""
    from .mcp_server import GraphQueries

    p = Path(db_path) if db_path else default_db_path(root)
    if not p.exists():
        raise click.ClickException(f"No graph at {p}. Run `graphskill index` first.")
    gq = GraphQueries(p, root)
    for line in gq.repo_map(dir_prefix, budget):
        click.echo(line)
    gq.close()


@main.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
def audit(root: str) -> None:
    """Estimate per-turn input-token overhead of the MCP tool surface + skill."""
    from .mcp_server import estimate_tool_overhead

    ov = estimate_tool_overhead()
    skill = Path(root).resolve() / ".claude" / "skills" / "graphskill" / "SKILL.md"
    skill_tokens = (len(skill.read_text()) // 4) if skill.exists() else 0
    per_turn = ov["tokens"] + skill_tokens

    click.echo(f"Tools:            {ov['tool_count']}")
    click.echo(f"Tool schemas:     ~{ov['tokens']} tokens / turn")
    click.echo(f"SKILL.md:         ~{skill_tokens} tokens / turn")
    click.echo(f"Total overhead:   ~{per_turn} tokens / turn")
    # README measures ~5,000 tokens saved per graph query vs grep+Read on average.
    avg_saving = 5000
    breakeven = max(1, round(per_turn / avg_saving, 2))
    click.echo(f"Break-even:       ~{breakeven} graph queries/session (@ ~{avg_saving} tok/query saved)")
    click.echo("\nTop 5 tools by schema size:")
    for name, chars in ov["per_tool"][:5]:
        click.echo(f"  {name:<26} ~{chars // 4} tokens")


@main.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--db", "db_path", type=click.Path(), default=None)
def serve(root: str, db_path: str | None) -> None:
    """Run the MCP server over the graph (stdio)."""
    from .mcp_server import run_server

    p = Path(db_path) if db_path else default_db_path(root)
    run_server(str(p), str(Path(root).resolve()))


@main.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--db", "db_path", type=click.Path(), default=None)
@click.option("--port", default=8001, show_default=True, help="Local port for the viewer.")
@click.option("--stop", is_flag=True, help="Stop the running viewer and exit.")
def view(root: str, db_path: str | None, port: int, stop: bool) -> None:
    """Open this project's graph in Kuzu Explorer (browser), stopping any other.

    Only one project is viewable at a time: any existing viewer is shut down
    first, so the viewer always shows the project you ask for.
    """
    import shutil
    import subprocess

    import kuzu

    from .registry import graphskill_home

    container = "graphskill-explorer"

    def docker(*args, **kw):
        try:
            return subprocess.run(["docker", *args], **kw)
        except FileNotFoundError:
            raise click.ClickException("Docker not found. Install/start Docker to use the viewer.")

    # Shut down any existing viewer (legacy name too) — one project at a time.
    docker("rm", "-f", container, "kuzu-explorer", capture_output=True)
    if stop:
        click.echo("Viewer stopped.")
        return

    p = Path(db_path) if db_path else default_db_path(root)
    if not p.exists():
        raise click.ClickException(f"No graph at {p}. Run `graphskill index {root}` first.")

    # Explorer mounts a directory and is pinned to the Kuzu storage version, so
    # copy this project's DB into a fresh view dir and use the matching image.
    view_dir = graphskill_home() / "view"
    if view_dir.exists():
        shutil.rmtree(view_dir)
    view_dir.mkdir(parents=True)
    (shutil.copytree if p.is_dir() else shutil.copy)(p, view_dir / "graph.kuzu")

    res = docker(
        "run", "-d", "--name", container, "-p", f"{port}:8000",
        "-v", f"{view_dir}:/database",
        "-e", "KUZU_DIR=/database", "-e", "KUZU_FILE=graph.kuzu", "-e", "MODE=READ_ONLY",
        f"kuzudb/explorer:{kuzu.__version__}",
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise click.ClickException("Failed to start viewer:\n" + (res.stderr or res.stdout))
    click.echo(f"Viewing {Path(root).resolve().name} → http://localhost:{port}  (read-only)")
    click.echo("Stop with: graphskill view --stop")


@main.command()
def projects() -> None:
    """List all indexed projects and their isolated graph locations."""
    rows = list_projects()
    if not rows:
        click.echo("No projects indexed yet. Run `graphskill setup <project>`.")
        return
    for e in rows:
        st = e.get("stats", {})
        click.echo(
            f"{e['root']}\n  db:   {e['db']}\n  "
            f"symbols={st.get('Symbol','?')} files={st.get('File','?')} "
            f"indexed={e.get('last_indexed','?')}"
        )


@main.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--force", is_flag=True, help="Overwrite existing .mcp.json / SKILL.md.")
@click.option("--index/--no-index", "do_index", default=True, help="Build the graph now.")
def setup(root: str, force: bool, do_index: bool) -> None:
    """Wire graphskill into a project: write .mcp.json + skill, then index.

    Each project gets its own isolated graph and a project-scoped MCP server, so
    opening that project in Claude Code starts only that project's server.
    """
    root_path = Path(root).resolve()

    # Project-scoped MCP server. Use this interpreter so PATH never matters and
    # the server always resolves to THIS project's isolated DB.
    mcp = {
        "mcpServers": {
            "graphskill": {
                "command": sys.executable,
                "args": ["-m", "graphskill", "serve", str(root_path)],
                "env": {},
            }
        }
    }
    mcp_file = root_path / ".mcp.json"
    if mcp_file.exists() and not force:
        click.echo(f"skip (exists): {mcp_file}  (use --force)")
    else:
        mcp_file.write_text(json.dumps(mcp, indent=2) + "\n")
        click.echo(f"wrote {mcp_file}")

    skill_file = root_path / ".claude" / "skills" / "graphskill" / "SKILL.md"
    if skill_file.exists() and not force:
        click.echo(f"skip (exists): {skill_file}  (use --force)")
    else:
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(_SKILL_TEMPLATE)
        click.echo(f"wrote {skill_file}")

    click.echo(f"graph dir: {project_dir(root_path)}")
    if do_index:
        build_index(root_path, log=click.echo)
    click.echo("Done. Open this project in Claude Code and approve the MCP server.")


if __name__ == "__main__":
    main()
