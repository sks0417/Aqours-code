"""Undo CRLF line endings that a worker introduced.

An agent sometimes rewrites a file with ``\\r\\n`` line endings although the
repository uses ``\\n``. Every line of the file then counts as changed: the
diff is the whole file, and merging it with another node's change to the same
file conflicts on everything. After each attempt, before the commit, the
Coordinator puts such files back to ``\\n``.
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import gitops

CRLF, LF, NUL = b"\r\n", b"\n", b"\0"


class LineEndings:
    """Restores line endings in worktrees of one clone.

    Whether a commit's tracked text files are all LF is found once per commit.
    """

    def __init__(self, repo: Path):
        self.repo = repo
        self._all_lf: dict[str, bool] = {}
        self._lock = threading.Lock()

    def repository_is_lf(self, commit: str) -> bool:
        """True if no tracked text file of ``commit`` contains ``\\r\\n``."""
        with self._lock:
            if commit not in self._all_lf:
                self._all_lf[commit] = not gitops.text_files_have_crlf(self.repo, commit)
            return self._all_lf[commit]

    def restore(self, worktree: Path, start: str) -> list[str]:
        """Replace ``\\r\\n`` by ``\\n`` in files the worker turned into CRLF.

        Looks at the files of ``worktree`` that differ from its last commit:

        - a file that exists at ``start`` (the commit the node started from)
          is restored if it had no ``\\r\\n`` there and has some now;
        - a new file is restored if no tracked text file at ``start`` has
          ``\\r\\n``;
        - a binary file (one that contains a NUL byte) is never touched.

        Returns the restored paths, sorted.
        """
        restored = []
        for path in gitops.uncommitted_files(worktree):
            file = worktree / path
            if file.is_symlink() or not file.is_file():
                continue
            data = file.read_bytes()
            if CRLF not in data or NUL in data:
                continue
            original = gitops.blob_at(worktree, start, path)
            if original is None:
                if not self.repository_is_lf(start):
                    continue
            elif CRLF in original or NUL in original:
                continue
            file.write_bytes(data.replace(CRLF, LF))
            restored.append(path)
        return sorted(restored)
