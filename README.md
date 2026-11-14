# trails

A memory and task store for agents, shared across harnesses: goals and tasks with dependencies
and claims, notes, claims about what is true, a person's preferences, and the links between
them, each write attributed to the harness, session and transcript that made it. Binaries:
`trails` (CLI) and `trails-mcp` (MCP server on stdio). Distribution name `agent-trails`; import
name `trails`.

## What it does, and what it does not

trails stores, presents, links and tracks entries. **It decides nothing about their meaning.**
Every state it reports (a task is ready, a goal is ready to close, a claim is an observation)
is derived from stored links, events and version fields, never from what an entry says. No LLM
runs inside any operation.

The store refuses exactly two things, both as `ValidationError`:

1. **Malformed input**: an empty title, a link to an entry that does not exist, a priority on a
   note, `addresses` naming a comment that is not open on the same entry, a value wider than its
   column, or two tags the database's case- and accent-insensitive comparison treats as one.
2. **An attempt to overwrite**: a new version written from a stale or missing `base_version`,
   or a claim on a task another session holds without `take_over_reason`.

Everything else is recorded as given, with a warning where it is worth noticing.

## Entries

Every entry has one kind, fixed at creation:

```
goal        one initiative's framing and success criteria; never claimed; closed by judgment
task        something to do; ready queue, claims, close outcomes
note        an agent's scratch pad; evidence for other entries; ranks below other kinds
claim       anything stated as true or possibly true; standing comes from what backs it
preference  how the person wants work done; printed in full at session start
skill       how to do something; written only by the offline consolidation pass
```

Ids are the kind's prefix (`gol`, `tsk`, `note`, `clm`, `prf`, `skl`) plus five lowercase base36
characters, six after repeated collisions. An entry has append-only versions on that one id.
A version carries the full content: title, Markdown body, tags, an optional JSON `value`,
artifact references, code captures, `priority` (0 to 4, goals and tasks) and `stated_by`
(`agent` or `person`, claims and preferences). `write(id=..., base_version=..., reason=...)`
writes the next version; omitted fields keep their values and `[]` clears a list.

Every write records the OS user, the harness, and the session. The harness comes from the MCP
client name or the process tree, decided before any harness variable is read; Kiro sessions are
found through `~/.kiro/session-leases`, Claude Code sessions through `CLAUDE_CODE_SESSION_ID`.
Each session's transcript path and agent name are stored once.

## Links, tasks and comments

Seven relations: `waits_on`, `part_of`, `discovered_from`, `supersedes`, `contradicts`,
`supports`, `references`. Links are add and remove events, and each add records the target
version it was written against, so `show` and `lint` can say "written against v2, now v3".

A task is **ready** when it is not closed or retired, none of its children is open, and every
goal or task it or an ancestor waits on is closed `completed`. Close outcomes: `completed`
unblocks dependents; `dropped` and `failed` leave them waiting and flag them for replanning;
`superseded` with `by=[ids]` moves every dependent onto every replacement. A parent with every
child closed and none failed is **ready to close**; a failed child means it **needs a
decision**. Every task-changing response redraws the graph around the change with each entry's
state.

Comments sit beside an entry and never create a version. A comment is open until another
comment `addresses` it. A comment on a goal or task pins the version of everything under it, and
`show(id, since_comment=...)` lists what changed after it.

## Code capture

`write(repos=[...])` captures each repository's whole working tree, uncommitted and untracked
files included, as a commit in `<db>/captures.git`, a bare repository trails owns. Nothing is
written to your repository: the commit is built through a temporary index with git's object
directory pointed at the store, and the repository's own objects are read as an alternate. The
capture reads after the repository is gone.

A file over 50 MiB (`TRAILS_MAX_CAPTURE_BYTES`) refuses the write, and the refusal prints two
complete calls: one leaving the file out with `exclude`, one keeping it with `allow_large`.

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
ready             the next tasks, and what needs a decision or replanning
write             create an entry or write its next version
link / unlink     add or remove a link
claim / release   take or give back a task
close / reopen    close a goal or task with an outcome, or reopen it
add_comment       feedback that needs a response, or the response that closes it
show              read entries or projects by id
search            find entries by meaning, id or filters; also projects
retire            mark an entry no longer true
register_project / update_project / set_projects
```

`trails-mcp --toolset full` adds `lint`, `alias`, `embed` and `version`. The CLI has every
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
