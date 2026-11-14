# Decisions for Derek to check

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
