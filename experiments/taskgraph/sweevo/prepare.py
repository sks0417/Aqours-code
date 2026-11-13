"""Prepare SWE-EVO instances as task-graph tasks, on this machine.

    python experiments/taskgraph/sweevo/prepare.py [<instance_id> ...] [--pilot]
        [--dataset C:\\tg\\sweevo\\hf_out\\hf_dataset\\test] [--out C:\\tg\\sweevo-tasks]
        [--no-docker]

Without ids it prepares every instance in ``instances.txt`` that is not
marked ``dropped``; ``--pilot`` only the two pilot instances. For each instance it writes ``<out>/<id>/``:

    repo/                  the repository at base_commit, as one commit (LF, no history)
    request.md             the release notes in SWE-agent's instruction (see below)
    request_iface.md       request.md plus the release's interface (sweevo_interface.py)
    sandbox.json           the image and environment workers and checks run in
    single.json            the single-agent graph, with the smoke checks
    single_iface.json      the same graph with request_iface.md (request_id single_iface)
    sandbox_skips.txt      a copy of instances/<id>/sandbox_skips.txt, if there is one
    grading/instance.json  ids, test command, log parser, FAIL_TO_PASS, PASS_TO_PASS
    grading/test.patch     the release's test changes (never applied in repo/)
    grading/gold.patch     the release's source changes (for validation only)

Nothing under ``grading/`` is in ``repo/``, and workers see only a clone of
``repo/``, so they can never read the grading data. ``request.md`` contains
the problem statement verbatim and no test file, test name or patch.
``request_iface.md`` adds the names and signatures of the public definitions
the gold patch adds, changes or removes in non-test files (no bodies, no
test file or text of the test patch).

Then it pulls the instance's image and builds ``aqours-sweevo:<id>`` from it
(see ``Dockerfile`` below and README.md), and prints both sizes.

The dataset is a ``datasets.save_to_disk`` folder; reading it needs
``pyarrow`` (``pip install pyarrow``). The repository is fetched from GitHub
at ``base_commit`` (``git fetch --depth 1``) and committed once with a fixed
author and date, so its commit id is the same on every machine.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # the Aqours checkout, for aqours_code.taskgraph

from aqours_code.docker_utils import host_container_user  # noqa: E402
from aqours_code.taskgraph.schema import dump_graph  # noqa: E402
from aqours_code.taskgraph.single import single_graph  # noqa: E402
from aqours_code.taskgraph.sweevo_interface import (  # noqa: E402
    interface_section, item_count, release_interface)

DEFAULT_DATASET = Path(r"C:\tg\sweevo\hf_out\hf_dataset\test")
DEFAULT_OUT = Path(r"C:\tg\sweevo-tasks")
INSTANCES = HERE / "instances.txt"
SKIPS_DIR = HERE / "instances"
IMAGE_PREFIX = "aqours-sweevo"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "SWE-EVO Task",
    "GIT_AUTHOR_EMAIL": "task@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "SWE-EVO Task",
    "GIT_COMMITTER_EMAIL": "task@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}

# SWE-agent's instance template (SWE-EVO's SWE-agent/config/default.yaml), word
# for word. Left out, as tool-specific: the <uploaded_files> block and the
# directory placeholder (our worker's workspace is the repository itself).
REQUEST_TEMPLATE = """\
I've uploaded a python code repository in the current directory. Consider the following PR description:

<pr_description>
{problem_statement}
</pr_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <pr_description> are met?
I've already taken care of all changes to any of the test files described in the <pr_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!
Your task is to make the minimal changes to non-tests files in the current directory to ensure the <pr_description> is satisfied.
Follow these steps to resolve the issue:
1. As a first step, it might be a good idea to find and read code relevant to the <pr_description>
2. Create a script to reproduce the error and execute it with `python <filename.py>` using the bash tool, to confirm the error
3. Edit the sourcecode of the repo to resolve the issue
4. Rerun your reproduce script and confirm that the error is fixed!
5. Think about edgecases and make sure your fix handles them as well
Your thinking should be thorough and so it's fine if it's very long.
"""

# The SWE-bench images keep the repository at /testbed, installed in editable
# mode in the conda environment "testbed". In our containers the code under
# test is the worktree at /workspace: the environment's tools come first on
# PATH, and /workspace comes first on sys.path, so `import <package>` loads
# the worktree. Everything a test writes goes to /workspace or /tmp.
TESTBED = "/opt/miniconda3/envs/testbed"
SANDBOX_ENV = {
    "PATH": f"{TESTBED}/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin",
    "CONDA_PREFIX": TESTBED,
    "CONDA_DEFAULT_ENV": "testbed",
    "PYTHONPATH": "/workspace",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_NO_INDEX": "1",
    "MPLCONFIGDIR": "/tmp/matplotlib",
    "XDG_CACHE_HOME": "/tmp/cache",
    "XDG_CONFIG_HOME": "/tmp/config",
}

# dvc's tests/conftest.py raises its open-files limit to 2048, which a
# container started with the sandbox's default hard limit (256) refuses.
SANDBOX_NOFILE = 4096
# Threads count against the containers' process limit (256 by default). dask
# sizes its thread pools from the host's CPUs (32 here), and its tests that
# start a local cluster then cannot create their worker processes and hang
# (the cgroup's pids.events counted 16,269 refusals in one test run).
SANDBOX_PIDS = 4096
# dvc before 2.0 runs `git rev-parse HEAD` in its own directory when it is
# imported, and fails when git cannot read the repository: a worker's
# worktree is a git worktree whose .git names a host path. With git_view the
# sandbox mounts a small repository of the worktree's start commit in its place.
SANDBOX_GIT_VIEW = True
# Seconds one test may run when grading (pytest-timeout, in the images that
# have it): a test that hangs in our sandbox (dask's tests that start a real
# local cluster) then fails alone instead of stopping the whole test run.
TEST_TIMEOUT_S = 300

# Per repository: the package to import, and the tests the smoke check collects.
REPOS = {
    "dask/dask": {"package": "dask", "tests": "dask"},
    "iterative/dvc": {"package": "dvc", "tests": "tests"},
    "pydantic/pydantic": {"package": "pydantic", "tests": "tests"},
}


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git with a configuration independent of the user's."""
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "core.longpaths=true",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: "
                           f"{(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def load_rows(dataset: Path) -> dict[str, dict]:
    """The dataset's rows by instance id (needs pyarrow)."""
    try:
        import pyarrow as pa  # noqa: PLC0415
    except ImportError:
        sys.exit("error: reading the dataset needs pyarrow: pip install pyarrow")
    files = sorted(Path(dataset).glob("*.arrow"))
    if not files:
        sys.exit(f"error: no .arrow file in {dataset}")
    rows = []
    for path in files:
        with pa.memory_map(str(path)) as source:
            rows += pa.ipc.open_stream(source).read_all().to_pylist()
    return {row["instance_id"]: row for row in rows}


def id_list(value) -> list[str]:
    return json.loads(value) if isinstance(value, str) else list(value)


def selected_ids() -> tuple[list[str], list[str], list[str]]:
    """(used ids, pilot ids, dropped ids) from instances.txt, in its order.

    ``pilot`` after an id marks a pilot, ``dropped`` an instance left out.
    """
    ids, pilots, dropped = [], [], []
    for line in INSTANCES.read_text(encoding="utf-8").splitlines():
        words = line.split("#")[0].split()
        if not words:
            continue
        (dropped if "dropped" in words[1:] else ids).append(words[0])
        if "pilot" in words[1:]:
            pilots.append(words[0])
    return ids, pilots, dropped


def make_repo(row: dict, repo_dir: Path) -> dict:
    """The repository at base_commit as one commit; returns its commit and tree."""
    url = f"https://github.com/{row['repo']}.git"
    message = f"SWE-EVO task {row['instance_id']}: {row['repo']} at {row['base_commit']}"
    if repo_dir.exists():
        tree = git(repo_dir, "rev-parse", "HEAD^{tree}")
        commit = git(repo_dir, "rev-parse", "HEAD")
        if git(repo_dir, "status", "--porcelain"):
            raise RuntimeError(f"{repo_dir} has uncommitted changes; delete it to rebuild")
        return {"task_commit": commit, "tree": tree}
    repo_dir.mkdir(parents=True)
    git(repo_dir, "init", "-q")
    for key, value in (("core.autocrlf", "false"), ("core.longpaths", "true")):
        git(repo_dir, "config", key, value)
    git(repo_dir, "fetch", "-q", "--depth", "1", "--no-tags", url, row["base_commit"])
    tree = git(repo_dir, "rev-parse", "FETCH_HEAD^{tree}")
    commit = git(repo_dir, "commit-tree", tree, "-m", message)
    git(repo_dir, "update-ref", "refs/heads/main", commit)
    git(repo_dir, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo_dir, "checkout", "-q", "-f", "main")
    # Forget the upstream commit: the repository has one commit and no history.
    (repo_dir / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    (repo_dir / ".git" / "shallow").unlink(missing_ok=True)
    git(repo_dir, "reflog", "expire", "--expire=now", "--all")
    git(repo_dir, "gc", "-q", "--prune=now")
    if git(repo_dir, "cat-file", "-t", row["base_commit"], check=False):
        raise RuntimeError(f"{repo_dir} still has the upstream commit")
    if git(repo_dir, "rev-list", "--count", "--all") != "1":
        raise RuntimeError(f"{repo_dir} does not have exactly one commit")
    return {"task_commit": commit, "tree": tree}


def collect_command(repo: str, ignore: list[str] = ()) -> str:
    """``pytest --collect-only`` on the repository's tests, leaving out ``ignore``.

    The warning options are those SWE-EVO adds to every test command: some
    repositories turn deprecation warnings into errors, which fails the
    collection of a few unchanged test modules.
    """
    ignored = "".join(f" --ignore={path}" for path in ignore)
    return ("python -m pytest --collect-only -qq -p no:cacheprovider --color=no "
            "-W default::DeprecationWarning -W default::pytest.PytestDeprecationWarning"
            f"{ignored} {REPOS[repo]['tests']}")


def smoke_checks(repo: str, ignore: list[str] = ()) -> list[str]:
    """The single agent's checks: the package imports from /workspace, its tests collect.

    They use nothing from the grading data: no test file or id of the release.
    ``ignore`` are test modules that do not collect on the unchanged base in
    the task image (a package of the image has the wrong version for them).
    """
    package = REPOS[repo]["package"]
    return [
        f"python -c \"import sys, {package}; f = {package}.__file__; print(f); "
        f"sys.exit(0 if f.startswith('/workspace/') else 'not the worktree')\"",
        collect_command(repo, ignore),
    ]


def base_collection_errors(task_dir: Path, repo: str) -> tuple[list[str], str]:
    """Test modules that fail to collect on the base in the task image, and the output.

    The base is exported from ``repo/`` and collected in the same container
    the single agent's check uses.
    """
    from aqours_code.taskgraph import gitops  # noqa: PLC0415
    from aqours_code.taskgraph.checks import checks_for, run_checks  # noqa: PLC0415
    from aqours_code.taskgraph.cleanup import remove_tree  # noqa: PLC0415
    from aqours_code.taskgraph.sandbox import load_sandbox_config  # noqa: PLC0415

    work = task_dir / "_collect"
    remove_tree(work)
    gitops.export_commit(task_dir / "repo", "HEAD", work)
    try:
        outcome = run_checks([collect_command(repo)], work, 900,
                             checks=checks_for(load_sandbox_config(task_dir / "sandbox.json")))
    finally:
        remove_tree(work)
    errors = sorted(set(re.findall(r"^ERROR (\S+?)(?: - .*)?$", outcome.text, re.M)))
    if not outcome.ok and (not errors or any("::" in path for path in errors)):
        raise RuntimeError(f"{task_dir.name}: the base does not collect in the task image, "
                           f"and not because of whole test modules:\n{outcome.text[-3000:]}")
    return errors, outcome.text


def dockerfile(row: dict) -> str:
    """The task image: the instance image plus what SWE-EVO's harness does at eval time.

    SWE-EVO's eval script installs some packages before the tests run (its
    "hot fix" for dask releases of 2023 and earlier); our containers have no
    network, so the image gets them here. Every copy of the project newer
    than the base goes: the git history of /testbed (it holds the release's
    commits) and the package caches (conda's keeps a newer dask-core in the
    dask images). The sandbox's non-root user gets a passwd entry:
    ``getpass.getuser()`` and git look the user up, and fail without one.
    """
    uid, gid = host_container_user().split(":")
    lines = [f"FROM {row['image']}", "USER root",
             # Other copies of the project: the git history, and conda's package
             # cache (the dask images keep dask-core 2024.8.1 there), pip's and pdm's.
             "RUN rm -rf /testbed/.git /opt/miniconda3/pkgs /root/.cache",
             f"RUN getent group {gid} || groupadd -g {gid} sandbox; "
             f"getent passwd {uid} || useradd -u {uid} -g {gid} -M -d /tmp/home "
             "-s /bin/sh sandbox",
             # The mounted code belongs to another user; git (and libgit2) must
             # still read its repository (SWE-EVO's script adds /testbed for its
             # non-root user the same way).
             "RUN git config --system --add safe.directory '*'"]
    pip = f"{TESTBED}/bin/python -m pip install --no-cache-dir"
    if row["repo"] == "dask/dask" and int(row["start_version"].split(".")[0]) <= 2023:
        packages = ["'pandas<2.0'"]
        if row["end_version"] == "2023.6.1":
            packages.append("'distributed==2023.6.0'")
        lines.append(f"RUN {pip} {' '.join(packages)}")
    return "\n".join(lines) + "\n"


def has_pytest_timeout(image: str) -> bool:
    """True when the image's testbed environment has the pytest-timeout plugin."""
    proc = subprocess.run(["docker", "run", "--rm", "--network", "none", image,
                           f"{TESTBED}/bin/python", "-c", "import pytest_timeout"],
                          capture_output=True, text=True)
    return proc.returncode == 0


def image_tag(instance_id: str) -> str:
    return f"{IMAGE_PREFIX}:{instance_id.lower()}"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {(proc.stderr or proc.stdout)[-2000:]}")
    return proc


def image_size(image: str) -> int:
    return int(docker("image", "inspect", "--format", "{{.Size}}", image).stdout.strip())


def build_image(row: dict, task_dir: Path) -> dict:
    """Pull the instance image and build the task image; return names and sizes."""
    if docker("image", "inspect", row["image"], check=False).returncode != 0:
        print(f"  docker pull {row['image']}", flush=True)
        docker("pull", row["image"])
    tag = image_tag(row["instance_id"])
    text = dockerfile(row)
    (task_dir / "Dockerfile").write_text(text, encoding="utf-8", newline="\n")
    with tempfile.TemporaryDirectory() as context:
        shutil.copy(task_dir / "Dockerfile", Path(context) / "Dockerfile")
        print(f"  docker build -t {tag}", flush=True)
        docker("build", "-q", "-t", tag, context)
    return {"instance_image": row["image"], "instance_image_bytes": image_size(row["image"]),
            "task_image": tag, "task_image_bytes": image_size(tag)}


def prepare(row: dict, out: Path, *, use_docker: bool) -> dict:
    """Write one task directory; return what was made."""
    instance_id = row["instance_id"]
    if row["repo"] not in REPOS:
        raise RuntimeError(f"{instance_id}: no settings for {row['repo']} in REPOS")
    task_dir = out / instance_id
    grading = task_dir / "grading"
    grading.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads((grading / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    made = make_repo(row, task_dir / "repo")
    request = REQUEST_TEMPLATE.format(problem_statement=row["problem_statement"])
    (task_dir / "request.md").write_text(request, encoding="utf-8", newline="\n")
    interface = release_interface(task_dir / "repo", row["patch"])
    section = interface_section(interface)
    request_iface = request.rstrip("\n") + "\n\n" + section
    (task_dir / "request_iface.md").write_text(request_iface, encoding="utf-8", newline="\n")
    instance = {key: row[key] for key in (
        "instance_id", "repo", "base_commit", "image", "test_cmds", "log_parser",
        "start_version", "end_version", "end_version_commit", "version",
        "environment_setup_commit")}
    instance.update(made)
    instance["package"] = REPOS[row["repo"]]["package"]
    instance["FAIL_TO_PASS"] = id_list(row["FAIL_TO_PASS"])
    instance["PASS_TO_PASS"] = id_list(row["PASS_TO_PASS"])
    (grading / "instance.json").write_text(json.dumps(instance, indent=1) + "\n",
                                           encoding="utf-8", newline="\n")
    for name, key in (("test.patch", "test_patch"), ("gold.patch", "patch")):
        (grading / name).write_text(row[key], encoding="utf-8", newline="\n")
    sandbox = {"comment": f"SWE-EVO {instance_id}: the instance image plus eval-time "
                          "packages; the testbed environment first on PATH, the worktree "
                          "(/workspace) first on sys.path",
               "image": image_tag(instance_id), "env": SANDBOX_ENV, "nofile": SANDBOX_NOFILE,
               "pids": SANDBOX_PIDS, "git_view": SANDBOX_GIT_VIEW}
    (task_dir / "sandbox.json").write_text(json.dumps(sandbox, indent=1) + "\n",
                                           encoding="utf-8", newline="\n")
    skips = SKIPS_DIR / instance_id / "sandbox_skips.txt"
    if skips.is_file():
        shutil.copy(skips, task_dir / "sandbox_skips.txt")
    else:
        (task_dir / "sandbox_skips.txt").unlink(missing_ok=True)
    result = {"instance_id": instance_id, "task_dir": str(task_dir), **made,
              "f2p": len(instance["FAIL_TO_PASS"]), "p2p": len(instance["PASS_TO_PASS"]),
              "skips": skips.is_file()}
    if use_docker:
        result.update(build_image(row, task_dir))
        ignore, _ = base_collection_errors(task_dir, row["repo"])
        timeout = TEST_TIMEOUT_S if has_pytest_timeout(image_tag(instance_id)) else None
    else:  # keep what an earlier run with Docker found
        ignore, timeout = previous.get("smoke_ignore", []), previous.get("test_timeout")
    instance["smoke_ignore"] = ignore
    instance["test_timeout"] = timeout
    (grading / "instance.json").write_text(json.dumps(instance, indent=1) + "\n",
                                           encoding="utf-8", newline="\n")
    result["smoke_ignore"], result["test_timeout"] = ignore, timeout
    checks = smoke_checks(row["repo"], ignore)
    dump_graph(single_graph(request, task_dir / "repo", checks), task_dir / "single.json")
    graph_iface = single_graph(request_iface, task_dir / "repo", checks)
    dump_graph(graph_iface.model_copy(update={"request_id": "single_iface"}),
               task_dir / "single_iface.json")
    result["interface_items"] = item_count(interface)
    result["interface_chars"] = len(section)
    result["interface_errors"] = [item.path for item in interface if item.error]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="*", help="instance ids (default: instances.txt)")
    parser.add_argument("--pilot", action="store_true", help="only the pilot instances")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-docker", action="store_true",
                        help="do not pull or build images")
    args = parser.parse_args(argv)
    every, pilots, _ = selected_ids()
    ids = args.ids or (pilots if args.pilot else every)
    rows = load_rows(args.dataset)
    missing = [instance_id for instance_id in ids if instance_id not in rows]
    if missing:
        sys.exit(f"error: not in the dataset: {', '.join(missing)}")
    for instance_id in ids:
        print(f"{instance_id}", flush=True)
        result = prepare(rows[instance_id], args.out, use_docker=not args.no_docker)
        print(f"  task: {result['task_dir']}  commit {result['task_commit'][:12]}  "
              f"F2P {result['f2p']}  P2P {result['p2p']}  "
              f"skips: {'yes' if result['skips'] else 'none'}")
        print(f"  per-test timeout when grading: "
              f"{result['test_timeout'] or 'none (no pytest-timeout in the image)'}")
        print(f"  interface: {result['interface_items']} items, "
              f"{result['interface_chars']} characters"
              + (f"; NOT READ: {', '.join(result['interface_errors'])}"
                 if result["interface_errors"] else ""))
        if result["smoke_ignore"]:
            print(f"  smoke check ignores (no collection on the base): "
                  f"{', '.join(result['smoke_ignore'])}")
        if "task_image" in result:
            print(f"  image {result['instance_image']}: "
                  f"{result['instance_image_bytes'] / 1e9:.2f} GB; task image "
                  f"{result['task_image']}: {result['task_image_bytes'] / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
