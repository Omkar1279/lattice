import pytest
from lattice.indexer.graph import (
    init_tree_sitter,
    extract_edges,
    get_parser,
    get_language,
    EXTENSION_MAP,
    LanguageProfile,
)


@pytest.fixture(autouse=True)
def init_parser():
    init_tree_sitter()


# --- LanguageProfile / EXTENSION_MAP structure ---

def test_extension_map_shares_js_jsx_profile():
    assert EXTENSION_MAP['.js'] is EXTENSION_MAP['.jsx']


def test_extension_map_shares_ts_tsx_queries():
    assert EXTENSION_MAP['.ts'].queries is EXTENSION_MAP['.tsx'].queries


def test_all_profiles_have_required_query_keys():
    required = {'imports', 'calls', 'exports', 'implements'}
    for ext, profile in EXTENSION_MAP.items():
        assert required <= set(profile.queries.keys()), f'{ext} missing query keys'


def test_unsupported_extension_returns_empty():
    assert extract_edges('file.unknown', 'anything') == []
    assert get_parser('.xyz') is None
    assert get_language('.xyz') is None


# --- TypeScript ---

class TestTypeScript:
    def test_import_and_call_with_confidence_boost(self):
        source = 'import { helper } from "./util";\nhelper();'
        edges = extract_edges('test.ts', source)

        imports = [e for e in edges if e['kind'] == 'imports']
        assert len(imports) == 1
        assert imports[0]['target_symbol'] == './util'
        assert imports[0]['confidence'] == 1.0

        calls = [e for e in edges if e['kind'] == 'calls']
        assert len(calls) == 1
        assert calls[0]['target_symbol'] == 'helper'
        assert calls[0]['confidence'] == 0.85  # boosted because imported

    def test_unimported_call_has_base_confidence(self):
        source = 'unknownFunc();'
        edges = extract_edges('test.ts', source)
        calls = [e for e in edges if e['kind'] == 'calls']
        assert len(calls) == 1
        assert calls[0]['confidence'] == 0.6

    def test_export_function(self):
        source = 'export function helper() { return 1; }'
        edges = extract_edges('test.ts', source)
        exports = [e for e in edges if e['kind'] == 'exports']
        assert len(exports) == 1
        assert exports[0]['target_symbol'] == 'helper'
        assert exports[0]['confidence'] == 1.0

    def test_export_class(self):
        source = 'export class MyService {}'
        edges = extract_edges('test.ts', source)
        exports = [e for e in edges if e['kind'] == 'exports']
        assert len(exports) == 1
        assert exports[0]['target_symbol'] == 'MyService'

    def test_implements_interface(self):
        source = 'class MyClass implements MyInterface {}'
        edges = extract_edges('test.ts', source)
        impl = [e for e in edges if e['kind'] == 'implements']
        assert len(impl) == 1
        assert impl[0]['target_symbol'] == 'MyInterface'
        assert impl[0]['confidence'] == 0.95

    def test_extends_base_class(self):
        source = 'class MyClass extends BaseClass {}'
        edges = extract_edges('test.ts', source)
        ext = [e for e in edges if e['kind'] == 'extends']
        assert len(ext) == 1
        assert ext[0]['target_symbol'] == 'BaseClass'

    def test_multiple_imports(self):
        source = 'import { a } from "./a";\nimport { b } from "./b";'
        edges = extract_edges('test.ts', source)
        imports = [e for e in edges if e['kind'] == 'imports']
        assert len(imports) == 2
        symbols = {e['target_symbol'] for e in imports}
        assert symbols == {'./a', './b'}

    def test_line_numbers_are_zero_based(self):
        source = 'const x = 1;\nfoo();'
        edges = extract_edges('test.ts', source)
        calls = [e for e in edges if e['kind'] == 'calls']
        assert calls[0]['line'] == 1  # second line


# --- Python ---

class TestPython:
    def test_import_from(self):
        source = 'from os.path import join'
        edges = extract_edges('test.py', source)
        imports = [e for e in edges if e['kind'] == 'imports']
        assert len(imports) == 1
        assert imports[0]['target_symbol'] == 'os.path'

    def test_import_statement(self):
        source = 'import json'
        edges = extract_edges('test.py', source)
        imports = [e for e in edges if e['kind'] == 'imports']
        assert len(imports) == 1
        assert imports[0]['target_symbol'] == 'json'

    def test_function_call(self):
        source = 'result = process(data)'
        edges = extract_edges('test.py', source)
        calls = [e for e in edges if e['kind'] == 'calls']
        assert any(c['target_symbol'] == 'process' for c in calls)

    def test_method_call(self):
        source = 'obj.method()'
        edges = extract_edges('test.py', source)
        calls = [e for e in edges if e['kind'] == 'calls']
        assert any(c['target_symbol'] == 'method' for c in calls)

    def test_class_and_function_exports(self):
        source = 'class Foo:\n    pass\n\ndef bar():\n    pass'
        edges = extract_edges('test.py', source)
        exports = [e for e in edges if e['kind'] == 'exports']
        names = {e['target_symbol'] for e in exports}
        assert names == {'Foo', 'bar'}

    def test_class_inheritance(self):
        source = 'class Dog(Animal):\n    pass'
        edges = extract_edges('test.py', source)
        ext = [e for e in edges if e['kind'] == 'extends']
        assert len(ext) == 1
        assert ext[0]['target_symbol'] == 'Animal'


# --- JavaScript (shares profile with JSX) ---

class TestJavaScript:
    def test_function_call_extraction(self):
        source = 'const result = helper(); process();'
        edges = extract_edges('test.js', source)
        calls = [e for e in edges if e['kind'] == 'calls']
        names = {e['target_symbol'] for e in calls}
        assert 'helper' in names
        assert 'process' in names

    def test_function_export(self):
        source = 'export function myHelper() { return 1; }'
        edges = extract_edges('test.js', source)
        exports = [e for e in edges if e['kind'] == 'exports']
        assert any(e['target_symbol'] == 'myHelper' for e in exports)

    def test_jsx_uses_same_parser(self):
        source = 'export function App() { return 1; }'
        js_edges = extract_edges('test.js', source)
        jsx_edges = extract_edges('test.jsx', source)
        assert len(js_edges) == len(jsx_edges)


# --- Edge cases ---

class TestEdgeCases:
    def test_empty_source_returns_empty(self):
        assert extract_edges('test.ts', '') == []

    def test_syntax_error_returns_partial(self):
        # Tree-sitter is error-tolerant, should still parse valid parts
        source = 'import { x } from "./x";\nfunction {{{ broken'
        edges = extract_edges('test.ts', source)
        imports = [e for e in edges if e['kind'] == 'imports']
        assert len(imports) == 1

    def test_large_file_does_not_crash(self):
        source = '\n'.join(f'export function f{i}() {{ return {i}; }}' for i in range(200))
        edges = extract_edges('test.ts', source)
        assert len(edges) >= 200  # at least 200 exports
