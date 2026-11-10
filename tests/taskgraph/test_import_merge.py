"""The import-conflict resolver as a pure function on text (``import_merge.py``).

Several cases are the conflicts of the jp-modular v3 run, where five nodes
added their modules to the same two import lines of ``jobrunner/runner.py``.
Where the exact hunk shapes matter, the conflicted files are made by
``git merge-file``, in the default style and in the diff3 style.
"""
from __future__ import annotations

import subprocess

import pytest

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph.import_merge import (
    merge_imports,
    parse_conflicts,
    resolve_import_conflicts,
)


def git_merge(tmp_path, ours: str, base: str, theirs: str, style: str = "merge") -> str:
    """The file ``git merge-file`` writes for a conflict, in ``style`` merge or diff3."""
    paths = []
    for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
        path = tmp_path / f"{name}.py"
        path.write_bytes(text.encode("utf-8"))
        paths.append(str(path))
    args = ["git", "merge-file", "-p", "-L", "HEAD", "-L", "base", "-L", "theirs"]
    if style == "diff3":
        args.append("--diff3")
    proc = subprocess.run([*args, *paths], capture_output=True, check=False)
    assert proc.returncode > 0, "the three versions do not conflict"
    return proc.stdout.decode("utf-8")


def resolve(tmp_path, ours: str, base: str, theirs: str, path: str = "runner.py"):
    """Resolve the conflict as the Coordinator does: git's file plus its diff3 version."""
    written = git_merge(tmp_path, ours, base, theirs)
    return written, resolve_import_conflicts(
        path, written, git_merge(tmp_path, ours, base, theirs, "diff3"))


# ── one hunk ──

def test_both_sides_add_a_name_to_one_from_import():
    merged = merge_imports(ours="from . import cancellation, rate_limits, recovery\n",
                           base="from . import cancellation, recovery\n",
                           theirs="from . import audit, cancellation, recovery\n")
    assert merged == "from . import audit, cancellation, rate_limits, recovery\n"


def test_a_side_that_wrapped_the_line_in_parentheses():
    # node E of the jp run: E wrapped the line, G (merged first) added audit
    merged = merge_imports(
        ours=("from . import (cancellation, rate_limits, recovery, retry, scheduler,\n"
              "               transitions)\n"),
        base="from . import cancellation, recovery, retry, scheduler, transitions\n",
        theirs="from . import audit, cancellation, recovery, retry, scheduler, transitions\n")
    assert merged == ("from . import (audit, cancellation, rate_limits, recovery, retry, scheduler,\n"
                      "               transitions)\n")


def test_class_names_from_a_module_and_a_long_line_wrapped():
    assert merge_imports(ours="from .models import Job, JobStatus, Outcome, RateLimit\n",
                         base="from .models import Job, JobStatus, Outcome\n",
                         theirs="from .models import AuditEntry, Job, JobStatus, Outcome\n"
                         ) == "from .models import AuditEntry, Job, JobStatus, Outcome, RateLimit\n"
    # node F: the merged line is too long and is wrapped under the parenthesis
    assert merge_imports(
        ours="from .models import Job, JobStatus, Notification, Outcome, Subscription\n",
        base="from .models import Job, JobStatus, Outcome\n",
        theirs="from .models import AuditEntry, Job, JobStatus, Outcome, RateLimit\n",
    ) == ("from .models import (AuditEntry, Job, JobStatus, Notification, Outcome,\n"
          "                     RateLimit, Subscription)\n")


def test_plain_imports_one_module_per_line():
    assert merge_imports(ours="import os\nimport sys\n", base="import os\n",
                         theirs="import json\nimport os\n") == "import json\nimport os\nimport sys\n"
    assert merge_imports(ours="import os, sys\n", base="import os\n",
                         theirs="import os\nimport re\n") == "import os\nimport sys\nimport re\n"


def test_aliases_are_kept():
    # node H: its alias stays on a line of its own, as H wrote it
    assert merge_imports(
        ours="from . import audit, cancellation, recovery\nfrom . import stats as stats_module\n",
        base="from . import audit, cancellation, recovery\n",
        theirs="from . import (audit, cancellation, rate_limits,\n               recovery)\n",
    ) == ("from . import audit, cancellation, rate_limits, recovery\n"
          "from . import stats as stats_module\n")
    # a side that lists the alias with the other names: they stay together
    assert merge_imports(ours="from . import audit, cancellation\n", base="from . import audit\n",
                         theirs="from . import audit, stats as stats_module\n"
                         ) == "from . import audit, cancellation, stats as stats_module\n"
    # an alias one side introduced replaces the plain import
    assert merge_imports(ours="import numpy as np\n", base="import numpy\n",
                         theirs="import numpy\nimport pandas\n") == "import numpy as np\nimport pandas\n"


def test_a_name_one_side_removed_is_dropped():
    assert merge_imports(ours="from .models import Job, JobStatus\n",
                         base="from .models import Job, JobStatus, Outcome\n",
                         theirs="from .models import Job, JobStatus, Outcome, RateLimit\n"
                         ) == "from .models import Job, JobStatus, RateLimit\n"
    assert merge_imports(ours="import os\n", base="import os\nimport sys\n",
                         theirs="import json\nimport os\nimport sys\n") == "import json\nimport os\n"


def test_theirs_layout_is_kept_and_ours_statements_follow_their_neighbour():
    merged = merge_imports(ours="import os\nimport sys\n\nfrom . import a\n",
                           base="import os\n\nfrom . import a\n",
                           theirs="import os\n\nfrom . import a, b\n")
    assert merged == "import os\nimport sys\n\nfrom . import a, b\n"


def test_unsorted_names_keep_theirs_order():
    merged = merge_imports(ours="from m import c, a, x\n", base="from m import c, a\n",
                           theirs="from m import c, b, a\n")
    assert merged == "from m import c, b, a, x\n"


def test_a_module_too_long_for_aligned_wrapping_is_wrapped_one_name_per_line():
    module = "package." + "m" * 60
    merged = merge_imports(ours=f"from {module} import alpha, gamma\n",
                           base=f"from {module} import alpha\n",
                           theirs=f"from {module} import alpha, beta\n")
    assert merged == f"from {module} import (\n    alpha,\n    beta,\n    gamma,\n)\n"
    compile(merged, "m.py", "exec")


@pytest.mark.parametrize("ours", [
    "from .models import Job\nfrom .models import RecurringJob  # noqa: F401\n",  # node A_B_C_D
    "from . import audit\nLIMIT = 3\n",
    "from . import *\n",
    "    from . import audit\n",
    "from . import (audit,\n",
])
def test_a_side_that_is_not_only_imports_is_not_merged(ours):
    assert merge_imports(ours=ours, base="from .models import Job\n",
                         theirs="from .models import Job, Outcome\n") is None


def test_a_side_that_deleted_the_lines_drops_their_names():
    assert merge_imports(ours="\n", base="from .models import Job\n",
                         theirs="from .models import Job, Outcome\n") == "from .models import Outcome\n"


# ── one file ──

RUNNER_BASE = """\
from __future__ import annotations

from . import cancellation, recovery
from .errors import JobNotFound
from .models import Job, JobStatus
from .store import JobStore


def run_once() -> None:
    pass
"""


def test_hunks_git_joins_are_resolved_together(tmp_path):
    # node F: two conflicts one line apart are one hunk in git's default style
    ours = (RUNNER_BASE.replace("cancellation, recovery", "cancellation, notifications, recovery")
            .replace("Job, JobStatus", "Job, JobStatus, Notification"))
    theirs = (RUNNER_BASE.replace("cancellation, recovery", "audit, cancellation, recovery")
              .replace("Job, JobStatus", "AuditEntry, Job, JobStatus"))
    written, result = resolve(tmp_path, ours, RUNNER_BASE, theirs)
    assert written.count("<<<<<<<") == 1
    assert (result.resolved, result.left, result.complete) == (1, 0, True)
    assert result.text == RUNNER_BASE.replace(
        "cancellation, recovery", "audit, cancellation, notifications, recovery").replace(
        "Job, JobStatus", "AuditEntry, Job, JobStatus, Notification")


def test_lines_git_moves_out_of_a_hunk_are_kept_once(tmp_path):
    # both sides add the same first import; git's default style moves it out of the hunk
    base = "import a\n\n\nVALUE = 1\n"
    written, result = resolve(tmp_path, "import b\nimport c\n\n\nVALUE = 1\n", base,
                              "import b\nimport d\n\n\nVALUE = 1\n")
    assert written.startswith("import b\n<<<<<<<")
    assert result.complete and result.text == "import b\nimport c\nimport d\n\n\nVALUE = 1\n"


def test_a_hunk_that_mixes_imports_and_code_keeps_its_markers(tmp_path):
    base = "from . import audit\n\n\ndef run():\n    pass\n\n\nLIMIT = 2\n"
    ours = base.replace("import audit", "import audit, rate_limits").replace(
        "LIMIT = 2", "import json\nLIMIT = 3")
    theirs = base.replace("import audit", "import audit, notifications").replace(
        "LIMIT = 2", "LIMIT = 4")
    written, result = resolve(tmp_path, ours, base, theirs)
    assert (result.resolved, result.left, result.complete) == (1, 1, False)
    hunks = [segment for segment in parse_conflicts(written) if not isinstance(segment, str)]
    code_hunk = "".join(hunks[1].raw)
    assert "import json\nLIMIT = 3\n" in code_hunk
    assert result.text == ("from . import audit, notifications, rate_limits\n\n\ndef run():\n"
                           "    pass\n\n\n" + code_hunk)


def test_a_file_that_is_not_python_is_left_alone(tmp_path):
    ours, base, theirs = "import a, c\n", "import a\n", "import a, b\n"
    written, result = resolve(tmp_path, ours, base, theirs, path="notes.txt")
    assert (result.text, result.resolved, result.left) == (written, 0, 1)


def test_a_comment_in_a_hunk_leaves_it_to_the_worker(tmp_path):
    base = "from .models import Job\n\nLIMIT = 1\n"
    ours = "from .models import Job\nfrom .models import RecurringJob  # noqa: F401\n\nLIMIT = 1\n"
    written, result = resolve(tmp_path, ours, base, "from .models import AuditEntry, Job\n\nLIMIT = 1\n")
    assert (result.text, result.resolved) == (written, 0)


def test_imports_inside_a_string_are_not_resolved():
    written = ('DOC = """Example:\n<<<<<<< HEAD\nimport a\n||||||| base\n=======\n'
               'import b\n>>>>>>> theirs\n"""\n')
    assert merge_imports("import a\n", "", "import b\n") == "import b\nimport a\n"
    result = resolve_import_conflicts("doc.py", written)
    assert (result.text, result.resolved) == (written, 0)


def test_without_a_base_section_nothing_is_resolved():
    written = "<<<<<<< HEAD\nimport a, c\n=======\nimport a, b\n>>>>>>> theirs\n"
    result = resolve_import_conflicts("m.py", written)
    assert (result.text, result.resolved, result.left) == (written, 0, 1)


def test_files_that_do_not_line_up_are_left_alone():
    written = "<<<<<<< HEAD\nimport c\n=======\nimport b\n>>>>>>> theirs\n"
    diff3 = "<<<<<<< ours\nimport x\n||||||| base\n=======\nimport b\n>>>>>>> theirs\n"
    assert resolve_import_conflicts("m.py", written, diff3).resolved == 0


def test_crlf_line_endings_are_kept(tmp_path):
    base = "import a\r\n\r\n\r\nVALUE = 1\r\n"
    written, result = resolve(tmp_path, base.replace("import a", "import a\r\nimport c"), base,
                              base.replace("import a", "import a\r\nimport b"))
    assert result.complete
    assert result.text == "import a\r\nimport b\r\nimport c\r\n\r\n\r\nVALUE = 1\r\n"


def test_conflict_diff3_leaves_the_file_as_git_wrote_it(toy_repo, tmp_path):
    repo = tmp_path / "clone"
    base = gitops.clone_for_run(toy_repo.path, repo, toy_repo.commit)
    worktree = tmp_path / "wt"
    gitops.add_node_worktree(repo, "N", worktree, base)
    (repo / "runner.py").write_bytes(b"from store import JobStore, X\n")
    gitops.commit_all(repo, "integration change")
    (worktree / "runner.py").write_bytes(b"from store import JobStore, Y\n")
    gitops.commit_all(worktree, "node change")
    assert gitops.merge_keep_conflicts(worktree, gitops.head(repo)) == ["runner.py"]
    written = (worktree / "runner.py").read_bytes()
    diff3 = gitops.conflict_diff3(worktree, "runner.py")
    assert diff3 is not None and b"||||||| " in diff3 and b"||||||| " not in written
    assert b"from store import JobStore\n" in diff3  # the base side
    assert (worktree / "runner.py").read_bytes() == written
    assert gitops.conflict_diff3(worktree, "missing.py") is None
