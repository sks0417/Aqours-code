#!/usr/bin/env python3
"""Analyze raw content of run files from the 5000-file test."""
import argparse
import json
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Path to a .codepilot run directory")
    return parser.parse_args()


RUN_DIR = parse_args().run_dir

# 1. metadata.json
print("=== metadata.json ===")
with (RUN_DIR / "metadata.json").open("r", encoding="utf-8", errors="replace") as f:
    print(f.read())

# 2. final.md
print("\n=== final.md ===")
with (RUN_DIR / "final.md").open("r", encoding="utf-8", errors="replace") as f:
    content = f.read()
    print(f"Size: {len(content)} bytes")
    if content.strip():
        print(content[:2000])
    else:
        print("(empty file)")

# 3. timeline.md - first portion
print("\n=== timeline.md (first 3000 chars) ===")
with (RUN_DIR / "timeline.md").open("r", encoding="utf-8", errors="replace") as f:
    content = f.read()
    print(content[:3000])

# 4. trace.jsonl - last entries
print("\n=== trace.jsonl (last 3000 chars) ===")
with (RUN_DIR / "trace.jsonl").open("r", encoding="utf-8", errors="replace") as f:
    content = f.read()
    lines = content.strip().split('\n')
    print(f"Total lines: {len(lines)}")
    # Show last 5 entries
    for line in lines[-5:]:
        try:
            obj = json.loads(line)
            print(json.dumps(obj, indent=2, ensure_ascii=False)[:500])
        except:
            print(f"Raw: {line[:300]}")
    print("---")
    # Show first 3 entries
    for line in lines[:3]:
        try:
            obj = json.loads(line)
            print(json.dumps(obj, indent=2, ensure_ascii=False)[:500])
        except:
            print(f"Raw: {line[:300]}")
