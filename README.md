# trails

A general memory store for agents: records, the links between them, the data they point at,
and the code that produced them. Binaries: `trails` (CLI) and `trails-mcp` (MCP server on
stdio). Distribution name `agent-trails`; import name `trails`.

## What it does, and what it does not

trails stores, presents, links and tracks records. **It decides nothing.** It computes no
statistics, judges no result, enforces no workflow and holds no opinion about which record is
right. It serves an ad hoc analysis, a design session and a formal study the same way. No LLM
runs inside any operation.

The store refuses exactly two things, both as `ValidationError`:

1. **Malformed input**: an empty title, a supersession with no reason, a link to a record that
   does not exist, a malformed id or digest, a value wider than its column, or two tags the
   database's case- and accent-insensitive comparison treats as one (`cafe` and `café`).
2. **An attempt to overwrite a record**: publishing under an id that already exists.

Everything else is recorded as given.

## Records

Every record has one explicit entry type and one caller-selected work status.

```
id          type prefix + "-" + 5 lowercase base36 characters normally; 6 after repeated collisions
entry_type  problem-statement, hypothesis, procedure, result, verification, fact,
            insight, decision, report, or note
work_status open when another action is expected on this exact record; closed otherwise
title       one line, required
body        Markdown
tags        sorted, lowercase descriptive labels; an entry type is not a tag
projects    zero or more stable project IDs with current human names
author      the operating system user
created_at  UTC
value       optional JSON object: structured numbers or fields
artifacts   references to data: a local path, an s3:// URI, a query, or free text,
            each with an optional sha256 digest and notes; data is never copied
code        captured commits of the repositories that produced the record, held in
            trails' own git store
```

Generated record id prefixes are `prb-`, `hyp-`, `pro-`, `res-`, `ver-`, `fac-`, `ins-`,
`dec-`, `rpt-`, and `note-`, matching the entry types above. Projects use generated `prj-`
ids. Generation tries ten five-character
base36 candidates, then ten six-character candidates, checking the complete ID for a collision.
The old `rec-` plus 16-hex form and the prior type-prefixed 16-hex form remain recognizable for
archived identifiers but are not generated.

`publish` requires an entry type and initial work status. The initial status needs no reason.
`set_work_status` appends an `open` or `closed` transition with its time, author and required
reason. Any entry type may move in either direction; trails records the caller's decision and
imposes no workflow conditions. This work status is separate from the derived lifecycle of
current, superseded or retired.

Four link relations: `builds_on`, `supersedes`, `contradicts`, `cites`.

History is append-only. A revision is a new record linked `supersedes` onto the old one, with
a required one-sentence `reason`; the old record then reports `superseded_by`. `retire` marks a
record no longer true without replacing it. Work-status and project-membership changes append
events. A superseding publish inherits current memberships when `projects` is omitted; an explicit
list replaces them, and `projects=[]` intentionally makes the revision unscoped. Nothing updates
or deletes a record, status event, membership event, or framing version. Current project paths are
metadata edited in place, with Dolt's history keeping every version.

## Code capture

`publish(repos=[...])` captures each repository's whole working tree, uncommitted and
untracked files included, as a commit in `<db>/captures.git`, a bare repository trails owns.
Nothing is written to your repository: the commit is built through a temporary index with git's
object directory pointed at the store, and the repository's own objects are read as an
alternate. Every object the capture needs is then packed into the store, so the capture reads
after the repository is gone. The capture commit has no parent; the commit you had checked out
is recorded beside it.

A file over 50 MiB (`TRAILS_MAX_CAPTURE_BYTES`) refuses the publish, and the refusal prints
two complete calls: one leaving the file out with `exclude` and referencing it as an artifact,
one keeping it with `allow_large`. A committed large file already in the store is not refused
again.

## Projects

A project is a stable body of work, not a repository. `register_project` creates a generated
`prj-` id for a unique one-line human name and records its creator, host, creation time, initial
paths and optional initial framing. Repeating registration returns the existing project without
mutating it. `update_project` applies later path or framing changes. Neither command creates
anything at a registered path, and missing local paths produce warnings.

Optional project framing is attached to the project. Its one-line title and Markdown body state
purpose and motivation: why the project exists, who it serves, long-term success, and what is out
of scope. Operational knowledge belongs in paths or ordinary records. Initial framing supplied
at registration becomes version 1 without a base or reason. Later changed framing requires the
current `base_version` and a reason; use base 0 to add the first framing after registration. The
store writes `base_version + 1`. Unchanged title and body are a no-op, and path-only changes do
not increment framing. Old versions and embeddings remain; current framing is searchable.

Records belong to zero, one or several projects through append-only membership events.
`publish(projects=[...])` writes initial memberships atomically. `set_projects` changes grouping
later without changing the record. `prime()` lists a small project index page and current unscoped count; its default page holds 100
projects because the measured 1,000-project JSON response without paging was 155,184 bytes.
`prime(project=...)` shows one project's current framing and paths in full, then its current
records without bodies: problem statements as title lines with the `show` call that reads them,
open and contradicted records one line each, and per entry type the count of closed records with
the `search` call that lists them. Measured on a 317-record project, the earlier full-record
response was one 10,612,327-byte MCP message, which Kiro dropped; the compact one is about 180 KB.

## A possible secret is a warning

Every write is scanned with detect-secrets (keyword, AWS key, token and private-key detectors;
the two high-entropy detectors are off, because they flag every sha256 digest). A finding names
the field, the line and the detector, never the matched text, and the record is still stored.
The agent is told to tell the user. Removing a written secret requires the user to perform a
manual database-history rewrite. A secret written in plain prose is not detected.

## Record files

A record exports to Markdown with TOML front matter between `+++` lines. The header includes the
record's required `entry_type`, latest `work_status`, and an explicit `projects` list of stable
IDs. Current project names appear in a TOML comment for readability and are not parsed as identity;
`publish(from_file=...)` reads the IDs back. A file whose header names an existing id publishes a superseding record and needs a
`reason` line in the header. TOML rather than YAML, because YAML silently coerces values such as
`no` to false.

## Operations

Each operation is one async function under `src/trails/ops/`, registered on both surfaces from
the same signature and docstring. Every result renders as text: the CLI prints it (or the model
as JSON with `--json`), and MCP returns the text together with the model as structured content.
Search returns separately ranked current-framing project matches before compact record hits.
Project matches are capped at three and report `project_total`; record-only filters suppress them.
Record hits contain compact metadata and a body excerpt when the matching passage is known. A body-keyword hit gets a window of at most 240 characters centered on its first match; a
semantic hit without a body-keyword match gets the complete selected embedding chunk. Hits omit
status history, provenance, and retrieval scores. Use `show` to read the complete record. A record-writing operation ends with a `Next:` line naming the `show()` call for the record it wrote.

The MCP server's default toolset is the twelve agent operations: `prime`, `search`, `show`,
`lineage`, `register_project`, `update_project`, `publish`, `set_projects`, `set_work_status`,
`link`, `retire` and `export`. `trails-mcp --toolset full` adds `lint`, `alias`, `mark_verified`, `embed` and `version`. The CLI has every operation; `trails <name> --help` describes each.

## Concurrency

Every write goes through `dolt.run_script`, which holds an exclusive `fcntl.flock` on
`<db>/.trails-write.lock`; every read holds the same lock shared. Without the shared read
lock, a read that overlaps a commit makes that commit fail on dolt 2.4.0 ("cannot update
manifest: database is read only"). An earlier stress measurement persisted 400 of 400 writes
from 8 processes in about 52 seconds, about 8 writes per second. The default regression holds
the real lock in one process and verifies that another process waits before writing once; the
ordinary suite does not repeat the stress run. There is no retry.

## Development

```bash
uv sync                      # core, plus the dev group
uv sync --extra search       # the vector arm alone (onnxruntime + tokenizers)

uv run ruff check . && uv run ruff format --check . && uv run ty check . && uv run pytest
uv run pytest -m slow        # tests that download the ONNX model
```

`ty check` exits 0 even when it prints a diagnostic, so read its output. The suite runs in
parallel via `pytest-xdist` against real Dolt, about a minute; there is no fake store, because
the properties worth testing are the ones a fake would stub out.

The store lives at `~/.trails/db`, overridable with `TRAILS_DB`. A database is created with
the current schema on first use; there is no migration.

## License

AGPL-3.0-only.
