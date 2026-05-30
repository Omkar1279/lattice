"""AST-based code graph extraction using tree-sitter."""

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LanguageProfile:
    """Bundles tree-sitter module info + queries for one language."""
    module: str
    func: str
    queries: dict[str, str]
    import_names_query: str = ''

# --- Query definitions (grammar-specific, unavoidable) ---

_TS_QUERIES = {
    'imports': '''
        (import_statement source: (string) @import_path)
        (export_statement source: (string) @import_path)
    ''',
    'calls': '''
        (call_expression function: (identifier) @callee)
        (call_expression function: (member_expression property: (property_identifier) @callee))
    ''',
    'exports': '''
        (export_statement declaration: (function_declaration name: (identifier) @export_name))
        (export_statement declaration: (class_declaration name: (type_identifier) @export_name))
        (export_statement declaration: (lexical_declaration (variable_declarator name: (identifier) @export_name)))
    ''',
    'implements': '''
        (class_declaration
          name: (type_identifier) @class_name
          (class_heritage (implements_clause (type_identifier) @interface_name)))
        (class_declaration
          name: (type_identifier) @class_name
          (class_heritage (extends_clause value: (identifier) @parent_name)))
    ''',
}

_TS_IMPORT_NAMES = '''
    (import_statement (import_clause (named_imports (import_specifier name: (identifier) @imported_name))))
    (import_statement (import_clause (identifier) @imported_name))
'''

_JS_QUERIES = {
    'imports': '''
        (import_declaration source: (string) @import_path)
        (call_expression function: (identifier) @require_call (#eq? @require_call "require") arguments: (arguments (string) @import_path))
    ''',
    'calls': '''
        (call_expression function: (identifier) @callee)
        (call_expression function: (member_expression property: (property_identifier) @callee))
    ''',
    'exports': '''
        (export_statement declaration: (function_declaration name: (identifier) @export_name))
        (export_statement declaration: (class_declaration name: (identifier) @export_name))
        (export_statement declaration: (lexical_declaration (variable_declarator name: (identifier) @export_name)))
    ''',
    'implements': '''
        (class_declaration
          name: (identifier) @class_name
          (class_heritage (extends_clause value: (identifier) @parent_name)))
    ''',
}

_PY_QUERIES = {
    'imports': '''
        (import_from_statement module_name: (dotted_name) @import_path)
        (import_statement name: (dotted_name) @import_path)
    ''',
    'calls': '''
        (call function: (identifier) @callee)
        (call function: (attribute attribute: (identifier) @callee))
    ''',
    'exports': '''
        (function_definition name: (identifier) @export_name)
        (class_definition name: (identifier) @export_name)
    ''',
    'implements': '''
        (class_definition
          name: (identifier) @class_name
          superclasses: (argument_list (identifier) @parent_name))
    ''',
}

_RS_QUERIES = {
    'imports': '''
        (use_declaration argument: (scoped_identifier) @import_path)
        (use_declaration argument: (identifier) @import_path)
        (use_declaration argument: (use_wildcard) @import_path)
    ''',
    'calls': '''
        (call_expression function: (identifier) @callee)
        (call_expression function: (scoped_identifier name: (identifier) @callee))
        (call_expression function: (field_expression field: (field_identifier) @callee))
    ''',
    'exports': '''
        (function_item name: (identifier) @export_name)
        (struct_item name: (type_identifier) @export_name)
        (enum_item name: (type_identifier) @export_name)
        (trait_item name: (type_identifier) @export_name)
        (impl_item trait: (type_identifier) @export_name)
    ''',
    'implements': '''
        (impl_item
          trait: (type_identifier) @interface_name
          type: (type_identifier) @class_name)
    ''',
}

_GO_QUERIES = {
    'imports': '''
        (import_spec path: (interpreted_string_literal) @import_path)
    ''',
    'calls': '''
        (call_expression function: (identifier) @callee)
        (call_expression function: (selector_expression field: (field_identifier) @callee))
    ''',
    'exports': '''
        (function_declaration name: (identifier) @export_name)
        (method_declaration name: (field_identifier) @export_name)
        (type_declaration (type_spec name: (type_identifier) @export_name))
    ''',
    'implements': '''
        (type_declaration
          (type_spec
            name: (type_identifier) @class_name
            type: (struct_type)))
    ''',
}

# --- Profiles: single source of truth per language ---

TS_PROFILE = LanguageProfile('tree_sitter_typescript', 'language_typescript', _TS_QUERIES, _TS_IMPORT_NAMES)
TSX_PROFILE = LanguageProfile('tree_sitter_typescript', 'language_tsx', _TS_QUERIES, _TS_IMPORT_NAMES)
JS_PROFILE = LanguageProfile('tree_sitter_javascript', 'language', _JS_QUERIES)
PY_PROFILE = LanguageProfile('tree_sitter_python', 'language', _PY_QUERIES)
RS_PROFILE = LanguageProfile('tree_sitter_rust', 'language', _RS_QUERIES)
GO_PROFILE = LanguageProfile('tree_sitter_go', 'language', _GO_QUERIES)

EXTENSION_MAP: dict[str, LanguageProfile] = {
    '.ts': TS_PROFILE,
    '.tsx': TSX_PROFILE,
    '.js': JS_PROFILE,
    '.jsx': JS_PROFILE,
    '.py': PY_PROFILE,
    '.rs': RS_PROFILE,
    '.go': GO_PROFILE,
}

# --- Parser cache ---

_parsers: dict[str, Any] = {}
_languages: dict[str, Any] = {}


def get_language(ext: str):
    if ext in _languages:
        return _languages[ext]

    profile = EXTENSION_MAP.get(ext)
    if not profile:
        return None

    try:
        import importlib
        from tree_sitter import Language
        mod = importlib.import_module(profile.module)
        lang = Language(getattr(mod, profile.func)())
        _languages[ext] = lang
        return lang
    except Exception:
        return None


def get_parser(ext: str):
    if ext in _parsers:
        return _parsers[ext]

    lang = get_language(ext)
    if not lang:
        return None

    from tree_sitter import Parser
    parser = Parser(lang)
    _parsers[ext] = parser
    return parser


def init_tree_sitter() -> None:
    """Pre-warm all available AST parsers into memory."""
    for ext in EXTENSION_MAP:
        get_parser(ext)


# --- Edge extraction (table-driven) ---

_CAPTURE_KIND = {
    'import_path': 'imports',
    'callee': 'calls',
    'export_name': 'exports',
}

_BASE_CONFIDENCE = {
    'imports': 1.0,
    'exports': 1.0,
    'calls': 0.6,
}


def _node_text(node) -> str:
    return node.text.decode('utf8') if isinstance(node.text, bytes) else node.text


def _run_query(lang, query_str: str, tree):
    """Run a tree-sitter query, yield (pattern_idx, captures_dict) per match."""
    try:
        from tree_sitter import Query, QueryCursor
        query = Query(lang, query_str)
        cursor = QueryCursor(query)
        yield from cursor.matches(tree.root_node)
    except Exception:
        return


def extract_edges(file_path: str, source: str) -> list[dict[str, Any]]:
    """Extract code graph edges via AST queries."""
    ext = os.path.splitext(file_path)[1]
    parser = get_parser(ext)
    if not parser:
        return []

    profile = EXTENSION_MAP.get(ext)
    if not profile:
        return []

    lang = get_language(ext)
    tree = parser.parse(source.encode('utf8'))
    edges: list[dict[str, Any]] = []

    # Collect imported names for call confidence boosting
    imported_names: set[str] = set()
    if profile.import_names_query:
        for _pat, captures in _run_query(lang, profile.import_names_query, tree):
            for node in captures.get('imported_name', []):
                imported_names.add(_node_text(node))

    # Extract imports, calls, exports (simple single-capture → edge)
    for kind, query_str in profile.queries.items():
        if kind == 'implements':
            continue

        for _pat, captures in _run_query(lang, query_str, tree):
            for cap_name, nodes in captures.items():
                edge_kind = _CAPTURE_KIND.get(cap_name)
                if not edge_kind:
                    continue
                for node in nodes:
                    text = _node_text(node).strip('\'"')
                    confidence = _BASE_CONFIDENCE[edge_kind]
                    if edge_kind == 'calls':
                        confidence = 0.85 if text in imported_names else 0.6
                    edges.append({
                        'kind': edge_kind,
                        'target_symbol': text,
                        'line': node.start_point[0],
                        'confidence': confidence,
                    })

    # Implements/extends (multi-capture per match)
    if 'implements' in profile.queries:
        for _pat, captures in _run_query(lang, profile.queries['implements'], tree):
            class_nodes = captures.get('class_name', [])
            iface_nodes = captures.get('interface_name', [])
            parent_nodes = captures.get('parent_name', [])

            class_name = _node_text(class_nodes[0]) if class_nodes else ''
            target = ''
            is_interface = False

            if iface_nodes:
                target = _node_text(iface_nodes[0])
                is_interface = True
            elif parent_nodes:
                target = _node_text(parent_nodes[0])

            if class_name and target:
                edges.append({
                    'kind': 'implements' if is_interface else 'extends',
                    'target_symbol': target,
                    'line': class_nodes[0].start_point[0],
                    'confidence': 0.95,
                })

    return edges
