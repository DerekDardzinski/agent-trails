# trails

A memory and task store for agents, shared across harnesses: tasks with dependencies and
claims, notes on what is known, a person's preferences, skills, and the links between them, each write attributed to the harness, session and transcript that made it. Binaries:
`trails` (CLI) and `trails-mcp` (MCP server on stdio). Distribution name `agent-trails`; import
name `trails`.

## What it does, and what it does not

trails stores, presents, links and tracks entries. **It decides nothing about their meaning.**
The one state it works out is whether a task is ready; everything else it reports is a stored
fact, such as how each child task ended or the state of a task another one waits on. No LLM
runs inside any operation. `docs/philosophy.md` holds the seven principles every change is
checked against.

The store refuses exactly two things, both as `ValidationError`:

1. **Malformed input**: an empty title, a link that would close a dependency cycle, a link to an entry that does not exist, a link between
   the wrong kinds (work links join tasks, evidence links join notes, preferences and skills), a
   priority on a note, `addresses` naming a comment that is not open on the same entry, a value wider than its
   column, or two tags the database's case- and accent-insensitive comparison treats as one.
2. **An attempt to overwrite**: a new version written from a stale or missing `base_version`,
   or a claim on a task another session holds that does not name that session as `holder`.

Everything else is recorded as given. Warnings cover only what happens outside the store: data
in a temporary folder, code outside git, a path that does not exist, a possible secret, or an
operational failure such as an embedding error.

## Entries

Every entry has one kind, fixed at creation:

```
task        something to do; ready queue, claims, close outcomes; a task with children is a parent
note        anything known or found; evidence for other notes
preference  how the person wants work done; printed in full at session start
skill       how to do something; written by the offline consolidation pass
```

A goal is a tag on a parent task. Ids are the kind's prefix (`tsk`, `note`, `prf`, `skl`) plus five lowercase base36
characters, six after repeated collisions. An entry has append-only versions on that one id.
A version carries the full content: title, Markdown body, tags, an optional JSON `value`,
artifact references, code captures and `priority` (0 to 4, tasks); each kind takes only its own
fields. Each kind has its own create and revise operation (`create_task`, `revise_task`, and so
on); a revise takes the id (its pattern names the kind), the `base_version` you read and a
reason, and omitted fields keep their values while `[]` clears a list. `set_priority` changes a
task's priority. Ids are minted under the write lock, so two concurrent creations never collide.

Every write records the OS user, the harness, and the session. The harness comes from the MCP
client name or the process tree, decided before any harness variable is read; Kiro sessions are
found through `~/.kiro/session-leases`, Claude Code sessions through `CLAUDE_CODE_SESSION_ID`.
Each session's transcript path and agent name are stored once.

## Links, tasks and comments

Work relations `waits_on` and `part_of` join tasks; evidence relations `supports` and
`contradicts` join notes, preferences and skills; `supersedes` is written only when a task is
closed superseded. Links are add and remove events, and each add records the target
version it was written against, so `show` and `lint` can say "written against v2, now v3".
`link_work` and `unlink_work` take task ids; `link_evidence` and `unlink_evidence` take note,
preference and skill ids. Linking evidence again after its target changed records it at the new
version, shown with the harness and session that vouched for it.

A task is **ready** when it is not closed or retired, none of its children is open, and every
task it or an ancestor waits on is closed `completed`. `close` records `completed`, which
unblocks dependents, or `dropped` or `failed`, which leave them waiting; `replace` closes a task
`superseded` by other tasks and moves every dependent onto every replacement. Claiming or
closing a task that is already closed changes nothing and names its state. A task's state is shown as facts: "children: 2 completed, 1
failed", "waits on tsk-x (closed dropped)". `blocked` lists the tasks waiting on a task that is
not closed completed. `show` names the tasks the creating session held claims on when an entry
was created. Every task-changing response redraws the graph around the change.

Comments sit beside an entry and never create a version. A comment is open until another
comment `addresses` it. A comment on a task pins the version of everything under it, and
`show(id, since_comment=...)` lists what changed after it.

## Code capture

`create_note(repos=[{"path": ...}])` captures each repository's whole working tree, uncommitted and untracked
files included, as a commit in `<db>/captures.git`, a bare repository trails owns. Nothing is
written to your repository: the commit is built through a temporary index with git's object
directory pointed at the store, and the repository's own objects are read as an alternate. The
capture reads after the repository is gone.

A file over 50 MiB (`TRAILS_MAX_CAPTURE_BYTES`) refuses the write, and the refusal prints two
complete calls: one leaving the file out with that repository's `exclude`, one keeping it with
its `allow_large`. Both lists live inside the repository object, so neither can be written
without a repository.

## Projects

A project is a stable body of work, not a repository. `register_project` creates a generated
`prj-` id for a unique human name with paths and optional framing; `update_project` changes them,
and changed framing needs the current `base_version` and a reason. Entries belong to zero or more
projects through append-only membership events; `set_projects` changes grouping without a new
version. A preference with no project is global.

## A possible secret is a warning

Every write is scanned with detect-secrets (keyword, AWS key, token and private-key detectors;
the two high-entropy detectors are off, because they flag every sha256 digest). A finding names
the field, the line and the detector, never the matched text, and the entry is still stored.
Removing a written secret requires a manual database-history rewrite by the user.

## Operations

Each operation is one async function under `src/trails/ops/`, registered on both surfaces from
the same signature and docstring. The MCP server's default toolset:

```
prime             start a session: preferences in full, then where work stands
ready             the next tasks
blocked           the tasks waiting on another task, with that task's state
create_task / create_note / create_preference
revise_task / revise_note / revise_preference
set_priority      change a task's priority
link_work / unlink_work          a dependency or parent link between tasks
link_evidence / unlink_evidence  a supports or contradicts link between knowledge entries
claim / release   take or give back a task (claim names the holder it takes over)
close / reopen    close a task with an outcome, or reopen it
replace           close a task as superseded by other tasks
add_comment       feedback that needs a response, or the response that closes it
show              read entries or projects by id
search            find entries by meaning, id or filters; also projects
retire            mark a note, preference or skill no longer true
register_project / update_project / set_projects
```

`trails-mcp --toolset dreamer` adds `create_skill` and `revise_skill` for the consolidation
pass; `--toolset full` adds those and `lint`, `alias`, `embed` and `version`. The CLI has every
operation, with multi-word names split (`trails add comment`, `trails register project`);
`trails <name> --help` describes each.

## Concurrency

Every write goes through `dolt.run_script`, which holds an exclusive `fcntl.flock` on
`<db>/.trails-write.lock`; every read holds the same lock shared. Without the shared read
lock, a read that overlaps a commit makes that commit fail on dolt 2.4.0 ("cannot update
manifest: database is read only"). A write holds the lock across its `base_version` check and
its insert, so two processes writing from one base cannot both land; a test runs exactly that
race. There is no retry.

## Development

```bash
uv sync                      # core, plus the dev group
uv sync --extra search       # the vector arm alone (onnxruntime + tokenizers)

uv run ruff check . && uv run ruff format --check . && uv run ty check . && uv run pytest
uv run pytest -m slow        # tests that download the ONNX model
```

`ty check` exits 0 even when it prints a diagnostic, so read its output. The suite runs in
parallel via `pytest-xdist` against real Dolt; there is no fake store, because the properties
worth testing are the ones a fake would stub out.

The store lives at `~/.agent-trails/db`, overridable with `TRAILS_DB`. A database is created
with the current schema on first use; there is no migration, and a database holding the old
record schema (such as `~/.trails/db`) is refused rather than read.

## License

AGPL-3.0-only.
