"""Subprocess fake model for the real worker/coordinator soft-wall integration test."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace as NS

from aqours_code.taskgraph.worker_entry import run_worker
from aqours_code.taskgraph.workers import write_json_atomic


def tool(name, ident, **arguments):
    return NS(content=[NS(type='tool_use', id=ident, name=name, input=arguments)],
              stop_reason='tool_use', usage={})


def done():
    return NS(content=[NS(type='text', text='done; full store.py was needed to check compatibility')],
              stop_reason='end_turn', usage={})


class ScriptedModel:
    def __init__(self, node):
        self.node, self.calls = node, 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.node == 'A':
            return tool('write_file', 'create-a', path='a.txt', content='A') if self.calls == 1 else done()
        results = {item['tool_use_id']: item['content']
                   for message in kwargs['messages'] if isinstance(message.get('content'), list)
                   for item in message['content'] if isinstance(item, dict) and item.get('type') == 'tool_result'}
        if self.calls == 1:
            return tool('read_file', 'first-read', path='store.py')
        if self.calls == 2:
            assert 'Tool not run:' in results['first-read']
            assert 'CONFIRMED_SOURCE_TOKEN' not in results['first-read']
            return tool('read_file', 'second-read', path='store.py')
        if self.calls == 3:
            assert 'CONFIRMED_SOURCE_TOKEN' in results['second-read']
            return tool('edit_file', 'edit-own', path='runner.py', old_text='count = 0', new_text='count = 42')
        return done()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--describe', action='store_true')
    parser.add_argument('--config')
    args = parser.parse_args()
    if args.describe:
        print(json.dumps({'worker': 'scripted-soft-wall'}))
        return
    config = json.loads(Path(args.config).read_text())
    result = run_worker(config, model_client=ScriptedModel(config['node_id']))
    write_json_atomic(Path(config['result_path']), result.to_dict())
    raise SystemExit(0 if result.ok else 1)


if __name__ == '__main__':
    main()
