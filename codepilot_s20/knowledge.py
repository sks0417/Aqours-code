"""Deterministic, run-scoped working memory.

RunKnowledge is intentionally not a long-term memory store.  It contains only
facts observed or produced during one Agent run and ties evidence to concrete
file versions so a mutation invalidates the smallest possible scope.
"""
from __future__ import annotations

import ast
import hashlib
import re
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


_TEST_COMMAND = re.compile(
    r"(?i)(?:^|[;&|]\s*|\s)(?:"
    r"pytest|py\.test|python(?:\d+(?:\.\d+)*)?\s+-m\s+(?:pytest|unittest)|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
    r"cargo\s+test|go\s+test|mvnw?\s+test|gradlew?\s+test"
    r")(?:\s|$)"
)
EVIDENCE_VERIFIED = "verified"
EVIDENCE_STALE = "stale"
EVIDENCE_UNBOUND = "unbound"
EVIDENCE_STATES = frozenset({
    EVIDENCE_VERIFIED,
    EVIDENCE_STALE,
    EVIDENCE_UNBOUND,
})
_WORKSPACE_STATE_EXCLUDED_DIRS = frozenset({
    ".git",
    ".codepilot",
    ".transcripts",
    ".task_outputs",
    ".tasks",
    ".mailboxes",
    ".worktrees",
})


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
    source_refs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    evidence_state: str = EVIDENCE_UNBOUND
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def evidence_valid(self) -> bool:
        return self.evidence_state == EVIDENCE_VERIFIED


@dataclass
class TestKnowledge:
    test_id: str
    command: str
    passed: bool
    exit_code: int | None
    timed_out: bool
    result: str
    workspace_versions_at_run: dict[str, int] = field(default_factory=dict)
    workspace_fingerprints_at_run: dict[str, str] = field(default_factory=dict)
    covered_source_versions: dict[str, int] = field(default_factory=dict)
    workspace_changed_since_run: list[str] = field(default_factory=list)


def snapshot_workspace(workdir: str | Path) -> dict[str, str]:
    """Return content fingerprints for user-visible Workspace files."""
    root = Path(workdir).resolve()
    fingerprints: dict[str, str] = {}
    if not root.is_dir():
        return fingerprints
    for candidate in root.rglob("*"):
        try:
            relative = candidate.relative_to(root)
            if any(
                part in _WORKSPACE_STATE_EXCLUDED_DIRS
                for part in relative.parts[:-1]
            ):
                continue
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
            fingerprints[normalize_knowledge_path(relative.as_posix())] = (
                content_digest(candidate.read_bytes())
            )
        except (OSError, ValueError):
            continue
    return fingerprints


@contextmanager
def workspace_mutation_reconciliation(
    knowledge: "RunKnowledge | None",
    workdir: str | Path,
):
    """Reconcile actual filesystem changes around a mutating operation."""
    if knowledge is None:
        yield
        return
    with knowledge.mutation_boundary(workdir):
        yield


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
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )
    _mutation_lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )

    def clear(self) -> None:
        with self._lock:
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

    def _evidence_state(
        self,
        source_versions: dict[str, int],
        source_refs: dict[str, tuple[str, ...]] | None = None,
    ) -> str:
        refs = source_refs or {}
        has_source = bool(source_versions) or any(refs.values())
        if not has_source:
            return EVIDENCE_UNBOUND
        if not self._evidence_is_current(source_versions):
            return EVIDENCE_STALE
        tests = {item.test_id: item for item in self.recent_tests}
        for test_id in refs.get("tests", ()):
            test = tests.get(test_id)
            if test is None:
                return EVIDENCE_UNBOUND
            if not test.passed or test.workspace_changed_since_run:
                return EVIDENCE_STALE
        for finding_id in refs.get("reviewer_findings", ()):
            finding = self.reviewer_findings.get(finding_id)
            if finding is None:
                return EVIDENCE_UNBOUND
            if finding.evidence_state != EVIDENCE_VERIFIED:
                return EVIDENCE_STALE
        return EVIDENCE_VERIFIED

    def observe_file(self, path: str, content: bytes | str) -> FileKnowledge:
        with self._lock:
            normalized = normalize_knowledge_path(path)
            digest = content_digest(content)
            current = self.files.get(normalized)
            if current is None:
                current = FileKnowledge(path=normalized, digest=digest)
                self.files[normalized] = current
            elif current.digest != digest:
                self._invalidate_file(
                    normalized, digest=digest, modified=False,
                )
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
        with self._lock:
            return self._invalidate_file(
                path, digest=digest, modified=modified,
            )

    def _invalidate_file(
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

    def reconcile_workspace(
        self,
        before: dict[str, str],
        after: dict[str, str],
    ) -> tuple[str, ...]:
        """Invalidate exactly the paths whose content fingerprint changed."""
        with self._lock:
            changed = sorted(
                path for path in set(before) | set(after)
                if before.get(path) != after.get(path)
            )
            for path in changed:
                self._invalidate_file(
                    path,
                    digest=after.get(path, ""),
                    modified=True,
                )
            return tuple(changed)

    @contextmanager
    def mutation_boundary(self, workdir: str | Path):
        """Serialize snapshot/execute/reconcile windows across worker threads."""
        with self._mutation_lock:
            before = snapshot_workspace(workdir)
            try:
                yield
            finally:
                after = snapshot_workspace(workdir)
                self.reconcile_workspace(before, after)

    def _invalidate_linked_evidence(self, path: str, version: int) -> None:
        for collection in (
            self.confirmed_symbols,
            self.confirmed_contracts,
            self.reviewer_findings,
        ):
            for evidence in collection.values():
                expected = evidence.source_versions.get(path)
                if expected is not None and expected != version:
                    evidence.evidence_state = EVIDENCE_STALE
        for item in self.acceptance.values():
            expected = item.get("source_versions", {}).get(path)
            if expected is not None and expected != version:
                item["evidence_state"] = EVIDENCE_STALE
                item["evidence_valid"] = False
        for test in self.recent_tests:
            expected = test.workspace_versions_at_run.get(path)
            expected_digest = test.workspace_fingerprints_at_run.get(path)
            workspace_changed = (
                expected is not None and expected != version
            ) or (
                bool(test.workspace_fingerprints_at_run)
                and expected_digest != self.files[path].digest
            )
            if workspace_changed:
                if path not in test.workspace_changed_since_run:
                    test.workspace_changed_since_run.append(path)
        self._refresh_reference_states()

    def _refresh_reference_states(self) -> None:
        for evidence in self.confirmed_contracts.values():
            if evidence.evidence_state == EVIDENCE_UNBOUND:
                continue
            evidence.evidence_state = self._evidence_state(
                evidence.source_versions, evidence.source_refs,
            )
        for item in self.acceptance.values():
            state = self._evidence_state(
                item.get("source_versions", {}),
                item.get("source_refs", {}),
            )
            item["evidence_state"] = state
            item["evidence_valid"] = state == EVIDENCE_VERIFIED

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
                evidence_state=EVIDENCE_VERIFIED,
                metadata={"path": path, "kind": kind, "line": line},
            )

    def sync_acceptance(self, todos: list[dict]) -> None:
        with self._lock:
            active: dict[str, dict[str, Any]] = {}
            active_contracts: set[str] = set()
            for index, todo in enumerate(todos, 1):
                if todo.get("kind") != "acceptance":
                    continue
                item_id = str(todo.get("id") or f"accept:{index}")
                evidence_text = str(todo.get("evidence", ""))
                raw_sources = todo.get("evidence_sources", {})
                if not isinstance(raw_sources, dict):
                    raw_sources = {}
                files = tuple(
                    normalize_knowledge_path(path)
                    for path in raw_sources.get("files", ())
                    if normalize_knowledge_path(path)
                )
                source_versions = self._source_versions(files)
                source_refs = {
                    "tests": tuple(
                        str(item) for item in raw_sources.get("tests", ())
                        if str(item)
                    ),
                    "reviewer_findings": tuple(
                        str(item)
                        for item in raw_sources.get("reviewer_findings", ())
                        if str(item)
                    ),
                }
                state = self._evidence_state(
                    source_versions, source_refs,
                )
                if (
                    todo.get("status") != "completed"
                    or not evidence_text
                ):
                    state = EVIDENCE_UNBOUND
                active[item_id] = {
                    "id": item_id,
                    "content": str(todo.get("content", "")),
                    "status": str(todo.get("status", "pending")),
                    "evidence": evidence_text,
                    "source_versions": source_versions,
                    "source_refs": source_refs,
                    "evidence_state": state,
                    "evidence_valid": state == EVIDENCE_VERIFIED,
                }
                contract_key = f"acceptance:{item_id}"
                active_contracts.add(contract_key)
                self.confirmed_contracts[contract_key] = EvidenceKnowledge(
                    key=contract_key,
                    text=str(todo.get("content", "")),
                    source_versions=source_versions,
                    source_refs=source_refs,
                    evidence_state=state,
                    metadata={
                        "status": str(todo.get("status", "pending")),
                    },
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
        with self._lock:
            source_versions = self._source_versions(source_paths)
            self.confirmed_contracts[str(key)] = EvidenceKnowledge(
                key=str(key),
                text=str(text)[:500],
                source_versions=source_versions,
                evidence_state=self._evidence_state(source_versions),
                metadata=dict(metadata),
            )

    def record_test(
        self,
        command: str,
        *,
        exit_code: int | None,
        timed_out: bool,
        result: str,
        covered_paths=(),
        workspace_fingerprints: dict[str, str] | None = None,
    ) -> None:
        if not is_test_command(command):
            return
        with self._lock:
            workspace_versions = {
                path: record.version
                for path, record in self.files.items()
            }
            covered_versions = self._source_versions(covered_paths)
            next_id = int(
                self.recent_tests[-1].test_id.rsplit(":", 1)[-1]
            ) + 1 if self.recent_tests else 1
            self.recent_tests.append(TestKnowledge(
                test_id=f"test:{next_id}",
                command=str(command)[:500],
                passed=exit_code == 0 and not timed_out,
                exit_code=exit_code,
                timed_out=bool(timed_out),
                result=str(result)[-1200:],
                workspace_versions_at_run=workspace_versions,
                workspace_fingerprints_at_run=dict(
                    workspace_fingerprints or {}
                ),
                covered_source_versions=covered_versions,
            ))
            del self.recent_tests[:-5]

    def record_reviewer_findings(
        self,
        findings: list[dict],
        revision: int,
    ) -> None:
        with self._lock:
            for index, finding in enumerate(findings[:5], 1):
                key = f"review:r{revision}:f{index}"
                path = normalize_knowledge_path(
                    str(finding.get("file", ""))
                )
                sources = self._source_versions([path] if path else [])
                self.reviewer_findings[key] = EvidenceKnowledge(
                    key=key,
                    text=str(
                        finding.get("requirement")
                        or finding.get("evidence")
                        or "Reviewer finding"
                    )[:500],
                    source_versions=sources,
                    evidence_state=self._evidence_state(sources),
                    metadata={
                        "severity": str(
                            finding.get("severity", "warning")
                        )[:30],
                        "path": path,
                        "symbol": str(finding.get("symbol", ""))[:160],
                    },
                )

    def as_dict(self) -> dict[str, Any]:
        def evidence_dict(value: EvidenceKnowledge) -> dict[str, Any]:
            result = asdict(value)
            result["evidence_valid"] = value.evidence_valid
            return result

        with self._lock:
            return {
                "read_files": {
                    path: asdict(record) for path, record in self.files.items()
                },
                "confirmed_symbols": {
                    key: evidence_dict(value)
                    for key, value in self.confirmed_symbols.items()
                },
                "confirmed_contracts": {
                    key: evidence_dict(value)
                    for key, value in self.confirmed_contracts.items()
                },
                "modified_files": sorted(self.modified_files),
                "recent_tests": [asdict(test) for test in self.recent_tests],
                "acceptance": {
                    key: dict(value) for key, value in self.acceptance.items()
                },
                "reviewer_findings": {
                    key: evidence_dict(value)
                    for key, value in self.reviewer_findings.items()
                },
            }

    def prompt_view(self) -> str:
        with self._lock:
            lines = [
                "RunKnowledge (authoritative for this run; stale or unbound "
                "evidence must not be used as verified proof):"
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
                    "Modified files: "
                    + ", ".join(sorted(self.modified_files)[-24:])
                )
            if self.recent_tests:
                lines.append("Recent tests:")
                for test in self.recent_tests[-3:]:
                    state = (
                        "workspace-changed:"
                        + ",".join(test.workspace_changed_since_run)
                        if test.workspace_changed_since_run
                        else ("pass" if test.passed else "fail")
                    )
                    lines.append(
                        f"- [{test.test_id} {state}] {test.command} | "
                        f"{test.result[-300:]}"
                    )
            if self.acceptance:
                lines.append("Acceptance:")
                for item in list(self.acceptance.values())[-12:]:
                    lines.append(
                        f"- [{item['id']} {item['status']} "
                        f"{item.get('evidence_state', EVIDENCE_UNBOUND)}] "
                        f"{item['content'][:240]}"
                    )
            if self.reviewer_findings:
                lines.append("Reviewer findings:")
                for item in list(self.reviewer_findings.values())[-5:]:
                    lines.append(
                        f"- [{item.key} {item.evidence_state}] "
                        f"{item.text[:240]}"
                    )
            lines.append(
                "Do not re-read a valid unchanged file merely to rediscover "
                "its path, version, symbols, acceptance state, or prior test "
                "status."
            )
            return "\n".join(lines)
