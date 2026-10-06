"""Reproducible offline size sample for jp-modular P (no model or code execution).

    python tests/taskgraph/context_pack_fixture.py /tmp/jp-context-sample

Build the generated base, apply the contract's shared files from the reference,
and replace its six feature modules with reference signatures/docstrings. This
is a deterministic stand-in for A's output, not an actual model-produced A run.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from aqours_code.taskgraph import load_graph
from aqours_code.taskgraph.context_pack import build_context_pack, interface_summary

TASK = Path(__file__).resolve().parents[2] / 'experiments/taskgraph/job_platform'


def create_sample(out: Path) -> dict:
    spec = importlib.util.spec_from_file_location('job_platform_make_repo', TASK / 'make_repo.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = out / 'repo'
    module.make_repo('modular', repo)
    graph = load_graph(TASK / 'modular/graphs/handwritten.json')
    contract = next(n for n in graph.nodes if n.id == 'A')
    feature_paths = {p for n in graph.nodes if n.id != 'A' for p in n.edit_set.modify}
    for path in [*contract.edit_set.modify, *contract.edit_set.create]:
        source = (TASK / 'modular/reference' / path).read_text(encoding='utf-8')
        if path in feature_paths:
            source, fallback = interface_summary(source, path)
            assert not fallback
        (repo / path).write_text(source, encoding='utf-8')
    module.git(repo, 'add', '-A')
    module.git(repo, 'commit', '-q', '-m', 'offline reference-derived contract fixture')
    node = next(n for n in graph.nodes if n.id == 'P')
    pack = build_context_pack(node, repo)
    dest = out / 'nodes/P'
    dest.mkdir(parents=True)
    (dest / 'context_1.md').write_text(pack.text, encoding='utf-8')
    baseline = sum(len(p.read_bytes()) for p in repo.rglob('*.py')) + len((repo / 'SPEC.md').read_bytes())
    report = {**pack.report(), 'baseline_bytes': baseline,
              'reduction_percent': round(100 * (1 - len(pack.text.encode('utf-8')) / baseline), 2),
              'fixture': 'reference-derived contract; not a live model run'}
    (out / 'size_report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    print(json.dumps(create_sample(Path(sys.argv[1])), indent=2))
