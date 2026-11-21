"""The split gate with scripted model answers: search, location, decision, CLI, evaluation."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import aqours_code.taskgraph.gate as gate
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.gate import (B0, MAX_REFERENCE, P, SINGLE, SPLIT, W, FeatureFiles,
                                        FeatureSearch, GateOptions, SearchHit,
                                        build_locate_prompt, decide, estimate_tokens,
                                        extract_identifiers, feature_groups, features_from_gate,
                                        options_from_report, parse_locations, resolve_request,
                                        reusable_draft, run_gate, search_feature)
from aqours_code.taskgraph.outline import _Entry, _File, _read, is_source_file
from aqours_code.taskgraph.planner import DraftError, DraftItem
from aqours_code.taskgraph.repo_index import RepoIndex
from aqours_code.taskgraph.workers import WorkerResult, write_json_atomic
from taskgraph_support import commit_files
from test_planner import REQUEST, ScriptedDrafter, answer, items_answer

FILE_TOOLS = {"read_file", "glob", "grep", "bash", "write_file", "edit_file"}
SCRIPT = (Path(__file__).resolve().parents[2] / "experiments" / "taskgraph" / "gate"
          / "evaluate.py")


def kinds(text: str) -> dict[str, str]:
    return {ident.text: ident.kind for ident in extract_identifiers(text)}


def toy_files(toy_repo) -> list[_File]:
    from aqours_code.taskgraph.repo_index import build_index
    index = build_index(toy_repo.path, toy_repo.commit)
    return [_read(toy_repo.path, path) for path in sorted(index.files) if is_source_file(path)]


def defining(path: str, *names: str) -> _File:
    return _File(path, 10, [_Entry("def", name, f"def {name}()", "L1") for name in names])


def locate_answer(entries: list[dict]) -> str:
    return answer({"features": entries})


# ── identifiers ──

def test_identifiers_are_quoted_names_paths_modules_camel_and_snake_words():
    found = kinds("Add `JobStore.cancel(job_id)` in store.py and src/pkg/mod.py, call runJobs "
                  "and MAX_ATTEMPTS from pkg.sub.mod; see `Runner`.")
    assert found == {"JobStore.cancel": "module", "JobStore": "quoted", "cancel": "quoted",
                     "job_id": "quoted", "Runner": "quoted", "store.py": "path",
                     "src/pkg/mod.py": "path", "pkg.sub.mod": "module", "runJobs": "camel",
                     "MAX_ATTEMPTS": "snake"}


def test_plain_words_and_slashed_phrases_are_not_identifiers():
    found = kinds("Store the job, e.g. in d/h/m/s units or min/max; i.e. the Duration "
                  "and URL of HTTPRemote, see dvc/remote.")
    assert found == {"HTTPRemote": "camel"}


def test_a_quoted_path_and_a_windows_path_are_normalised():
    found = kinds(r"Change `.\pkg\mod.py` and pkg\other.py.")
    assert found == {"pkg/mod.py": "path", "pkg/other.py": "path"}


# ── the program search ──

def test_search_finds_named_files_first_then_files_defining_the_names(toy_repo):
    files = toy_files(toy_repo)
    assert [file.path for file in files] == ["broken.py", "models.py", "runner.py", "store.py"]
    search = search_feature("Make `run_loop` skip jobs from list_unfinished; touch models.py",
                            files)
    assert [(hit.path, hit.why) for hit in search.hits] == [
        ("models.py", ["path models.py"]),
        ("runner.py", ["defines run_loop"]),
        ("store.py", ["defines list_unfinished"]),
    ]
    assert search.common == [] and search.cut == 0


def test_search_leaves_out_short_dunder_and_common_names_and_matches_modules():
    files = [defining(f"pkg/m{number}.py", "save", "run", "__init__") for number in range(5)]
    files += [defining("src/pkg/core.py", "Engine"), defining("pkg/__init__.py", "setup")]
    search = search_feature("Change `save`, `run` and `__init__`; extend `pkg.core.Engine` "
                            "and the package's `__init__.py`.", files)
    assert search.common == ["save"]
    assert [(hit.path, hit.why) for hit in search.hits] == [
        ("src/pkg/core.py", ["module pkg.core", "defines Engine"])]


def test_search_keeps_at_most_the_limit_of_files():
    files = [defining(f"f{number}.py", "only_here") for number in range(3)]
    search = search_feature("use only_here", files, limit=2)
    assert [hit.path for hit in search.hits] == ["f0.py", "f1.py"] and search.cut == 1


# ── checking the answer ──

ITEMS = [DraftItem(id="1", title="one"), DraftItem(id="2", title="two"),
         DraftItem(id="3", title="three")]
INDEX = RepoIndex(commit="abc", files={"a.py", "b.py", "c.py", "d.py", "e.py",
                                       "pkg/__init__.py", "docs/guide.md"})


def test_paths_not_in_the_repository_are_dropped_and_recorded():
    located = parse_locations(locate_answer([
        {"id": 1, "modify": ["a.py", "missing.py", ".\\b.py", "a.py"],
         "reference": ["a.py", "c.py", "gone/x.py", "d.py", "e.py", "docs/guide.md"]},
        {"id": "2", "modify": "a.py"},
        {"id": "9", "modify": ["a.py"]},
    ]), ITEMS, INDEX)
    first, second, third = located.features
    assert (first.modify, first.reference) == (["a.py", "b.py"], ["c.py", "d.py", "e.py"])
    assert (second.modify, second.reference) == ([], [])
    assert (third.id, third.modify) == ("3", [])
    assert located.dropped == [
        {"feature": "1", "role": "modify", "path": "missing.py",
         "why": "not in the repository"},
        {"feature": "1", "role": "reference", "path": "gone/x.py",
         "why": "not in the repository"},
        {"feature": "1", "role": "reference", "path": "docs/guide.md",
         "why": f"more than {MAX_REFERENCE} references"},
    ]
    assert located.notes == ["entry for unknown feature '9' was left out",
                             "feature 2: modify is not a list; left out",
                             "feature 3 has no entry: it modifies no file"]


@pytest.mark.parametrize(("text", "message"), [
    ("no json here", "no ```json code block"),
    ("```json\n{not json}\n```", "not valid JSON"),
    (answer({"files": []}), "no `features` list"),
])
def test_an_answer_that_cannot_be_read_is_a_format_error(text, message):
    with pytest.raises(DraftError, match=message):
        parse_locations(text, ITEMS, INDEX)


# ── the decision ──

def test_m_adds_b0_every_involved_file_once_and_p_per_feature():
    features = [FeatureFiles("A", "a", ["a.py"], ["r.py"]),
                FeatureFiles("B", "b", ["b.py"], ["r.py"])]
    tokens = {"a.py": 10_000, "b.py": 20_000, "r.py": 5_000}
    result = decide(features, tokens)
    assert result["material"] == {"M": B0 + 35_000 + 2 * P, "B0": B0, "code": 35_000,
                                  "features_x_P": 2 * P, "features": 2, "files": 3,
                                  "W": W, "P": P}
    assert [feature["tokens"] for feature in result["features"]] == [15_000, 25_000]
    assert result["features"][0]["reference"] == [{"path": "r.py", "tokens": 5_000}]
    modify_only = decide(features, tokens, with_reference=False)
    assert modify_only["material"]["code"] == 30_000


def test_features_modifying_one_source_file_are_one_group():
    features = [FeatureFiles("A", "a", ["x.py", "pkg/__init__.py"]),
                FeatureFiles("B", "b", ["x.py", "y.py"]),
                FeatureFiles("C", "c", ["y.py", "tests/test_c.py"]),
                FeatureFiles("D", "d", ["pkg/__init__.py", "tests/test_c.py", "README.md"]),
                FeatureFiles("E", "e", ["README.md"], ["x.py"])]
    assert feature_groups(features) == [[0, 1, 2], [3], [4]]
    tokens = {"x.py": 1000, "y.py": 2000, "pkg/__init__.py": 100, "tests/test_c.py": 400,
              "README.md": 50}
    result = decide(features, tokens, p=1000)
    m = B0 + 3550 + 5 * 1000
    assert result["material"]["M"] == m
    largest = result["groups"][0]
    assert largest["features"] == ["A", "B", "C"]
    assert largest["shared_files"] == ["x.py", "y.py"]
    assert largest["material"] == 3500 + 3 * 1000  # x, y, __init__, the test file
    assert largest["share"] == pytest.approx(6500 / m)
    assert result["largest_group"] == ["A", "B", "C"]
    assert result["largest_group_share"] == pytest.approx(6500 / m)
    # E only reads x.py: reading does not connect
    assert [group["features"] for group in result["groups"][1:]] == [["E"], ["D"]]


def test_a_request_one_agent_holds_is_single():
    features = [FeatureFiles("A", "a", ["a.py"]), FeatureFiles("B", "b", ["b.py"])]
    result = decide(features, {"a.py": 1000, "b.py": 1000})
    assert result["material"]["M"] == B0 + 2000 + 2 * P <= W
    assert (result["decision"], result["reason_code"]) == (SINGLE, "fits")
    assert "one agent holds it" in result["reason"]


def test_features_concentrated_on_a_shared_file_are_single():
    features = [FeatureFiles("A", "a", ["core.py"]), FeatureFiles("B", "b", ["core.py"]),
                FeatureFiles("C", "c", ["small.py"])]
    result = decide(features, {"core.py": 60_000, "small.py": 1000})
    share = (60_000 + 2 * P) / result["material"]["M"]
    assert share >= 0.7 and result["largest_group_share"] == pytest.approx(share)
    assert (result["decision"], result["reason_code"]) == (SINGLE, "shared")
    assert "(A, B)" in result["reason"]


def test_independent_features_over_the_window_are_split():
    features = [FeatureFiles(name, name, [f"{name}.py"]) for name in "ABC"]
    result = decide(features, {"A.py": 20_000, "B.py": 20_000, "C.py": 20_000})
    assert result["material"]["M"] > W and result["largest_group_share"] < 0.7
    assert (result["decision"], result["reason_code"]) == (SPLIT, "split")


def test_a_file_over_the_window_is_oversized_without_changing_the_decision():
    features = [FeatureFiles(name, name, [f"{name}.py"]) for name in "ABC"]
    tokens = {"A.py": W + 1, "B.py": W, "C.py": W}
    result = decide(features, tokens)
    assert result["oversized"] == [{"path": "A.py", "tokens": W + 1}]
    assert result["decision"] == SPLIT


def test_features_and_tokens_are_read_back_from_a_gate_output():
    features = [FeatureFiles("A", "a", ["a.py"], ["r.py"]), FeatureFiles("B", "b", ["b.py"])]
    data = decide(features, {"a.py": 7, "b.py": 8, "r.py": 9})
    again, tokens = features_from_gate(data)
    assert again == features and tokens == {"a.py": 7, "b.py": 8, "r.py": 9}


# ── the prompt and its input limit ──

def many_files(count: int) -> list[_File]:
    return [_File(f"pkg/module_{number:03}.py", 100,
                  [_Entry("class", f"Thing{number}", f"class Thing{number}", "L1")])
            for number in range(count)]


def test_the_prompt_has_the_request_features_outline_and_search_results():
    files = many_files(3)
    items = [DraftItem(id="1", title="Thing", description="Extend `Thing2`.")]
    searches = {"1": search_feature("Thing\nExtend `Thing2`.", files)}
    prompt = build_locate_prompt(REQUEST, items, files, searches)
    text = prompt.text
    assert text.startswith(gate.load_locate_prompt().rstrip())
    for part in ("# The request\n\n" + REQUEST.strip(), "- 1: **Thing**. Extend `Thing2`.",
                 "- pkg/module_000.py (100 lines): Thing0", "## Feature 1: Thing",
                 "- pkg/module_002.py: defines Thing2"):
        assert part in text
    assert prompt.report()["outline_cut"] is False and prompt.omitted == []
    assert prompt.estimated_tokens == estimate_tokens(len(text))


def test_a_prompt_over_the_limit_has_its_outline_cut_and_keeps_the_searched_files():
    files = many_files(400)
    items = [DraftItem(id="1", title="Thing")]
    searches = {"1": FeatureSearch([], [SearchHit("pkg/module_399.py", ["defines Thing399"])])}
    whole = build_locate_prompt(REQUEST, items, files, searches)
    limit = whole.estimated_tokens - 1000
    prompt = build_locate_prompt(REQUEST, items, files, searches, limit)
    assert prompt.omitted and "pkg/module_399.py" not in prompt.omitted
    assert prompt.omitted == [file.path for file in files[-len(prompt.omitted) - 1:-1]]
    assert "- pkg/module_399.py (100 lines): Thing399" in prompt.text
    assert (f"(Outline cut to fit: {len(prompt.omitted)} more source files are not listed"
            in prompt.text)
    assert prompt.estimated_tokens <= limit and not prompt.over_limit
    report = prompt.report()
    assert report["outline_cut"] is True and report["outline_files"] == 400 - len(prompt.omitted)
    tiny = build_locate_prompt(REQUEST, items, files, searches, 10)
    assert tiny.over_limit and len(tiny.omitted) == 399


# ── running the gate ──

TOY_LOCATION = [{"id": "S", "modify": ["store.py"], "reference": ["models.py"]},
                {"id": "R", "modify": ["runner.py", "nope.py"]}]


def write_request(tmp_path: Path) -> Path:
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    return request


def test_the_gate_runs_step_1_then_one_location_call_without_file_tools(toy_repo, tmp_path):
    drafter = ScriptedDrafter([items_answer(["S", "R"])])
    locator = ScriptedDrafter([locate_answer(TOY_LOCATION)])
    out = tmp_path / "gate" / "toy.json"
    result = run_gate(GateOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                                  out=out, timeout_s=60), locator, drafter)
    data = result.data
    assert result.ok and json.loads(out.read_text(encoding="utf-8")) == data
    assert data["base_commit"] == toy_repo.commit and data["model"] == "scripted-model"
    assert data["draft"]["source"] == "step1" and len(data["draft"]["rounds"]) == 1
    assert [item["id"] for item in data["draft"]["items"]] == ["S", "R"]
    request = locator.requests[0]
    assert request.model_budget == {"max_calls": 1, "max_input_tokens": None}
    assert request.repo_guard and request.log_dir == out.with_name("toy.json.logs") / "locate"
    assert not FILE_TOOLS & {name for names in locator.clients[0].tool_names for name in names}
    assert len(locator.clients[0].tool_names) == 1  # one model call
    assert "- store.py (" in request.prompt and "## Feature S: item S" in request.prompt
    sizes = data["level0"]["sizes"]
    assert set(sizes) == {"broken.py", "models.py", "runner.py", "store.py"}
    assert sizes["store.py"] == len((toy_repo.path / "store.py").read_text(encoding="utf-8"))
    assert data["level0"]["tokens"] == sum(estimate_tokens(chars) for chars in sizes.values())
    code = sum(estimate_tokens(sizes[path]) for path in ("store.py", "models.py", "runner.py"))
    assert data["material"] == {"M": B0 + code + 2 * P, "B0": B0, "code": code,
                                "features_x_P": 2 * P, "features": 2, "files": 3, "W": W,
                                "P": P}
    assert (data["decision"], data["reason_code"]) == (SINGLE, "fits")
    assert data["dropped"] == [{"feature": "R", "role": "modify", "path": "nope.py",
                                "why": "not in the repository"}]
    assert data["locate"]["input_tokens"] == 100 and data["locate"]["output_tokens"] == 10
    assert data["totals"] == {"model_calls": 2, "input_tokens": 200, "output_tokens": 20}
    assert data["constants"]["provisional"] is True


def test_a_location_answer_that_cannot_be_read_gives_no_decision(toy_repo, tmp_path):
    result = run_gate(GateOptions(request_path=write_request(tmp_path), repo=toy_repo.path),
                      ScriptedDrafter(["no answer"]), ScriptedDrafter([items_answer(["S"])]))
    assert not result.ok and result.data["decision"] is None
    assert [issue["code"] for issue in result.data["errors"]] == ["FORMAT"]
    assert result.data["logs"] is None and result.data["out"] is None


def test_the_input_limit_is_passed_to_the_prompt(toy_repo, tmp_path):
    options = GateOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                          input_limit=10)
    result = run_gate(options, ScriptedDrafter([locate_answer(TOY_LOCATION)]),
                      ScriptedDrafter([items_answer(["S", "R"])]))
    locate = result.data["locate"]
    assert locate["outline_cut"] and locate["over_limit"] and locate["input_limit"] == 10
    assert result.data["constants"]["locate_input_limit"] == 10


def make_plan(tmp_path: Path, toy_repo, request: str, *, draft_attempts: list[WorkerResult]
              ) -> Path:
    """A finished plan's report and draft log, as ``plan`` leaves them."""
    plan = tmp_path / "plans"
    draft_dir = plan / "graph.json.logs" / "draft"
    draft_dir.mkdir(parents=True)
    for number, attempt in enumerate(draft_attempts, start=1):
        write_json_atomic(draft_dir / f"worker_{number}.json", attempt.to_dict())
    report = plan / "graph.json.report.json"
    write_json_atomic(report, {"request": request, "repo": str(toy_repo.path),
                               "base_commit": toy_repo.commit,
                               "draft_items": [{"id": "X", "title": "from the report"}],
                               "estimated_changed_lines": 10,
                               "stage_totals": {"stage1": {"model_calls": 1,
                                                           "input_tokens": 7,
                                                           "output_tokens": 3}}})
    return report


def drafted(ids, **usage) -> WorkerResult:
    return WorkerResult(ok=True, final_answer=items_answer(ids), model_calls=1,
                        input_tokens=usage.get("input_tokens", 5000), output_tokens=50)


def test_from_report_reuses_the_draft_and_reads_the_repository_at_the_base_commit(
        toy_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "AQOURS_ROOT", tmp_path)
    request = tmp_path / "experiments" / "task" / "request.md"
    request.parent.mkdir(parents=True)
    request.write_text(REQUEST, encoding="utf-8")
    report = make_plan(tmp_path, toy_repo, "experiments\\task\\request.md",
                       draft_attempts=[drafted(["S", "R"])])
    commit_files(toy_repo.path, {"later.py": "def later():\n    pass\n"}, "after the plan")
    options = options_from_report(report, tmp_path / "gate.json")
    assert options.request_path == tmp_path / "experiments" / "task" / "request.md"
    assert options.base_commit == toy_repo.commit
    assert options.draft_from == tmp_path / "plans" / "graph.json"
    locator = ScriptedDrafter([locate_answer([
        {"id": "S", "modify": ["store.py", "later.py"]}, {"id": "R", "modify": ["runner.py"]}])])
    result = run_gate(options, locator, ScriptedDrafter([]))  # step 1 must not run
    data = result.data
    assert result.ok and data["from_report"] == str(report)
    assert data["base_commit"] == toy_repo.commit
    assert data["draft"]["source"] == "reused"
    assert data["draft"]["path"].endswith("worker_1.json")
    assert data["draft"]["usage"]["input_tokens"] == 5000
    assert data["totals"] == {"model_calls": 1, "input_tokens": 100, "output_tokens": 10}
    assert "later.py" not in data["level0"]["sizes"]
    assert data["dropped"] == [{"feature": "S", "role": "modify", "path": "later.py",
                                "why": "not in the repository"}]


def test_the_first_readable_draft_attempt_is_reused_then_the_report(toy_repo, tmp_path):
    report = make_plan(tmp_path, toy_repo, "request.md", draft_attempts=[
        WorkerResult(ok=True, final_answer="not json"), drafted(["A", "B"])])
    graph = report.with_name("graph.json")
    draft = reusable_draft(graph)
    assert draft.source == "reused" and draft.path.endswith("worker_2.json")
    assert [item.id for item in draft.items.items] == ["A", "B"]
    for path in (graph.with_name("graph.json.logs") / "draft").glob("worker_*.json"):
        path.unlink()
    draft = reusable_draft(graph)
    assert draft.source == "report" and draft.path == str(report)
    assert [item.id for item in draft.items.items] == ["X"]
    assert draft.usage == {"model_calls": 1, "input_tokens": 7, "output_tokens": 3}
    report.unlink()
    assert reusable_draft(graph) is None


def test_a_relative_request_in_a_report_is_under_the_aqours_root(tmp_path):
    relative = resolve_request("experiments\\taskgraph\\click_batch\\request.md")
    assert relative == gate.AQOURS_ROOT / "experiments" / "taskgraph" / "click_batch" / \
        "request.md"
    assert relative.is_file()  # the click batch request is in this repository
    absolute = tmp_path / "request.md"
    assert resolve_request(str(absolute)) == absolute


@pytest.mark.parametrize(("content", "message"), [
    (None, "plan report not found"),
    ("{not json", "cannot read the plan report"),
    (json.dumps({"request": "r.md"}), "does not name the repo, base_commit"),
])
def test_a_report_that_cannot_be_used_is_an_input_error(tmp_path, content, message):
    report = tmp_path / "graph.json.report.json"
    if content is not None:
        report.write_text(content, encoding="utf-8")
    with pytest.raises(gate.GateError, match=message):
        options_from_report(report)


# ── the gate command ──

def patch_gate_workers(monkeypatch, locate_answers: list[str], draft_answers: list[str]):
    import aqours_code.taskgraph.planner as planner_module
    locator, drafter = ScriptedDrafter(locate_answers), ScriptedDrafter(draft_answers)
    monkeypatch.setattr(gate, "locate_worker", lambda: locator)
    monkeypatch.setattr(planner_module, "draft_worker", lambda: drafter)
    return locator, drafter


def test_gate_command_prints_the_decision_and_writes_the_output(toy_repo, tmp_path,
                                                                monkeypatch, capsys):
    patch_gate_workers(monkeypatch, [locate_answer(TOY_LOCATION)], [items_answer(["S", "R"])])
    out = tmp_path / "gate.json"
    code = main(["gate", str(write_request(tmp_path)), "--repo", str(toy_repo.path),
                 "--out", str(out)])
    output = capsys.readouterr().out
    assert code == 0 and out.is_file()
    assert "features: 2 (draft: step1)" in output
    assert "dropped paths: 1" in output and "R modify nope.py: not in the repository" in output
    assert "decision (provisional thresholds): single: one agent holds it" in output
    assert f"wrote {out}" in output


def test_gate_command_json_and_draft_from(toy_repo, tmp_path, monkeypatch, capsys):
    report = make_plan(tmp_path, toy_repo, "request.md", draft_attempts=[drafted(["S", "R"])])
    patch_gate_workers(monkeypatch, [locate_answer(TOY_LOCATION)], [])
    code = main(["gate", str(write_request(tmp_path)), "--repo", str(toy_repo.path),
                 "--draft-from", str(report.with_name("graph.json")), "--json"])
    # the scripted worker runs in this process and prints the runtime's hook lines first
    lines = capsys.readouterr().out.splitlines()
    data = json.loads("\n".join(lines[lines.index("{"):]))
    assert code == 0 and data["draft"]["source"] == "reused" and data["decision"] == SINGLE


def test_gate_command_from_report(toy_repo, tmp_path, monkeypatch, capsys):
    request = write_request(tmp_path)
    report = make_plan(tmp_path, toy_repo, str(request), draft_attempts=[drafted(["S", "R"])])
    patch_gate_workers(monkeypatch, [locate_answer(TOY_LOCATION)], [])
    assert main(["gate", "--from-report", str(report)]) == 0
    assert "features: 2 (draft: reused from" in capsys.readouterr().out


def test_gate_command_without_a_decision_exits_1(toy_repo, tmp_path, monkeypatch, capsys):
    patch_gate_workers(monkeypatch, ["nothing"], [items_answer(["S"])])
    code = main(["gate", str(write_request(tmp_path)), "--repo", str(toy_repo.path)])
    output = capsys.readouterr().out
    assert code == 1 and "decision: none" in output and "error: [FORMAT]" in output


@pytest.mark.parametrize(("flags", "message"), [
    (["--from-report", "r.json", "--repo", "."], "give no request, --repo or --draft-from"),
    (["--repo", "."], "give a request and --repo, or --from-report"),
    (["--from-report", "missing.report.json"], "plan report not found"),
    (["missing.md", "--repo", "."], "request file not found"),
    (["--timeout", "0", "--repo", "."], "--timeout must be > 0"),
])
def test_gate_command_refuses_inputs_it_cannot_use(tmp_path, monkeypatch, capsys, flags,
                                                   message):
    monkeypatch.chdir(tmp_path)
    assert main(["gate", *flags]) == 2
    assert message in capsys.readouterr().err


# ── the offline evaluation ──

@pytest.fixture(scope="module")
def evaluate():
    spec = importlib.util.spec_from_file_location("gate_evaluate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


PATCH = """\
diff --git a/pkg/a.py b/pkg/a.py
index 1..2 100644
--- a/pkg/a.py
+++ b/pkg/a.py
diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
diff --git a/pkg/old.py b/pkg/old.py
deleted file mode 100644
diff --git a/pkg/before.py b/pkg/after.py
similarity index 90%
rename from pkg/before.py
rename to pkg/after.py
diff --git a/tests/test_a.py b/tests/test_a.py
diff --git a/README.md b/README.md
"""


def test_the_files_of_a_patch_and_of_a_plan(evaluate):
    assert evaluate.diff_files(PATCH) == {"pkg/a.py": "modified", "pkg/new.py": "new",
                                          "pkg/old.py": "deleted", "pkg/after.py": "modified"}
    graph = {"nodes": [
        {"edit_set": {"modify": ["pkg/a.py", "tests/test_a.py"], "create": ["pkg/new.py"]}},
        {"edit_set": {"modify": ["pkg/new.py", "README.md"], "create": ["pkg/c.py"]}}]}
    assert evaluate.plan_files(graph) == {"pkg/a.py": "modify", "pkg/new.py": "modify",
                                          "pkg/c.py": "create"}


def test_recall_counts_the_reference_files_the_gate_found(evaluate):
    reference = {"a.py": "modify", "b.py": "modify", "c.py": "modify", "n.py": "create"}
    result = evaluate.hits({"a.py", "b.py", "x.py"}, reference)
    assert result == {"reference": 4, "new": 1, "gate": 3, "hits": 2, "recall": 0.5,
                      "recall_existing": pytest.approx(2 / 3), "extra": ["x.py"],
                      "missed": ["c.py", "n.py"]}
    assert evaluate.hits(set(), {})["recall"] is None


def gate_output(features: list[FeatureFiles], tokens: dict[str, int]) -> dict:
    """The decision fields of a gate output (what the evaluation reads)."""
    return decide(features, tokens)


def test_the_sensitivity_table_decides_again_for_each_p_and_file_mode(evaluate):
    features = [FeatureFiles("A", "a", ["a.py"], ["big.py"]),
                FeatureFiles("B", "b", ["b.py"], ["big.py"])]
    data = gate_output(features, {"a.py": 4000, "b.py": 4000, "big.py": 20_000})
    table = evaluate.sensitivity(data)
    assert set(table) == {(p, mode) for p in (0, 2000, 5000, 10000)
                          for mode in ("modify+reference", "modify only")}
    # with the reference: M = 8000 + 28000 + 2P, and each feature's group holds
    # 24000 + P of it (reading big.py does not connect A and B)
    assert table[(0, "modify+reference")] == SINGLE  # M = 36000 <= W
    assert table[(2000, "modify+reference")] == SPLIT  # M = 40000, groups 0.65
    assert table[(10000, "modify+reference")] == SPLIT  # M = 56000, groups 0.61
    # modified files only: M = 8000 + 8000 + 2P
    assert table[(5000, "modify only")] == SINGLE  # M = 26000 <= W
    assert table[(10000, "modify only")] == SINGLE  # M = 36000 <= W


def test_evaluate_task_and_the_tables(evaluate, tmp_path):
    plan = tmp_path / "plan" / "graph.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps({"nodes": [
        {"edit_set": {"modify": ["pkg/a.py"], "create": ["pkg/new.py"]}}]}), encoding="utf-8")
    run = tmp_path / "run1"
    (run / "nodes" / "solve").mkdir(parents=True)
    (run / "nodes" / "solve" / "diff.patch").write_text(PATCH, encoding="utf-8")
    task = evaluate.Task("toy", plan, (run, tmp_path / "missing-run"), "split", "v3 1M, single 2M")
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir()
    data = gate_output([FeatureFiles("A", "a", ["pkg/a.py", "pkg/x.py"]),
                        FeatureFiles("B", "b", ["pkg/b.py"])],
                       {"pkg/a.py": 30_000, "pkg/x.py": 1000, "pkg/b.py": 30_000})
    (gate_dir / "toy.json").write_text(json.dumps(data), encoding="utf-8")
    row = evaluate.evaluate_task(task, gate_dir)
    # M = 8000 + 61000 + 2 x 5000 = 79000; groups 36000 (0.46) and 35000 (0.44)
    assert (row["decision"], row["reason_code"]) == (SPLIT, "split")
    against_plan, against_run, missing = row["hits"]
    assert (against_plan["recall"], against_plan["recall_existing"]) == (0.5, 1.0)
    assert against_plan["extra"] == ["pkg/b.py", "pkg/x.py"]
    assert against_run["reference"] == 4 and against_run["hits"] == 1
    assert missing["note"].startswith("cannot read")
    assert row["sensitivity"]["P=5000 modify+reference"] == SPLIT
    absent = evaluate.evaluate_task(evaluate.Task("none", plan, (), "single", "-"), gate_dir)
    assert absent["gate_note"] == "no gate output (not found)"
    text = evaluate.render([row, absent])
    for part in ("## 1. File hits", "## 2. Decision", "## 3. Sensitivity",
                 "| toy | plan | 2 (1) | 3 | 1 | 0.50 | 1.00 | 2 |",
                 "P=5000 modify+reference (default)", "| none | - |"):
        assert part in text
    assert "| toy | split | M = 79,000 > W = 37,000" in text
    assert "| v3 1M, single 2M | split | yes |" in text


def test_evaluate_main_writes_the_tables(evaluate, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(evaluate, "TASKS", ())
    out = tmp_path / "evaluation.md"
    assert evaluate.main(["--gate-dir", str(tmp_path), "--out", str(out),
                          "--json", str(tmp_path / "rows.json")]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Split gate: offline evaluation")
    assert json.loads((tmp_path / "rows.json").read_text(encoding="utf-8")) == []
    assert f"wrote {out}" in capsys.readouterr().out
