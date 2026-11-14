# trails redesign, round 2: implementation plan

Goal: trails `prb-nfrzr`. Decisions: trails `dec-d1krx` (Derek, round 2), from
`trails-redesign-handoff-2.md` (worktree root) and Derek's replies on 2026-11-14. Philosophy:
`docs/philosophy.md` (replaced in step R1 with section 1 of that handoff). This plan supersedes
the parts of `docs/redesign-plan.md` listed under "Superseded" below; the rest of that plan (data
model, versions, sessions, capture) stands.

Every step is checked against the seven principles. The two that drive most changes:

- **Principle 5, one way to make each change.** No parameter applies only some of the time; no
  two operations make the same change; a refusal that points to another operation for the same
  intent means the tools need redesigning.
- **Principle 7, warnings only for what tools cannot prevent.** In-store mistakes are prevented by
  the schema of each operation, not warned about.

## Superseded in `docs/redesign-plan.md`

D1 (one `write`), D3 (`take_over_reason`), D5 (note penalty), D9 (skills through `write`), D10
(`waits_on` to a non-task warns), D16 ("ready to close" and "needs a decision"), D18 (follow-ups
under the closed task's parent), D21 (`stated_by` default), D22 (warning on closing a parent with
open children), D24's overflow wording (the cap stays), the kind list, the relation list, the
operation list, and the "needs replanning" state. The triage reversals are in section 2.5 of the
handoff.

## Target model

- **Kinds**: `task` (tsk), `note` (note), `preference` (prf), `skill` (skl). A parent is a task with
  children; `goal` and `claim` are gone (a goal can be a tag).
- **Relations**: work `waits_on`, `part_of` (task to task); evidence `supports`, `contradicts`
  (between notes, preferences and skills); `supersedes`, written only by `replace`.
- **Task events**: claim, release, close (outcome `completed`, `dropped`, `failed`), replace
  (outcome `superseded`), reopen.
- **Derived state**: only readiness. A task is ready when it is not closed or retired, has no
  child that is not closed, and every `waits_on` target of it and its ancestors is closed
  `completed`. Everything else is shown as stored facts ("children: 2 completed, 1 failed",
  "waits on tsk-x (closed dropped)").
- **Agent-supplied fields are shown as the agent's word**: a digest prints "(as given)", an
  outcome and a verdict print with their author's harness and session.
- **Every write records harness and session**, project, framing, path and alias writes included.

## Target operations

Agent toolset (MCP default):

| operation | inputs |
|---|---|
| `create_task` | title, body, tags, priority, parent, waits_on, projects |
| `create_note` | title, body, tags, value, artifacts, repos, projects |
| `create_preference` | title, body, projects (none means global) |
| `revise_task` | id (tsk pattern), base_version, reason, title, body, tags |
| `revise_note` | id (note pattern), base_version, reason, title, body, tags, value, artifacts, repos |
| `revise_preference` | id (prf pattern), base_version, reason, title, body |
| `set_priority` | id (tsk), base_version, priority, reason |
| `link_work`, `unlink_work` | source, relation (`waits_on`, `part_of`), target, both tsk; unlink needs a reason. A cycle is refused as malformed. A target already closed is stored as given. |
| `link_evidence`, `unlink_evidence` | source, relation (`supports`, `contradicts`), target, both knowledge ids; linking an existing current link again records it at the target's current version |
| `close` | id (tsk), outcome (`completed`, `dropped`, `failed`), summary |
| `replace` | id (tsk), by (tsk ids), reason; writes `supersedes` and moves dependents' `waits_on` |
| `claim` | id (tsk), base_version, holder (the session holding it when you read it; omitted means it was unclaimed); a mismatch is refused as an overwrite |
| `release`, `reopen` | id (tsk), reason |
| `retire` | id (note, prf or skl pattern), reason |
| `comment` | id, comment |
| `reply` | addresses (comment ids, all on one entry), comment |
| `review` | id (tsk), verdict, comment (optional) |
| `set_projects` | id, add, remove, reason; the only way to change an existing entry's projects |
| `register_project` | name, paths, framing |
| `set_paths` | project, paths, remove_paths |
| `revise_framing` | project, framing, base_version, reason |
| `prime`, `search`, `show`, `ready`, `blocked`, `changes_since_comment` | reads |

Dreamer toolset: the agent toolset plus `create_skill`, `revise_skill`, `merge(id, into, reason)`
and `changes(since, until, session)`. Full toolset: everything, plus `lint`, `alias`, `embed`,
`version`.

`repos` is a list of `{path, exclude, allow_large}` objects, so the capture options cannot be
written without a repository. Closing or claiming a task that is already closed is refused as an
overwrite: the refusal states the task's current outcome, who closed it and when, and names no
other operation (Derek's review fix 1, `trails-round2-review-fixes.md`). Reads are not bound by principle 5.

## Steps

Run the full gate after each step against real Dolt; while iterating, run only the touched test
files. Each new test names the silent failure it catches and fails on the commit before it.

- **R1. Docs.** Replace `docs/philosophy.md` with the handoff's section 1, with principle 5's
  last sentence scoped to "for the same intent", the intro updated to four kinds, and lint
  hashing recorded as a named exception under principle 3. Add this plan.
- **R2. Model removals.** Merge `claim` into `note` and `goal` into `task`; remove `stated_by`,
  the observation/assertion standing, `NOTE_PENALTY`, `discovered_from`, `references`, the derived
  labels (ready to close, needs a decision, needs replanning) in favor of facts, `follow_ups`,
  `supersedes` in `link`, and the in-store warnings (wrong-kind link, closing a parent with open
  children). Add `blocked`. Review after R2.
- **R3. Create and revise.** Split `write` into `create_*` and `revise_*` with id patterns; add
  `set_priority`; `repos` objects; mint ids under the write lock (rename the capture ref when a
  collision forces a new id).
- **R4. Links and task state.** `link_work`/`unlink_work` (cycle refused), `link_evidence`/
  `unlink_evidence` (refresh), `replace`, `retire` patterns, claim by holder session, no-op claim
  and close on closed tasks. Review after R4.
- **R5. Comments and projects.** `comment`, `reply`, `review`, `changes_since_comment` (and `show`
  drops `since_comment`); `set_paths`, `revise_framing`; harness and session on project, framing,
  path and alias writes.
- **R6. Gaps and dreamer.** Close summaries and comment bodies searchable; `changes` feed; lease
  pid liveness; accept git `notice:` lines in the capture fsck test; prime `more:` calls scoped to
  the project; CLI command names equal operation names; dreamer toolset; agent-word display;
  lint flags a review by its author's session; digests compared only by `lint`; a test that
  parameter descriptions avoid conditional wording, with an allowlist. Review after R6.
- **R7. Release prep.** README, AGENTS.md, CLAUDE.md; delete and recreate `~/.agent-trails/db`.
  The AIM-managed `~/.kiro/skills/trails` stays out of reach and is listed for Derek.
