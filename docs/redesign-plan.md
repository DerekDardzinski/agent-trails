# trails redesign: implementation plan (first version), revision 2

Goal: trails record `prb-vqur3`. Specification: decision records `dec-o0gww` (sessions as
metadata, versioned stable ids, tasks, planning, comments), `dec-ix8zd` (six kinds),
`dec-9b2ta` (preferences), `dec-ylv0a` (skills, deferred here), and fact `fac-ahxap` (Claude
Code session discovery). Every choice this plan makes that those records do not is listed
under "Decisions this plan makes" (D-numbers below refer there) and in
`docs/redesign-review-list.md` for Derek.

Revision 2 answers the plan review on Beads `labbook-a7p.1` (5 blocking findings plus 1 held
back; all addressed below).

Built on branch `redesign` in worktree `../agent-trails-redesign`, against a fresh database.
No migration. The default database moves to `~/.agent-trails/db`; the old `~/.trails/db` stays
untouched as an archive.

## Scope

In: six kinds; stable ids with append-only versions and `base_version`; seven link relations
with add/remove events; harness and session provenance on every write (Kiro and Claude Code
adapters); tasks (create, claim, release, close with outcomes, reopen, ready queue); comments
with `addresses` and an optional verdict; preferences printed by `prime` under a
10,000-character cap; `prime`, `search` and `show` reworked; projects, tags and aliases, code
capture, artifacts, secret scanning and embeddings carried over.

Deferred: the dreamer; skill writing by the dreamer and symlinks (D9); `export`/record files,
`lineage`, `mark_verified` (D4); `parent_session` (D12).

## Vocabulary (`vocab.py`, which also generates ENUM columns)

- Kinds and id prefixes: `goal` gol, `task` tsk, `note` note, `claim` clm, `preference` prf,
  `skill` skl. Id = prefix + `-` + 5 (then 6) base36 characters, as today.
- Relations: `waits_on` (D2), `part_of`, `discovered_from`, `supersedes`, `contradicts`,
  `supports`, `references`.
- Outcomes: `completed`, `dropped`, `failed`, `superseded`.
- Verdicts: `approve`, `changes_requested`.
- Stated by: `agent`, `person` (claims and preferences; see Claim standing).
- Task states: `open`, `claimed`, `closed`. "Not closed" means `open` or `claimed`.

## Data model

All text columns `utf8mb4_0900_ai_ci` except paths, as today.

- `entry` (id PK, kind, created_at): identity, written once.
- `entry_version` (entry_id, version, title, body, value JSON, priority, stated_by, reason,
  author, harness, session_id, created_at; PK entry_id+version; FULLTEXT on title and body).
  Version 1 is the creation. `priority` is NULL except on goals and tasks (D6).
- `version_tag`, `version_artifact`, `version_code`: per version, as today's per-record tables.
- `link_event` (source_id, relation, target_id, sequence, action add|remove,
  target_version, reason, author, harness, session_id, created_at). A link is current when the
  latest event for its (source, relation, target) is `add`.
- `task_event` (entry_id, sequence, event claim|release|close|reopen, outcome, reason,
  author, harness, session_id, created_at). Claim and release only on tasks; close and reopen
  on tasks and goals.
- `comment` (id `cmt-xxxxx` PK, entry_id, entry_version, body, verdict, author, harness,
  session_id, created_at), `comment_addresses` (comment_id, addressed_id), `comment_pin`
  (comment_id, entry_id, version).
- `session` (id PK, harness, transcript, agent, first_seen): one row per session id, written
  on first sight.
- `retirement` (entry_id PK, retired_at, reason, author, harness, session_id).
- Projects, project paths, framing versions, membership events (keyed by entry id), tag
  aliases, embeddings (keyed by entry_id, version, chunk): as today.

`harness` is on every event row itself, not only on `session`, so a harness that gives no
session id is still recorded (held-back review finding).

Opening: a database that has a `record` table and no `entry` table is the old schema; open
refuses it with `StoreUnavailableError` naming the path and `TRAILS_DB` (D13).

## Writing versions

- `write` without `id` creates version 1. With `id` it requires `base_version` equal to the
  current version and a nonempty `reason`; it writes current + 1.
- A stale or missing `base_version` is refused as an attempted overwrite. The refusal names
  the current version, its author and time, and the complete retry call with
  `base_version=<current>`.
- A version carries its full content: title, body, tags, value, artifacts, code, priority.
  Omitted optional fields inherit the previous version's values; `tags=[]` etc. clears them
  (D14).
- `parent` and `waits_on` are accepted only at creation; later edges use `link` (D7).

## Derived state (deterministic, from stored links, events and version fields only)

Readiness and closing:

- A child of X is a not-retired entry with a current `part_of` link to X. Descendants are
  children, transitively.
- A task is **ready** when all hold: it is not closed and not retired; none of its children is
  not closed; and every current `waits_on` target that is a task or goal, of it and of each of
  its ancestors (via `part_of`), is closed `completed`. A `waits_on` target of any other kind
  is ignored for readiness and warned about at write time (D10); a task whose only `waits_on`
  target is a note is ready. A retired task or goal target counts as not completed.
- A claimed task can be ready; `ready` lists it with the claimant's session and claim age.
- `ready` lists ready tasks sorted by priority ascending (NULL last), then created_at
  ascending. It loads every not-closed task, its links and its ancestors' links in one read
  invocation and computes in Python (D15).
- An entry (goal or task) is **ready to close** when it is not closed, has at least one child,
  every child is closed, and no child closed `failed`. A leaf task is never ready to close. A
  parent with a child closed `failed` is listed as **needs a decision**, with the calls to add a
  replacement child or close the parent. Goals and tasks follow the same rule (D16).
- Closing a parent that has children not closed is stored as given, with a warning listing
  them. Their readiness is unchanged: a parent's closure does not close or block children.
- Dependents of a task closed `dropped` or `failed` keep waiting; `ready` and the close
  response list them as **needs replanning**.
- `close(outcome="superseded", by=[ids])` requires a nonempty `by`; every id must be an
  existing task or goal (else refused). In the same transaction it appends, for every current
  incoming `waits_on` to the closed entry, a `remove` event and an `add` to each replacement.
- `reopen(id, reason)` on any closed goal or task appends a reopen event. Dependents'
  readiness is recomputed from current links; links moved by a supersede stay moved (D11).
- A `waits_on` + `part_of` cycle warns at write time with the cycle's ids (D8).
- Every task-changing response (`write` on a goal or task, `link`/`unlink` of `waits_on` or
  `part_of`, `claim`, `release`, `close`, `reopen`) redraws the affected graph: the changed
  entry, its parent chain to the root, its children, and the entries at both ends of its
  current `waits_on` links, each with its state.

Claims and goals:

- `claim(id, base_version)` refuses a goal or any non-task kind (malformed), a closed task,
  and a stale `base_version` (attempted overwrite, same refusal text as writes). A task
  claimed by another session is refused unless `take_over_reason` is given (D3).
- Claim standing, from the current version and current links, all listed together:
  `observation` when the current version has an artifact, a code capture or a value, else
  `assertion`; `supported` when a not-retired entry has a current `supports` link to it;
  `contradicted` likewise for `contradicts`. Supported and contradicted can both hold.
- `stated_by` on claims and preferences defaults to `agent`; the agent passes `person` when
  recording what a person said (dec-ix8zd: the dreamer may only flag a conflict on a person's
  claim; this field is what lets it tell).
- A link whose stored `target_version` is below the target's current version shows as
  "written against vN, now vM"; lint lists them.

Comments:

- A comment is **open** when no comment has addressed it.
- `addresses` may name only open comments on the same entry; anything else is refused as
  malformed. A comment with nonempty `addresses` is a response and is never open itself.
- A response whose `entry_version` equals the addressed comment's `entry_version` is shown as
  "addressed with no new version".
- A comment on a goal or task pins the current version of every descendant (transitively).
  `show(id, since_comment=<cmt>)` lists entries whose version changed and links added or
  removed since that comment.

Notes and preferences:

- A note's search hit and `show` list the current entries that link to it with `supports`,
  `contradicts` or `references` ("used as evidence by").
- In search, a note's fused score is halved before sorting (D5).
- `prime` prints preferences in full under a 10,000-character cap: project preferences first,
  then global, each group newest version first. Once the next one would exceed the cap, the
  rest print as title lines with their `show` call, followed by a warning that the cap was hit
  (D17). Current `contradicts` links between current preferences print as conflicts.

## Session and harness provenance

`trails/sessions.py` resolves (harness, session_id, transcript, agent) on every write, in this
order, deciding the harness before reading any harness-specific variable:

1. Harness name: the MCP client name if the MCP binder bound one (`claude-code` -> claude; a
   name containing `kiro` -> kiro; anything else is stored as given). Otherwise (CLI), the
   first ancestor process whose command names Kiro (`kiro-cli`, `kiro`) or Claude Code
   (`claude`). Otherwise `unknown`.
2. Kiro: walk parent PIDs and match a `pid` in `~/.kiro/session-leases/*.lease`; transcript
   `~/.kiro/sessions/*/<session>/messages.jsonl`; agent from that session's `session.json`
   `agentMode`. Fallback `KIRO_SESSION_ID`.
3. Claude Code: `CLAUDE_CODE_SESSION_ID`; transcript `~/.claude/projects/*/<id>.jsonl`;
   agent NULL.
4. Anything else: the harness name only, no session. Never an error.

Not checked by probe: interactive Claude Code and Claude Code subagents (headless only, per
`fac-ahxap`).

## Operations (agent toolset unless noted)

- `prime(project=None)`: index as today plus global preferences; scoped: framing, paths,
  preferences as above, open goals with their ready-to-close / needs-a-decision state, the
  count of ready tasks with the `ready` call, the count of open comments with the search call,
  contradicted claims, counts per kind and task state. Prime's other sections stay fixed-size
  as today; the 10,000-character cap applies to the preference section (D17).
- `write(kind, title, body, tags, value, artifacts, repos, exclude, allow_large, projects,
  priority, stated_by, parent, waits_on, id, base_version, reason)`.
- `link(source, relation, target, reason=None)`; `unlink(source, relation, target, reason)`.
- `claim(id, base_version, take_over_reason=None)`; `release(id, reason)`.
- `close(id, outcome, summary, by=[], follow_ups=[])`, where each follow-up is
  `{title, body="", priority=None, tags=[]}`; each becomes a new task with `discovered_from`
  to the closed entry and `part_of` the closed entry's parent if it has one (D18). The
  response names the closed entry's dependents with their new state.
- `reopen(id, reason)`.
- `ready(project=None, limit=20)`.
- `add_comment(id, comment, addresses=[], verdict=None)`.
- `show(id or ids, versions=False, comments=5, since_comment=None)`: current version, derived
  state, links with versions, latest comments with total and paging call, task events,
  provenance (harness, session, transcript).
- `search(query, kinds, projects, tags, status, include_retired)`: current versions; `status`
  takes `open`, `claimed`, `closed` or an outcome (D19). Hits carry standing, open comment
  count and, for notes, "used as evidence by".
- `retire(id, reason)`, `register_project`, `update_project`, `set_projects`: as today.
- Full toolset: `lint`, `alias`, `embed`, `version`.

## Decisions this plan makes (not in the records)

D1. One `write` operation for every kind, not a separate `add_task`.
D2. The stored relation is `waits_on` (source waits on target), not `blocks`.
D3. Taking over another session's claim needs `take_over_reason`; without it the claim is
    refused as an attempted overwrite of the current claim. Every Synth handoff starts a new
    session, so a resumed agent gives a reason to take its own task back.
D4. `export`/record files, `lineage` and `mark_verified` are dropped from this version.
D5. Notes rank below other kinds by halving their fused search score. dec-ix8zd says "below
    claims, tasks and skills covering the same thing"; "covering the same thing" needs
    judgment, so the store applies a fixed penalty instead.
D6. `priority` is a goal and task field; passing it for another kind is refused as malformed.
D7. `parent` and `waits_on` only at creation; later edges through `link`.
D8. Cycle detection runs over `waits_on` and `part_of` together, because a block on a parent
    applies to its children, so a child waiting on its own ancestor never becomes ready.
D9. `write` accepts `kind="skill"` (the dreamer will use the same operation), but the tool
    description tells agents that only the dreamer writes skills. The store cannot tell the
    dreamer from an agent: both run as the same OS user.
D10. A `waits_on` target that is not a task or goal is stored with a warning and ignored for
    readiness (principle 5: warn, don't refuse).
D11. `release` drops a claim; the task stays not closed. `reopen` moves a closed goal or task
    back to `open`; supersede link moves are not undone.
D12. `parent_session` is deferred: nothing reliably tells an MCP server which session launched
    its session.
D13. The default database is `~/.agent-trails/db`; an old-schema database is refused on open.
D14. Omitted optional fields in a new version inherit the previous version's values; an
    explicit empty list clears them.
D15. `ready` reads in one Dolt invocation and computes readiness in Python.
D16. "Ready to close" needs at least one child, all closed, none `failed`, for goals and tasks
    alike. dec-o0gww says "completed" and dec-ix8zd says "closed"; this rule takes both:
    `dropped` and `superseded` children count as closed, `failed` needs a decision.
D17. Preferences: project first, then global, newest first; the cap applies to the preference
    section only.
D18. A follow-up created by `close` is `part_of` the closed entry's parent.
D19. Search `status` values: `open`, `claimed`, `closed`, or an outcome.
D20. Tests that are not store guards and dominate runtime may be marked `slow` (Derek asked for
    efficient test runs); each one marked is listed in the commit message.
D21. `stated_by` defaults to `agent`.
D22. Closing a parent with children not closed is stored with a warning, not refused
    (principle 5).
D23. `link` takes an optional reason; `unlink` requires one, because removing a dependency
    changes what is ready and a later reader needs to know why.
D24. The preference cap counts everything in the preference section, overflow title lines and
    the warning included. "Newest first" compares the current version's created_at.
D25. The default database path switches to `~/.agent-trails/db` in step 4, with the switch of
    operations.

Out of reach for this repository: the installed trails skill at `~/.kiro/skills/trails` (AIM
managed) still teaches publish, export and mark_verified. It needs updating by its owner after
this lands; listed for Derek.

## Steps

The new core is built BESIDE the old one, so the full gate passes at the end of every step: new
modules and new tests are added in steps 1 to 3 and the old store, ops and tests stay untouched
until step 4 switches the operations over and then deletes the old code in the commit after.

1. `vocab.py` new vocabularies (added beside the old ones), new schema and `EntryStore` in new
   modules, `sessions.py`. Acceptance tests: create then version an entry; a stale and a
   missing `base_version` are refused and the refusal names the current version and the retry
   call; version history and per-version tags are kept; omitted fields inherit and `[]`
   clears; a session row is written once per session id; harness is recorded with no session;
   Kiro lease lookup and Claude variable lookup each return the right session from fixture
   files and env (process tree and home directory are the external boundaries replaced in
   tests); `KIRO_SESSION_ID` present under a Claude client is ignored; old-schema database
   refused.
2. Links with events and derived task state in `EntryStore`. Acceptance tests: a task waiting
   on a task closed `dropped` stays waiting and is flagged; a parent with a not-closed child is
   not ready; a block on a parent keeps its child waiting; claim refuses a goal, a closed
   task, a stale base and another session's claim without a reason; supersede moves every
   dependent onto every replacement and refuses an empty `by`; ready-to-close excludes a leaf
   and a parent with a `failed` child; a cycle warns; ready sorts by priority then age;
   reopen leaves moved links moved.
3. Comments in `EntryStore`. Acceptance tests: `addresses` naming a comment on another entry
   or an already-addressed comment is refused; a response closes what it addresses and is
   never open; a same-version response is flagged; a comment on a goal pins every descendant;
   `since_comment` lists exactly the changed versions and links.
4. Switch: new operation modules (`write`, `link`, `unlink`, `claim`, `release`, `close`,
   `reopen`, `ready`, `add_comment`, `show`, `search`, `prime`, `retire`, projects), render,
   guidance, the MCP binder's client-name context variable. Acceptance tests: the existing invariant suites
   (`test_descriptions`, `test_ops_registry*`, `test_surfaces`, `test_module_boundary`,
   `test_mcp_toolsets`) pass over the new operations; prime's preference cap prints project
   preferences first and warns on overflow; a note hit lists its users; claim standing shows
   observation/assertion, supported and contradicted together; every task-changing response
   redraws the affected graph. The deletion of the old store record code, old ops and their
   tests is a separate commit right after the switch commit, so the review can read them apart.
   Step 2 also tests that a task whose only `waits_on` target is a note is ready.
5. `lint` (stale link versions, cycles, plus the carried-over artifact, capture, path, embedding
   and secret checks), `alias`, `embed`, `version`; docs (`README.md`, `AGENTS.md`,
   `docs/philosophy.md` principles 3 and 4, `CLAUDE.md` if it duplicates them); remove
   `probes/`'s config file from the tree. Acceptance: lint reports a link written against an
   older version and a cycle; docs name every operation and no removed one.
6. Independent code review against the decision records and this plan; fix demonstrated
   findings; one check of the fixes.

Testing policy (Derek, 2026-11-13): run only the touched test files while iterating; the full
gate at each step's end (D20).
