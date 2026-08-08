from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

from aqours_code import agent_loop, config, subagent
from evals import metrics, run_eval


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, data: dict, block_id: str):
    return SimpleNamespace(type="tool_use", name=name, input=data, id=block_id)


def response(*blocks):
    has_tool = any(block.type == "tool_use" for block in blocks)
    return SimpleNamespace(
        content=list(blocks),
        stop_reason="tool_use" if has_tool else "end_turn",
    )


class ScriptedClient:
    def __init__(self, responses):
        self.messages = self
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class BudgetedScriptedClient(ScriptedClient):
    def __init__(self, responses, max_calls: int, used_calls: int = 0):
        super().__init__(responses)
        self.max_calls = max_calls
        self.used_calls = used_calls

    def create(self, **kwargs):
        if self.used_calls + len(self.calls) >= self.max_calls:
            raise RuntimeError("scripted broker hard limit exceeded")
        return super().create(**kwargs)

    def budget_snapshot(self):
        return {
            "max_calls": self.max_calls,
            "call_count": self.used_calls + len(self.calls),
            "max_provider_retries": 0,
        }


def high_complexity_task() -> str:
    return (
        "Implement an end-to-end repository change from the README contract. "
        "Preserve the public API and compatibility across multiple files. "
        "Keep atomic concurrent state transitions, transaction rollback, "
        "idempotency, consistency, and exception behavior correct. "
        "Add focused tests and run the complete regression test suite.\n"
        "1. Inspect all documented requirements.\n"
        "2. Update the implementation.\n"
        "3. Verify error paths and persistence.\n"
        "4. Preserve API compatibility.\n"
    )


def prepare_workspace(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "The public VALUE must be 2 after the change.\n", encoding="utf-8",
    )
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_service.py").write_text(
        "import service\n\ndef test_value():\n    assert service.VALUE == 2\n",
        encoding="utf-8",
    )


def lead_edit():
    return response(tool_block(
        "edit_file",
        {"path": "service.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
        "lead-edit",
    ))


def verifier_test(command: str | None = None):
    command = command or f'"{sys.executable}" -m pytest -q'
    return response(tool_block("bash", {"command": command}, "verify-test"))


def lead_test():
    return response(tool_block(
        "bash",
        {"command": (
            f'"{sys.executable}" -c "import service; '
            'assert service.VALUE == 2"'
        )},
        "lead-test",
    ))


def verifier_json(status: str, findings=None, *, legacy: bool = False):
    payload = {
        "status": status,
        "summary": "independent evidence collected",
        "tests_run": [],
        "findings" if not legacy else "blockers": findings or [],
    }
    return response(text_block(json.dumps(payload)))


def test_verifier_defaults_are_centralized():
    assert config.VERIFIER_COMPLEXITY_THRESHOLD == 6
    assert config.VERIFIER_MAX_MODEL_CALLS == 8
    assert config.VERIFIER_MAX_TOOL_CALLS == 12
    assert config.VERIFIER_MAX_TESTS == 5
    assert config.VERIFIER_MAX_RUNS_PER_TASK == 1
    assert config.VERIFIER_RESOLUTION_RESERVE == 2
    assert config.VERIFIER_SAFETY_MARGIN == 1


def test_low_complexity_change_does_not_invoke_verifier(tmp_path):
    prepare_workspace(tmp_path)
    trace = tmp_path / "trace.jsonl"
    client = ScriptedClient([lead_edit(), response(text_block("small final"))])

    result = agent_loop.run_agent_task(
        "Fix the typo in service.py.", str(tmp_path), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "small final"
    assert len(client.calls) == 2
    report = metrics.trace_metrics(trace)
    assert report["verifier_invoked"] is False
    assert report["verifier_skipped_reason"] == "below_complexity_threshold"


def test_high_complexity_without_workspace_changes_skips_verifier(tmp_path):
    prepare_workspace(tmp_path)
    trace = tmp_path / "trace.jsonl"
    client = ScriptedClient([response(text_block("no changes needed"))])

    result = agent_loop.run_agent_task(
        high_complexity_task(), str(tmp_path), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "no changes needed"
    assert metrics.trace_metrics(trace)["verifier_skipped_reason"] == (
        "workspace_unchanged"
    )


def test_verifier_pass_accepts_pending_lead_final_with_no_extra_lead_call(
    tmp_path,
):
    prepare_workspace(tmp_path)
    trace = tmp_path / "trace.jsonl"
    pending_final = "SECRET LEAD FINAL"
    client = ScriptedClient([
        lead_edit(), lead_test(), response(text_block(pending_final)),
        verifier_test(), verifier_json("pass"),
    ])

    result = agent_loop.run_agent_task(
        high_complexity_task(), str(tmp_path), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == pending_final
    verifier_calls = [
        call for call in client.calls
        if "You are the verifier role" in call["system"]
    ]
    lead_calls = [
        call for call in client.calls
        if "You are the verifier role" not in call["system"]
    ]
    assert len(verifier_calls) == 2
    assert len(lead_calls) == 3
    assert {tool["name"] for tool in verifier_calls[0]["tools"]} == {
        "bash", "read_file", "glob",
    }
    verifier_context = json.dumps(
        verifier_calls[0]["messages"], ensure_ascii=False, default=str,
    )
    assert pending_final not in verifier_context
    assert "lead-test" not in verifier_context
    assert "assert service.VALUE == 2" in verifier_context
    assert "pass:" in verifier_context
    assert "Git metadata is unavailable" in verifier_context
    assert "service.py" in verifier_context
    assert pending_final not in verifier_calls[0]["system"]
    assert high_complexity_task() in verifier_calls[0]["system"]
    assert all(call["max_tokens"] == 6000 for call in verifier_calls)
    report = metrics.trace_metrics(trace)
    assert report["verifier_invoked"] is True
    assert report["verifier_status"] == "pass"
    assert report["verifier_model_calls"] == 2
    assert report["verifier_tool_calls"] == 1
    assert report["verifier_tests_run"] == 1
    assert report["verifier_blockers_found"] == 0
    assert report["verifier_workspace_modified"] is False


def test_candidate_finding_ignored_twice_finishes_verification_incomplete(
    tmp_path,
):
    prepare_workspace(tmp_path)
    trace = tmp_path / "trace.jsonl"
    pending_final = "UNVERIFIED FINAL MUST DISAPPEAR"
    blocker = {
        "requirement": "Public VALUE is stable",
        "location": "service.py:1",
        "expected": "VALUE remains valid on retry",
        "observed": "counterexample returns stale state",
        "evidence": "focused test and diff inspection",
    }
    client = ScriptedClient([
        lead_edit(), response(text_block(pending_final)),
        verifier_test(), verifier_json("findings", [blocker]),
        response(text_block("first ignored final")),
        response(text_block("second ignored final")),
    ])

    result = agent_loop.run_agent_task(
        high_complexity_task(), str(tmp_path), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"].startswith("verification_incomplete:")
    assert len([
        call for call in client.calls
        if "You are the verifier role" in call["system"]
    ]) == 2
    resolution_messages = json.dumps(
        client.calls[-1]["messages"], ensure_ascii=False, default=str,
    )
    assert "candidate findings" in resolution_messages
    assert "verification_resolution_reminder" in resolution_messages
    assert "Public VALUE is stable" in resolution_messages
    assert pending_final not in resolution_messages
    report = metrics.trace_metrics(trace)
    assert report["verifier_candidate_findings"] == 1
    assert report["verifier_resolution_status"] == "incomplete"


def test_invalid_json_and_pass_without_bash_test_are_inconclusive(
    tmp_path, monkeypatch,
):
    prepare_workspace(tmp_path)
    client = ScriptedClient([
        response(text_block("not json")),
        response(text_block("still not json")),
        verifier_json("pass"),
    ])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", high_complexity_task())

    invalid = subagent.run_role_agent(
        "verifier", "verify independently", tmp_path,
    )
    no_test = subagent.run_role_agent(
        "verifier", "verify independently", tmp_path,
    )

    assert invalid["status"] == "inconclusive"
    assert invalid["failure_reason"] == "invalid_verifier_json"
    assert no_test["status"] == "inconclusive"
    assert no_test["failure_reason"] == "public_suite_not_run"


def test_verifier_workspace_mutation_invalidates_result_without_reset(
    tmp_path, monkeypatch,
):
    prepare_workspace(tmp_path)
    mutation = (
        f'"{sys.executable}" -c "from pathlib import Path; '
        "Path('service.py').write_text('BAD = True\\n')\""
    )
    client = ScriptedClient([
        response(tool_block("bash", {"command": mutation}, "bad-write")),
        verifier_json("pass"),
    ])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", high_complexity_task())
    events = []
    monkeypatch.setattr(
        subagent, "record_event",
        lambda event_type, **payload: events.append((event_type, payload)),
    )

    outcome = subagent.run_independent_verifier(
        tmp_path, None, complexity_score=8,
    )

    assert outcome["status"] == "inconclusive"
    assert outcome["failure_reason"] == "verifier_modified_workspace"
    assert outcome["workspace_modified"] is True
    assert "BAD = True" in (tmp_path / "service.py").read_text(encoding="utf-8")
    assert events[-1][0] == "verification_result"
    assert events[-1][1]["workspace_modified"] is True


def test_verifier_budget_skip_is_explicit_and_does_not_call_model(
    tmp_path, monkeypatch,
):
    prepare_workspace(tmp_path)
    client = BudgetedScriptedClient([], max_calls=4)
    monkeypatch.setattr(subagent, "client", client)
    events = []
    monkeypatch.setattr(
        subagent, "record_event",
        lambda event_type, **payload: events.append((event_type, payload)),
    )

    outcome = subagent.run_independent_verifier(
        tmp_path, None, complexity_score=8,
    )

    assert outcome["invoked"] is False
    assert outcome["failure_reason"] == "insufficient_model_budget"
    assert client.calls == []
    assert events[-1][0] == "verification_skipped"
    assert events[-1][1]["verification_skipped_reason"] == (
        "insufficient_model_budget"
    )


def test_last_model_call_records_verifier_budget_skip_and_tells_lead(tmp_path):
    prepare_workspace(tmp_path)
    trace = tmp_path / "trace.jsonl"
    client = BudgetedScriptedClient([
        lead_edit(), response(text_block("forced final")),
    ], max_calls=2)

    result = agent_loop.run_agent_task(
        high_complexity_task(), str(tmp_path), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "forced final"
    assert len(client.calls) == 2
    assert "independent verifier cannot run" in json.dumps(
        client.calls[-1]["messages"], ensure_ascii=False, default=str,
    )
    report = metrics.trace_metrics(trace)
    assert report["verifier_invoked"] is False
    assert report["verifier_skipped_reason"] == "insufficient_model_budget"


def candidate_finding(*, evidence_test_ids=None) -> dict:
    return {
        "requirement": "Public VALUE follows the documented contract",
        "location": "service.py:1",
        "expected": "VALUE has the documented value",
        "observed": "Verifier suspects a mismatched value",
        "evidence": "Candidate observation requiring Harness validation",
        "evidence_test_ids": evidence_test_ids or [],
    }


def targeted_assert_command(expected: int = 2) -> str:
    return (
        f'"{sys.executable}" -c "import service; '
        f'assert service.VALUE == {expected}"'
    )


def test_false_positive_candidate_is_dismissed_by_new_tests(tmp_path):
    prepare_workspace(tmp_path)
    trace = tmp_path / "trace.jsonl"
    public = f'"{sys.executable}" -m pytest -q'
    client = ScriptedClient([
        lead_edit(), response(text_block("pending candidate final")),
        verifier_test(public), verifier_json("findings", [candidate_finding()]),
        response(
            tool_block("bash", {"command": targeted_assert_command(2)}, "counter"),
            tool_block("bash", {"command": public}, "public"),
        ),
        response(text_block("accepted after counterevidence")),
    ])

    result = agent_loop.run_agent_task(
        high_complexity_task(), str(tmp_path), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "accepted after counterevidence"
    report = metrics.trace_metrics(trace)
    assert report["verifier_status"] == "findings"
    assert report["verifier_candidate_findings"] == 0
    assert report["verifier_dismissed_findings"] == 1
    assert report["verifier_resolution_status"] == "dismissed"
    assert report["verifier_public_suite_run"] is True
    assert report["verifier_public_suite_passed"] is True


def _prepare_confirmed_workspace(tmp_path: Path) -> str:
    prepare_workspace(tmp_path)
    (tmp_path / "test_service.py").write_text(
        "import service\n\ndef test_value():\n    assert service.VALUE == 3\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" -m pytest -q'


def test_confirmed_finding_is_resolved_after_fix_and_replay(tmp_path):
    public = _prepare_confirmed_workspace(tmp_path)
    trace = tmp_path / "trace.jsonl"
    client = ScriptedClient([
        lead_edit(), response(text_block("pending wrong-value final")),
        verifier_test(public),
        verifier_json("findings", [candidate_finding(
            evidence_test_ids=["verifier_test_1"],
        )]),
        response(
            tool_block(
                "edit_file",
                {"path": "service.py", "old_text": "VALUE = 2", "new_text": "VALUE = 3"},
                "fix",
            ),
            tool_block("bash", {"command": public}, "replay"),
        ),
        response(text_block("accepted after confirmed fix")),
    ])

    result = agent_loop.run_agent_task(
        high_complexity_task(), str(tmp_path), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "accepted after confirmed fix"
    assert "VALUE = 3" in (tmp_path / "service.py").read_text(encoding="utf-8")
    report = metrics.trace_metrics(trace)
    assert report["verifier_confirmed_findings"] == 0
    assert report["verifier_resolved_findings"] == 1
    assert report["verifier_resolution_status"] == "resolved"


def test_confirmed_finding_still_reproduces_and_is_unresolved(tmp_path):
    public = _prepare_confirmed_workspace(tmp_path)
    trace = tmp_path / "trace.jsonl"
    client = ScriptedClient([
        lead_edit(), response(text_block("pending wrong-value final")),
        verifier_test(public),
        verifier_json("findings", [candidate_finding(
            evidence_test_ids=["verifier_test_1"],
        )]),
        response(
            tool_block(
                "edit_file",
                {"path": "service.py", "old_text": "VALUE = 2", "new_text": "VALUE = 4"},
                "bad-fix",
            ),
            tool_block("bash", {"command": public}, "failed-replay"),
        ),
        response(text_block("first unresolved final")),
        response(text_block("second unresolved final")),
    ])

    result = agent_loop.run_agent_task(
        high_complexity_task(), str(tmp_path), str(trace),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"].startswith("verification_incomplete:")
    report = metrics.trace_metrics(trace)
    assert report["verifier_unresolved_findings"] == 1
    assert report["verifier_resolution_status"] == "unresolved"


def _direct_verifier(tmp_path: Path, monkeypatch, responses) -> dict:
    client = ScriptedClient(responses)
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", high_complexity_task())
    return subagent.run_role_agent(
        "verifier", "verify independently", tmp_path,
    )


def test_targeted_python_assert_cannot_satisfy_pass_gate(tmp_path, monkeypatch):
    prepare_workspace(tmp_path)
    result = _direct_verifier(tmp_path, monkeypatch, [
        verifier_test(targeted_assert_command(1)), verifier_json("pass"),
    ])

    assert result["status"] == "inconclusive"
    assert result["failure_reason"] == "public_suite_not_run"
    assert result["tests_run"][0]["scope"] == "targeted"


def test_single_targeted_pytest_cannot_satisfy_pass_gate(tmp_path, monkeypatch):
    prepare_workspace(tmp_path)
    (tmp_path / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    targeted = f'"{sys.executable}" -m pytest test_service.py -q'
    result = _direct_verifier(tmp_path, monkeypatch, [
        verifier_test(targeted), verifier_json("pass"),
    ])

    assert result["status"] == "inconclusive"
    assert result["failure_reason"] == "public_suite_not_run"
    assert result["tests_run"][0]["scope"] == "targeted"


def test_complete_public_pytest_allows_pass(tmp_path, monkeypatch):
    prepare_workspace(tmp_path)
    (tmp_path / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    result = _direct_verifier(tmp_path, monkeypatch, [
        verifier_test(), verifier_json("pass"),
    ])

    assert result["status"] == "pass"
    assert result["tests_run"][0]["scope"] == "public_suite"
    assert result["tests_run"][0]["exit_code"] == 0


def test_failed_public_suite_creates_confirmed_finding(tmp_path, monkeypatch):
    prepare_workspace(tmp_path)
    result = _direct_verifier(tmp_path, monkeypatch, [
        verifier_test(), verifier_json("pass"),
    ])

    assert result["status"] == "findings"
    assert result["failure_reason"] == "public_suite_failed"
    assert result["findings"][0]["state"] == "confirmed"
    assert result["findings"][0]["evidence_test_ids"] == ["verifier_test_1"]


def test_legacy_blockers_are_candidate_findings(tmp_path, monkeypatch):
    prepare_workspace(tmp_path)
    result = _direct_verifier(tmp_path, monkeypatch, [
        verifier_json("blockers", [candidate_finding()], legacy=True),
    ])

    assert result["status"] == "findings"
    assert result["findings"][0]["id"] == "finding_1"
    assert result["findings"][0]["state"] == "candidate"
    assert result["findings"][0]["evidence_test_ids"] == []


def test_nonexistent_model_evidence_ids_are_discarded(tmp_path, monkeypatch):
    prepare_workspace(tmp_path)
    (tmp_path / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    finding = candidate_finding(
        evidence_test_ids=["verifier_test_1", "invented_test_99"],
    )
    client = ScriptedClient([
        verifier_test(), verifier_json("findings", [finding]),
    ])
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", high_complexity_task())

    result = subagent.run_role_agent(
        "verifier", "verify independently", tmp_path,
    )

    assert result["status"] == "findings"
    assert result["findings"][0]["state"] == "candidate"
    assert result["findings"][0]["evidence_test_ids"] == ["verifier_test_1"]


def test_verifier_dynamic_budget_allocates_six_of_nine_remaining(
    tmp_path, monkeypatch,
):
    prepare_workspace(tmp_path)
    (tmp_path / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    client = BudgetedScriptedClient([
        verifier_test(), verifier_json("pass"),
    ], max_calls=45, used_calls=36)
    monkeypatch.setattr(subagent, "client", client)
    monkeypatch.setattr(subagent, "MODEL", "scripted")
    monkeypatch.setattr(subagent, "CURRENT_ROOT_TASK", high_complexity_task())
    events = []
    monkeypatch.setattr(
        subagent, "record_event",
        lambda event_type, **payload: events.append((event_type, payload)),
    )

    outcome = subagent.run_independent_verifier(
        tmp_path, None, complexity_score=8,
    )

    assert outcome["invoked"] is True
    assert outcome["allocated_model_calls"] == 6
    assert len(client.calls) == 2
    assert client.used_calls + len(client.calls) <= client.max_calls
    start = next(payload for event, payload in events if event == "verification_start")
    assert start["remaining_model_calls"] == 9
    assert start["allocated_model_calls"] == 6
    assert start["resolution_reserve"] == 2
    assert start["safety_margin"] == 1
