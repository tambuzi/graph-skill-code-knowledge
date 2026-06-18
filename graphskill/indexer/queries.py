"""Per-language tree-sitter queries for structural extraction.

Capture conventions (read by ``extract.py``):

* ``@def.<kind>`` — the *whole* definition node (function/method/class/...).
  The suffix is the symbol ``kind``. Its byte span is the symbol body.
* ``@name``       — the identifier naming the nearest ``@def.*`` in the match.
* ``@import``     — a module/path string referenced by an import statement.
* ``@inherit``    — a base-class/interface identifier (pairs with ``@name``).
* ``@call``       — the callee identifier at a call site.

Languages without an entry fall back to a generic node-type walk
(``extract.py: generic_definitions``), so every language yields at least
function/class symbols.
"""

from __future__ import annotations

DEFS: dict[str, str] = {
    "python": """
        (function_definition name: (identifier) @name) @def.function
        (class_definition name: (identifier) @name) @def.class
    """,
    "javascript": """
        (function_declaration name: (identifier) @name) @def.function
        (method_definition name: (property_identifier) @name) @def.method
        (class_declaration name: (identifier) @name) @def.class
    """,
    "typescript": """
        (function_declaration name: (identifier) @name) @def.function
        (method_definition name: (property_identifier) @name) @def.method
        (class_declaration name: (type_identifier) @name) @def.class
        (interface_declaration name: (type_identifier) @name) @def.interface
    """,
    "go": """
        (function_declaration name: (identifier) @name) @def.function
        (method_declaration name: (field_identifier) @name) @def.method
        (type_declaration (type_spec name: (type_identifier) @name)) @def.type
    """,
    "rust": """
        (function_item name: (identifier) @name) @def.function
        (struct_item name: (type_identifier) @name) @def.struct
        (enum_item name: (type_identifier) @name) @def.enum
        (trait_item name: (type_identifier) @name) @def.trait
    """,
    "java": """
        (method_declaration name: (identifier) @name) @def.method
        (class_declaration name: (identifier) @name) @def.class
        (interface_declaration name: (identifier) @name) @def.interface
    """,
    "php": """
        (function_definition name: (name) @name) @def.function
        (method_declaration name: (name) @name) @def.method
        (class_declaration name: (name) @name) @def.class
        (interface_declaration name: (name) @name) @def.interface
        (trait_declaration name: (name) @name) @def.trait
        (enum_declaration name: (name) @name) @def.enum
    """,
}
# tsx shares typescript grammar shape for these constructs.
DEFS["tsx"] = DEFS["typescript"]

IMPORTS: dict[str, str] = {
    "python": """
        (import_statement name: (dotted_name) @import)
        (import_from_statement module_name: (dotted_name) @import)
        (import_from_statement module_name: (relative_import (dotted_name) @import))
    """,
    "javascript": "(import_statement source: (string) @import)",
    "typescript": "(import_statement source: (string) @import)",
    "tsx": "(import_statement source: (string) @import)",
    "go": "(import_spec path: (interpreted_string_literal) @import)",
    "rust": "(use_declaration argument: (_) @import)",
    "java": "(import_declaration (scoped_identifier) @import)",
    "php": "(namespace_use_clause (qualified_name) @import)",
}

INHERITS: dict[str, str] = {
    "python": """
        (class_definition
            name: (identifier) @name
            superclasses: (argument_list (identifier) @inherit))
    """,
    "typescript": """
        (class_declaration
            name: (type_identifier) @name
            (class_heritage (extends_clause value: (identifier) @inherit)))
    """,
    "javascript": """
        (class_declaration
            name: (identifier) @name
            (class_heritage (identifier) @inherit))
    """,
    "java": """
        (class_declaration
            name: (identifier) @name
            superclass: (superclass (type_identifier) @inherit))
    """,
    "php": """
        (class_declaration name: (name) @name (base_clause (name) @inherit))
        (class_declaration name: (name) @name (class_interface_clause (name) @inherit))
    """,
}
INHERITS["tsx"] = INHERITS["typescript"]

CALLS: dict[str, str] = {
    "python": """
        (call function: (identifier) @call)
        (call function: (attribute attribute: (identifier) @call))
    """,
    "javascript": """
        (call_expression function: (identifier) @call)
        (call_expression function: (member_expression property: (property_identifier) @call))
    """,
    "typescript": """
        (call_expression function: (identifier) @call)
        (call_expression function: (member_expression property: (property_identifier) @call))
    """,
    "go": """
        (call_expression function: (identifier) @call)
        (call_expression function: (selector_expression field: (field_identifier) @call))
    """,
    "rust": "(call_expression function: (identifier) @call)",
    "java": "(method_invocation name: (identifier) @call)",
    "php": """
        (function_call_expression function: (name) @call)
        (member_call_expression name: (name) @call)
        (scoped_call_expression name: (name) @call)
    """,
}
CALLS["tsx"] = CALLS["typescript"]

# Class-level type references → USES (DEPENDS_ON) edges between type symbols.
# Captures type-hints (params/properties/returns), `new X()`, static `X::...`,
# and `X::class`. Non-type names (constants, the `class` keyword, method names)
# are captured too but harmlessly dropped at resolution, which only links names
# that match a class/interface/trait/enum symbol.
USES: dict[str, str] = {
    "php": """
        (named_type (name) @use)
        (named_type (qualified_name) @use)
        (object_creation_expression (name) @use)
        (object_creation_expression (qualified_name) @use)
        (scoped_call_expression scope: (name) @use)
        (scoped_call_expression scope: (qualified_name) @use)
        (class_constant_access_expression (name) @use)
    """,
    # Simple type names only — generics (Optional[X], List[X]) not captured.
    "python": """
        (typed_parameter type: (type (identifier) @use))
        (function_definition return_type: (type (identifier) @use))
        (assignment type: (type (identifier) @use))
    """,
    # Captures all type_identifier nodes inside type annotations (method params,
    # return types, property declarations). Generic args like Array<X> are also
    # captured; non-class names are silently dropped at resolution.
    "typescript": "(type_annotation (type_identifier) @use)",
}
USES["tsx"] = USES["typescript"]

# Generic fallback for languages without a DEFS entry. A node is treated as a
# definition when its type ends with one of these suffixes or matches an exact
# name below; its `kind` comes from the first keyword found in the type.
GENERIC_DEF_SUFFIXES = ("_definition", "_declaration", "_item", "_specifier")
GENERIC_DEF_EXACT = {
    "method", "class", "module", "function", "interface", "struct", "trait",
    "enum", "singleton_method", "constructor",
}
# Ordered: first keyword found in the node type wins.
GENERIC_KIND_KEYWORDS = [
    ("singleton_method", "method"),
    ("method", "method"),
    ("function", "function"),
    ("constructor", "method"),
    ("class", "class"),
    ("struct", "struct"),
    ("interface", "interface"),
    ("trait", "trait"),
    ("enum", "enum"),
    ("module", "module"),
]
# Body/block node types: stop scanning for a name once we reach one.
GENERIC_BODY_TYPES = {
    "block", "body", "body_statement", "class_body", "declaration_list",
    "field_declaration_list", "compound_statement", "statement_block",
    "enum_variant_list",
}
GENERIC_NAME_TYPES = {
    "identifier", "type_identifier", "field_identifier", "constant",
    "property_identifier", "name",
}


def generic_kind(node_type: str) -> str | None:
    if node_type not in GENERIC_DEF_EXACT and not node_type.endswith(GENERIC_DEF_SUFFIXES):
        return None
    for kw, kind in GENERIC_KIND_KEYWORDS:
        if kw in node_type:
            return kind
    return None
