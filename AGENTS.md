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

A store for agents' memory. It stores, presents, links and tracks records, and **it decides
nothing**: no verdicts, no statistics, no workflow, no gate. If you find yourself writing a
rule that judges a record, stop; that is a misreading of the scope.

A record has one explicit entry type, an id whose prefix matches that type plus five lowercase
base36 characters normally and six after repeated collisions, an `open | closed` work status,
title, Markdown body, descriptive tags, zero or more stable project references, author (the OS
user), created_at, an optional JSON `value`, artifact references and code captures.
`vocab.ENTRY_TYPES` defines problem-statement, hypothesis, procedure, result, verification, fact,
insight, decision, report and note. An entry type is never repeated in tags. `vocab.WORK_STATUSES` says only whether the caller expects another action on this exact
record; it imposes no workflow conditions.

Four link relations: `builds_on`, `supersedes`, `contradicts`, `cites`. History is append-only: a
revision is a new record with a `supersedes` link and a required `reason`; `set_work_status`
appends a reasoned status event after the initial publication; `retire` marks a record no longer
true; nothing updates or deletes a record, status event, membership event, or framing version.
There is no deletion operation: removing a record from history requires a manual database-history
rewrite by the user. Current project paths are metadata edited in place.

A project has a generated `prj-` id, unique human name, provenance, current paths and optional
append-only framing. `register_project` creates or returns without mutating existing state;
`update_project` applies effective path and framing changes. Changed post-registration framing
requires the current base version and a reason; base 0 adds the first framing, and unchanged
framing is a no-op. Path-only changes do not increment framing. Records have zero-to-many project
memberships through `record_project_event`; the latest event for each pair sets current state.
`set_projects` appends effective changes. A superseding publish with `projects=None` inherits
current memberships under the same exclusive lock; an explicit list replaces them and `[]`
intentionally leaves the revision unscoped.

trails writes only to its own data: the database, its locks, the model cache and the capture
store `<db>/captures.git`. Read `docs/philosophy.md` before adding anything that writes. The
one exception is a write the agent names, such as export to a given path. Code capture builds
its commit with `GIT_OBJECT_DIRECTORY` set to the store and the user's repository as an
alternate, in a temporary index, so the user's repository is never written; a test compares
its whole `.git` before and after.

THE STORE REFUSES EXACTLY TWO THINGS, both as `ValidationError`: malformed input, and an
attempt to overwrite a record. Everything else is recorded as given. Malformed includes a
value wider than its column and two tags the case- and accent-insensitive collation treats as
one; the database itself decides the second, on the failure path of `write_record`.

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
- **SEARCH HITS ARE COMPACT PREVIEWS, NOT RECORDS.** `SearchHit` carries identity, lifecycle,
  latest work status, tags, matched methods, located keyword fields, and one body excerpt when
  known. A body-keyword hit gets a window of at most 240 characters centered on its first match; a
  semantic hit without a body-keyword match gets the complete selected embedding chunk. It does
  not carry a `Record`, full body, status history, provenance, rank, cosine, or chunk number.
  `retrieval.Ranked` keeps diagnostics internally; `show` returns the full record.
  A `work_status` filter returns current versions unless `include_history=true`; searches without
  `work_status` continue to include superseded versions.
- **PRIME ENTRIES ARE REFERENCES, NOT RECORDS.** Scoped prime lists every current record of a
  project, so `PrimeRecordRef` carries id, entry type, title, work status, lifecycle, the id it
  replaces and the last verification, and no body or other `Record` field. Full records there
  made one 10,612,327-byte MCP reply on a 317-record project, and Kiro dropped the connection.
  What prime lists is chosen only from stored fields: the work status the agent set and stored
  `contradicts` links. Every collapsed set comes with the complete `show` or `search` call that
  returns it.
- Vocabularies (`ENTRY_TYPES`, `WORK_STATUSES`, `PROJECT_ACTIONS`, `ENTRY_TYPE_PREFIXES`,
  `RELATIONS`, the record and project id patterns, and `stale_days()`) are defined once in `src/trails/vocab.py`, which also generates
  the schema's ENUM columns.
- `author` is the OS user, from `author.current_author()`. There is no `--author` flag.
- The vector arm is the `search` extra. Everything must keep working when it is absent.
  `engine.build_engine()` is THE construction seam for the embedding engine; the test suite
  patches it to keep the default gate offline.
- Never commit ONNX model weights. `models/` is gitignored.
- The record file (`record_file.py`) is Markdown with TOML front matter between `+++` lines.
  TOML, not YAML, because YAML coerces `no` to false. `value` is one TOML string holding JSON,
  because TOML has no null. An export omits the record's own `supersedes` link: publishing the
  file supersedes the exported record. Exported files always write explicit stable project IDs;
  current names appear only in a TOML comment.
- `reachability.py` reads local files and the capture store only, never a remote location; a
  URI, query or free text is `unresolvable`, which means not checked. `show` summary checks
  existence only; provenance and lint compare digests. A directory is `referenced`.
- There is no migration. A database is created with the current schema on first open.

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
  batch-first: `Store.get_records` assembles any number of records in ONE invocation, ten SELECTs through `dolt.read_result_sets` (dolt prints one JSON document per statement, `{}`
  for an empty set). A plain `write_record` performs one candidate-existence read, then one write
  invocation. The exclusive lock spans both so an older equal-timestamp row cannot be mistaken for
  this command landing before it reported failure.
- **A failed write is classified by re-reading, never by matching Dolt's message**
  (`Store._classify_failed_write`).
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
