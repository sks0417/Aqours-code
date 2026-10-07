"""Soft-wall decisions, public-hook lifetime, and a real offline worker process."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from aqours_code.taskgraph.context_metrics import trace_metrics
from aqours_code.taskgraph.prompting import (
    BEFORE_START, CONTEXT_INSPECTION, SINGLE_NODE_CONTEXT, AttemptFailure, build_node_prompt,
)
from aqours_code.taskgraph.soft_wall import SoftWall, bash_read_paths, read_wall_log
from taskgraph_support import commit_files, make_edge, make_graph, make_node


def block(tool, ident='call', **args):
    return NS(name=tool, id=ident, input=args)


@pytest.fixture
def wall(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'jobrunner').mkdir(parents=True)
    for file in ['own.py', 'full.py', 'jobrunner/store.py', 'jobrunner/other.py']:
        (repo / file).write_text('contents')
    return SoftWall(repo, ['own.py'], ['full.py'], tmp_path / 'soft_wall_1.jsonl')


def test_allow_own_full_nonexistent_glob_and_canonical_second_read(wall):
    for path in ['own.py', './full.py', 'new.py']:
        assert wall.decide('read_file', {'path': path}) is None
    assert wall.decide('glob', {'pattern': '**/*.py'}) is None
    assert wall.decide('read_file', {'path': 'jobrunner/store.py'}).decision == 'blocked'
    decision = wall.decide('read_file', {'path': '/workspace/jobrunner/store.py'})
    assert decision.decision == 'allowed' and decision.paths == ('jobrunner/store.py',)
    assert wall.decide('read_file', {'path': str(wall.workspace / 'jobrunner/store.py')}).decision == 'allowed'


@pytest.mark.parametrize('command', [
    'cat jobrunner/store.py', 'grep -n x jobrunner/*.py', r'type jobrunner\store.py',
    'head -n 3 jobrunner/store.py', 'tail jobrunner/store.py', 'less jobrunner/store.py',
    'more jobrunner/store.py', "sed -n '1,4p' jobrunner/store.py", "awk '{print}' jobrunner/store.py",
    'nl jobrunner/store.py', r'findstr x jobrunner\store.py',
    'python -c "print(open(\'jobrunner/store.py\').read())"',
    'ls jobrunner && cat jobrunner/store.py',
])
def test_read_commands_block_once_then_confirm_exact_command(wall, command):
    assert 'jobrunner/store.py' in bash_read_paths(command, wall.workspace)
    assert wall.decide('bash', {'command': command}).decision == 'blocked'
    assert wall.decide('bash', {'command': command}).decision == 'allowed'
    assert wall.decide('bash', {'command': command + ' '}).decision == 'blocked'


@pytest.mark.parametrize('command', [
    'python -m pytest -q tests', 'pytest tests', 'ls jobrunner', 'dir jobrunner',
    'cat own.py full.py', 'echo hello > own.py', 'cat > own.py',
    "sed -i 's/a/b/' own.py", "sed -i 's/a/b/' jobrunner/store.py",
    'python -c "from pathlib import Path; Path(\'own.py\').write_text(\'x\')"',
    'python -c "open(\'jobrunner/store.py\', \'w\').write(\'x\')"',
    'python -c "print(42)"', 'cat missing.py', 'cd jobrunner && cat store.py',
    "sed 's/a/b/w own.py' jobrunner/store.py",
    'python -c "open(\'own.py\', \'w\').write(\'x\')" && cat jobrunner/store.py',
    "sed -n 'w own.py' jobrunner/store.py", 'touch own.py && cat jobrunner/store.py',
    'python -c "import shutil; print(open(\'jobrunner/store.py\').read()); shutil.copy(\'own.py\', \'copy.py\')"',
])
def test_writes_tests_listings_and_ambiguous_commands_pass(wall, command):
    assert wall.decide('bash', {'command': command}) is None


def test_written_and_shell_created_files_are_allowed(wall):
    from aqours_code.hooks import recoverable_tool_rejection
    edit = block('edit_file', path='jobrunner/store.py')
    wall.post_tool(edit, 'OK')
    assert wall.decide('read_file', {'path': 'jobrunner/store.py'}) is None
    failed = block('write_file', path='jobrunner/other.py')
    wall.post_tool(failed, 'Error: refused')
    assert wall.decide('read_file', {'path': 'jobrunner/other.py'}).decision == 'blocked'
    shell = block('bash', command='echo content > generated.py')
    assert wall.pre_tool(shell, recoverable_tool_rejection) is None
    (wall.workspace / 'generated.py').write_text('created')
    wall.post_tool(shell, '(no output)')
    assert wall.decide('read_file', {'path': 'generated.py'}) is None


def test_log_counts_only_successful_confirmations_and_deactivation(wall):
    from aqours_code.hooks import recoverable_tool_rejection
    first = block('read_file', 'r1', path='jobrunner/store.py')
    rejection = wall.pre_tool(first, recoverable_tool_rejection)
    assert rejection['recoverable'] and rejection['kind'] == 'tool_policy_rejection'
    assert 'call read_file again' in rejection['message']
    second = block('read_file', 'r2', path='./jobrunner/store.py')
    assert wall.pre_tool(second, recoverable_tool_rejection) is None
    wall.post_tool(second, 'Error: unreadable')
    third = block('read_file', 'r3', path='jobrunner/store.py')
    assert wall.pre_tool(third, recoverable_tool_rejection) is None
    wall.post_tool(third, 'actual contents')
    entries = read_wall_log(wall.log_path)
    assert [e['decision'] for e in entries] == ['blocked', 'allowed']
    assert [e['tool_use_id'] for e in entries] == ['r1', 'r3']
    assert all(e['time'] and e['paths'] == ['jobrunner/store.py'] for e in entries)
    with wall.log_path.open('ab') as stream:
        stream.write(b'{\"partial\": \"\xe4')
    assert read_wall_log(wall.log_path) == entries
    wall.active = False
    assert wall.pre_tool(block('read_file', path='jobrunner/other.py'), recoverable_tool_rejection) is None


def test_shell_confirmation_uses_executor_success(wall):
    from aqours_code.hooks import recoverable_tool_rejection
    from aqours_code.taskgraph.worker_entry import ObservedExecutor
    from aqours_code.command_executor import LocalCommandExecutor
    command = 'cat jobrunner/store.py'
    assert wall.pre_tool(block('bash', 'b1', command=command), recoverable_tool_rejection)
    second = block('bash', 'b2', command=command)
    assert wall.pre_tool(second, recoverable_tool_rejection) is None
    executor = ObservedExecutor(LocalCommandExecutor(), wall)
    result = executor.execute(command, wall.workspace, 5)
    wall.post_tool(second, result['stdout'])
    assert [e['decision'] for e in read_wall_log(wall.log_path)] == ['blocked', 'allowed']
    third = block('bash', 'b3', command=command)
    wall.pre_tool(third, recoverable_tool_rejection)
    (wall.workspace / 'jobrunner/store.py').unlink()
    failed = executor.execute(command, wall.workspace, 5)
    wall.post_tool(third, failed['stderr'])
    assert len(read_wall_log(wall.log_path)) == 2


def test_single_node_prompt_keeps_old_intro_and_no_tail(toy_index):
    graph = make_graph([make_node('P', modify=('runner.py',))])
    prompt = build_node_prompt(graph, graph.nodes[0], toy_index, 1, None)
    assert SINGLE_NODE_CONTEXT in prompt
    assert CONTEXT_INSPECTION not in prompt and BEFORE_START not in prompt


def test_multinode_prompt_reminder_is_after_retry(toy_index):
    graph = make_graph([make_node('A', create=('a.py',)), make_node('P', modify=('runner.py',))])
    prompt = build_node_prompt(graph, graph.nodes[1], toy_index, 2, AttemptFailure('failure', 'retry detail'))
    assert CONTEXT_INSPECTION in prompt and SINGLE_NODE_CONTEXT not in prompt
    assert prompt.endswith(BEFORE_START)
    assert prompt.index('retry detail') < prompt.index('# Before you start')


def test_single_node_registers_no_wall(monkeypatch, toy_repo, tmp_path):
    from aqours_code.taskgraph.worker_entry import run_worker
    from aqours_code import hooks
    from test_workers import FakeClient, final, worker_config
    registrations = []
    monkeypatch.setattr(hooks, 'register_hook', lambda *args: registrations.append(args))
    config = worker_config(toy_repo.path, tmp_path / 'single')
    config['soft_wall'] = {'enabled': False}
    assert run_worker(config, model_client=FakeClient([final('done')])).ok
    assert registrations == []
    assert not list((tmp_path / 'single').glob('soft_wall*'))


def test_importing_worker_and_planner_does_not_register_runtime_hooks():
    script = '''import sys
import aqours_code.taskgraph.worker_entry
import aqours_code.taskgraph.planner
assert 'aqours_code.hooks' not in sys.modules
assert 'aqours_code.agent_loop' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_real_worker_process_soft_wall_and_summary(toy_repo, tmp_path):
    from aqours_code.taskgraph.coordinator import RunOptions, run_graph
    from aqours_code.taskgraph.workers import AqoursWorker
    source = (toy_repo.path / 'store.py').read_text() + '\n# CONFIRMED_SOURCE_TOKEN\n'
    commit = commit_files(toy_repo.path, {'store.py': source}, 'confirmation fixture')
    check = (f'"{sys.executable}" -c "pass"',)
    graph = make_graph([
        make_node('A', create=('a.txt',), commands=check),
        make_node('P', modify=('runner.py',), context_files=('store.py',), commands=check),
    ], [make_edge('A', 'P')], final_checks=check)
    graph.base_commit = commit
    entry = Path(__file__).with_name('soft_wall_worker.py')
    worker = AqoursWorker(entry_command=[sys.executable, str(entry)])
    result = run_graph(graph, toy_repo.path, worker, RunOptions(out_dir=tmp_path / 'runs', max_attempts=1))
    summary = result.summary
    assert summary['status'] == 'success', summary
    record = summary['nodes']['P']
    assert record['soft_wall_blocked'] == 1
    assert record['confirmed_reads'] == ['store.py']
    assert record['reads_outside_pack'] == 1
    assert summary['totals']['soft_wall_blocked'] == 1
    assert summary['totals']['confirmed_reads'] == 1
    events = read_wall_log(result.run_dir / 'nodes/P/soft_wall_1.jsonl')
    assert [e['decision'] for e in events] == ['blocked', 'allowed']
    assert [e['tool_use_id'] for e in events] == ['first-read', 'second-read']
    assert all(e['paths'] == ['store.py'] for e in events)
    assert 'count = 42' in (result.run_dir / 'repo/runner.py').read_text()
    config = json.loads((result.run_dir / 'nodes/P/worker_1_config.json').read_text())
    assert config['soft_wall']['enabled'] and config['soft_wall']['own_files'] == ['runner.py']


def test_trace_counts_only_completed_successful_reads(tmp_path):
    records = []
    for ident, output in [('blocked', 'Tool not run: stop'), ('failed', 'Error: missing'),
                          ('ok', 'source'), ('unfinished', None)]:
        records.append({'type': 'tool_use', 'tool_use_id': ident, 'tool': 'read_file',
                        'input': {'path': 'other.py'}})
        if output is not None:
            records.append({'type': 'tool_result', 'tool_use_id': ident, 'tool': 'read_file', 'content': output})
    trace = tmp_path / 'trace.jsonl'
    trace.write_text('\n'.join(json.dumps(e) for e in records))
    assert trace_metrics([trace], set(), set())['reads_outside_pack'] == 1


def test_callbacks_survive_run_agent_task_and_are_inactive_afterwards(toy_repo, tmp_path, monkeypatch):
    from aqours_code import hooks
    from aqours_code.taskgraph import worker_entry
    from test_workers import FakeClient, final, tool_use, worker_config
    config = worker_config(toy_repo.path, tmp_path / 'lifetime')
    config['soft_wall'] = {'enabled': True, 'own_files': ['runner.py'], 'full_files': [],
                           'log_path': str(tmp_path / 'lifetime/soft_wall_1.jsonl')}
    registered = []
    original = hooks.register_hook

    def register(event, callback):
        registered.append((event, callback))
        original(event, callback)

    monkeypatch.setattr(hooks, 'register_hook', register)
    client = FakeClient([tool_use('read_file', call_id='r1', path='store.py'), final('done')])
    assert worker_entry.run_worker(config, model_client=client).ok
    assert [event for event, _ in registered] == ['PreToolUse', 'PostToolUse']
    assert read_wall_log(Path(config['soft_wall']['log_path']))[0]['decision'] == 'blocked'
    # Runtime bootstrap/isolation did not clear registrations; subsequent planner
    # or scripted runs cannot be affected by these now-inactive callbacks.
    pre = registered[0][1]
    assert pre(block('read_file', 'next', path='models.py')) is None
    assert pre in hooks.HOOKS['PreToolUse']


def test_wall_logs_aggregate_confirmed_bash_reads_across_attempts(tmp_path):
    from aqours_code.taskgraph.context_metrics import node_context_metrics
    attempts = []
    for n in [1, 2]:
        attempts.append({'attempt': n, 'workspace': str(tmp_path), 'own_files': [], 'full_files': []})
        entries = [
            {'decision': 'blocked', 'tool': 'bash', 'paths': ['store.py', 'web.py']},
            {'decision': 'allowed', 'tool': 'bash', 'paths': ['store.py', 'web.py']},
        ]
        (tmp_path / f'soft_wall_{n}.jsonl').write_text('\n'.join(json.dumps(e) for e in entries))
    result = node_context_metrics(tmp_path, attempts)
    assert result['soft_wall_blocked'] == 2
    assert result['confirmed_reads'] == ['store.py', 'web.py']
    assert result['reads_outside_pack'] == 4
