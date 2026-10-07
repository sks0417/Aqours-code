"""Unrestricted single-agent controls and specification precedence, without models."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from aqours_code.taskgraph import build_index, load_graph, validate
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.context_pack import build_context_pack
from aqours_code.taskgraph.coordinator import RunOptions, run_graph
from aqours_code.taskgraph.prompting import build_node_prompt
from aqours_code.taskgraph.schema import EditSet
from aqours_code.taskgraph.single import SINGLE_INSTRUCTION, single_graph
from aqours_code.taskgraph.workers import CommandWorker
from taskgraph_support import make_graph, make_node

SPEC_RULE = ('If your goal and the specification disagree, follow the specification, and\n'
             'say in your final answer where they disagree.')
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('checks', [[], ['python -m pytest -q tests'], ['first check', 'second check']])
def test_single_command_only_uses_original_request_and_head(toy_repo, tmp_path, checks):
    request = tmp_path / 'request.md'
    text = '\nImplement the requested behaviour.\nFollow SPEC.md.\n'
    request.write_text(text, encoding='utf-8')
    out = tmp_path / 'output/graph.json'
    args = ['single', str(request), '--repo', str(toy_repo.path), '--out', str(out)]
    for command in checks:
        args += ['--final-check', command]
    assert main(args) == 0
    graph = load_graph(out)
    assert graph.base_commit == toy_repo.commit and graph.request == text
    assert graph.final_checks == checks and graph.generator.kind == 'manual'
    assert len(graph.nodes) == 1 and graph.edges == []
    node = graph.nodes[0]
    assert node.goal == text + '\n\n' + SINGLE_INSTRUCTION
    assert node.edit_set.any_file
    assert node.edit_set.modify == node.edit_set.create == []
    assert node.context_files == node.provides == node.requires == node.requires_impl == []
    assert node.check.commands == (checks or ['git diff --check'])
    assert 'single command' in graph.revision_log[0].reason
    report = validate(graph, build_index(toy_repo.path, toy_repo.commit))
    assert report.ok, report.format()


def test_any_file_defaults_false_and_is_strict_boolean():
    assert not EditSet(modify=[], create=[]).any_file
    with pytest.raises(ValidationError):
        EditSet(any_file='true', modify=[], create=[])


def test_any_file_rejected_in_multiple_node_graph(toy_index):
    first = make_node('A')
    first['edit_set']['any_file'] = True
    graph = make_graph([first, make_node('B', modify=('runner.py',))])
    report = validate(graph, toy_index)
    assert report.codes() == ['V13']
    assert 'single-node graph' in report.errors[0].message


def test_ordinary_empty_edit_set_still_rejected(toy_index):
    assert 'V7' in validate(make_graph([make_node('A')]), toy_index).codes()


@pytest.mark.parametrize('count', [1, 2])
def test_all_worker_prompts_prioritize_specification(toy_index, count):
    graph = make_graph([make_node(str(i), modify=('runner.py',)) for i in range(count)])
    for node in graph.nodes:
        prompt = build_node_prompt(graph, node, toy_index, 1, None)
        assert SPEC_RULE in prompt[prompt.index('# Rules'):]


def test_any_file_prompt_and_pack_contain_no_repository_hints(toy_repo, toy_index):
    fixture = toy_repo.path / 'tests/conftest.py'
    fixture.write_text('SECRET_FIXTURE = True\n')
    graph = single_graph('Implement the request.', toy_repo.path)
    node = graph.nodes[0]
    pack = build_context_pack(node, toy_repo.path)
    assert pack.text == '' and pack.own_files == pack.full_files == []
    for kwargs in [{'context_pack': pack}, {'workspace': toy_repo.path}, {}]:
        prompt = build_node_prompt(graph, node, toy_index, 1, None, **kwargs)
        assert 'You may modify or create any file in the repository.' in prompt
        for forbidden in ['# Context', 'Modify these existing files:', 'Create these new files:',
                          'Do not modify, create, or delete any other file.', '# Before you start',
                          'SECRET_FIXTURE', 'store.py', 'models.py']:
            assert forbidden not in prompt
        assert SPEC_RULE in prompt


def test_any_file_command_worker_merges_arbitrary_edits(toy_repo, tmp_path):
    check = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'chosen/new.py\').read_text() == \'NEW\'; assert Path(\'README.md\').read_text() == \'changed\'"'
    graph = single_graph('Implement the request.', toy_repo.path, [check])
    command = f'"{sys.executable}" -c "from pathlib import Path; Path(\'chosen\').mkdir(); Path(\'chosen/new.py\').write_text(\'NEW\'); Path(\'README.md\').write_text(\'changed\')"'
    result = run_graph(graph, toy_repo.path, CommandWorker({'solve': command}),
                       RunOptions(out_dir=tmp_path / 'runs', max_attempts=1))
    assert result.summary['status'] == 'success'
    record = result.summary['nodes']['solve']
    assert record['out_of_scope_files'] == []
    assert set(record['changed_files']) == {'README.md', 'chosen/new.py'}
    assert record['context_chars'] == record['soft_wall_blocked'] == 0
    assert not list((result.run_dir / 'nodes/solve').glob('soft_wall*'))
    prompt = (result.run_dir / 'nodes/solve/prompt_1.md').read_text()
    assert '# Context' not in prompt and '# Before you start' not in prompt


@pytest.mark.parametrize('task', ['job_platform', 'job_runner'])
@pytest.mark.parametrize('variant', ['modular', 'coupled'])
def test_committed_controls_match_single_command_exactly(tmp_path, task, variant):
    base = ROOT / 'experiments/taskgraph' / task
    repo = tmp_path / task / variant
    created = subprocess.run([sys.executable, str(base / 'make_repo.py'), variant, str(repo)],
                             text=True, capture_output=True)
    assert created.returncode == 0, created.stderr
    output = tmp_path / 'single.json'
    assert main(['single', str(base / 'request.md'), '--repo', str(repo), '--out', str(output),
                 '--final-check', 'python -m pytest -q tests']) == 0
    assert output.read_bytes() == (base / variant / 'graphs/single.json').read_bytes()
    graph = load_graph(output)
    report = validate(graph, build_index(repo, graph.base_commit))
    assert report.ok and not report.warnings, report.format()


def test_empty_request_returns_input_error(toy_repo, tmp_path):
    request = tmp_path / 'request.md'
    request.write_text(' \n')
    assert main(['single', str(request), '--repo', str(toy_repo.path),
                 '--out', str(tmp_path / 'graph.json')]) == 2
    assert not (tmp_path / 'graph.json').exists()
