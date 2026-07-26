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
    "processed_tool_use_ids",
})
FILE_CARD_KEYS = frozenset({
    "path",
    "digest",
    "stale",
    "source_tool_use_ids",
    "supersede_fields",
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
        "processed_tool_use_ids": [],
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
                if any(not isinstance(entry, str) for entry in item):
                    return None
                normalized[section][key] = [
                    entry for entry in item if entry.strip()
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
        if (
            not isinstance(raw.get("path", ""), str)
            or not isinstance(raw.get("digest", ""), str)
            or not isinstance(raw.get("stale", False), bool)
            or not isinstance(raw.get("purpose", ""), str)
        ):
            return None
        path = normalize_knowledge_path(raw.get("path", ""))
        if not path:
            return None
        card = {
            "path": path,
            "digest": raw.get("digest", ""),
            "stale": raw.get("stale", False),
            "source_tool_use_ids": [],
            "supersede_fields": [],
            "purpose": str(raw.get("purpose", "")),
        }
        source_ids = raw.get("source_tool_use_ids", [])
        supersede_fields = raw.get("supersede_fields", [])
        if (
            not isinstance(source_ids, list)
            or any(not isinstance(item, str) for item in source_ids)
            or not isinstance(supersede_fields, list)
            or any(not isinstance(item, str) for item in supersede_fields)
        ):
            return None
        card["source_tool_use_ids"] = [
            item for item in source_ids if item.strip()
        ]
        allowed_supersede = {
            "purpose",
            "key_symbols",
            "key_behaviors",
            "important_conditions",
            "relationships",
            "relevant_ranges",
            "short_snippets",
            "conclusions",
            "uncertainties",
        }
        if any(item not in allowed_supersede for item in supersede_fields):
            return None
        card["supersede_fields"] = list(dict.fromkeys(supersede_fields))
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
            if (
                not isinstance(item, list)
                or any(not isinstance(entry, str) for entry in item)
            ):
                return None
            card[key] = [entry for entry in item if entry.strip()]
        normalized["files"].append(card)
    for key in (
        "decisions",
        "rejected_approaches",
        "failures",
        "open_questions",
        "next_actions",
        "processed_tool_use_ids",
    ):
        incoming = value.get(key, [])
        if (
            not isinstance(incoming, list)
            or any(not isinstance(entry, str) for entry in incoming)
        ):
            return None
        normalized[key] = [
            entry for entry in incoming if entry.strip()
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


def _observation_value(observation, name: str, default=""):
    if isinstance(observation, dict):
        return observation.get(name, default)
    return getattr(observation, name, default)


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
        observations: dict[str, Any] | None = None,
        current_digests: dict[str, str] | None = None,
    ) -> None:
        normalized = validate_semantic_delta(delta)
        if normalized is None:
            raise ValueError("invalid semantic memory delta")
        current = {
            normalize_knowledge_path(path): str(digest)
            for path, digest in (current_digests or {}).items()
        }
        observation_map = observations or {}
        with self._lock:
            task = normalized["task"]
            # The original goal belongs to the Runtime. A compact model may
            # improve constraints/understanding but cannot rewrite it.
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
            self.progress["current_focus"] = _short(
                progress["current_focus"],
            )
            completed = set(self.progress["completed"])
            # Current-state fields are snapshots, not historical logs.
            self.progress["remaining"] = [
                item for item in _merge_list([], progress["remaining"])
                if item not in completed
            ]
            for raw in normalized["files"]:
                card = deepcopy(raw)
                source_ids = list(card.get("source_tool_use_ids", ()))
                resolved = [
                    observation_map[source_id]
                    for source_id in source_ids
                    if source_id in observation_map
                ]
                versions = {
                    (
                        normalize_knowledge_path(str(
                            _observation_value(item, "path", "")
                        )),
                        str(_observation_value(item, "digest", "")),
                    )
                    for item in resolved
                    if _observation_value(item, "path", "")
                    and _observation_value(item, "digest", "")
                }
                if len(versions) == 1:
                    path, digest = next(iter(versions))
                else:
                    path = card["path"]
                    digest = "unknown"
                stale = (
                    digest == "unknown"
                    or not current.get(path)
                    or current.get(path) != digest
                )
                card["path"] = path
                card["digest"] = digest
                card["stale"] = stale
                if digest == "unknown":
                    card["uncertainties"] = _merge_list(
                        card.get("uncertainties", []),
                        [
                            "Historical read version could not be bound to one "
                            "runtime observation"
                        ],
                        10,
                    )
                identity = f"{path}@{digest}"
                existing = self.files.get(identity)
                if existing is None:
                    existing = {
                        "path": path,
                        "digest": digest,
                        "stale": stale,
                        "source_tool_use_ids": [],
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
                existing["source_tool_use_ids"] = _merge_list(
                    existing["source_tool_use_ids"], source_ids, 16,
                )
                supersede = set(card.get("supersede_fields", ()))
                if card.get("purpose"):
                    existing["purpose"] = _short(card["purpose"])
                elif "purpose" in supersede:
                    existing["purpose"] = ""
                existing["stale"] = stale
                for key in (
                    "key_symbols",
                    "key_behaviors",
                    "important_conditions",
                    "relationships",
                    "relevant_ranges",
                    "conclusions",
                    "uncertainties",
                ):
                    incoming = card.get(key, ())
                    existing[key] = (
                        _merge_list([], incoming, 10)
                        if key in supersede
                        else _merge_list(existing[key], incoming, 10)
                    )
                snippets = (
                    _short(item, MAX_SNIPPET)
                    for item in card.get("short_snippets", ())
                )
                existing["short_snippets"] = (
                    _merge_list([], snippets, 4)
                    if "short_snippets" in supersede
                    else _merge_list(existing["short_snippets"], snippets, 4)
                )
                # A plain dict preserves insertion order. Reinsert a touched
                # identity so the fixed card cap evicts least-recently merged
                # cards, not whichever path happened to be observed first.
                self.files[identity] = self.files.pop(identity)
            for key in (
                "decisions",
                "rejected_approaches",
                "failures",
            ):
                setattr(
                    self,
                    key,
                    _merge_list(getattr(self, key), normalized[key]),
                )
            self.open_questions = _merge_list(
                [], normalized["open_questions"],
            )
            self.next_actions = _merge_list(
                [], normalized["next_actions"],
            )
            self._trim_file_cards()
            self.compact_count += 1

    def _trim_file_cards(self) -> None:
        active_text = " ".join([
            self.progress.get("current_focus", ""),
            *self.progress.get("remaining", []),
            *self.next_actions,
        ]).lower()
        while len(self.files) > MAX_FILE_CARDS:
            candidates = []
            for index, (identity, card) in enumerate(self.files.items()):
                basename = card["path"].rsplit("/", 1)[-1].lower()
                symbols = [str(item).lower()
                           for item in card.get("key_symbols", ())]
                relevance = int(bool(
                    basename and basename in active_text
                )) + sum(
                    1 for symbol in symbols
                    if len(symbol) >= 3 and symbol in active_text
                )
                freshness = 1 if not card.get("stale", True) else 0
                candidates.append((relevance, freshness, index, identity))
            _, _, _, evict = min(candidates)
            del self.files[evict]

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
