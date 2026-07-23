"""Deterministic, run-scoped working memory.

RunKnowledge is intentionally not a long-term memory store.  It contains only
facts observed or produced during one Agent run and ties evidence to concrete
file versions so a mutation invalidates the smallest possible scope.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any


_TEST_COMMAND = re.compile(
    r"(?i)(?:^|[;&|]\s*|\s)(?:"
    r"pytest|py\.test|python(?:\d+(?:\.\d+)*)?\s+-m\s+(?:pytest|unittest)|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
    r"cargo\s+test|go\s+test|mvnw?\s+test|gradlew?\s+test"
    r")(?:\s|$)"
)


def normalize_knowledge_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    if value.startswith("./"):
        value = value[2:]
    return str(PurePosixPath(value)) if value else ""


def content_digest(content: bytes | str) -> str:
    raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def is_test_command(command: str) -> bool:
    return bool(_TEST_COMMAND.search(str(command or "")))


@dataclass
class FileKnowledge:
    path: str
    digest: str
    version: int = 1
    read_count: int = 0
    evidence_valid: bool = True


@dataclass
class EvidenceKnowledge:
    key: str
    text: str
    source_versions: dict[str, int] = field(default_factory=dict)
    evidence_valid: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestKnowledge:
    command: str
    passed: bool
    exit_code: int | None
    timed_out: bool
    result: str
    validated_versions: dict[str, int] = field(default_factory=dict)
    stale_paths: list[str] = field(default_factory=list)


@dataclass
class RunKnowledge:
    """Structured evidence that survives message compaction."""

    files: dict[str, FileKnowledge] = field(default_factory=dict)
    confirmed_symbols: dict[str, EvidenceKnowledge] = field(default_factory=dict)
    confirmed_contracts: dict[str, EvidenceKnowledge] = field(default_factory=dict)
    modified_files: set[str] = field(default_factory=set)
    recent_tests: list[TestKnowledge] = field(default_factory=list)
    acceptance: dict[str, dict[str, Any]] = field(default_factory=dict)
    reviewer_findings: dict[str, EvidenceKnowledge] = field(default_factory=dict)

    def clear(self) -> None:
        self.files.clear()
        self.confirmed_symbols.clear()
        self.confirmed_contracts.clear()
        self.modified_files.clear()
        self.recent_tests.clear()
        self.acceptance.clear()
        self.reviewer_findings.clear()

    def _source_versions(self, paths) -> dict[str, int]:
        versions = {}
        for raw_path in paths:
            path = normalize_knowledge_path(raw_path)
            record = self.files.get(path)
            if path and record is not None:
                versions[path] = record.version
        return versions

    def _evidence_is_current(self, source_versions: dict[str, int]) -> bool:
        return all(
            path in self.files
            and self.files[path].evidence_valid
            and self.files[path].version == version
            for path, version in source_versions.items()
        )

    def observe_file(self, path: str, content: bytes | str) -> FileKnowledge:
        normalized = normalize_knowledge_path(path)
        digest = content_digest(content)
        current = self.files.get(normalized)
        if current is None:
            current = FileKnowledge(path=normalized, digest=digest)
            self.files[normalized] = current
        elif current.digest != digest:
            self.invalidate_file(normalized, digest=digest, modified=False)
            current = self.files[normalized]
        current.digest = digest
        current.read_count += 1
        current.evidence_valid = True
        self._record_python_symbols(normalized, content, current.version)
        return current

    def invalidate_file(
        self,
        path: str,
        *,
        digest: str = "",
        modified: bool = True,
    ) -> FileKnowledge:
        normalized = normalize_knowledge_path(path)
        current = self.files.get(normalized)
        if current is None:
            current = FileKnowledge(
                path=normalized, digest=digest, evidence_valid=False,
            )
            self.files[normalized] = current
        else:
            current.version += 1
            current.digest = digest
            current.evidence_valid = False
        if modified:
            self.modified_files.add(normalized)
        self._invalidate_linked_evidence(normalized, current.version)
        return current

    def _invalidate_linked_evidence(self, path: str, version: int) -> None:
        for collection in (
            self.confirmed_symbols,
            self.confirmed_contracts,
            self.reviewer_findings,
        ):
            for evidence in collection.values():
                expected = evidence.source_versions.get(path)
                if expected is not None and expected != version:
                    evidence.evidence_valid = False
        for item in self.acceptance.values():
            expected = item.get("source_versions", {}).get(path)
            if expected is not None and expected != version:
                item["evidence_valid"] = False
        for test in self.recent_tests:
            expected = test.validated_versions.get(path)
            if expected is not None and expected != version:
                if path not in test.stale_paths:
                    test.stale_paths.append(path)

    def _record_python_symbols(
        self,
        path: str,
        content: bytes | str,
        version: int,
    ) -> None:
        if not path.lower().endswith(".py"):
            return
        text = (
            content.decode("utf-8", errors="replace")
            if isinstance(content, bytes) else str(content)
        )
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        found: list[tuple[str, str, int]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((node.name, "function", node.lineno))
            elif isinstance(node, ast.ClassDef):
                found.append((node.name, "class", node.lineno))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found.append(
                            (f"{node.name}.{child.name}", "method", child.lineno)
                        )
        for name, kind, line in found[:80]:
            key = f"{path}:{name}"
            self.confirmed_symbols[key] = EvidenceKnowledge(
                key=key,
                text=name,
                source_versions={path: version},
                evidence_valid=True,
                metadata={"path": path, "kind": kind, "line": line},
            )

    def sync_acceptance(self, todos: list[dict]) -> None:
        active: dict[str, dict[str, Any]] = {}
        active_contracts: set[str] = set()
        known_paths = tuple(self.files)
        for index, todo in enumerate(todos, 1):
            if todo.get("kind") != "acceptance":
                continue
            item_id = str(todo.get("id") or f"accept:{index}")
            evidence_text = str(todo.get("evidence", ""))
            combined = f"{todo.get('content', '')} {evidence_text}"
            sources = [
                path for path in known_paths
                if path and path.lower() in combined.replace("\\", "/").lower()
            ]
            source_versions = self._source_versions(sources)
            evidence_valid = (
                bool(evidence_text)
                and self._evidence_is_current(source_versions)
                if todo.get("status") == "completed" else True
            )
            active[item_id] = {
                "id": item_id,
                "content": str(todo.get("content", "")),
                "status": str(todo.get("status", "pending")),
                "evidence": evidence_text,
                "source_versions": source_versions,
                "evidence_valid": evidence_valid,
            }
            contract_key = f"acceptance:{item_id}"
            active_contracts.add(contract_key)
            self.confirmed_contracts[contract_key] = EvidenceKnowledge(
                key=contract_key,
                text=str(todo.get("content", "")),
                source_versions=source_versions,
                evidence_valid=evidence_valid,
                metadata={"status": str(todo.get("status", "pending"))},
            )
        self.acceptance = active
        for key in list(self.confirmed_contracts):
            if key.startswith("acceptance:") and key not in active_contracts:
                del self.confirmed_contracts[key]

    def record_contract(
        self,
        key: str,
        text: str,
        source_paths=(),
        **metadata,
    ) -> None:
        source_versions = self._source_versions(source_paths)
        self.confirmed_contracts[str(key)] = EvidenceKnowledge(
            key=str(key),
            text=str(text)[:500],
            source_versions=source_versions,
            evidence_valid=self._evidence_is_current(source_versions),
            metadata=dict(metadata),
        )

    def record_test(
        self,
        command: str,
        *,
        exit_code: int | None,
        timed_out: bool,
        result: str,
    ) -> None:
        if not is_test_command(command):
            return
        versions = {
            path: record.version
            for path, record in self.files.items()
            if path in self.modified_files
        }
        self.recent_tests.append(TestKnowledge(
            command=str(command)[:500],
            passed=exit_code == 0 and not timed_out,
            exit_code=exit_code,
            timed_out=bool(timed_out),
            result=str(result)[-1200:],
            validated_versions=versions,
        ))
        del self.recent_tests[:-5]

    def record_reviewer_findings(
        self,
        findings: list[dict],
        revision: int,
    ) -> None:
        for index, finding in enumerate(findings[:5], 1):
            key = f"review:r{revision}:f{index}"
            path = normalize_knowledge_path(str(finding.get("file", "")))
            sources = self._source_versions([path] if path else [])
            self.reviewer_findings[key] = EvidenceKnowledge(
                key=key,
                text=str(
                    finding.get("requirement")
                    or finding.get("evidence")
                    or "Reviewer finding"
                )[:500],
                source_versions=sources,
                evidence_valid=self._evidence_is_current(sources),
                metadata={
                    "severity": str(finding.get("severity", "warning"))[:30],
                    "path": path,
                    "symbol": str(finding.get("symbol", ""))[:160],
                },
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "read_files": {
                path: asdict(record) for path, record in self.files.items()
            },
            "confirmed_symbols": {
                key: asdict(value)
                for key, value in self.confirmed_symbols.items()
            },
            "confirmed_contracts": {
                key: asdict(value)
                for key, value in self.confirmed_contracts.items()
            },
            "modified_files": sorted(self.modified_files),
            "recent_tests": [asdict(test) for test in self.recent_tests],
            "acceptance": dict(self.acceptance),
            "reviewer_findings": {
                key: asdict(value)
                for key, value in self.reviewer_findings.items()
            },
        }

    def prompt_view(self) -> str:
        lines = [
            "RunKnowledge (authoritative for this run; stale evidence must not "
            "be used as current proof):"
        ]
        files = list(self.files.values())[-24:]
        lines.append("Files:")
        if not files:
            lines.append("- (none read)")
        for record in files:
            state = "valid" if record.evidence_valid else "stale"
            lines.append(
                f"- {record.path} v{record.version} {state} "
                f"sha256:{record.digest[:12]} reads:{record.read_count}"
            )
        valid_symbols = [
            item for item in self.confirmed_symbols.values()
            if item.evidence_valid
        ][-30:]
        if valid_symbols:
            lines.append("Confirmed symbols:")
            for item in valid_symbols:
                meta = item.metadata
                lines.append(
                    f"- {meta.get('path', '')}:{meta.get('line', '?')} "
                    f"{meta.get('kind', 'symbol')} {item.text}"
                )
        if self.modified_files:
            lines.append(
                "Modified files: " + ", ".join(sorted(self.modified_files)[-24:])
            )
        if self.recent_tests:
            lines.append("Recent tests:")
            for test in self.recent_tests[-3:]:
                state = (
                    "stale:" + ",".join(test.stale_paths)
                    if test.stale_paths else ("pass" if test.passed else "fail")
                )
                lines.append(f"- [{state}] {test.command} | {test.result[-300:]}")
        if self.acceptance:
            lines.append("Acceptance:")
            for item in list(self.acceptance.values())[-12:]:
                validity = (
                    "valid" if item.get("evidence_valid") else "stale"
                )
                lines.append(
                    f"- [{item['id']} {item['status']} {validity}] "
                    f"{item['content'][:240]}"
                )
        if self.reviewer_findings:
            lines.append("Reviewer findings:")
            for item in list(self.reviewer_findings.values())[-5:]:
                state = "valid" if item.evidence_valid else "stale"
                lines.append(f"- [{item.key} {state}] {item.text[:240]}")
        lines.append(
            "Do not re-read a valid unchanged file merely to rediscover its "
            "path, version, symbols, acceptance state, or prior test status."
        )
        return "\n".join(lines)
