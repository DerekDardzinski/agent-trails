# Agent Instructions — trails

## Issue tracking

Issues for this package live in the `trails` Beads database on the shared Dolt server at
`~/.beads/shared-server`, which `bd` starts on demand. Run `bd` from the repo root and it
resolves there; do not set `BEADS_DIR`. Auto-export is off, so `.beads/issues.jsonl` is
gitignored rather than committed, because that file is the source of merge conflicts when
several worktrees write at once.

```bash
bd status                  # counts
bd ready --json            # unblocked work
bd kv get tooling.gate     # the four-command gate, below
```

The design lives in the root epic `labbook-79y` and its children (`bd show labbook-79y`).

## Tool gate

Run all four before reporting work complete:

```bash
uv run ruff check . && uv run ruff format --check . && uv run ty check . && uv run pytest
```

`ty check` exits 0 even when it prints a diagnostic, so read its output. The suite is parallel
by default via `pytest-xdist` in `addopts`; do not remove that. `uv run pytest -m slow` covers
tests that download the ONNX embedding model, and those stay off the default gate. About a
minute of the default gate is `tests/test_concurrency.py`, whose 400 writes are serialized by
the write lock.

## What this package is

A memory and task store for agents across harnesses. It stores, presents, links and tracks
entries, and **it decides nothing about their meaning**: no verdicts, no statistics, no gate.
Derived state (readiness, ready to close, claim standing) comes only from stored links, events
and version fields. If you find yourself writing a rule that reads what an entry says, stop;
that is a misreading of the scope.

An entry has one fixed kind (`vocab.KINDS`: goal, task, note, claim, preference, skill), an id
whose prefix matches the kind (`gol`, `tsk`, `note`, `clm`, `prf`, `skl`) plus five lowercase
base36 characters normally and six after repeated collisions, and append-only versions. A
version carries the full content: title, Markdown body, tags, an optional JSON `value`,
artifact references, code captures, `priority` (goals and tasks) and `stated_by` (claims and
preferences). Every version, link event, task event, comment, retirement and membership event
records `author` (the OS user), `harness` and `session_id`; `sessions.py` resolves the harness
and session, and the agent never passes them. One `session` row per session id holds its
transcript path and agent name.

Seven link relations (`vocab.LINK_RELATIONS`): `waits_on`, `part_of`, `discovered_from`,
`supersedes`, `contradicts`, `supports`, `references`. A link is current when the latest event
for its (source, relation, target) is `add`, and each add records the target version it was
written against. Task events are claim, release, close (with an outcome from
`vocab.OUTCOMES`) and reopen. Comments sit beside an entry and write no version; a comment
with `addresses` closes the open comments it names.

History is append-only. A new version requires `base_version` equal to the current version
and a reason; nothing updates or deletes a version, an event or a comment. There is no
deletion operation: removing data from history requires a manual database-history rewrite by
the user. Current project paths are metadata edited in place.

A project has a generated `prj-` id, unique human name, provenance, current paths and optional
append-only framing. `register_project` creates or returns without mutating existing state;
`update_project` applies effective path and framing changes. Changed post-registration framing
requires the current base version and a reason; base 0 adds the first framing. Entries have
zero-to-many project memberships through `entry_project_event`; a preference with none is
global.

trails writes only to its own data: the database, its locks, the model cache and the capture
store `<db>/captures.git`. Read `docs/philosophy.md` before adding anything that writes. Code
capture builds its commit with `GIT_OBJECT_DIRECTORY` set to the store and the user's
repository as an alternate, in a temporary index, so the user's repository is never written; a
test compares its whole `.git` before and after.

THE STORE REFUSES EXACTLY TWO THINGS, both as `ValidationError`: malformed input, and an
attempt to overwrite, which is a version written from a stale or missing `base_version`, or a
claim on a task another session holds without `take_over_reason`. Everything else is recorded
as given, with a warning where it is worth noticing. Malformed includes a value wider than its
column and two tags the case- and accent-insensitive collation treats as one; the database
itself decides the second, on the failure path of a write.

A possible secret never blocks a write. `secret_scan.scan_for_secrets` runs detect-secrets
through its Python API with the two high-entropy plugins off (with them on, every sha256
digest is flagged) and returns field, line and detector, never the matched text.

## Conventions

- Python 3.12, src layout, hatchling, uv.
- **ONE DEFINITION PER OPERATION, BOUND TO TWO SURFACES.** Every operation lives in its own
  module under `src/trails/ops/`, exposing exactly one async function whose name matches the
  module, discovered by `ops.operations()`. Two binders under `src/trails/surfaces/` register
  all of them, `cli.py` with cyclopts and `mcp.py` with fastmcp, and neither contains a
  per-operation branch. Add an operation module rather than editing a binder.
- **`surfaces/cli.py` must never import fastmcp**, directly or transitively. Measured:
  `import fastmcp` costs 480ms against 62ms for `import cyclopts`. A test asserts it.
- **The MCP binder binds the client's name per call** into `sessions.CLIENT_NAME`, which is how
  a write learns its harness on that surface. Session detection decides the harness before it
  reads any harness variable, because a harness launched from another's shell inherits its
  variables (trails `fac-ahxap`).
- Rules for an operation, each with a reason not visible from either surface alone:
  1. **A signature contains ONLY operation parameters.** The store, `--db`, `--json` and every
     `--<name>-from-file` twin are synthesized by the binder.
  2. **Every enum-valued parameter is `Literal`, sourced from `vocab.py`.**
  3. **A cross-parameter rule goes in the operation BODY**, never in a cyclopts validator,
     which would run on the CLI only.
  4. **Operations RAISE and binders translate.** Exit 2 for a usage error, exit 1 for a
     refusal. MCP returns a refusal as an error result with `isError: true` and readable
     `{"error": ...}` structured content.
  5. **EVERY operation returns a `RenderedResult` subclass that overrides `render_text`.**
     The CLI prints `render_text()` (JSON with `--json`); MCP returns it as text content
     beside the model as structured content. A bare union or optional model would get a
     synthetic `{"result": ...}` envelope on MCP only, so `ops.operations()` refuses it.
  6. **Each operation module declares `OPERATION = Operation(...)`** (`ops/_marks.py`):
     toolset `agent`, `full` or `cli`, and the read_only, destructive and idempotent hints.
  7. **The docstring has a summary and the sections `Use this when:`, `Use instead:` and
     `Returns:`, and every parameter has a `Field(description=...)`.**
     `tests/test_descriptions.py` asserts this for every discovered operation.
  8. **NO module under `ops/` carries `from __future__ import annotations`.** With it,
     pydantic's `FieldInfo` has no value equality and `get_type_hints(fn) != get_type_hints(fn)`,
     which breaks the signature equality `context.bind` must preserve. The binders omit it too.
- **NO response model uses a pydantic serialization alias.** fastmcp honours an alias and the
  CLI's `model_dump(mode="json")` ignores it, so the two surfaces would emit different keys.
- **EVERY TASK-CHANGING RESPONSE REDRAWS THE AFFECTED GRAPH** (`present.graph_lines`): the
  changed entry, its parent chain, its children and both ends of its `waits_on` links, each with
  its state. The store cannot know which links the agent meant to add; this is the only guard
  against a forgotten or reversed one.
- **SEARCH HITS ARE COMPACT PREVIEWS, NOT ENTRIES.** A hit carries id, kind, version, title,
  state, tags, projects, claim standing, open comment count, a note's evidence users, the
  rankers that matched, and an excerpt. `show` returns the full entry. Search reads current
  versions only, and a note's fused score is halved.
- **PRIME'S SIZE DOES NOT GROW WITH HISTORY**, except the preference section, which prints
  preferences in full under a 10,000-character cap (Claude Code's SessionStart limit) and then
  title lines. Listing every record once made one 10,612,327-byte MCP reply on a 317-record
  project, which Kiro dropped.
- Vocabularies (`KINDS`, `KIND_PREFIXES`, `LINK_RELATIONS`, `OUTCOMES`, `VERDICTS`,
  `STATED_BY_VALUES`, `TASK_STATES`, `TASK_EVENTS`, `SEARCH_STATUSES`, `PROJECT_ACTIONS` and
  the id patterns) are defined once in `src/trails/vocab.py`, which also generates the schema's
  ENUM columns.
- `author` is the OS user, from `author.current_author()`. There is no `--author` flag.
- The vector arm is the `search` extra. Everything must keep working when it is absent.
  `engine.build_engine()` is THE construction seam for the embedding engine; the test suite
  patches it to keep the default gate offline. Embeddings are keyed by entry, version and chunk.
- Never commit ONNX model weights. `models/` is gitignored.
- `reachability.py` reads local files and the capture store only, never a remote location; a
  URI, query or free text is `unresolvable`, which means not checked. A directory is
  `referenced`.
- There is no migration. A database is created with the current schema on first open, and a
  database holding the old record schema is refused. The default is `~/.agent-trails/db`.

## Dolt constraints, all established by probe

- **One `dolt sql -q` invocation is one transaction**, and `CALL DOLT_COMMIT` must be its
  single finisher. A statement failing mid-script aborts the rest and rolls back.
- **`CALL DOLT_COMMIT` raises "nothing to commit"** on a transaction that changed nothing, so
  every idempotent path returns before reaching it.
- **Concurrent dolt processes lose writes unless serialized.** A `dolt sql` process that starts
  while another holds the database opens it read-only, and a writer opened that way fails at
  `CALL DOLT_COMMIT` with "cannot update manifest: database is read only". So `dolt.run_script`
  holds `<db>/.trails-write.lock` EXCLUSIVE and `read_rows` and `read_result_sets` hold it
  SHARED. Measured on dolt 2.4.0: with writes alone locked, 8 x 50 writers failed 4 of 4
  runs; with reads locked shared as well, 400 of 400 in every run, about 52 s. No retries.
- **Every read is a subprocess, and invocations are the cost** (about 70ms each). Reads are
  batch-first: `EntryStore.get_entries` assembles any number of entries in ONE invocation, and
  `EntryStore.task_graph` loads every goal, task and their links in one, through
  `dolt.read_result_sets` (dolt prints one JSON document per statement, `{}` for an empty set).
  A write holds the exclusive lock across its reads and its one write invocation, so the
  `base_version` check and the insert are one serialized decision.
- **A failed write is classified by re-reading, never by matching Dolt's message**
  (`EntryStore._commit`).
- **`dolt sql -r json` omits a NULL column** from the row object; read nullable columns with
  `.get`.
- **An ENUM column returns its ORDINAL once it passes through `COALESCE`**; compare each value
  to its literal separately.
- **A JSON column arrives already decoded.** Do not parse it again.
- **There is no client-side parameter binding.** `dolt.sql_literal` builds literals and
  `store._bind` substitutes `?` positionally. Search terms are always bound.
- **Every text column is `COLLATE utf8mb4_0900_ai_ci`, except `project_path.path`**, which is
  binary so that two paths differing only in case are two paths. The default utf8mb4_0900_bin makes
  FULLTEXT MATCH and LIKE case-sensitive. `SHOW FULL COLUMNS` reports a column collation only
  inside its `Type` field; `information_schema.columns.COLLATION_NAME` reports it directly.
- **Dolt FULLTEXT never matches tokens under 3 characters.**
- **Vectors are written only as `STRING_TO_VECTOR('[...]')`** with fixed-point formatting
  (`dolt.vector_literal`).

## Testing

Real Dolt, no fake store: the properties worth testing are exactly the ones a fake would stub
out. The `store` fixture copies a session-scoped template rather than initialising per test.

Every test should name the specific silent failure it catches. Use hardcoded literal expected
values, never a value recomputed with the implementation's own logic.

## Non-interactive shell

Use non-interactive flags so nothing blocks on a prompt: `cp -f`, `mv -f`, `rm -f`, `rm -rf`.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
