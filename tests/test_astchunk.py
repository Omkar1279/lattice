import pytest
from lattice.indexer.graph import init_tree_sitter, get_parser
from lattice.indexer.indexer import _ast_chunk, chunk_file, make_chunk_id, ChunkData, TARGET_CHUNK_CHARS


@pytest.fixture(autouse=True)
def init_parser():
    init_tree_sitter()


# --- make_chunk_id ---

class TestMakeChunkId:
    def test_deterministic(self):
        assert make_chunk_id('/a/b.ts', 10) == make_chunk_id('/a/b.ts', 10)

    def test_different_for_different_lines(self):
        assert make_chunk_id('/a/b.ts', 0) != make_chunk_id('/a/b.ts', 1)

    def test_different_for_different_files(self):
        assert make_chunk_id('/a.ts', 0) != make_chunk_id('/b.ts', 0)

    def test_returns_16_char_hex(self):
        result = make_chunk_id('file.ts', 42)
        assert len(result) == 16
        assert all(c in '0123456789abcdef' for c in result)


# --- _ast_chunk (low-level) ---

class TestAstChunk:
    def test_merges_small_siblings_into_one_chunk(self):
        parser = get_parser('.js')
        if not parser:
            pytest.skip('tree-sitter JS not available')

        source = 'function a() { return 1; }\nfunction b() { return 2; }\nfunction c() { return 3; }'
        tree = parser.parse(source.encode('utf8'))
        chunks = _ast_chunk(tree, source)

        assert len(chunks) == 1
        assert chunks[0]['body'].count('function') == 3

    def test_splits_when_exceeding_budget(self):
        parser = get_parser('.js')
        if not parser:
            pytest.skip('tree-sitter JS not available')

        # Generate enough functions to exceed TARGET_CHUNK_CHARS
        funcs = [f'function f{i}() {{ return "{"x" * 200}"; }}' for i in range(20)]
        source = '\n'.join(funcs)
        tree = parser.parse(source.encode('utf8'))
        chunks = _ast_chunk(tree, source)

        assert len(chunks) > 1
        for c in chunks:
            # Each chunk body should be <= budget (except oversized single nodes)
            assert len(c['body']) <= TARGET_CHUNK_CHARS * 2

    def test_empty_source_returns_single_chunk(self):
        parser = get_parser('.js')
        if not parser:
            pytest.skip('tree-sitter JS not available')

        tree = parser.parse(b'')
        chunks = _ast_chunk(tree, '')

        assert len(chunks) == 1
        assert chunks[0]['body'] == ''
        assert chunks[0]['start_line'] == 0

    def test_preserves_balanced_braces(self):
        parser = get_parser('.js')
        if not parser:
            pytest.skip('tree-sitter JS not available')

        source = 'function a() { if (true) { return 1; } }\nfunction b() { return 2; }'
        tree = parser.parse(source.encode('utf8'))
        chunks = _ast_chunk(tree, source)

        for c in chunks:
            assert c['body'].count('{') == c['body'].count('}')

    def test_oversized_single_node_gets_own_chunk(self):
        parser = get_parser('.js')
        if not parser:
            pytest.skip('tree-sitter JS not available')

        big_body = 'x'.join(['a'] * (TARGET_CHUNK_CHARS + 500))
        source = f'function big() {{ return "{big_body}"; }}\nfunction small() {{ return 1; }}'
        tree = parser.parse(source.encode('utf8'))
        chunks = _ast_chunk(tree, source)

        assert len(chunks) == 2


# --- chunk_file (high-level pure function) ---

class TestChunkFile:
    def test_returns_chunk_data_objects(self):
        source = 'function hello() { return "world"; }'
        result = chunk_file('/project/hello.js', source)

        assert len(result) >= 1
        assert all(isinstance(c, ChunkData) for c in result)

    def test_single_chunk_heading_is_filename(self):
        source = 'const x = 1;'
        result = chunk_file('/project/config.js', source)

        assert len(result) == 1
        assert result[0].heading == 'config.js'

    def test_multi_chunk_heading_includes_line_range(self):
        funcs = [f'function f{i}() {{ return "{"x" * 300}"; }}' for i in range(15)]
        source = '\n'.join(funcs)
        result = chunk_file('/project/big.js', source)

        assert len(result) > 1
        assert ':' in result[0].heading  # e.g. "big.js:1-5"

    def test_unsupported_extension_falls_back_to_full_source(self):
        source = 'some random content\nline 2\nline 3'
        result = chunk_file('/project/data.txt', source)

        assert len(result) == 1
        assert result[0].body == source

    def test_chunk_ids_are_unique(self):
        funcs = [f'function f{i}() {{ return "{"x" * 300}"; }}' for i in range(15)]
        source = '\n'.join(funcs)
        result = chunk_file('/project/big.js', source)

        ids = [c.id for c in result]
        assert len(ids) == len(set(ids))

    def test_python_file_chunking(self):
        source = 'class Foo:\n    def bar(self):\n        pass\n\ndef baz():\n    return 1'
        result = chunk_file('/project/module.py', source)

        assert len(result) >= 1
        combined = ''.join(c.body for c in result)
        assert 'class Foo' in combined
        assert 'def baz' in combined
