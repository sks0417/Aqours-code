#!/usr/bin/env python3
"""Analyze trace.jsonl entries from the 5000-file test."""
import argparse
import json
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Path to a .codepilot run directory")
    return parser.parse_args()


RUN_DIR = parse_args().run_dir

with (RUN_DIR / "trace.jsonl").open("r", encoding="utf-8", errors="replace") as f:
    lines = f.read().strip().split('\n')

print(f"Total trace entries: {len(lines)}")

for i, line in enumerate(lines):
    try:
        obj = json.loads(line)
        t = obj.get('type', '?')
        ts = obj.get('ts', 0)
        tool = obj.get('tool', '')
        
        if t == 'user_prompt':
            prompt = obj.get('prompt', '')[:150]
            print(f"\n[{i:3d}] {t:20s} | ts={ts} | {prompt}")
        elif t == 'llm_request':
            mc = obj.get('message_count', 0)
            tc = obj.get('tool_count', 0)
            print(f"[{i:3d}] {t:20s} | ts={ts} | messages={mc} tools={tc}")
        elif t == 'llm_response':
            stop = obj.get('stop_reason', '?')
            print(f"[{i:3d}] {t:20s} | ts={ts} | stop={stop}")
        elif t == 'tool_use':
            inp = obj.get('input', {})
            cmd = str(inp.get('command', inp.get('pattern', '')))[:120]
            tid = obj.get('tool_use_id', '')
            print(f"[{i:3d}] {t:20s} | ts={ts} | {tool:15s} | {tid[:30]} | {cmd}")
        elif t == 'tool_result':
            result_str = str(obj.get('result', ''))
            is_err = obj.get('isError', False)
            print(f"[{i:3d}] {t:20s} | ts={ts} | {tool:15s} | err={is_err} | len={len(result_str)}")
        elif t == 'hook':
            name = obj.get('name', '')
            decision = obj.get('decision', '')
            stage = obj.get('stage', '')
            print(f"[{i:3d}] {t:20s} | ts={ts} | {name:15s} | {stage:8s} | {decision}")
        elif t == 'error':
            err = obj.get('error', '')[:200]
            print(f"\n[{i:3d}] ERROR | ts={ts} | {err}")
        else:
            rest = json.dumps(obj, ensure_ascii=False)[:150]
            print(f"[{i:3d}] {t:20s} | ts={ts} | {rest}")
    except Exception as e:
        print(f"[{i:3d}] Parse error: {e} | line[:200]={line[:200]}")

# Check timeline.jsonl raw content
print("\n\n=== timeline.jsonl raw entries ===")
with (RUN_DIR / "timeline.jsonl").open("r", encoding="utf-8", errors="replace") as f:
    tlines = f.read().strip().split('\n')
print(f"Total timeline entries: {len(tlines)}")
for i, line in enumerate(tlines):
    if not line.strip():
        continue
    print(f"[{i}] {line[:300]}")
