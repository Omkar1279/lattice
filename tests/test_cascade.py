import pytest
from lattice.retrieval.cascade import (
    CascadePipelineFactory,
    ReciprocalRankFuser,
    BudgetPacker,
    extract_identifiers,
    is_identifier,
    SOURCE_FILTERS,
)
from lattice.retrieval.freshness import ExponentialFreshnessScorer
from lattice.core.interfaces import Chunk
from helpers import create_test_vault
from datetime import datetime
import time


def mk(id: str, source: str = 'code_index', **kwargs) -> Chunk:
    now = datetime.now().isoformat()
    defaults = dict(
        id=id, heading=id, body=id, source=source,
        path=f'/x/{id}.ts', tags=[], created_at=now,
        last_seen_at=now, last_validated_at=now,
        supersedes=None, superseded_by=None, pinned=False,
    )
    defaults.update(kwargs)
    return Chunk(**defaults)


# --- is_identifier ---

class TestIsIdentifier:
    def test_camel_case(self):
        assert is_identifier('parseJSON') is True
        assert is_identifier('OAuth2PasswordBearer') is True

    def test_snake_case(self):
        assert is_identifier('solve_dependencies') is True

    def test_dotted(self):
        assert is_identifier('foo.bar') is True

    def test_plain_word_rejected(self):
        assert is_identifier('hello') is False
        assert is_identifier('cache') is False

    def test_short_rejected(self):
        assert is_identifier('ab') is False

    def test_starts_with_number_rejected(self):
        assert is_identifier('123abc') is False


# --- extract_identifiers ---

class TestExtractIdentifiers:
    def test_whole_query_is_identifier(self):
        assert extract_identifiers('HTTPException') == ['HTTPException']
        assert extract_identifiers('solve_dependencies') == ['solve_dependencies']
        assert extract_identifiers('foo.bar') == ['foo.bar']

    def test_extracts_from_natural_language(self):
        assert extract_identifiers('where is OAuth2PasswordBearer defined?') == ['OAuth2PasswordBearer']
        assert extract_identifiers('where is solve_dependencies defined?') == ['solve_dependencies']

    def test_ignores_plain_english(self):
        assert extract_identifiers('where is auth') == []
        assert extract_identifiers('cache user') == []
        assert extract_identifiers('how does it work?') == []

    def test_empty_string(self):
        assert extract_identifiers('') == []
        assert extract_identifiers('   ') == []

    def test_multiple_identifiers_in_query(self):
        result = extract_identifiers('Compare parseJSON vs formatOutput')
        assert 'parseJSON' in result
        assert 'formatOutput' in result

    def test_deduplicates(self):
        result = extract_identifiers('parseJSON parseJSON parseJSON')
        assert result == ['parseJSON']


# --- SOURCE_FILTERS ---

class TestSourceFilters:
    def test_code_filter(self):
        assert SOURCE_FILTERS['code'] == ['code_index']

    def test_notes_filter(self):
        assert SOURCE_FILTERS['notes'] == ['human_note', 'auto_capture']

    def test_auto_is_none(self):
        assert SOURCE_FILTERS['auto'] is None


# --- ReciprocalRankFuser ---

class TestReciprocalRankFuser:
    def setup_method(self):
        self._fuser = ReciprocalRankFuser(k=60)

    def test_cross_list_agreement_boosts_rank(self):
        a, b, c = mk('a'), mk('b'), mk('c')
        fused = self._fuser.fuse([[a, b], [a, c], [b]], float('-inf'), None, time.time() * 1000)
        ids = [f.id for f in fused]
        assert ids[0] == 'a'  # appears in all 3 lists
        assert ids.index('b') < ids.index('c')  # b in 2 lists, c in 1

    def test_drops_superseded_chunks(self):
        live = mk('live')
        dead = mk('dead', superseded_by='live')
        fused = self._fuser.fuse([[dead, live]], float('-inf'), None, time.time() * 1000)
        assert [f.id for f in fused] == ['live']

    def test_applies_source_filter(self):
        code_chunk = mk('c1', source='code_index')
        note_chunk = mk('n1', source='human_note')
        fused = self._fuser.fuse(
            [[code_chunk, note_chunk]], float('-inf'),
            source_filter=['code_index'], now_ts=time.time() * 1000,
        )
        ids = [f.id for f in fused]
        assert 'c1' in ids
        assert 'n1' not in ids

    def test_applies_since_filter(self):
        now_ts = time.time() * 1000
        old_ts = '2020-01-01T00:00:00'
        old_chunk = mk('old', last_seen_at=old_ts)
        new_chunk = mk('new')

        # since_ts set to 2023 — should exclude 2020 chunk
        since_ts = datetime(2023, 1, 1).timestamp() * 1000
        fused = self._fuser.fuse([[old_chunk, new_chunk]], since_ts, None, now_ts)
        ids = [f.id for f in fused]
        assert 'old' not in ids
        assert 'new' in ids

    def test_empty_lists_returns_empty(self):
        fused = self._fuser.fuse([[], []], float('-inf'), None, time.time() * 1000)
        assert fused == []

    def test_single_item(self):
        a = mk('only')
        fused = self._fuser.fuse([[a]], float('-inf'), None, time.time() * 1000)
        assert len(fused) == 1
        assert fused[0].id == 'only'


# --- BudgetPacker ---

class TestBudgetPacker:
    def test_packs_within_budget(self):
        scorer = ExponentialFreshnessScorer()
        packer = BudgetPacker(budget_tokens=100, scorer=scorer)
        chunks = [mk(f'c{i}', body='short body') for i in range(10)]
        results = packer.pack(chunks, time.time() * 1000)
        total = sum(r.tokens for r in results)
        assert total <= 100

    def test_stops_at_budget_limit(self):
        scorer = ExponentialFreshnessScorer()
        packer = BudgetPacker(budget_tokens=50, scorer=scorer)
        chunks = [mk(f'c{i}', body='x' * 500) for i in range(10)]
        results = packer.pack(chunks, time.time() * 1000)
        # With tiny budget and large bodies, should get very few
        assert len(results) < 10

    def test_empty_input(self):
        scorer = ExponentialFreshnessScorer()
        packer = BudgetPacker(budget_tokens=1000, scorer=scorer)
        assert packer.pack([], time.time() * 1000) == []

    def test_result_has_freshness_score(self):
        scorer = ExponentialFreshnessScorer()
        packer = BudgetPacker(budget_tokens=1000, scorer=scorer)
        chunks = [mk('c1', body='hello world', source='human_note')]
        results = packer.pack(chunks, time.time() * 1000)
        assert results[0].freshness > 0


# --- Integration: CascadePipelineFactory ---

@pytest.fixture
def vault():
    v = create_test_vault()
    yield v
    v.close()


class TestCascadePipeline:
    def test_returns_results_for_matching_content(self, vault):
        vault.insert_chunk(heading='auth handler', body='JWT token validation logic', source='code_index')
        pipeline = CascadePipelineFactory.create(vault.db)
        results = pipeline.retrieve(query='JWT token', budget_tokens=2500, kind='all')
        assert len(results) >= 1

    def test_respects_kind_filter(self, vault):
        vault.insert_chunk(id='code1', heading='service.ts', body='auth service', source='code_index')
        vault.insert_chunk(id='note1', heading='auth note', body='auth decision', source='human_note')

        pipeline = CascadePipelineFactory.create(vault.db)

        code_results = pipeline.retrieve(query='auth', budget_tokens=2500, kind='code')
        note_results = pipeline.retrieve(query='auth', budget_tokens=2500, kind='notes')

        code_ids = [r.chunk.id for r in code_results]
        note_ids = [r.chunk.id for r in note_results]

        assert 'note1' not in code_ids
        assert 'code1' not in note_ids

    def test_empty_vault_returns_empty(self, vault):
        pipeline = CascadePipelineFactory.create(vault.db)
        results = pipeline.retrieve(query='anything', budget_tokens=2500, kind='all')
        assert results == []
