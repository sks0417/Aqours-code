#!/usr/bin/env python3
"""Analyze the timeline file from the 5000-file large-dir test."""
import argparse
import json
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Path to a .codepilot run directory")
    return parser.parse_args()


RUN_DIR = parse_args().run_dir

# Check what files are in the run dir
print("=== Files in run dir ===")
for path in RUN_DIR.iterdir():
    print(f"  {path.name}: {path.stat().st_size} bytes")

# Read timeline
print("\n=== Timeline Analysis ===")
with (RUN_DIR / "timeline.jsonl").open("r", encoding="utf-8", errors="replace") as f:
    content = f.read()

lines = content.strip().split('\n')
print(f"Total entries: {len(lines)}")

for i, line in enumerate(lines):
    if not line.strip():
        continue
    try:
        entry = json.loads(line)
        etype = entry.get('type', 'unknown')
        timestamp = entry.get('timestamp', '')
        
        if etype == 'user_message':
            print(f"\n[{i}] Type: {etype} | {timestamp}")
            msg = entry.get('message', '')
            print(f"  Message: {msg[:200]}")
        elif etype == 'assistant_message':
            print(f"\n[{i}] Type: {etype} | {timestamp}")
            msg = entry.get('message', '')
            print(f"  Message: {msg[:200]}")
        elif etype == 'tool_call':
            print(f"\n[{i}] Type: {etype} | {timestamp}")
            tc = entry.get('toolCall', {})
            name = tc.get('name', '')
            args = str(tc.get('arguments', {}))
            print(f"  Tool: {name}")
            print(f"  Args: {args[:300]}")
        elif etype == 'tool_result':
            tool_name = entry.get('toolName', entry.get('toolCall', {}).get('name', ''))
            result = entry.get('result', '')
            if isinstance(result, str):
                result_len = len(result)
            elif isinstance(result, dict):
                result_len = len(str(result))
            else:
                result_len = 0
            is_error = entry.get('isError', False)
            status = 'ERROR' if is_error else 'OK'
            print(f"[{i}] Tool Result: {tool_name} | {status} | size={result_len}")
        elif etype == 'error':
            print(f"\n[{i}] Type: ERROR | {timestamp}")
            print(f"  Error: {entry.get('error', '')[:300]}")
    except json.JSONDecodeError as e:
        print(f"[{i}] JSON parse error: {e}")
        print(f"  Content: {line[:200]}")
