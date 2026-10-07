"""Context packaging and trace accounting, entirely offline."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from aqours_code.taskgraph import build_index, validate
from aqours_code.taskgraph.context_pack import (
    MAX_PACK_BYTES, build_context_pack, interface_summary, markdown_headings, markdown_section,
)
from aqours_code.taskgraph.context_metrics import node_context_metrics, trace_metrics
from aqours_code.taskgraph.schema import Node
from taskgraph_support import commit_files, make_graph, make_node as node_dict


def make_node(*args, **kwargs):
    return Node.model_validate(node_dict(*args, **kwargs))


MODULE = '''"""Module documentation."""
from dataclasses import dataclass, field
MAX = 4
Alias = list[str] | None
LONG = (
    "not a simple one-line constant"
)

def plain(a: int = 3, /, *, flag: bool = True) -> str:
    """Function documentation."""
    return "SECRET_BODY"

@cache(maxsize=10)
async def decorated(x: str = "default") -> None:
    """Async documentation."""
    raise ValueError("SECRET_ASYNC")

@dataclass(frozen=True)
class Record(Base, metaclass=Meta):
    """Class documentation."""
    name: str
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_name(cls, name: str) -> "Record":
        """Method documentation."""
        return cls("SECRET_METHOD")
'''


def put(root, path, text):
    dest = root / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")


def test_interface_keeps_signatures_docs_fields_and_simple_constants():
    result, fallback = interface_summary(MODULE)
    assert not fallback
    assert "SECRET" not in result and "LONG" not in result
    for text in ['Module documentation.', 'MAX = 4', 'Alias = list[str] | None',
                 'a: int=3, /, *, flag: bool=True', '@cache(maxsize=10)',
                 'async def decorated', '@dataclass(frozen=True)',
                 'class Record(Base, metaclass=Meta)', 'name: str',
                 'tags: list[str] = field(default_factory=list)', '@classmethod',
                 'Function documentation.', 'Async documentation.',
                 'Class documentation.', 'Method documentation.']:
        assert text in result
    for node in ast.walk(ast.parse(result)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert ast.get_docstring(node)
            assert isinstance(node.body[-1], ast.Expr)
            assert node.body[-1].value.value is Ellipsis
            assert len(node.body) == 2


def test_parse_failure_returns_full_source():
    source = 'def broken(:\n    return "keep everything"\n'
    assert interface_summary(source) == (source, True)


MARKDOWN = '''# Spec
intro
## Feature
feature content
### Details
detail content
#### Deep
deep content
```md
## Not a heading
```
## Other
other content
# End
end content
'''


def test_nested_sections_and_fences():
    section = markdown_section(MARKDOWN, 'Feature')
    assert section.startswith('## Feature\n')
    assert 'deep content' in section and '## Not a heading' in section
    assert 'other content' not in section and '# End' not in section
    assert markdown_section(MARKDOWN, 'Details').endswith('```\n')
    assert 'Not a heading' not in [h.title for h in markdown_headings(MARKDOWN)]
    with pytest.raises(ValueError, match='does not exist'):
        markdown_section(MARKDOWN, 'feature')
    assert markdown_section('Title\n=====\ntext\nNext\n====\nend', 'Title') == 'Title\n=====\ntext\n'
    assert markdown_section('## Title ###\nx\n', 'Title') == '## Title ###\nx\n'


def test_pack_priorities_requirements_multiple_sections_and_fixture(tmp_path):
    put(tmp_path, 'own.py', 'def own():\n    return "OWN_BODY"\n')
    put(tmp_path, 'created.py', 'STUB = "upstream"\n')
    put(tmp_path, 'other.py', MODULE)
    put(tmp_path, 'impl.py', 'def run():\n    return "IMPL_BODY"\n')
    put(tmp_path, 'SPEC.md', MARKDOWN)
    put(tmp_path, 'tests/conftest.py', 'def fixture():\n    return "FIXTURE_BODY"\n')
    node = make_node('P', modify=('own.py',), create=('created.py', 'new.py'),
                     context_files=('own.py', 'other.py', 'SPEC.md#Feature', 'SPEC.md#Other',
                                    'tests/conftest.py'),
                     requires=('other.py::plain',), requires_impl=('impl.py::run',))
    pack = build_context_pack(node, tmp_path)
    assert 'OWN_BODY' in pack.text and 'STUB = "upstream"' in pack.text
    assert 'SECRET' not in pack.text and 'IMPL_BODY' not in pack.text
    assert 'FIXTURE_BODY' in pack.text
    assert 'feature content' in pack.text and 'other content' in pack.text
    assert 'end content' not in pack.text
    assert pack.text.count('## other.py (interface)') == 1
    assert pack.text.index('own.py') < pack.text.index('other.py') < pack.text.index('SPEC.md') < pack.text.index('tests/conftest.py')
    assert set(pack.full_files) == {'own.py', 'created.py', 'tests/conftest.py'}
    assert not pack.missing
    assert pack.report()['chars'] == len(pack.text)


def test_fallback_report_and_full_markdown(tmp_path):
    put(tmp_path, 'bad.py', 'def broken(:\n FULL_FALLBACK')
    put(tmp_path, 'README.md', MARKDOWN)
    node = make_node('P', create=('new.py',), context_files=('bad.py', 'README.md'))
    pack = build_context_pack(node, tmp_path)
    assert pack.parse_fallbacks == ['bad.py']
    assert pack.full_files == ['bad.py', 'README.md']
    assert MARKDOWN in pack.text


def test_limit_keeps_own_files_then_truncates_in_order(tmp_path):
    put(tmp_path, 'own.py', 'x = "' + '自' * 20 + '"\n')
    put(tmp_path, 'other.py', '"""' + '文' * 500 + '"""\n')
    put(tmp_path, 'SPEC.md', MARKDOWN)
    put(tmp_path, 'tests/conftest.py', 'FIXTURE = True\n')
    node = make_node('P', modify=('own.py',), context_files=('other.py', 'SPEC.md'))
    pack = build_context_pack(node, tmp_path, max_bytes=350)
    assert len(pack.text.encode('utf-8')) <= 350
    assert (tmp_path / 'own.py').read_text() in pack.text
    assert '[truncated' in pack.text
    assert pack.truncated == ['other.py', 'SPEC.md', 'tests/conftest.py']
    assert pack.full_files == ['own.py']
    put(tmp_path, 'own.py', 'x' * (MAX_PACK_BYTES + 10))
    pack = build_context_pack(node, tmp_path)
    assert 'x' * (MAX_PACK_BYTES + 10) in pack.text
    assert pack.report()['own_files_exceed_limit']
    assert pack.truncated == ['other.py', 'SPEC.md', 'tests/conftest.py']


def test_truncated_fixture_is_not_a_full_file(tmp_path):
    put(tmp_path, 'tests/conftest.py', '#' * 500)
    pack = build_context_pack(make_node('P', create=('new.py',)), tmp_path, max_bytes=200)
    assert pack.full_files == []
    assert pack.truncated == ['tests/conftest.py']


def test_pack_rejects_symlink_escape(tmp_path):
    outside = tmp_path / 'outside.py'
    outside.write_text('secret')
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    (workspace / 'other.py').symlink_to(outside)
    with pytest.raises(ValueError, match='escapes workspace'):
        build_context_pack(make_node('P', create=('own.py',), context_files=('other.py',)), workspace)


@pytest.mark.parametrize('ref', ['../SPEC.md#Feature', '/SPEC.md#Feature', 'a.py#Feature',
                                'SPEC.md#', 'SPEC.md# Feature'])
def test_invalid_context_reference(ref):
    with pytest.raises(ValidationError):
        make_node('P', modify=('runner.py',), context_files=(ref,))


def test_heading_validation_uses_indexed_commit(toy_repo):
    commit = commit_files(toy_repo.path, {'SPEC.md': MARKDOWN}, 'spec')
    index = build_index(toy_repo.path, commit)
    # Dirty working tree must not change base-commit validation.
    (toy_repo.path / 'SPEC.md').write_text('# Wrong\n')
    valid = make_graph([make_node('P', modify=('runner.py',),
                                 context_files=('SPEC.md#Feature', 'SPEC.md#Other'))])
    assert validate(valid, index).ok
    invalid = make_graph([make_node('P', modify=('runner.py',),
                                   context_files=('SPEC.md#Missing',))])
    report = validate(invalid, index)
    assert report.codes() == ['V3']
    assert 'SPEC.md#Missing' in report.errors[0].message


def event(tool, path):
    return {'type': 'tool_use', 'tool': tool, 'input': {'path': path}}


def write_trace(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    completed = []
    for number, event in enumerate(events):
        if event.get('type') == 'tool_use':
            event = {**event, 'tool_use_id': f'call-{number}'}
        completed.append(event)
        if event.get('type') == 'tool_use':
            completed.append({'type': 'tool_result', 'tool': event['tool'],
                              'tool_use_id': event['tool_use_id'], 'content': 'fixture content'})
    path.write_text('\n'.join(json.dumps(e) for e in completed) + '\n{partial', encoding='utf-8')


def test_trace_counts_repeated_reads_and_first_write(tmp_path):
    trace = tmp_path / 'trace.jsonl'
    write_trace(trace, [
        {'type': 'llm_request'}, event('read_file', './own.py'),
        event('read_file', '/workspace/tests/conftest.py'),
        event('read_file', '/tmp/wt/other.py'),
        {'type': 'tool_result', 'tool': 'read_file'},
        {'type': 'llm_request'}, event('read_file', 'SPEC.md'),
        event('edit_file', 'own.py'), {'type': 'llm_request'},
        event('read_file', 'other.py'), event('read_file', 'bad.py'),
    ])
    result = trace_metrics([trace], {'tests/conftest.py', 'bad.py'}, {'own.py'}, workspace='/tmp/wt')
    assert result == {'reads_outside_pack': 3, 'calls_before_first_write': 2,
                      'first_write_seen': True}


def test_trace_no_write_retries_and_timeout_fallback(tmp_path):
    metadata = [{'attempt': n, 'full_files': ['README.md'], 'own_files': ['own.py'],
                 'workspace': '/tmp/wt'} for n in [1, 2, 3]]
    write_trace(tmp_path / 'trace_1.jsonl', [{'type': 'llm_request'}, event('read_file', 'x.py')])
    write_trace(tmp_path / 'aqours_2/trace/run/trace.jsonl', [
        {'type': 'llm_request'}, event('write_file', 'own.py'), {'type': 'llm_request'}])
    write_trace(tmp_path / 'trace_3.jsonl', [{'type': 'llm_request'}, event('read_file', 'README.md')])
    result = node_context_metrics(tmp_path, metadata)
    assert result == {'reads_outside_pack': 1, 'calls_before_first_write': 2,
                      'soft_wall_blocked': 0, 'confirmed_reads': []}
    assert trace_metrics([], set(), set())['calls_before_first_write'] == 0


def test_coordinator_packages_merged_worktree_refreshes_retry_and_records_metrics(toy_repo, tmp_path):
    import sys
    from aqours_code.taskgraph.coordinator import RunOptions, run_graph
    from aqours_code.taskgraph.workers import WorkerResult
    from taskgraph_support import make_edge

    check = (f'"{sys.executable}" -c "pass"',)
    graph = make_graph([
        make_node('A', kind='contract', create=('shared.py',), provides=('shared.py::shared',), commands=check),
        make_node('B', modify=('runner.py',), requires=('shared.py::shared',), commands=check),
    ], [make_edge('A', 'B', 'interface')], final_checks=check)
    graph.base_commit = toy_repo.commit

    class Worker:
        def describe(self):
            return {'worker': 'offline-context-test'}

        def run(self, request):
            raw = (request.log_dir / f'context_{request.attempt}.md').read_text()
            assert raw in request.prompt and '# Context' in request.prompt
            assert '# Read these files first' not in request.prompt
            assert 'Treat it as already read.' in request.prompt
            if request.node_id == 'A':
                put(request.workspace, 'shared.py', 'def shared():\n    """MERGED_DOC"""\n    return "HIDDEN_BODY"\n')
                return WorkerResult(ok=True)
            assert 'MERGED_DOC' in raw and 'HIDDEN_BODY' not in raw
            assert 'count += 1' in raw  # own runner.py is full source
            target = request.workspace / 'runner.py'
            if request.attempt == 2:
                assert '# FIRST_ATTEMPT' in raw
            target.write_text(target.read_text() + f'\n# {"FIRST_ATTEMPT" if request.attempt == 1 else "DONE"}\n')
            write_trace(request.log_dir / f'trace_{request.attempt}.jsonl', [
                {'type': 'llm_request'}, event('read_file', 'shared.py'),
                event('read_file', 'runner.py'), event('edit_file', 'runner.py'),
                {'type': 'llm_request'},
            ])
            return WorkerResult(ok=request.attempt == 2, reason='worker_error' if request.attempt == 1 else '')

    result = run_graph(graph, toy_repo.path, Worker(), RunOptions(out_dir=tmp_path / 'runs'))
    assert result.summary['status'] == 'success'
    record = result.summary['nodes']['B']
    assert record['attempts'] == 2
    assert record['reads_outside_pack'] == 2
    assert record['calls_before_first_write'] == 1
    packs = [(result.run_dir / 'nodes/B' / f'context_{n}.md').read_text() for n in [1, 2]]
    assert record['context_chars'] == sum(map(len, packs))
    assert [p['chars'] for p in record['context_packs']] == list(map(len, packs))
    saved = json.loads((result.run_dir / 'summary.json').read_text())
    assert saved['totals']['reads_outside_pack'] == 2
    assert saved['totals']['calls_before_first_write'] == 1


def test_modular_size_sample_has_complete_own_stub_and_no_other_feature_source(tmp_path):
    from context_pack_fixture import create_sample
    report = create_sample(tmp_path / 'sample')
    pack = (tmp_path / 'sample/nodes/P/context_1.md').read_text(encoding='utf-8')
    own = (tmp_path / 'sample/repo/jobrunner/priority.py').read_text(encoding='utf-8')
    assert own in pack
    assert 'jobrunner/recurring.py (own file' not in pack
    assert report['bytes'] < report['baseline_bytes'] // 2
    assert report['chars'] == len(pack)
    assert not report['truncated'] and not report['parse_fallbacks'] and not report['missing']
