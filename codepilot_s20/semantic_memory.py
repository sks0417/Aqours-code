"""Bounded, run-scoped memory of semantics already understood by the model."""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .knowledge import normalize_knowledge_path


SEMANTIC_MEMORY_PROMPT_LIMIT = 12000
MAX_FILE_CARDS = 24
MAX_LIST_ITEMS = 16
MAX_TEXT = 600
MAX_SNIPPET = 500

SEMANTIC_DELTA_KEYS = frozenset({
    "task",
    "progress",
    "files",
    "decisions",
    "rejected_approaches",
    "failures",
    "open_questions",
    "next_actions",
})
FILE_CARD_KEYS = frozenset({
    "path",
    "digest",
    "stale",
    "purpose",
    "key_symbols",
    "key_behaviors",
    "important_conditions",
    "relationships",
    "relevant_ranges",
    "short_snippets",
    "conclusions",
    "uncertainties",
})


def empty_semantic_delta() -> dict[str, Any]:
    return {
        "task": {
            "goal": "",
            "constraints": [],
            "definition_of_done": [],
        },
        "progress": {
            "completed": [],
            "current_focus": "",
            "remaining": [],
        },
        "files": [],
        "decisions": [],
        "rejected_approaches": [],
        "failures": [],
        "open_questions": [],
        "next_actions": [],
    }


def validate_semantic_delta(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if set(value) - SEMANTIC_DELTA_KEYS:
        return None
    normalized = empty_semantic_delta()
    for section in ("task", "progress"):
        incoming = value.get(section, {})
        if not isinstance(incoming, dict):
            return None
        for key in normalized[section]:
            item = incoming.get(key, normalized[section][key])
            if isinstance(normalized[section][key], list):
                if not isinstance(item, list):
                    return None
                normalized[section][key] = [
                    str(entry) for entry in item if str(entry).strip()
                ]
            elif not isinstance(item, str):
                return None
            else:
                normalized[section][key] = item
    files = value.get("files", [])
    if not isinstance(files, list):
        return None
    for raw in files:
        if not isinstance(raw, dict) or set(raw) - FILE_CARD_KEYS:
            return None
        path = normalize_knowledge_path(str(raw.get("path", "")))
        if not path:
            return None
        card = {
            "path": path,
            "digest": str(raw.get("digest", "")),
            "stale": bool(raw.get("stale", False)),
            "purpose": str(raw.get("purpose", "")),
        }
        for key in (
            "key_symbols",
            "key_behaviors",
            "important_conditions",
            "relationships",
            "relevant_ranges",
            "short_snippets",
            "conclusions",
            "uncertainties",
        ):
            item = raw.get(key, [])
            if isinstance(item, str):
                item = [item]
            if not isinstance(item, list):
                return None
            card[key] = [str(entry) for entry in item if str(entry).strip()]
        normalized["files"].append(card)
    for key in (
        "decisions",
        "rejected_approaches",
        "failures",
        "open_questions",
        "next_actions",
    ):
        incoming = value.get(key, [])
        if not isinstance(incoming, list):
            return None
        normalized[key] = [
            str(entry) for entry in incoming if str(entry).strip()
        ]
    return normalized


def _short(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _merge_list(current: list[str], incoming, limit=MAX_LIST_ITEMS) -> list[str]:
    merged = list(current)
    for value in incoming:
        text = _short(value)
        if text and text not in merged:
            merged.append(text)
    return merged[-limit:]


@dataclass
class SessionSemanticMemory:
    """Canonical semantic checkpoint for one Agent run.

    This state is useful continuation context, not verified evidence.
    """

    task: dict[str, Any] = field(default_factory=lambda: {
        "goal": "",
        "constraints": [],
        "definition_of_done": [],
    })
    progress: dict[str, Any] = field(default_factory=lambda: {
        "completed": [],
        "current_focus": "",
        "remaining": [],
    })
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    decisions: list[str] = field(default_factory=list)
    rejected_approaches: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    compact_count: int = 0
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )

    def clear(self, goal: str = "") -> None:
        with self._lock:
            self.task = {
                "goal": _short(goal, 2000),
                "constraints": [],
                "definition_of_done": [],
            }
            self.progress = {
                "completed": [],
                "current_focus": "",
                "remaining": [],
            }
            self.files.clear()
            self.decisions.clear()
            self.rejected_approaches.clear()
            self.failures.clear()
            self.open_questions.clear()
            self.next_actions.clear()
            self.compact_count = 0

    def merge(
        self,
        delta: dict[str, Any],
        *,
        digest_lookup: dict[str, str] | None = None,
    ) -> None:
        normalized = validate_semantic_delta(delta)
        if normalized is None:
            raise ValueError("invalid semantic memory delta")
        digests = {
            normalize_knowledge_path(path): str(digest)
            for path, digest in (digest_lookup or {}).items()
        }
        with self._lock:
            task = normalized["task"]
            if task["goal"]:
                self.task["goal"] = _short(task["goal"], 2000)
            self.task["constraints"] = _merge_list(
                self.task["constraints"], task["constraints"],
            )
            self.task["definition_of_done"] = _merge_list(
                self.task["definition_of_done"],
                task["definition_of_done"],
            )
            progress = normalized["progress"]
            self.progress["completed"] = _merge_list(
                self.progress["completed"], progress["completed"],
            )
            if progress["current_focus"]:
                self.progress["current_focus"] = _short(
                    progress["current_focus"],
                )
            self.progress["remaining"] = _merge_list(
                self.progress["remaining"], progress["remaining"],
            )
            for raw in normalized["files"]:
                card = deepcopy(raw)
                path = card["path"]
                digest = card["digest"] or digests.get(path, "unknown")
                card["digest"] = digest
                identity = f"{path}@{digest}"
                existing = self.files.get(identity)
                if existing is None:
                    existing = {
                        "path": path,
                        "digest": digest,
                        "stale": bool(card.get("stale", False)),
                        "purpose": "",
                        "key_symbols": [],
                        "key_behaviors": [],
                        "important_conditions": [],
                        "relationships": [],
                        "relevant_ranges": [],
                        "short_snippets": [],
                        "conclusions": [],
                        "uncertainties": [],
                    }
                    self.files[identity] = existing
                if card.get("purpose"):
                    existing["purpose"] = _short(card["purpose"])
                existing["stale"] = bool(card.get("stale", False))
                for key in (
                    "key_symbols",
                    "key_behaviors",
                    "important_conditions",
                    "relationships",
                    "relevant_ranges",
                    "conclusions",
                    "uncertainties",
                ):
                    existing[key] = _merge_list(
                        existing[key], card.get(key, ()), 10,
                    )
                existing["short_snippets"] = _merge_list(
                    existing["short_snippets"],
                    (_short(item, MAX_SNIPPET)
                     for item in card.get("short_snippets", ())),
                    4,
                )
                # A plain dict preserves insertion order. Reinsert a touched
                # identity so the fixed card cap evicts least-recently merged
                # cards, not whichever path happened to be observed first.
                self.files[identity] = self.files.pop(identity)
            while len(self.files) > MAX_FILE_CARDS:
                oldest = next(iter(self.files))
                del self.files[oldest]
            for key in (
                "decisions",
                "rejected_approaches",
                "failures",
                "open_questions",
                "next_actions",
            ):
                setattr(
                    self,
                    key,
                    _merge_list(getattr(self, key), normalized[key]),
                )
            self.compact_count += 1

    def observe_file(self, path: str, digest: str) -> None:
        normalized = normalize_knowledge_path(path)
        with self._lock:
            for card in self.files.values():
                if card["path"] == normalized:
                    card["stale"] = card["digest"] != digest

    def mark_file_stale(self, path: str, current_digest: str = "") -> None:
        normalized = normalize_knowledge_path(path)
        with self._lock:
            for card in self.files.values():
                if card["path"] == normalized:
                    card["stale"] = (
                        not current_digest or card["digest"] != current_digest
                    )

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "task": deepcopy(self.task),
                "progress": deepcopy(self.progress),
                "files": [deepcopy(card) for card in self.files.values()],
                "decisions": list(self.decisions),
                "rejected_approaches": list(self.rejected_approaches),
                "failures": list(self.failures),
                "open_questions": list(self.open_questions),
                "next_actions": list(self.next_actions),
            }

    def prompt_view(
        self,
        max_chars: int = SEMANTIC_MEMORY_PROMPT_LIMIT,
    ) -> str:
        """Return one strictly bounded canonical injection."""
        max_chars = max(1000, int(max_chars))
        with self._lock:
            payload = self.as_dict()
        prefix = (
            "SessionSemanticMemory (continuation context, not verified proof):\n"
        )
        rendered = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        )
        if len(prefix) + len(rendered) <= max_chars:
            return prefix + rendered
        # Shrink complete cards/items; never cut JSON in the middle.
        payload["files"] = payload["files"][-12:]
        for card in payload["files"]:
            card["short_snippets"] = card["short_snippets"][-1:]
            for key in (
                "key_symbols",
                "key_behaviors",
                "important_conditions",
                "relationships",
                "relevant_ranges",
                "conclusions",
                "uncertainties",
            ):
                card[key] = card[key][-4:]
        for key in (
            "decisions",
            "rejected_approaches",
            "failures",
            "open_questions",
            "next_actions",
        ):
            payload[key] = payload[key][-8:]
        for key in ("constraints", "definition_of_done"):
            payload["task"][key] = payload["task"][key][-8:]
        for key in ("completed", "remaining"):
            payload["progress"][key] = payload["progress"][key][-8:]
        rendered = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        )
        while payload["files"] and len(prefix) + len(rendered) > max_chars:
            payload["files"].pop(0)
            rendered = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"),
            )
        if len(prefix) + len(rendered) > max_chars:
            minimal = {
                "task": payload["task"],
                "progress": payload["progress"],
                "decisions": payload["decisions"][-4:],
                "open_questions": payload["open_questions"][-4:],
                "next_actions": payload["next_actions"][-4:],
            }
            rendered = json.dumps(
                minimal, ensure_ascii=False, separators=(",", ":"),
            )
        if len(prefix) + len(rendered) > max_chars:
            # Preserve valid JSON while bounding long scalar values.
            minimal["task"]["goal"] = _short(
                minimal["task"].get("goal", ""), 300,
            )
            minimal["progress"]["current_focus"] = _short(
                minimal["progress"].get("current_focus", ""), 200,
            )
            for section, keys in (
                (minimal["task"], ("constraints", "definition_of_done")),
                (minimal["progress"], ("completed", "remaining")),
            ):
                for key in keys:
                    section[key] = [
                        _short(item, 120) for item in section.get(key, [])[-4:]
                    ]
            for key in ("decisions", "open_questions", "next_actions"):
                minimal[key] = [
                    _short(item, 120) for item in minimal.get(key, [])[-3:]
                ]
            rendered = json.dumps(
                minimal, ensure_ascii=False, separators=(",", ":"),
            )
        if len(prefix) + len(rendered) > max_chars:
            rendered = json.dumps(
                {"task": {"goal": _short(
                    minimal["task"].get("goal", ""), 120,
                )}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return prefix + rendered
