# trails design philosophy

trails is a memory and task store for agents. Agents use it to plan work as tasks, and to keep
what they learn as notes, preferences and skills. trails links these entries to each other and
to the code and data behind them. Every write records which agent tool made it (for
example Claude Code or Kiro) and which session, so a reader can go back to the conversation it
came from.

trails keeps the record of the work. It does not decide how the work is done.

Every design decision is checked against the seven principles below. A change that breaks one is
redesigned, or it is recorded here as a named exception with the reason for it.

## 1. Your work is yours

trails does not care how you organize scripts, notebooks, data or outputs. It asks for one thing:
code must be in a git repository. A commit pins the exact code that ran, and a later reader can
check it out and run it again. Any repository works, including a throwaway one. Folder layout, file
names and workflow are up to you and your agents.

A project in trails is a label for a body of work, not a folder. It has a name, an optional
statement of what the work is for, and a list of the places where its files live. Registering a
project does not create anything at those places. An entry can belong to several projects, or to
none.

## 2. trails writes only to its own data

trails changes nothing outside its own data directory. That directory holds the database, lock
files, the search model and trails' own copy of captured code. trails never creates, edits, moves
or deletes your files, folders, git branches or ignore files.

When trails captures code, it reads your repository and copies what it needs into its own store.
Your repository is left untouched, and the capture still works if you later rewrite or delete the
repository.

There is one allowed exception. An agent may ask trails to write to a specific path that it names
in the request, for example writing a skill out to a skills folder. trails writes only to that path
and changes nothing else.

## 3. trails stores, links and shows. It does not judge

trails never decides what an entry means or whether it is right. It does not label a note as
proven, a plan as finished or a result as good. No language model runs inside trails, and every
word it prints comes either from stored data or from a fixed template.

There is one state trails works out for itself: whether a task is ready to start. This is not a
judgment. An agent recorded that the task depends on other tasks, and the task is ready once all of
them are done. Other questions, such as whether a goal is complete or whether a plan needs to
change, are left to the agent. trails shows the facts it needs: which tasks are closed, how each one
ended, and what depends on what.

Some fields are only an agent's word. A checksum, a task's outcome or a review verdict is whatever
the agent wrote. trails shows each one as written by that agent, with the agent's name beside it,
and never presents it as something trails checked.

All judgment belongs to agents. That includes the consolidation agent that reviews the store
between sessions. Its changes are ordinary writes, each with an author and a reason, like any other
agent's.

trails does not copy datasets or read their contents. An entry points to data by where it lives,
such as a file path or a cloud storage address. The agent can add whatever identifies the exact
data used, such as a manifest file or the list of partitions it read.

One exception is named here. When an agent or person runs the store check explicitly, trails
hashes local files that entries reference and reports where a file no longer matches the checksum
an agent recorded. That comparison is mechanical, it runs only when asked, and it is the only way
a recorded checksum can become more than an agent's word.

## 4. History is never rewritten

Fixing an entry adds a new version. The old version stays readable. Every version, every link and
every change to a task's state is kept, along with the reason for the change.

An entry that turns out to be wrong is retired. A retired entry stays in history and can still be
read, but searches leave it out unless asked to include it.

trails has no way to delete data. If something must not exist at all, such as a leaked password,
removing it means rewriting the database history by hand. You do this yourself, or an agent does it
on your instruction.

## 5. There is one way to make each change

Each kind of change to the store has exactly one operation that makes it. An agent never has to
choose between two operations that would do the same thing.

The tools are designed so that the wrong request cannot be written. Each operation only accepts
options that apply every time it is used. An option that only makes sense in some cases goes into
its own operation. For example, creating an entry and revising an existing one are separate
operations. They take different inputs, so neither has options that sometimes do not apply. And if
two operations could make the same change, such as changing which projects an entry belongs to,
one of them drops that option.

Problems that better tool design can fix are fixed that way. They are not patched with extra
instructions, tool descriptions or error messages. If the store ever has to refuse a request and
tell the agent to use a different operation for the same intent, the tools need to be redesigned.
A refusal because the request was based on state that is now out of date is not this case; it is
principle 6's overwrite.

## 6. The store refuses only what it cannot store

trails refuses a write for only two reasons:

- **The request is malformed.** For example, a required field is missing, a value has the wrong
  type, or an id does not exist.
- **The write would overwrite someone else's work.** For example, two agents read the same entry
  and both revise it. The second revision was based on a version that is now out of date, so it is
  refused rather than allowed to erase the first agent's change. The agent reads the entry again
  and retries.

Everything else is stored as given.

There is one deliberate exception. When a code capture would include a very large file, trails
refuses until the agent names that file and says whether to leave it out or include it. A dataset
captured by mistake is much harder to clean up than one extra request is to make.

## 7. Warnings cover what the tools can't prevent

Some mistakes happen outside the store, in the agent's own files and environment, so no tool design
can stop them. Examples:

- the data or script behind an entry is saved in a temporary folder that will be wiped
- the code behind an entry is not in a git repository, so it cannot be captured
- an entry points to a file or folder that does not exist
- an entry's text appears to contain a password or key

In these cases trails stores the write anyway and returns a warning. The warning says what is
wrong, why it matters, and gives the exact command or request that fixes it.

Mistakes inside the store, such as using the wrong operation or passing an option that does not
apply, are not handled with warnings. They are prevented by tool design under principle 5.
