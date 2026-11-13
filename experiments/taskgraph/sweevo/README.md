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
