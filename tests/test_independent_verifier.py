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
    def __init__(self, responses, max_calls: int):
        super().__init__(responses)
        self.max_calls = max_calls

    def budget_snapshot(self):
        return {
            "max_calls": self.max_calls,
            "call_count": len(self.calls),
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


def verifier_json(status: str, blockers=None):
    return response(text_block(json.dumps({
        "status": status,
        "summary": "independent evidence collected",
        "tests_run": [],
        "blockers": blockers or [],
    })))


def test_verifier_defaults_are_centralized():
    assert config.VERIFIER_COMPLEXITY_THRESHOLD == 6
    assert config.VERIFIER_MAX_MODEL_CALLS == 8
    assert config.VERIFIER_MAX_TOOL_CALLS == 12
    assert config.VERIFIER_MAX_TESTS == 5
    assert config.VERIFIER_MAX_RUNS_PER_TASK == 1


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


def test_blockers_discard_pending_final_and_return_json_to_lead_once(tmp_path):
    prepare_workspace(tmp_path)
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
        verifier_test(), verifier_json("blockers", [blocker]),
        response(text_block("lead final after handling blocker")),
    ])

    result = agent_loop.run_agent_task(
        high_complexity_task(), str(tmp_path),
        model_client=client, model_provider="scripted", model="scripted",
        tool_policy=run_eval.DOCKER_EVAL_TOOL_POLICY,
    )

    assert result["final_answer"] == "lead final after handling blocker"
    assert len([
        call for call in client.calls
        if "You are the verifier role" in call["system"]
    ]) == 2
    final_lead_messages = json.dumps(
        client.calls[-1]["messages"], ensure_ascii=False, default=str,
    )
    assert "Independent verification found potential blockers" in final_lead_messages
    assert "Public VALUE is stable" in final_lead_messages
    assert pending_final not in final_lead_messages


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
    assert no_test["failure_reason"] == "no_actual_bash_test"


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
