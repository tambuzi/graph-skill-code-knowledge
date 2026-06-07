# graphskill — code knowledge graph skill, containerised.
#
# Build:
#   docker build -t graphskill .
#
# Index a project (writes <project>/.graphskill on the host via the mount):
#   docker run --rm -v "$PWD":/workspace graphskill index /workspace
#
# Run the MCP server over stdio (this is what .mcp.json invokes):
#   docker run -i --rm -v "$PWD":/workspace graphskill serve /workspace
#
# Tip: add `--user "$(id -u):$(id -g)"` so files written to .graphskill are
# owned by you rather than root.

FROM python:3.11-slim

# Faster, quieter Python in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the package. Native deps (tree-sitter, kuzu) ship manylinux/aarch64
# wheels, so no compiler toolchain is needed.
COPY pyproject.toml README.md ./
COPY graphskill ./graphskill
RUN pip install .

# tree-sitter-language-pack's native backend needs a writable HOME for its
# grammar cache. /tmp is world-writable, so the image works even when run with
# an arbitrary `--user` (e.g. `--user $(id -u):$(id -g)`).
ENV HOME=/tmp

# Projects are mounted here; `.` then resolves to the mounted repo.
WORKDIR /workspace

ENTRYPOINT ["graphskill"]
CMD ["--help"]
