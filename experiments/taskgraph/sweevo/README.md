# SWE-EVO tasks

Nine instances of [SWE-EVO](https://github.com/SWE-EVO/SWE-EVO) (MIT
licence), run with the task-graph harness. A SWE-EVO instance is one real
release of a Python project: the agent gets the repository at the old version
and the release notes, and must make the release's changes, which touch
several modules. It is graded with SWE-bench-style test ids:
`FAIL_TO_PASS` (F2P) tests fail before the release and pass after it,
`PASS_TO_PASS` (P2P) tests pass both times.

Only code, the list of instances, the exclusion lists and the validation
reports are here. The dataset, the task directories (repositories, grading
data) and the Docker images stay outside the repository.

```text
README.md            this file
instances.txt        the nine instance ids; "pilot" marks the pilot pair, "dropped" one left out
prepare.py           writes the task directories and builds the task images
validate_gold.py     grades the base and the gold patch; writes the exclusion lists
validate_interface.py   checks the interface sections (size, test imports, no body or test text)
clean_tasks.py       writes the cleaned (v2) tasks, see "Cleaned tasks (v2)"
instances/<id>/sandbox_skips.txt   tests left out of every method's score
validation/<id>.json               validation counts, times and image sizes
validation/README.md               the validation table
validation/interface.md            the interface table (and interface.json)
```

| Instance | F2P | P2P |
| --- | --- | --- |
| iterative__dvc_1.1.7_1.1.8 | 8 | 82 |
| dask__dask_2023.6.1_2023.7.0 | 5 | 707 |
| iterative__dvc_1.6.3_1.6.4 | 9 | 2 |
| dask__dask_2023.8.0_2023.8.1 (pilot) | 11 | 2,796 |
| iterative__dvc_0.89.0_0.90.0 (pilot) | 27 | 282 |
| iterative__dvc_3.43.1_3.44.0 (**dropped**, see below) | 22 | 104 |
| iterative__dvc_2.19.0_2.20.0 | 14 | 66 |
| pydantic__pydantic_v2.7.0_v2.7.1 | 234 | 1,477 |
| dask__dask_2023.6.0_2023.6.1 | 105 | 3,415 |

## Preparing the tasks

```bash
git clone https://github.com/SWE-EVO/SWE-EVO C:/tg/sweevo
pip install pyarrow            # to read the dataset (hf_out/hf_dataset/test)
python experiments/taskgraph/sweevo/prepare.py            # all nine; --pilot for two
```

For each instance, `prepare.py` writes `C:\tg\sweevo-tasks\<id>\`:

| Path | What |
| --- | --- |
| `repo/` | the repository at `base_commit`, fetched from GitHub (`git fetch --depth 1`) and committed once with a fixed author and date: one commit, no history, LF line endings, the test patch not applied |
| `request.md` | the problem statement (the release notes), verbatim, inside SWE-EVO's SWE-agent instruction (below) |
| `request_iface.md` | `request.md` plus the section "Interface of this release" (below) |
| `sandbox.json` | the image and environment of every container (below) |
| `single.json` | the single-agent graph, with the smoke checks (below) |
| `single_iface.json` | the same graph with `request_iface.md` (`request_id` `single_iface`) |
| `Dockerfile` | the task image |
| `sandbox_skips.txt` | a copy of `instances/<id>/sandbox_skips.txt` |
| `grading/instance.json` | ids, the test command, the log parser, F2P and P2P |
| `grading/test.patch`, `grading/gold.patch` | the release's test changes, and its source changes (for validation only) |

Workers see only a clone of `repo/`: the coordinator clones it into the run
directory, a worker's file tools stay in its worktree, and its container
mounts only that worktree. Nothing under `grading/` can reach a worker.

**The request.** The text of `instance_template` in SWE-EVO's
`SWE-agent/config/default.yaml`, word for word, with two tool-specific parts
left out: the `<uploaded_files>` block, and the directory placeholder
(`{{working_dir}}`, `/testbed` there), which becomes "the current
directory", because our worker's workspace is the repository. It names no
test file, test id or patch. The template says the test files are already
taken care of; the `single` graph's goal still ends with the harness's own
"Keep the existing tests passing and add tests for the new behaviour."
Tests a worker adds do no harm: grading puts the files of the test patch
back to the base and applies the patch over them.

## The interface variant (`request_iface.md`)

A release's tests use names the release adds. The single agent's pilot run on
dvc 0.89 implemented the release notes and still scored F2P 6/27: 19 of the
F2P tests import `HTTPURLInfo` from `dvc.path_info`, a class whose name only
the gold patch has (the notes say "import-url: allow queries in URL"), and 2
call `is_protected`, a method only the gold patch defines. Such a score
measures whether the agent guessed internal names. So each task also has a
variant of its request that ends with the interface the release must
implement, as FeatureBench gives one: names and signatures, no bodies, by a
fixed rule (`aqours_code/taskgraph/sweevo_interface.py`):

- for every non-test `.py` file the gold patch touches, the public
  definitions at module level and class level of the base and of the base
  plus the gold patch are compared with `ast` (nested functions are never
  read);
- *added*: classes (with their bases and public methods), functions,
  methods, and names assigned at module level (the name only); *changed*:
  functions and methods whose signature differs (the new one is shown);
  *removed*: classes, functions and methods;
- a name is public if neither it nor its class starts with `_`; the dunder
  methods of a new class count;
- each added or changed item shows its decorators, its signature as written
  in the gold patch (defaults and annotations included) and its docstring (at
  most 15 lines), as a stub whose body is `...`; a file that does not parse
  is reported in the section.

The section, after the request:

````text
## Interface of this release

The release adds, changes or removes the public definitions below. Use exactly
these names and signatures, because other code relies on them. How they work
inside is up to you.

### `dvc/path_info.py`

```python
# added: class HTTPURLInfo
class HTTPURLInfo(URLInfo):
    def __init__(self, url):
        ...
...
```
````

`single_iface.json` is `single.json` with this request; the planner and team
are to be given `request_iface.md` the same way. `request.md` and
`single.json` are unchanged.

`validate_interface.py` (no model, no Docker) checks the sections of the
eight instances in use and writes [`validation/interface.md`](validation/interface.md):
their size; that every name the release's test files take from the package
(`from pkg.mod import Name`, or `pkg.mod.Name` on an imported module) exists
in the base commit or is an item of the section (through a re-export too);
that no line of a section is a line the gold patch adds in a body (outside
headers, decorators and docstrings); and that no test file path, test name or
line of the test patch is in a section. All eight pass with no miss. What the
import check cannot see: a method called on an object (`remote.is_protected`)
and a name reached through a local variable that shadows a module alias.

## The container: task image and `sandbox.json`

Workers, their checks and the grading all run in the instance's image, with
the code under test (a worktree, or the graded copy) mounted at
`/workspace`, through the harness's ordinary sandbox: no network, read-only
root file system, non-root user (`10001:10001` on Windows), 2 CPUs, 2 GiB,
`/tmp` a 64 MB `noexec` tmpfs (the limits of open files and processes are
raised, below). `run`, `team` and `rescore` take the task's `sandbox.json`
with `--sandbox-config`; nothing changes for other tasks.

The SWE-bench images keep the repository at `/testbed`, installed in
editable mode in the conda environment `testbed`
(`/opt/miniconda3/envs/testbed`). `sandbox.json` makes the worktree the
code under test:

```json
{"image": "aqours-sweevo:<id>",
 "env": {"PATH": "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
         "CONDA_PREFIX": "/opt/miniconda3/envs/testbed", "CONDA_DEFAULT_ENV": "testbed",
         "PYTHONPATH": "/workspace", "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_INDEX": "1",
         "MPLCONFIGDIR": "/tmp/matplotlib", "XDG_CACHE_HOME": "/tmp/cache",
         "XDG_CONFIG_HOME": "/tmp/config"},
 "nofile": 4096, "pids": 4096, "git_view": true}
```

- `PATH` puts the testbed environment first, so `python` and `pytest` are
  its own (`/opt/miniconda3/envs/testbed/bin/python`; pytest 8.3.2 for dask,
  7.4.4 for dvc 0.89). The variables are set with `docker run --env`, and
  the login shell of `docker exec /bin/sh -lc` keeps them.
- `PYTHONPATH=/workspace` puts the worktree on `sys.path` before
  site-packages. The images install the package with a PEP 660 editable
  finder (`__editable___dask_2023_8_0_finder.py`), which is appended to
  `sys.meta_path` and so only answers for what the path finder does not
  find: `import dask` loads `/workspace/dask/__init__.py`. Shown in the
  container, the smoke check prints it every time (`python -c "import dask;
  print(dask.__file__)"` gives `/workspace/dask/__init__.py`; `sys.path[:2]`
  is `['', '/workspace']`). The installed metadata stays that of the base
  (`importlib.metadata.version("dask")` is `2023.8.0`).
- Writes: pytest's cache and `__pycache__` would go to `/workspace` (the
  grading uses a scratch copy); `HOME` is `/tmp/home` and the caches and
  configuration of matplotlib and XDG point to `/tmp`. The root file
  system and `/testbed` are read-only (`touch /testbed/x` fails), so nothing
  else can be written.
- `nofile: 4096`: dvc's `tests/conftest.py` raises its open-files limit to
  2048, which fails under the sandbox's default hard limit of 256, and then
  no dvc test runs at all.
- `pids: 4096`: threads count against the containers' limit of processes,
  256 by default. dask sizes its thread pools from the host's CPUs (32
  here), and then its tests that start a local `distributed` cluster cannot
  create the cluster's processes and wait forever (the container's
  `pids.events` counted 16,269 refusals in one run of dask 2023.6.0's tests,
  and two cluster tests hung). With 4096 that file's tests run through
  (peak 254 processes and threads, no refusal).
- `git_view: true`: dvc before 2.0 runs `git rev-parse HEAD` in its own
  directory when it is imported (`dvc/version.py`), and the import fails
  when `.git` exists but git cannot read it. A worker's worktree is a git
  worktree whose `.git` file names a host path, so without this neither the
  agent nor the node check could import dvc 0.89, 1.1.7 or 1.6.3. The
  sandbox mounts a read-only one-commit repository of the worktree's start
  commit in its place (see the package README, `--sandbox-config`): git
  then works in the container like in SWE-EVO's `/testbed`, `git status`
  and `git diff` show the agent's changes, and `git commit` fails.

Checked through the worker's own executor (`worker_entry.command_executor_for`
with the config `AqoursWorker` writes) on a real worktree of the dvc 0.89
task with one edited file: `id` gives `uid=10001(sandbox)`, `ulimit -n`
4096, `which python` the testbed environment's; `git status --short` shows
` M dvc/version.py` and `git log` the one base commit; `import dvc` gives
`0.89.0+ecd9b0.mod /workspace/dvc/__init__.py`; `pytest
tests/unit/test_path_info.py` passes (17); `git commit` is refused
(read-only); neither DNS nor an IP address is reachable; and the host's
`.git` file is unchanged. Both smoke checks pass in a worktree and in the
integration clone for every used instance. The coordinator itself was run on
the dvc 0.89 task (`run_graph` on `single.json`, with a stand-in worker that
applies the gold patch instead of a model): the node check, the merge and
both final checks passed in the task image, and `sweevo-eval` of that run
gave F2P 27/27, P2P 281/281, resolved.

The **task image** `aqours-sweevo:<id>` is the instance image
(`xingyaoww/sweb.eval.x86_64.*`, the dataset's `image`) plus four small
layers (`<task>/Dockerfile`):

1. `rm -rf /testbed/.git /opt/miniconda3/pkgs /root/.cache`: every copy of
   the project newer than the base. `/testbed` is at the instance's base
   commit, but its git history is the full upstream one (dask 2023.8.0:
   11,941 commits and tags up to `2024.8.1`, including the release being
   asked for). conda's package cache in the dask images holds
   `dask-core-2024.8.1`, the complete source of a later dask. A worker
   could read either. Afterwards no copy of the package is left outside
   `/testbed` (checked in all nine images as the sandbox user: only conda's
   metadata file of that dask-core, a file list, and scipy's unrelated
   `array_api_compat/dask`).
2. A passwd entry for the sandbox user. `getpass.getuser()` and git look the
   user up and fail without one (`KeyError: 'getpwuid(): uid not found:
   10001'` while collecting dvc's `tests/unit/remote/ssh/test_ssh.py`).
3. `git config --system --add safe.directory '*'`: the mounted code belongs
   to root on Docker Desktop, and git refuses a repository of another user
   ("dubious ownership"). SWE-EVO's eval script does the same for
   `/testbed` for its non-root user.
4. The packages SWE-EVO's eval script installs before the tests (its "hot
   fix" for dask releases of 2023 and earlier: `pandas<2.0`, and
   `distributed==2023.6.0` for 2023.6.1), since our containers have no
   network. The images already have pandas 1.5.3, so for these instances
   the layer changes nothing.

Differences from SWE-EVO's own containers, which run as root with network:
the limits above; no reinstall of the package and its dependencies after the
patch (SWE-EVO runs `pip install -e .[...]` first, which needs the network);
a one-commit repository, so versioneer reports no release version; files on
a Windows bind mount belong to root, so the sandbox user cannot `chmod`
them; the CPU limit is visible in the cgroup. The tests these differences
break are found by the validation and excluded.

## The single-agent baseline and its checks

`single.json` is the graph of `python -m aqours_code.taskgraph single`
with two smoke checks as the node check and the final checks. They prove
only that the package still imports from the worktree and that the existing
tests still collect, and use nothing from the grading data:

```text
python -c "import sys, dask; f = dask.__file__; print(f); sys.exit(0 if f.startswith('/workspace/') else 'not the worktree')"
python -m pytest --collect-only -qq -p no:cacheprovider --color=no -W default::DeprecationWarning -W default::pytest.PytestDeprecationWarning dask
```

(`dvc` or `pydantic` and `tests` for the other repositories). The warning
options are those SWE-EVO adds to every test command: dask turns deprecation
warnings into errors, and without them three unchanged test modules fail to
collect. A test module that fails to collect on the unchanged base in the
task image is left out with `--ignore` (found by `prepare.py`, recorded as
`smoke_ignore` in `grading/instance.json`): `tests/unit/command/test_dag.py`
(dvc 1.1.7: networkx imports `fractions.gcd`, gone in Python 3.9),
`tests/unit/command/test_studio.py` (dvc 3.43.1: `dvc_studio_client` too
new) and `tests/unit/fs/test_azure.py` (dvc 2.19.0: `dvc_azure` and
`fsspec` do not match). On all nine bases both checks pass, in 11 to 20 s.

## Grading

```bash
python -m aqours_code.taskgraph sweevo-eval <run directory> --task C:\tg\sweevo-tasks\<id>
```

copies the run's `final/` to a scratch directory, puts the files of the test
patch back to the base and applies the patch, makes the copy a git repository
at the base commit with the rest as uncommitted changes (like SWE-bench's
`/testbed`; dvc's tests open the repository in the current directory), runs
the instance's test command on the patch's test files in the task image
(with `--timeout=300` per test where the image has pytest-timeout, all but
pydantic's, as a safety net: a test that hangs then fails alone instead of
holding the whole command to its limit), parses pytest's `-rA`
summary with the instance's log parser, and writes the result to the run's
`summary.json` (`sweevo`), `sweevo_results.json` and `sweevo_tests.txt`. It
reports F2P and P2P passed / total, `resolved` (every F2P and P2P test
passes), SWE-EVO's fix rate (the F2P pass rate if no P2P test fails, else 0),
and Q = F2P pass rate × P2P pass rate, with the tests of
`sandbox_skips.txt` left out. `score` then uses Q with T and C like for any
task and adds a SWE-EVO table. See the package README, "Grading a SWE-EVO
run".

## Validation with the gold patch

```bash
python experiments/taskgraph/sweevo/validate_gold.py            # all nine; --pilot for two
```

grades two fake runs per instance exactly as `sweevo-eval` grades a real
one: `final/` = the base (every F2P test should fail and every P2P test
pass) and `final/` = the base plus the gold patch (every test should pass).
No model is called. A listed test that does not pass with the gold patch
cannot be passed in our sandbox by any method, and an F2P test that passes
on the base gives every method a point for nothing; both go to
`instances/<id>/sandbox_skips.txt`, by status, and the gold run must then be
resolved. [`validation/README.md`](validation/README.md) has the table.

## Dropped: iterative__dvc_3.43.1_3.44.0

With the gold patch, 20 of its 22 F2P tests fail in our sandbox (P2P 104 of
104 pass), all with `ImportError: cannot import name 'GIT_OBJ_COMMIT' from
'pygit2'`. The image has scmrepo 2.1.1 with pygit2 1.15.1, which no longer
has that name. The release itself raises the dependencies in
`pyproject.toml` (`scmrepo>=3,<4`, `dvc-data>=3.10,<3.12`, `dvc-ssh>=4,<5`),
and SWE-EVO's harness reinstalls the package with its dependencies, over the
network, after applying a patch, so there the new versions come in. Our
containers have no network and keep the image's packages, so these tests
cannot pass for any method. Installing the release's versions into the image
would make them pass, but would grade a method that leaves `pyproject.toml`
alone as if it had raised them, so the instance is left out.

## Cleaned tasks (v2) — 修正版题目

A task gives the agent the repository before the release, with the old tests;
the release's test changes (`grading/test.patch`) are applied only when
grading. Two kinds of failing test then keep an agent investigating without
end, and neither says anything about its work:

1. **Old tests of behaviour the release changes.** Some tests that the test
   patch rewrites assert exactly what the release changes, so they fail when
   the agent gets it right.
2. **Tests that fail on the unchanged base.** Some of the image's packages are
   newer than the release expected, and some tests fail before any change
   (dvc 1.1.7's `tests/unit/command/test_dag.py` does not even collect:
   networkx imports `fractions.gcd`, gone in Python 3.9).

A v2 task fixes both. The original task directories, their grading and the
runs made with them are not touched; v2 tasks are written elsewhere:

```bash
python experiments/taskgraph/sweevo/clean_tasks.py iterative__dvc_1.1.7_1.1.8 [<id> ...] --validate
```

writes `C:\tg\sweevo-tasks-v2\<id>\`, with the files of the original task and:

| Path | What |
| --- | --- |
| `repo/` | the original repository (its one commit, `task_commit`, stays the first commit) plus a second commit, "remove tests the release rewrites", with a fixed author and date |
| `cleaning.json` | every test removed: its file, its name (`Class::test` for a method), whether it is parametrized, its lines in the base, why (changed or deleted) and the hunks of the test patch that touch it (index and `@@` header, with the old line numbers); test classes left empty and removed; files removed; the patch's files left alone and why; the collection check |
| `known_failing.txt` | the tests that fail or error on the base, one pytest id per line (`known_failing.json`: the command and counts; `known_failing.log`: the output) |
| `request_iface_v2.md` | `request_iface.md` unchanged, then the section "Tests that already fail before any change" |
| `single_iface.json` | the single-agent graph with `request_iface_v2.md`, at the v2 `HEAD` |
| `single.json` | the plain graph (`request.md`), only moved to the v2 `HEAD` |
| `validation.json` | `--validate`: base and gold of v1 and v2, compared |

`grading/`, `sandbox.json`, `sandbox_skips.txt`, `Dockerfile`, `request.md`
and `request_iface.md` are copied unchanged.

**Fix 1: the tests the release rewrites are removed.** The test patch is read
hunk by hunk, as old-file line numbers, and every existing test function or
test method (`test*`, in a module matching the repository's `python_files`,
by default `test_*.py` and `*_test.py`, and in classes `Test*`) that it
changes or deletes is located in the base with `ast` and removed whole, from
its first decorator to its last line, with the blank lines before it. A test
is changed when a removed line is inside it (decorators included, so a new
`parametrize` value counts), when added lines go inside it, or right after it
indented deeper than its `def`, or right before it ending with a decorator; a
change in a test class's header changes all its tests. Lines between
definitions, a class's other members (fixtures, helpers) and module code are
no test, so a patch that only adds tests to a file removes nothing there. A
test class whose every test is removed goes too; a file the patch deletes (or
renames) is removed. Tests the patch adds do not exist yet; every other test
of the same file is kept, byte for byte. Each changed file must still parse,
and in the task image pytest must collect from the patch's files exactly the
base's tests less the removed ones, or the task is not written.

**Fix 2: the tests that already fail are named.** The whole test suite
(`tests` or `dask`) runs on the base (`task_commit`, a git repository, like
for grading) in the task image with the task's own test command, the
grading's extra pytest options and `sandbox.json`; no model is called. Every
test that fails or errors (a module that does not collect is listed by its
path) goes to `known_failing.txt`, less any test fix 1 removed.
`request_iface_v2.md` appends to `request_iface.md`:

````text
## Tests that already fail before any change

The tests below already fail (or cannot be collected) in this environment before
any change is made to the repository. The reason has nothing to do with this task:
the environment's packages, not the code you are asked to change. You do not need
to fix them, and do not spend time investigating the environment because of them.

N tests fail:

```text
tests/...
```
````

With more than 60, the first 60 (in alphabetical order) are listed, with the
total and the name of the full list. The section comes from the test run
alone; before it is written, it is checked to contain no line of the test
patch (20 characters or more) and no name of a test the patch adds or that fix
1 removed.

**Grading is unchanged.** `sweevo-eval` puts every file of the test patch back
to the task's base commit, `task_commit` in `grading/instance.json`, before it
applies the patch, and makes the graded copy a repository at that commit
(`sweevo.stage_code`, `SweevoTask.base_ref`). Over the v2 `HEAD` the patch
would not apply: its context lines include the tests fix 1 removed. In every
v1 task `task_commit` is the repository's only commit, its `HEAD`, so v1 runs
are graded exactly as before (a task directory without `task_commit` still
uses `HEAD`). The graded files of a v1 and a v2 run with the same code are the
same.

`--validate` grades, as `validate_gold.py` does but writing nothing to its
exclusion lists or reports, four fake runs without exclusions: the base and
the base plus the gold patch, of v1 and of v2 (the v2 "base" is its `HEAD`,
what a v2 agent starts from). It requires every F2P test to fail on the v2
base, every F2P test to pass with the gold patch, and every listed test's
status to be the one of v1, and writes `validation.json`.

**What v2 costs.** Fix 1 tells an agent indirectly which old tests the release
rewrites: they are the ones missing (and the subject of the second commit,
which `git log` shows in the agent's container, says that tests were
removed; it names none). This is a slight leak of the grading data. The
known-failing section leaks nothing of the patch, but it tells the agent that
those failures are not its business. Results of v1 and v2 tasks therefore
cannot be put side by side: compare methods within one version only.

### iterative__dvc_1.1.7_1.1.8 (v2)

Second commit `a76fae8` (on `d6ebb16`, the v1 commit). Removed: 10 tests
(19 collected ids) in 4 files; no file and no class.

| File | Removed | Why |
| --- | --- | --- |
| `tests/func/test_run_multistage.py` | `test_run_params_default` | changed (`isinstance(...)` becomes `assert isinstance(...)`) |
| `tests/unit/dependency/test_params.py` | `test_dumpd_with_info`, `test_dumpd_without_info`, `test_read_params_unsupported_format`, `test_read_params_nested`, `test_save_info_missing_param` | changed (`"params.yaml"` becomes `DEFAULT_PARAMS_FILE`) |
| `tests/unit/remote/test_azure.py` | `test_get_file_hash` | changed (uses the new `azure` fixture) |
| `tests/unit/test_ignore.py` | `test_ignore_from_file_should_filter_dirs_and_files`, `test_ignore_order` | deleted |
| `tests/unit/test_ignore.py` | `test_match_ignore_from_file` (parametrized, 10 cases) | changed (its parameter list) |

Left alone: `tests/func/test_ignore.py` (the patch only adds tests after the
last one), `tests/remotes/__init__.py`, `azure.py`, `oss.py` (fixture modules,
no tests) and the new `tests/docker-compose.yml`. Collected from the four
files: 62 tests on the base, 43 on v2, the 19 removed ones less.

The whole suite on the base (433 s): 683 passed, 573 failed, 103 errors.
**674** tests are in `known_failing.txt` (2 more failed but are removed tests;
34 captured log lines that the log parser reads as statuses are not tests).
Most fail because the image's networkx imports `fractions.gcd`, gone in
Python 3.9 (`ImportError: cannot import name 'gcd' from 'fractions'` 504
times in the log): dvc builds its pipeline graph with networkx, so `add`,
`repro`, `checkout`, `run` and more fail on the unchanged base. The request
lists the first 60; none of them is in a file of the test patch (22 of the
674 are).

`--validate`, without exclusions:

| | F2P | P2P | Resolved |
| --- | --- | --- | --- |
| v1 base | 0/8 | 82/82 | no |
| v1 gold | 8/8 | 82/82 | yes |
| v2 base | 0/8 | 82/82 | no |
| v2 gold | 8/8 | 82/82 | yes |

Every listed test has the same status in v1 and v2, for the base and for
the gold patch, and v1 matches its earlier validation
([`validation/iterative__dvc_1.1.7_1.1.8.json`](validation/iterative__dvc_1.1.7_1.1.8.json)).

## Running the baseline

In PowerShell, from the Aqours checkout with this branch checked out and the
virtual environment active, the single-agent baseline on the two pilot
instances, graded and scored:

```powershell
$runs = @()
foreach ($id in @("dask__dask_2023.8.0_2023.8.1", "iterative__dvc_0.89.0_0.90.0")) {
    $task = "C:\tg\sweevo-tasks\$id"
    python -m aqours_code.taskgraph run "$task\single.json" --repo "$task\repo" --sandbox-config "$task\sandbox.json" --workers 1 --out C:\tg\runs | Tee-Object -Variable log
    $run = ($log | Select-String -Pattern '^run directory: (.+)$').Matches[0].Groups[1].Value
    python -m aqours_code.taskgraph sweevo-eval $run --task $task
    $runs += [pscustomobject]@{ task = $id; method = "single"; run = $run }
}
[System.IO.File]::WriteAllText("C:\tg\runs\sweevo_pilot_manifest.json", (ConvertTo-Json -InputObject $runs))
python -m aqours_code.taskgraph score C:\tg\runs\sweevo_pilot_manifest.json --out C:\tg\score\sweevo-pilot
```

The same with the interface variant: `single_iface.json` in place of
`single.json`, for example for the dvc pilot:

```powershell
$id = "iterative__dvc_0.89.0_0.90.0"
$task = "C:\tg\sweevo-tasks\$id"
python -m aqours_code.taskgraph run "$task\single_iface.json" --repo "$task\repo" --sandbox-config "$task\sandbox.json" --workers 1 --out C:\tg\runs | Tee-Object -Variable log
$run = ($log | Select-String -Pattern '^run directory: (.+)$').Matches[0].Groups[1].Value
python -m aqours_code.taskgraph sweevo-eval $run --task $task
[System.IO.File]::WriteAllText("C:\tg\runs\sweevo_iface_manifest.json", (ConvertTo-Json -InputObject @([pscustomobject]@{ task = $id; method = "single"; run = $run })))
python -m aqours_code.taskgraph score C:\tg\runs\sweevo_iface_manifest.json --out C:\tg\score\sweevo-iface
```

`run` prints its run directory on its last line; `sweevo-eval` grades it
(about 4 minutes for dask 2023.8.0, 1 for dvc 0.89) and `score` writes
`score.md` and `score.json` with the SWE-EVO table. The worker timeout is the
harness default (`--worker-timeout 1800`); give `run` a larger one for these
releases if 30 minutes are too few. `WriteAllText` writes UTF-8 without a
byte-order mark, which `score` needs. The other methods take the same
`--sandbox-config` (`run` of a planner graph, and `team`), and their runs
are graded with the same `sweevo-eval`.
