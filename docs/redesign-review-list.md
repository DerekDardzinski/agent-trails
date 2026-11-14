# Decisions for Derek to check

> **Partly superseded.** This list records round 1 of the redesign. Its triage section
> ("Triage of the open questions (lab-lead, 2026-11-14)") is partly superseded by section 2.5,
> "Reconciling the lab-lead triage", of Derek's round-2 handoff (`trails-redesign-handoff-2.md`),
> and round 2 (`docs/redesign-plan-2.md`) then changed the model it describes: four kinds
> instead of six, no derived labels such as "ready to close", and per-kind operations in place
> of `write`. Read an item here as history, and check the current code and README before
> relying on it.

Choices I made while working on my own. Each says what I chose and why. Nothing is pushed;
everything is on branch `redesign`. The D-numbers match "Decisions this plan makes" in
`docs/redesign-plan.md`, which has the full text.

## Most worth your attention

1. **"Ready to close" (D16).** The records disagree: `dec-o0gww` says a parent is ready to
   close when its children are `completed`, and `dec-ix8zd` says "all tasks closed". I chose: at
   least one child, all closed, none `failed`. A `failed` child puts the parent under "needs a
   decision". `dropped` and `superseded` children count as done.
2. **Skills can be written through `write` (D9).** `dec-ylv0a` says only the dreamer writes
   skills, but the store can't tell the dreamer from an agent, because both run as your OS user.
   So `write` accepts `kind="skill"`, and the tool description tells agents not to use it.
3. **One `write` operation for every kind, not a separate `add_task` (D1).**
4. **`stated_by` (agent or person) on claims and preferences.** This isn't new, since
   `dec-ix8zd` asks for it, but it is a field agents must fill in: they pass `person` when
   recording what you said.
5. **The installed trails skill (`~/.kiro/skills/trails`) is AIM-managed and outside this
   repo.** It still teaches `publish`, `export` and `mark_verified`, and it needs updating
   after this lands. I haven't touched it.

## Smaller choices

6. The stored relation is `waits_on`, not `blocks` (D2).
7. Taking over another session's claim needs `take_over_reason` (D3). Every Synth handoff
   starts a new session, so a resumed agent gives a reason to take back its own task.
8. `export`, record files, `lineage` and `mark_verified` are dropped from this version (D4).
9. Notes rank below other kinds by halving their search score (D5).
10. `priority` only on goals and tasks (D6). `parent` and `waits_on` only at creation, later
    through `link` (D7).
11. A `waits_on` link to a non-task is stored with a warning and ignored for readiness (D10).
12. `release` drops a claim, and `reopen` reopens a closed goal or task. Links moved by a
    supersede stay moved after a reopen (D11).
13. `parent_session` (which session launched a subagent) is deferred (D12).
14. Default database `~/.agent-trails/db`, and an old-schema database is refused on open (D13).
15. Fields omitted from a new version inherit the previous values, and `[]` clears them (D14).
16. A follow-up created by `close` goes under the closed task's parent (D18).
17. Tests may be marked `slow` when they aren't store guards. Each one is listed in its commit
    (D20).

## Settled by probe, not by choice

18. **The Claude Code session adapter is in this version.** A probe (trails `fac-ahxap`)
    showed Claude Code gives MCP servers `CLAUDE_CODE_SESSION_ID` and the client name
    `claude-code`. It also showed `KIRO_SESSION_ID` leaking into Claude's environment when
    Claude is launched from a Kiro shell, so detection decides the harness before reading any
    variable. Only headless Claude Code was checked.
19. Smaller defaults added after the plan's second review (D21-D25): `stated_by` defaults to
    `agent`; closing a parent with open children is stored with a warning; `unlink` requires a
    reason and `link` doesn't; the preference cap counts the overflow lines and the warning;
    the default database path switches in step 4.

## Added during implementation

Choices made while building, which neither the plan nor the records settle. The step that made
each one is in brackets.

20. **An unrecognized MCP client name falls back to the process tree before it is stored as
    the harness name** [step 1]. The plan said "anything else is stored as given". I don't know
    what client name Kiro sends (no probe ran under Kiro), so if it lacks the word `kiro`, the
    plan's rule would record Kiro writes under that name with no session. Checking the process
    tree first catches Kiro either way. A probe this session (trails `fac-7azma`) showed that
    Kiro runs as a `node` process whose path contains `kiro-cli`, so detection matches that
    substring anywhere in the command line, not the executable name.
21. **`value={}` clears an entry's value** [step 1]. D14 says an empty list clears; `value` is
    an object, so the empty object plays that role.
22. **A version identical to the current one is not written**; the response says nothing
    changed [step 1]. Otherwise a no-op edit would make every other writer's `base_version`
    stale for nothing.
23. **A lost-response retry is recognized**: a write with an old `base_version` whose next
    version already exists with exactly this content from this same harness and session
    returns that version instead of a stale-base refusal [step 1]. Same idea as the old store's
    landed-write check.
24. **`close(outcome="superseded", by=[...])` also writes a `supersedes` link from each
    replacement to the closed entry** [step 2]. The task event has no column for `by`, and the
    link is where the replacement is recorded and shown.
25. **Only goals and tasks count as children** [step 2]. A note `part_of` a task would
    otherwise keep its parent from ever being ready to close, since a note never closes. A
    `part_of` link to a non-work entry is stored with a warning, as D10 does for `waits_on`.
26. **Two writes with no session id from the same harness count as one claimant** [step 2],
    because nothing tells them apart. This matters only for a harness that gives no session.
27. **Closing an already-closed entry**: the same outcome changes nothing (so a retry is
    safe); a different outcome is refused with the `reopen` call [step 2].
28. **`release` of an unclaimed task and `reopen` of an entry that is not closed change
    nothing** and say so, rather than refusing [step 2].
29. **`ready(project=...)` filters by the task's own project membership**, not its parent's
    or goal's [step 2].
30. **Cycle detection covers creation too**: `write(parent=..., waits_on=...)` can close a
    cycle, so it warns the same way `link` does [step 2].
31. **`since_comment` also lists descendants added after the comment** (shown with no pinned
    version), and its link list covers link events from or to the commented entry and its
    descendants, old and new [step 3].
32. **How step 4 is split into two commits** [step 4]. The plan put "old ops and their tests"
    in the deletion commit, but once the operations open the new schema, the old operations
    (`publish`, `export`, `lineage`, `mark_verified`, `set_work_status`) cannot run, so the
    switch commit removes them and their operation-level tests. The deletion commit after it
    removes the dead store, model, retrieval and record-file code and their store-level tests.
33. **`search` takes fewer filters than before**: `query`, `kinds`, `projects`, `tags`,
    `status`, `open_comments`, `include_retired`, `limit`, `offset` [step 4]. The old `terms`,
    `since`, `until`, `links` and `unscoped` filters are gone, as the plan's signature lists.
    Identifier-shaped words in `query` (ids, paths, shas, file names) still match exactly and
    lead the results.
34. **`search(open_comments=True)` is new** [step 4]: prime has to name a call that lists the
    entries with open comments, and no filter did that.
35. **`show` of an unknown id lists it as not found** rather than refusing, so a mixed list
    still returns what exists [step 4].
36. **`lint` was rewritten in step 4**, with the step 5 checks (stale link versions, cycles)
    included, because it reads entries and had to work once the switch landed [step 4]. The
    `stale-procedures` category is gone with `mark_verified`.
37. **Embeddings are per version**; search's vector arm reads only the current version's
    chunks, and `embed` backfills current versions only [step 4].
38. **Capture refs are named `refs/research/<id>/v<version>/<position>`** [step 4], since an
    entry can capture code in more than one version. A failed write removes its refs only when
    no version holding that capture landed.
39. **`write` validates the whole draft before capturing code** [step 4], so a refused write
    leaves no capture ref or capture store behind (the old `publish` guaranteed this too).
40. **The default MCP toolset is 16 operations** [step 4]: `add_comment`, `claim`, `close`,
    `link`, `prime`, `ready`, `register_project`, `release`, `reopen`, `retire`, `search`,
    `set_projects`, `show`, `unlink`, `update_project`, `write`. The full toolset adds `alias`,
    `embed`, `lint`, `version`.
41. **`prime` counts kinds other than goals and tasks as `current`** in its per-kind counts,
    since only goals and tasks have a state [step 4].
42. **`scripts/import_beads_memories.py` now writes notes into the new store** [step 4].
43. **Scoped `prime` lists at most 20 open goals and 20 contradicted claims**, with totals and
    the search call for the rest [review fix]. The reviewer showed both lists grew without
    bound, against the rule that prime's size does not grow with history.
44. **Advisory review notes left open for you** [review]: `write` holds the store lock while it
    captures code (the old `publish` captured outside it); project, framing, path and alias
    writes record no harness or session; re-linking a current link keeps its old
    `target_version`; a `waits_on` added after its target closed `superseded` is not flagged;
    `show(since_comment=...)` on a comment of another entry prints nothing; a stale Kiro lease
    whose pid was reused could misattribute writes; set_projects reasons are not scanned for
    secrets; the preference cap can be exceeded when title lines alone pass 10,000 characters
    (over 100 preferences). The full list is the reviewer's comment on `labbook-a7p.7`.
45. **Review outcome** [step 6]: the code review of 00df15c found 5 blocking findings, all
    demonstrated, and the fix check of ecf84ac approved all five. Its remaining advisory notes
    are in item 44, plus one from the fix check: prime's `more:` call for contradicted claims
    lists every claim in the project, not only the hidden contradicted ones.

## Triage of the open questions (lab-lead, 2026-11-14)

Each open question is sorted into one class: **probe** (a test or probe settles it), **literature**
(papers settle it or bound it), or **Derek** (a product or preference judgment). "Decided by
lab-lead" means I was very confident and recorded a trails decision; everything else waits for
you. Probe facts: `fac-x8s2n` (Kiro), `fac-0fzsj` (Claude Code), `fac-wqx5c` (lock during
capture). Literature: `rpt-r7vyi` (lab-researcher, builds on `rpt-4anug`). Decisions:
`dec-irq9k` (session detection, commit 1b74c8c), `dec-66050` (capture outside the lock, 456f3d9),
`dec-kstgt` (five review notes, 22a0ef1), `dec-tafzz` (choices confirmed with no change).

### Review-list items 1 to 19 (plan decisions)

| item | class | status and why |
|---|---|---|
| 1 ready to close (D16) | Derek | Semantics of done; both records say different things. |
| 2 skills via `write` (D9) | Derek | Policy; the store cannot tell the dreamer apart. |
| 3 one `write` (D1) | Derek | API shape. |
| 4 `stated_by` filled by agents | literature, then Derek | `rpt-r7vyi`: no fill-rate study; under load models mostly omit. Recommendation below. |
| 5 installed trails skill | Derek | Outside this repo (AIM-managed). |
| 6 `waits_on` (D2) | Derek | Naming; follows `dec-o0gww`'s "named from the dependent's side". |
| 7 take-over reason (D3) | Derek | Policy. |
| 8 dropped export, lineage, mark_verified (D4) | Derek | Scope. |
| 9 note penalty x0.5 (D5) | Derek | A tuning constant; no literature on it. Measurable later in the evaluation. |
| 10 priority, parent, waits_on at creation (D6, D7) | Derek | API shape; ties to the open link-edit question. |
| 11 `waits_on` to a non-task warns (D10) | decided by lab-lead | Basis: principle 5 (warn, don't refuse). No change. |
| 12 release and reopen (D11) | Derek | Semantics. |
| 13 `parent_session` deferred (D12) | probe | Decided by lab-lead: stays deferred. `fac-x8s2n`, `fac-0fzsj`: an MCP server sees no subagent id in either harness, and a Synth child is a separate session that only Synth links. |
| 14 default path and old-schema refusal (D13) | Derek | Preference. |
| 15 inherit and clear (D14) | Derek | API shape. |
| 16 follow-up parent (D18) | Derek | Semantics. |
| 17 slow marks (D20) | Derek | Your test policy. |
| 18 Claude adapter, headless only | probe | Settled: `fac-0fzsj` shows interactive Claude Code gives the same variables, and subagent calls carry the parent's session. No change. |
| 19 D21 to D25 defaults | Derek | Small defaults; D24 is enforced by the item 44 cap fix below. |

### Items 20 to 43 (implementation choices)

| item | class | status and why |
|---|---|---|
| 20 unknown client name falls back to the process tree | probe | Settled: Kiro sends `kiro` (`fac-x8s2n`), so the fallback is not needed for Kiro; kept for other clients. |
| 21 `value={}` clears | decided by lab-lead | Direct consequence of D14 for an object field. |
| 22 identical version not written | decided by lab-lead | Consequence of `base_version`: a no-op must not make others' bases stale. |
| 23 lost-response retry recognized | decided by lab-lead | Same guarantee as the old store's landed-write check. |
| 24 supersede writes `supersedes` links | Derek | Where replacements are recorded; reasonable but a model choice. |
| 25 only goals and tasks are children | decided by lab-lead | Consequence of D16: a note never closes, so it would block forever. |
| 26 no-session writers are one claimant | Derek | Only matters for a harness with no session. |
| 27 closing twice | decided by lab-lead | Retry safety (principle 6) plus refusing a silent outcome change. |
| 28 release/reopen no-ops | decided by lab-lead | Principle 5: nothing to refuse. |
| 29 `ready(project=...)` by own membership | Derek | Scoping choice. |
| 30 cycle check at creation | decided by lab-lead | Consequence of D8. |
| 31 `since_comment` scope | Derek | Display choice. |
| 32 step 4 commit split | none | Process only; nothing to decide. |
| 33 fewer search filters | Derek | Scope. |
| 34 `open_comments` filter | decided by lab-lead | Prime must name a call that lists what it counts (principle 6). |
| 35 unknown id listed as not found | decided by lab-lead | Principle 5. |
| 36 lint rewritten, `stale-procedures` gone | decided by lab-lead | Consequence of D4. |
| 37 embeddings per version | decided by lab-lead | Consequence of search reading current versions. |
| 38 capture ref names | decided by lab-lead, changed | Changed with the lock fix below: refs now carry a per-attempt token, so two concurrent captures of one version cannot overwrite each other's pin. |
| 39 validate before capture | decided by lab-lead | Keeps the old `publish` guarantee; kept with the lock fix. |
| 40 16-tool default toolset | Derek | Toolset size. |
| 41 non-work kinds counted as `current` | decided by lab-lead | Consequence of `dec-ix8zd`: only goals and tasks have state. |
| 42 beads importer writes notes | Derek | Migration choice. |
| 43 prime list cap of 20 | decided by lab-lead | Consequence of "prime's size does not grow with history"; 20 is the existing figure. |

### Item 44 and 45 (advisory review notes)

| question | class | status and why |
|---|---|---|
| `write` holds the lock during capture | probe | Decided by lab-lead, changed: `fac-wqx5c` measured a search taking 7.35 s during a 7.5 s capture (0.93 s alone). Capture now runs outside the lock; the write still validates first and checks `base_version` under the lock. |
| project, framing, path and alias writes record no session | Derek | `prb-vqur3` says "every write", but projects were planned "as today". It needs columns on four tables; question below. |
| re-linking keeps the old `target_version` | Derek | Whether `link` on an existing link means "re-checked against the new version" is a semantic choice; question below. |
| `waits_on` added after its target closed superseded is not flagged | decided by lab-lead, changed | Same stuck state as `dropped` and `failed` (`dec-o0gww`): the dependent can never become ready. It is now listed as needs replanning. |
| `show(since_comment=...)` on another entry's comment prints nothing | decided by lab-lead, changed | Principle 6: it now warns and names the entry the comment is on. |
| stale Kiro lease with a reused pid | probe | Decided by lab-lead, changed: `fac-x8s2n` found a real stale lease. A lease now matches only when its `parentPid` matches the process's parent too, and the newest lease wins. |
| `set_projects` reasons not scanned | decided by lab-lead, changed | README: "Every write is scanned"; the review fix scanned every other reason. |
| preference cap exceeded past about 100 titles | probe | Decided by lab-lead, changed: reachable by construction (titles alone over 10,000 characters). Title lines are now cut at the cap too, with a count and the search call that lists the rest (D24). |
| prime's `more:` call lists every claim | decided by lab-lead, changed | Principle 6: `search` gains `contradicted=True`, the same pattern as `open_comments` (item 34). |

### Questions in the decision records and the plan

| question (source) | class | status and why |
|---|---|---|
| link edits in the same call as a version write (`dec-o0gww`) | literature, then Derek | `rpt-r7vyi`: no study compares combined with separate calls; tau-bench shows partial resolution of compound requests in 19% of 36 failures. Researcher's judgment: allow optional link edits in `write`. |
| failed-child state (`dec-o0gww`) | none | Implemented as "needs a decision" (D16); you check it under item 1. |
| a verdict needs comment text (`dec-o0gww`) | Derek | Whether an empty approval is malformed. |
| skill design: scope, symlink config, place on the session-start page (`dec-ix8zd`, `dec-ylv0a`) | Derek | Product design; skills are deferred in this version. |
| session-start digest content and size (`dec-ix8zd`, `dec-9b2ta`) | literature, then Derek | `rpt-r7vyi`: indirect only (lost in the middle; instruction following degrades with count). |
| preference loading mechanism per harness (`dec-9b2ta`) | probe | Settled as a fact, design left to you: a Kiro `agentSpawn` hook and a Claude Code `SessionStart` hook both put their stdout in the model's context and both know the session id (`fac-x8s2n`, `fac-0fzsj`). |
| the dreamer (`dec-ix8zd`) | Derek | Design not started. |
| evaluation design (`prb-vqur3`, `dec-ix8zd`) | literature, then Derek | `rpt-r7vyi` gives three designs; the choice and its cost are yours. |
| interactive Claude Code and Claude subagents (plan) | probe | Settled: `fac-0fzsj`. |
| Kiro client name, Kiro subagents, Kiro without Synth (`fac-7azma`) | probe | Client name and subagents settled (`fac-x8s2n`); headless Kiro checked, interactive Kiro not. |

### Found by the probes, not on any list

| question | class | status and why |
|---|---|---|
| an MCP server under Kiro reads an inherited `KIRO_SESSION_ID` | probe | Decided by lab-lead, changed: `fac-x8s2n` showed the variable belongs to the shell that launched Kiro, so the MCP path no longer reads it. |
| a Kiro agent's `trails` CLI command under Synth records `unknown` | probe | Decided by lab-lead, changed: under Synth the shell's parent is Synth, not Kiro. The CLI now uses `KIRO_SESSION_ID` when the process tree names no harness and that session has a lease. |

### Questions left for you, with my recommendation

1. **Session columns on project writes.** `prb-vqur3` says every write is attributable, but
   register, update_project, path and alias writes record no harness or session. Options: add
   harness and session columns to `project`, `project_framing_version`, `project_path` and
   `tag_alias` (fresh database, no migration); or declare project metadata out of that
   criterion. Recommendation: add them, because the goal says "every write" and the cost is
   four columns.
2. **Re-linking a current link.** Today `link` on an existing link changes nothing, so "written
   against v2, now v3" clears only through `unlink` (reason required) and `link`. Options:
   `link` on a current link with an older `target_version` appends a fresh add at the current
   version and says so; or keep it a no-op and have lint print the unlink-then-link pair.
   Recommendation: the refresh, because it is the call an agent will try, but it lets an agent
   "confirm" a link without reading the new version, which is why it is yours.
3. **`stated_by`.** The literature has no fill-rate data, and models under load mostly omit
   (`rpt-r7vyi`). Options: keep the `agent` default; make it required with no default on claims
   and preferences; or keep the default and have the dreamer check it against transcript roles.
   Recommendation: required with no default, since a missing value is then a visible refusal
   instead of a silent wrong default.
4. **Link edits inside `write`** (`dec-o0gww`). No study compares the two; tau-bench shows
   partial completion of compound requests. Options: keep `link` and `unlink` separate (today);
   or accept optional `add_links` and `remove_links` in `write` and echo the resulting link set.
   Recommendation: keep them separate for now, because every task-changing response already
   redraws the graph, which is the guard the researcher's option relies on.
5. **A verdict with no comment text** (`dec-o0gww`). Recommendation: allow an empty comment when
   a verdict is given, since "approve" with nothing to add is not malformed (principle 5).
6. **Session-start loading.** Both harnesses can run a hook whose stdout reaches the model and
   which knows the session id. Open design: which command the hook runs, how it picks the
   project from the working directory, and what shares the page with preferences. The
   researcher's reading: limit the number of preferences, not only characters; put them first;
   show goals and tasks as titles with the `show` call. Recommendation: a `trails prime` hook
   per harness, project chosen by the registered path that contains the working directory.
7. **Evaluation.** `rpt-r7vyi` offers three designs. Recommendation: its option B, replaying
   about 30 of your closed tasks from their pre-task commits under stateless, tasks-only,
   plus-transcripts, plus-skills and plus-dreaming arms, about 390 sessions and $100 to $1,200,
   with your labeling time as the limit.
8. **Skills and the dreamer.** Scope, the symlink configuration, and the dreamer's design are
   product design and stay with you; nothing in this version depends on them.

### Follow-up review (Beads `labbook-a7p.8`)

The review of 1b74c8c, 456f3d9 and 22a0ef1 found 1 blocking finding, which was demonstrated: an
MCP client that sent an empty name was treated as the CLI and got the inherited
`KIRO_SESSION_ID`. Commit 37f9ce3 fixed it, and the fix check approved it (gate: 339 passed).
These advisory notes are still open:

- The CLI accepts `KIRO_SESSION_ID` whenever its lease file exists. Stale leases do occur
  (`fac-x8s2n`), so a variable inherited from a session that crashed could still be used. A
  pid-liveness check on the lease would close this.
- The `more:` calls for omitted preferences and conflicts carry no project, so they list more
  than was omitted.
- `show --since-comment` with an unknown comment id now exits 1 ("no comment has id") where
  it used to print "not found".
- An entry id is now minted outside the lock. Two concurrent creations could draw the same id
  and get a spurious refusal; the chance is about 1 in 36^5 for each overlapping pair.
- The preference-cap search loop is quadratic in the number of preferences.
