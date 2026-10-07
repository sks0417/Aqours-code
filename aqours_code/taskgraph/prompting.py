"""Worker prompts for task graph nodes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .context_pack import ContextPack, build_context_pack

from .repo_index import RepoIndex
from .schema import Graph, Node
from .validate import ancestors, unique_nodes

FAILURE_EXCERPT_CHARS = 4000

NODE_PROMPT_TEMPLATE = """\
# Overall request

{request}

{task_scope}

# Your sub-task: {title}

{goal}

# Files you may change

{file_scope}

# Symbols you must provide

{provides}
{contract_note}
# Symbols you can rely on

{requires}

{context_section}# Done when

Before you finish, run these commands from the repository root and make sure
they all pass:
{checks}

# Rules

If your goal and the specification disagree, follow the specification, and
say in your final answer where they disagree.

Do not run git commands that change repository state (commit, checkout,
branch, reset, stash, merge, rebase, and so on). The coordinator commits your
work after you finish.

Change existing files with edit_file, only where your sub-task needs it;
never rewrite a whole existing file with write_file. Use write_file only
for files you create.

Work only inside your workspace. Files outside it are not available to you.
If you need something that is not in the workspace or in the interfaces your
sub-task can rely on, do not look for it elsewhere: do the best you can with
what you have, and state clearly in your final answer what was missing.
{sandbox}{retry}{before_start}"""

SINGLE_NODE_CONTEXT = """Everything your sub-task needs is below: your own files in full, the
interfaces you use (signatures and docstrings only), the parts of the
specification that concern you, and the test fixtures. Start working from
this. Do not read the other feature modules of the repository. Read another
file only if something you need is missing here, and say in your final
answer what was missing."""

CONTEXT_INSPECTION = """The inspection of contracts and source that your instructions ask for has
already been done for this sub-task: the context below IS that inspection.
It contains your own files in full, the interfaces you use (signatures and
docstrings), the parts of the specification that concern you, and the test
fixtures. Treat it as already read. Do not read these files again, and do
not read other modules of the repository."""

BEFORE_START = """
# Before you start

Start from the context above and write code. Reading other files of the
repository is blocked the first time; read one only if your sub-task
cannot be done without it, and say why in your final answer.
"""

SANDBOX_NOTE = """
Your bash commands run in a Linux container (POSIX sh, no network) whose
working directory, /workspace, is your workspace. Use relative paths and
POSIX shell syntax in commands.
"""

CONTRACT_NOTE = """
This is a contract sub-task. For each symbol, write the signature with a
docstring, plus a minimal default implementation or an in-memory fake that
runs, so that sub-tasks depending on these interfaces can run their checks
before the full implementation exists.
"""

RETRY_TEMPLATE = """
# Previous attempt failed

This is attempt {attempt}. Your earlier changes are still in the workspace.
Reason: {reason}

Output excerpt:
```
{output}
```
"""

IMPLEMENTED = "implemented"
INTERFACE_ONLY = ("interface only: the full implementation may not exist yet; "
                  "do not rely on its behaviour in your checks")
IMPLEMENTED_UPSTREAM = "implemented by an upstream sub-task"


@dataclass
class AttemptFailure:
    """Why the previous attempt of a node failed."""

    reason: str
    output: str = ""


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- `{item}`" for item in items) if items else "- (none)"


def requirement_status(graph: Graph, node: Node, index: RepoIndex,
                       symbol: str) -> str:
    """Describe how far ``symbol`` in ``node.requires`` is implemented."""
    if index.has_symbol(symbol):
        return IMPLEMENTED
    by_id = {other.id: other for other in unique_nodes(graph)}
    providers = [by_id[a] for a in ancestors(graph)[node.id] if symbol in by_id[a].provides]
    if any(provider.kind == "implement" for provider in providers):
        return IMPLEMENTED
    if providers:
        return INTERFACE_ONLY
    return "not found at the base commit or in an upstream sub-task"


def build_node_prompt(graph: Graph, node: Node, index: RepoIndex, attempt: int,
                      failure: AttemptFailure | None, *, sandbox: str = "none",
                      context_pack: ContextPack | None = None,
                      workspace: Path | None = None) -> str:
    """Return the English worker prompt for one attempt of ``node``.

    ``sandbox="docker"`` adds a note that bash runs in a Linux container.
    """
    if context_pack is None and workspace is not None:
        context_pack = build_context_pack(node, workspace)
    requires = [f"- `{symbol}`: {requirement_status(graph, node, index, symbol)}"
                for symbol in node.requires]
    requires += [f"- `{symbol}`: {IMPLEMENTED_UPSTREAM}" for symbol in node.requires_impl]
    retry = ""
    if failure is not None:
        retry = RETRY_TEMPLATE.format(
            attempt=attempt, reason=failure.reason,
            output=failure.output[-FAILURE_EXCERPT_CHARS:].strip() or "(no output)")
    if node.edit_set.any_file:
        file_scope = "You may modify or create any file in the repository."
        context_text = ""
    else:
        file_scope = (f"Modify these existing files:\n{_bullets(node.edit_set.modify)}\n\n"
                      f"Create these new files:\n{_bullets(node.edit_set.create)}\n\n"
                      "Do not modify, create, or delete any other file.")
        context_text = context_pack.text if context_pack is not None else "(context unavailable)"
    context_intro = CONTEXT_INSPECTION if len(graph.nodes) > 1 else SINGLE_NODE_CONTEXT
    context_section = f"# Context\n\n{context_intro}\n\n{context_text}\n\n" if context_text else ""
    return NODE_PROMPT_TEMPLATE.format(
        request=graph.request.strip(),
        task_scope=("This is one sub-task of the request above. Other sub-tasks are handled by\n"
                    "other workers; do only this one." if len(graph.nodes) > 1 else
                    "Complete the request in this repository."),
        title=node.title,
        goal=node.goal.strip(),
        file_scope=file_scope,
        provides=_bullets(node.provides),
        contract_note=CONTRACT_NOTE if node.kind == "contract" else "",
        requires="\n".join(requires) if requires else "- (none)",
        context_section=context_section,
        before_start=BEFORE_START if len(graph.nodes) > 1 else "",
        checks=_bullets(node.check.commands),
        sandbox=SANDBOX_NOTE if sandbox == "docker" else "",
        retry=retry,
    )
