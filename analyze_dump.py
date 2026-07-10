#!/usr/bin/env python3
"""Analyze raw content of run files from the 5000-file test."""
import os
import json

RUN_DIR = r"C:\Users\Alan_\Documents\New project\codepilot_s20\.codepilot\stress_workspace\20260707-230838\.codepilot\runs\20260707-230843-5f119e85"

# 1. metadata.json
print("=== metadata.json ===")
with open(os.path.join(RUN_DIR, 'metadata.json'), 'r', encoding='utf-8', errors='replace') as f:
    print(f.read())

# 2. final.md
print("\n=== final.md ===")
with open(os.path.join(RUN_DIR, 'final.md'), 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()
    print(f"Size: {len(content)} bytes")
    if content.strip():
        print(content[:2000])
    else:
        print("(empty file)")

# 3. timeline.md - first portion
print("\n=== timeline.md (first 3000 chars) ===")
with open(os.path.join(RUN_DIR, 'timeline.md'), 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()
    print(content[:3000])

# 4. trace.jsonl - last entries
print("\n=== trace.jsonl (last 3000 chars) ===")
with open(os.path.join(RUN_DIR, 'trace.jsonl'), 'r', encoding='utf-8', errors='replace') as f:
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
