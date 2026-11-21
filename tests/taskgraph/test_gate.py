"""The split gate with scripted model answers: search, location, decision, CLI, evaluation."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import aqours_code.taskgraph.gate as gate
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.gate import (B0, CLEAR_SINGLE, CLEAR_SPLIT, COMPACT_TRIGGER_ENV,
                                        MAX_REFERENCE, SINGLE, SPLIT, UNCLEAR,
                                        W_AT_DEFAULT_TRIGGER, FeatureFiles, FeatureSearch,
                                        GateOptions, SearchHit, Window, agent_window,
                                        build_locate_prompt, decide, estimate_tokens,
                                        extract_identifiers, feature_groups, features_from_gate,
                                        options_from_report, parse_locations, resolve_request,
                                        reusable_draft, run_gate, search_feature,
                                        window_for_trigger)
from aqours_code.taskgraph.outline import _Entry, _File, _read, is_source_file
from aqours_code.taskgraph.planner import DraftError, DraftItem
from aqours_code.taskgraph.repo_index import RepoIndex
from aqours_code.taskgraph.workers import WorkerResult, write_json_atomic
from taskgraph_support import commit_files
from test_planner import REQUEST, ScriptedDrafter, answer, items_answer

FILE_TOOLS = {"read_file", "glob", "grep", "bash", "write_file", "edit_file"}
SCRIPT = (Path(__file__).resolve().parents[2] / "experiments" / "taskgraph" / "gate"
          / "evaluate.py")
REAL_ROOT = gate.AQOURS_ROOT
W = W_AT_DEFAULT_TRIGGER
DEFAULT_WINDOW = Window(W_AT_DEFAULT_TRIGGER, 80_000, "default")


@pytest.fixture(autouse=True)
def no_compaction_setting(monkeypatch, tmp_path):
    """W from the default trigger: no setting in the environment or in a .env."""
    monkeypatch.delenv(COMPACT_TRIGGER_ENV, raising=False)
    monkeypatch.setattr(gate, "AQOURS_ROOT", tmp_path / "aqours-root")


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

def test_m_is_b0_plus_every_involved_file_once():
    features = [FeatureFiles("A", "a", ["a.py"], ["r.py"]),
                FeatureFiles("B", "b", ["b.py"], ["r.py"])]
    tokens = {"a.py": 10_000, "b.py": 20_000, "r.py": 5_000}
    result = decide(features, tokens)
    assert result["material"] == {"M": B0 + 35_000, "B0": B0, "code": 35_000, "files": 3,
                                  "W": W, "half_W": W // 2}
    assert [feature["tokens"] for feature in result["features"]] == [15_000, 25_000]
    assert result["features"][0]["reference"] == [{"path": "r.py", "tokens": 5_000}]
    modify_only = decide(features, tokens, with_reference=False)
    assert modify_only["material"]["M"] == B0 + 30_000


def test_the_concentration_is_the_largest_group_of_features_over_all_features():
    features = [FeatureFiles("A", "a", ["x.py", "pkg/__init__.py"]),
                FeatureFiles("B", "b", ["x.py", "y.py"]),
                FeatureFiles("C", "c", ["y.py", "tests/test_c.py"]),
                FeatureFiles("D", "d", ["pkg/__init__.py", "tests/test_c.py", "README.md"]),
                FeatureFiles("E", "e", ["README.md"], ["x.py", "y.py"])]
    # __init__.py, test files, other files and files only read connect nothing
    assert feature_groups(features) == [[0, 1, 2], [3], [4]]
    result = decide(features, {"x.py": 50_000, "y.py": 50_000})
    assert result["groups"] == [{"features": ["A", "B", "C"], "shared_files": ["x.py", "y.py"]},
                                {"features": ["D"], "shared_files": []},
                                {"features": ["E"], "shared_files": []}]
    assert result["shared"] is True
    assert result["concentration"] == {"value": 0.6, "largest_group": ["A", "B", "C"],
                                       "of": "3/5"}
    # E reads x.py and y.py, which A, B and C modify: still counted once in M
    assert result["material"]["M"] == B0 + 100_000


def files(*modified: str) -> list[FeatureFiles]:
    """One feature per argument, modifying the files named in it (``"a.py b.py"``)."""
    return [FeatureFiles(str(number), str(number), text.split())
            for number, text in enumerate(modified, start=1)]


HALF = 20_000  # W/2 for W = 40000 in these cases
ZONES = [
    # M <= W/2: clear single, whatever the concentration
    ("M == W/2", files("a.py", "b.py"), {"a.py": 6000, "b.py": 6000}, CLEAR_SINGLE, SINGLE),
    # just over W/2, at most W: unclear, single by default
    ("M == W/2 + 1", files("a.py", "b.py"), {"a.py": 6000, "b.py": 6001}, UNCLEAR, SINGLE),
    ("M == W, nothing shared", files("a.py", "b.py"), {"a.py": 16_000, "b.py": 16_000},
     UNCLEAR, SINGLE),
    # over W and nothing shared: clear split
    ("M == W + 1, nothing shared", files("a.py", "b.py"), {"a.py": 16_000, "b.py": 16_001},
     CLEAR_SPLIT, SPLIT),
    # concentration exactly 3/4: clear single even far over W
    ("concentration 3/4", files("core.py", "core.py", "core.py", "d.py"),
     {"core.py": 90_000, "d.py": 1000}, CLEAR_SINGLE, SINGLE),
    # over W, shared, concentration exactly 1/2: clear split
    ("concentration 1/2", files("core.py", "core.py", "c.py", "d.py"),
     {"core.py": 31_000, "c.py": 1000, "d.py": 1000}, CLEAR_SPLIT, SPLIT),
    # over W, shared, concentration between 1/2 and 3/4: unclear
    ("concentration 2/3", files("core.py", "core.py", "c.py"),
     {"core.py": 30_000, "c.py": 10_000}, UNCLEAR, SINGLE),
    # one feature: concentration 1
    ("one feature", files("a.py"), {"a.py": 100_000}, CLEAR_SINGLE, SINGLE),
]


@pytest.mark.parametrize(("case", "features", "tokens", "zone", "decision"), ZONES,
                         ids=[case[0] for case in ZONES])
def test_the_three_zones_and_their_boundaries(case, features, tokens, zone, decision):
    result = decide(features, tokens, w=2 * HALF)
    assert (result["zone"], result["decision"]) == (zone, decision)
    assert result["material"]["half_W"] == HALF


def test_each_zone_says_why():
    w = 2 * HALF
    reasons = {case: decide(features, tokens, w=w)["reason"]
               for case, features, tokens, _, _ in ZONES}
    assert reasons["M == W/2"] == "M = 20,000 <= W/2 = 20,000: one agent holds it"
    assert reasons["concentration 3/4"] == (
        "the largest group holds 3 of 4 features (0.75), at least 3/4: the features "
        "concentrate on shared files")
    assert reasons["M == W + 1, nothing shared"] == (
        "M = 40,001 > W = 40,000 and no two features share a modified file")
    assert reasons["concentration 1/2"] == (
        "M = 41,000 > W = 40,000 and the largest group holds 2 of 4 features (0.50), "
        "at most 1/2")
    assert reasons["M == W/2 + 1"].startswith(
        "between the clear zones (W/2 = 20,000 < M = 20,001 <= W = 40,000; the largest group "
        "holds 1 of 2 features (0.50)): single by default")
    assert "M = 48,000 > W = 40,000; the largest group holds 2 of 3 features (0.67)" in \
        reasons["concentration 2/3"]


def test_a_file_over_w_is_oversized_without_changing_the_decision():
    tokens = {"a.py": W + 1, "b.py": W, "c.py": 10}
    result = decide(files("a.py", "b.py", "c.py"), tokens)
    assert result["oversized"] == [{"path": "a.py", "tokens": W + 1}]
    assert (result["zone"], result["decision"]) == (CLEAR_SPLIT, SPLIT)


def test_features_and_tokens_are_read_back_from_a_gate_output():
    features = [FeatureFiles("A", "a", ["a.py"], ["r.py"]), FeatureFiles("B", "b", ["b.py"])]
    data = decide(features, {"a.py": 7, "b.py": 8, "r.py": 9})
    again, tokens = features_from_gate(data)
    assert again == features and tokens == {"a.py": 7, "b.py": 8, "r.py": 9}


# ── W from the compaction trigger ──

def test_w_scales_with_the_compaction_trigger():
    assert window_for_trigger(80_000) == 37_000
    assert window_for_trigger(120_000) == 55_500
    assert window_for_trigger(100_000) == 46_250
    assert window_for_trigger(40_000) == 18_500


def test_the_trigger_comes_from_the_environment_then_the_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    missing = tmp_path / "missing.env"
    assert agent_window({}, missing) == Window(37_000, 80_000, "default")
    assert agent_window({COMPACT_TRIGGER_ENV: "120000"}, missing) == \
        Window(55_500, 120_000, "environment")
    dotenv.write_text(f"# {COMPACT_TRIGGER_ENV}=1\nAQOURS_CODE_MODEL=x\n"
                      f'export {COMPACT_TRIGGER_ENV}="60000"\n', encoding="utf-8")
    assert agent_window({}, dotenv) == Window(27_750, 60_000, ".env")
    # the environment wins, as in aqours_code.config (load_dotenv(override=False))
    assert agent_window({COMPACT_TRIGGER_ENV: "100000"}, dotenv).trigger == 100_000


@pytest.mark.parametrize("value", ["abc", "0", "-5", ""])
def test_a_trigger_that_is_not_a_positive_number_counts_as_the_default(tmp_path, value):
    window = agent_window({COMPACT_TRIGGER_ENV: value}, tmp_path / "missing.env")
    assert (window.tokens, window.trigger, window.source) == (37_000, 80_000, "default")
    assert "is not a positive whole number; 80000 was used" in window.note


def test_the_default_window_reads_the_dotenv_of_the_aqours_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text(f"{COMPACT_TRIGGER_ENV}=40000 # smaller\n", encoding="utf-8")
    monkeypatch.setattr(gate, "AQOURS_ROOT", root)
    assert agent_window() == Window(18_500, 40_000, ".env")
    assert Window(18_500, 40_000, ".env").to_dict() == {
        "W": 18_500, "half_W": 9_250, "compact_trigger_tokens": 40_000,
        "trigger_source": ".env", "note": None}


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
    assert data["material"] == {"M": B0 + code, "B0": B0, "code": code, "files": 3, "W": W,
                                "half_W": W // 2}
    assert data["window"] == {"W": W, "half_W": W // 2, "compact_trigger_tokens": 80_000,
                              "trigger_source": "default", "note": None}
    assert (data["decision"], data["zone"]) == (SINGLE, CLEAR_SINGLE)
    assert data["concentration"] == {"value": 0.5, "largest_group": ["S"], "of": "1/2"}
    assert data["shared"] is False
    assert data["dropped"] == [{"feature": "R", "role": "modify", "path": "nope.py",
                                "why": "not in the repository"}]
    assert data["locate"]["input_tokens"] == 100 and data["locate"]["output_tokens"] == 10
    assert data["totals"] == {"model_calls": 2, "input_tokens": 200, "output_tokens": 20}
    assert data["constants"]["fixed"] is True
    assert data["constants"]["clear_single_concentration"] == "3/4"
    assert data["gate_version"] == "gate-v2"


def test_the_gate_takes_w_from_the_compaction_trigger(toy_repo, tmp_path, monkeypatch):
    monkeypatch.setenv(COMPACT_TRIGGER_ENV, "120000")
    result = run_gate(GateOptions(request_path=write_request(tmp_path), repo=toy_repo.path),
                      ScriptedDrafter([locate_answer(TOY_LOCATION)]),
                      ScriptedDrafter([items_answer(["S", "R"])]))
    window = result.data["window"]
    assert (window["W"], window["compact_trigger_tokens"], window["trigger_source"]) == \
        (55_500, 120_000, "environment")
    assert result.data["material"]["W"] == 55_500
    given = run_gate(GateOptions(request_path=write_request(tmp_path), repo=toy_repo.path,
                                 window=Window(10, 20, "test")),
                     ScriptedDrafter([locate_answer(TOY_LOCATION)]),
                     ScriptedDrafter([items_answer(["S", "R"])]))
    # S and R modify different files: over this W of 10 tokens they are split
    assert given.data["material"]["W"] == 10 and given.data["zone"] == CLEAR_SPLIT


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


def test_a_relative_request_in_a_report_is_under_the_aqours_root(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "AQOURS_ROOT", REAL_ROOT)
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
    assert "W = 37,000 (compaction trigger 80,000 from the default); W/2 = 18,500" in output
    assert "concentration: 1/2 = 0.50" in output
    assert "decision: single (clear_single): M = " in output
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


def gate_output(features: list[FeatureFiles], tokens: dict[str, int], w: int = W) -> dict:
    """The fields of a gate output that the evaluation reads."""
    return {**decide(features, tokens, w=w),
            "window": {"W": w, "half_W": w / 2, "compact_trigger_tokens": 0,
                       "trigger_source": "test", "note": None}}


def test_outcome_leaves_the_unclear_zone_uncounted(evaluate):
    assert evaluate.outcome(SINGLE, UNCLEAR, "split") == "default"
    assert evaluate.outcome(SINGLE, UNCLEAR, "single") == "default"
    assert evaluate.outcome(SPLIT, CLEAR_SPLIT, "either") == "either is fine"
    assert evaluate.outcome(SPLIT, CLEAR_SPLIT, "split") == "right"
    assert evaluate.outcome(SINGLE, CLEAR_SINGLE, "split") == "wrong"


def write_plan(tmp_path: Path, graph: dict, report: dict | None = None) -> Path:
    plan = tmp_path / "plan" / "graph.json"
    plan.parent.mkdir(exist_ok=True)
    plan.write_text(json.dumps(graph), encoding="utf-8")
    if report is not None:
        plan.with_name("graph.json.report.json").write_text(json.dumps(report),
                                                            encoding="utf-8")
    return plan


def test_evaluate_gate_outputs_hits_and_zones_for_both_materials(evaluate, tmp_path):
    plan = write_plan(tmp_path, {"nodes": [
        {"edit_set": {"modify": ["pkg/a.py"], "create": ["pkg/new.py"]}}]})
    run = tmp_path / "run1"
    (run / "nodes" / "solve").mkdir(parents=True)
    (run / "nodes" / "solve" / "diff.patch").write_text(PATCH, encoding="utf-8")
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir()
    split_case = [FeatureFiles("A", "a", ["pkg/a.py", "pkg/x.py"], ["pkg/r.py"]),
                  FeatureFiles("B", "b", ["pkg/b.py"])]
    tokens = {"pkg/a.py": 30_000, "pkg/x.py": 1000, "pkg/b.py": 30_000, "pkg/r.py": 4000}
    (gate_dir / "toy.json").write_text(json.dumps(gate_output(split_case, tokens)),
                                       encoding="utf-8")
    # the W the gate recorded counts: with W = 200000, M = 73000 <= W/2
    (gate_dir / "wide.json").write_text(json.dumps(gate_output(split_case, tokens, 200_000)),
                                        encoding="utf-8")
    middle = [FeatureFiles("C", "c", ["pkg/c.py"], ["pkg/big.py"]),
              FeatureFiles("D", "d", ["pkg/d.py"])]
    (gate_dir / "mid.json").write_text(json.dumps(gate_output(
        middle, {"pkg/c.py": 15_000, "pkg/big.py": 20_000})), encoding="utf-8")
    toy = evaluate.evaluate_gate_task(
        evaluate.Task("toy", plan, (run, tmp_path / "missing-run"), "split", "v3 1M, single 2M"),
        gate_dir)
    modes = toy["modes"]
    assert (modes["modify+reference"]["M"], modes["modify only"]["M"]) == (73_000, 69_000)
    assert [(mode["decision"], mode["zone"], mode["outcome"]) for mode in modes.values()] == \
        [(SPLIT, CLEAR_SPLIT, "right")] * 2
    against_plan, against_run, missing = toy["hits"]
    assert (against_plan["recall"], against_plan["recall_existing"]) == (0.5, 1.0)
    assert against_plan["extra"] == ["pkg/b.py", "pkg/x.py"]
    assert against_run["reference"] == 4 and against_run["hits"] == 1
    assert missing["note"].startswith("cannot read")
    wide = evaluate.evaluate_gate_task(evaluate.Task("wide", plan, (), "split", "-"), gate_dir)
    assert wide["W"] == 200_000 and wide["modes"]["modify+reference"]["zone"] == CLEAR_SINGLE
    mid = evaluate.evaluate_gate_task(evaluate.Task("mid", plan, (), "split", "-"), gate_dir)
    # with the reference M = 43000 > W, nothing shared: split; without it 23000: unclear
    assert mid["modes"]["modify+reference"]["outcome"] == "right"
    assert (mid["modes"]["modify only"]["zone"], mid["modes"]["modify only"]["outcome"]) == \
        (UNCLEAR, "default")
    absent = evaluate.evaluate_gate_task(evaluate.Task("none", plan, (), "single", "-"),
                                         gate_dir)
    assert absent["note"] == "no gate output (not found)"
    text = evaluate.render([toy, mid, absent], ideal=False)
    for part in ("# Split gate evaluation: gate outputs", "## File hits", "## Decision",
                 "## Numbers", "| toy | plan | 2 (1) | 3 | 1 | 0.50 | 1.00 | 2 |",
                 "| toy | v3 1M, single 2M | split | split (clear_split) | right | "
                 "split (clear_split) | right |",
                 "| mid | - | split | split (clear_split) | right | single (unclear) | default |",
                 "- modify+reference: right 2, wrong 0, default 0, either is fine 0",
                 "- modify only: right 1, wrong 0, default 1, either is fine 0 "
                 "(default: not counted)",
                 "| toy | 2 | 1/2 = 0.50 | 73,000 | 69,000 | 18,500 | 37,000 | - |",
                 "- toy: no two features share a modified file", "- none: no gate output"):
        assert part in text
    assert "Sensitivity" not in text and "P=" not in text


def test_the_ideal_location_is_the_full_plans_nodes_without_pre_and_reg(evaluate):
    graph = {"nodes": [
        {"id": "P0", "edit_set": {"modify": ["models.py"]}},
        {"id": "S", "title": "s", "edit_set": {"modify": ["store.py", "tests/test_basic.py"],
                                               "create": ["tests/test_new.py"]},
         "context_files": ["SPEC.md#Cancel", "store.py", "models.py", "runner.py", "broken.py",
                           "README.md", "models.py", "extra.py"]},
        {"id": "R", "edit_set": {"modify": ["runner.py", "later.py"]},
         "context_files": ["store.py#JobStore"]},
        {"id": "REG", "edit_set": {"modify": ["runner.py"]}},
    ]}
    features = evaluate.ideal_features(graph, {"pre_node": "P0", "reg_node": None})
    assert features == [
        FeatureFiles("S", "s", ["store.py"], ["models.py", "runner.py", "broken.py"]),
        FeatureFiles("R", "", ["runner.py", "later.py"], ["store.py"])]
    # without a report, PRE and REG are still left out by their names
    assert [feature.id for feature in evaluate.ideal_features(graph)] == ["P0", "S", "R"]


def test_evaluate_ideal_reads_the_sizes_at_the_base_commit(evaluate, toy_repo, tmp_path):
    from taskgraph_support import TOY_FILES
    graph = {"repo": str(toy_repo.path), "base_commit": toy_repo.commit, "nodes": [
        {"id": "PRE", "edit_set": {"modify": ["models.py"]}},
        {"id": "S", "edit_set": {"modify": ["store.py"]}, "context_files": ["models.py"]},
        {"id": "R", "edit_set": {"modify": ["runner.py", "later.py"]}},
    ]}
    plan = write_plan(tmp_path, graph, {"pre_node": "PRE"})
    commit_files(toy_repo.path, {"store.py": "x = 1\n" * 10_000, "later.py": "y = 2\n"},
                 "after the plan")
    row = evaluate.evaluate_ideal_task(evaluate.Task("toy", plan, (), "single", "-"),
                                       DEFAULT_WINDOW)
    size = {name: estimate_tokens(len(TOY_FILES[name])) for name in TOY_FILES}
    modes = row["modes"]
    assert modes["modify+reference"]["M"] == B0 + size["store.py"] + size["models.py"] + \
        size["runner.py"]
    assert modes["modify only"]["M"] == B0 + size["store.py"] + size["runner.py"]
    assert row["missing_at_base"] == ["later.py"]
    assert [(mode["zone"], mode["outcome"]) for mode in modes.values()] == \
        [(CLEAR_SINGLE, "right")] * 2
    assert row["location"] == [{"id": "S", "modify": ["store.py"], "reference": ["models.py"]},
                               {"id": "R", "modify": ["runner.py", "later.py"],
                                "reference": []}]
    absent = evaluate.evaluate_ideal_task(
        evaluate.Task("none", tmp_path / "missing.json", (), "split", "-"), DEFAULT_WINDOW)
    assert absent["note"].startswith("no plan (not found)")
    text = evaluate.render([row, absent], ideal=True, window=DEFAULT_WINDOW)
    for part in ("# Split gate evaluation: ideal location",
                 "W = 37,000 (compaction trigger 80,000 from the default); W/2 = 18,500.",
                 "| toy | - | single | single (clear_single) | right | single (clear_single) | "
                 "right |",
                 "- toy: not at the base commit (0 tokens): later.py",
                 "- none: no plan (not found)"):
        assert part in text
    assert "## File hits" not in text


def test_evaluate_main_writes_the_ideal_tables_then_appends_the_gate_tables(
        evaluate, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(evaluate, "TASKS", ())
    out = tmp_path / "evaluation.md"
    assert evaluate.main(["--ideal", "--out", str(out),
                          "--json", str(tmp_path / "ideal.json")]) == 0
    assert evaluate.main(["--gate-dir", str(tmp_path), "--out", str(out), "--append"]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Split gate evaluation: ideal location")
    assert text.index("# Split gate evaluation: gate outputs") > 0
    assert json.loads((tmp_path / "ideal.json").read_text(encoding="utf-8")) == []
    printed = capsys.readouterr().out
    assert f"wrote {out}" in printed and f"added to {out}" in printed
