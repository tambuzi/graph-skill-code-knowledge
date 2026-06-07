"""Tree-sitter wrapper: language detection + parsing.

Uses the core ``tree_sitter.Parser`` with a ``Language`` pulled from
``tree_sitter_language_pack``. Note: we deliberately do NOT use the pack's
``get_parser`` helper — at the installed versions it returns a parser whose
``Tree`` is a foreign type (``builtins.Tree``) with a different ABI, so
``root_node`` misbehaves. Building ``Parser(get_language(name))`` ourselves
yields the real ``tree_sitter.Tree``/``Node`` types.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from tree_sitter import Parser
from tree_sitter_language_pack import get_language

# Map file extension -> tree-sitter-language-pack language name.
# Language-agnostic by design: add a row to widen coverage.
EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".lua": "lua",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
}


def detect_language(path: str | Path) -> str | None:
    """Return the tree-sitter language name for a file, or None if unknown."""
    return EXT_TO_LANG.get(Path(path).suffix.lower())


@lru_cache(maxsize=None)
def get_ts_parser(lang: str) -> Parser:
    """Return a cached core tree-sitter Parser for a language name."""
    return Parser(get_language(lang))


def parse_source(source: bytes, lang: str):
    """Parse bytes for a known language; return the tree-sitter Tree."""
    return get_ts_parser(lang).parse(source)


def parse_file(path: str | Path) -> tuple[object, bytes, str] | None:
    """Read + parse a file.

    Returns ``(tree, source_bytes, lang)`` or ``None`` if the language is
    unknown or the file cannot be read as bytes.
    """
    lang = detect_language(path)
    if lang is None:
        return None
    try:
        source = Path(path).read_bytes()
    except (OSError, ValueError):
        return None
    return parse_source(source, lang), source, lang
