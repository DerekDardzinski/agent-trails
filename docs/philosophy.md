# trails design philosophy

trails is a memory and task store for agents. It holds goals, tasks, notes, claims,
preferences and skills, links them, records which harness and session wrote each one, and
points to the code and data behind them. It has no say in how the work itself is done.

Every design decision in this repository is checked against the six principles below. A
change that breaks one is either redesigned or recorded here as a named exception with its
reason.

## 1. Your work is yours

trails makes no assumptions about how you organize scripts, notebooks, data or outputs. The
one thing it asks is that code lives in a git repository, because a commit is the only
identity for code that a later reader can check. Any repository qualifies: new or old, a
Brazil package, a scratch repository. Folder layout, file naming and workflow belong to the
user and the agent, never to the store.

A project is a stable body of work with a generated id, unique human name, optional append-only
framing, and current paths. Framing states purpose and motivation: why the project exists, who it
serves, long-term success, and what is out of scope. Operational knowledge belongs in paths or
ordinary entries. Each path has an optional label and access instructions such as which AWS
profile reads an `s3://` URI. Registering a project creates nothing at those paths. Entries may
belong to zero, one or several projects, and membership changes append events without writing a
version.

## 2. trails writes only to its own data

trails changes nothing on disk outside its own data directory: the database, its lock files,
its model cache, and its own git store of captured code. It never creates, edits, moves or
deletes your files, indexes, branches, refs, ignore files or folders.

Capturing code reads your repository and writes every new git object into trails' own store,
then copies in any objects it borrowed, so a capture keeps working after your repository is
rewritten, garbage-collected or deleted. Your repository is not written to.

When the right next step is a change to your files, such as adding a line to `.gitignore`,
trails prints the exact command and leaves running it to the agent or the user.

The one exception would be a write the agent asks for by naming the target. No operation in
this version does that. The line trails does not cross is a side effect: a change to any file,
folder or repository the agent did not name in that call.

## 3. trails stores, links and presents. It does not judge

trails derives states only from stored links and events, never from the content's meaning: a
task is ready because of its links and the events on its dependencies, and a claim is an
observation because a version carries a method, never because of what either says. It reaches
no verdicts and runs no statistics. No language model runs inside any tool. Everything it prints comes from
stored data or from fixed templates, so a reader can trace every word to its source.

Data is referenced by location, a local path or an `s3://` URI, together with whatever identity
the agent records for it, such as a pipeline's manifest or the partitions read. trails does not
hash, copy or interpret datasets.

## 4. History is never rewritten by trails

Correction is a new version on the same id. Every version, link event, task event and comment is
appended and stays readable, with the reason for each change stored beside it, and an entry that
turns out to be wrong is retired. A new version must name the version it was written from, so an
edit made from a stale read is refused instead of overwriting a newer one.

trails has no operation that deletes data. Removing something that must not exist at all, such
as a leaked secret, requires a manual database-history rewrite carried out by the user or by an
agent on the user's instruction.

## 5. The store refuses only what it cannot store

A write is refused only when the input is malformed or would overwrite: a version written from a
stale `base_version`, or a claim on a task another session holds, taken without saying why.
Everything else is stored as given, and anything worth noticing comes back as a warning: a
possible secret, a nested repository left out of a capture, a registered path that does not
exist.

One exception is deliberate. A capture that would hold a file over the size cap is refused until
the agent names that file in `exclude` or in `allow_large`. A forgotten dataset committed into a
capture costs far more to undo than one retry costs to avoid, and naming each file keeps the
override from becoming a habit.

## 6. Every response tells the agent what to do next

Each tool says when to use it and what to use instead. Each warning names the problem, says why
it matters, and gives the complete call or command that fixes it, filled in from data the store
already holds. The aim is that the right thing is also the easiest thing, so an agent following
the output step by step ends up doing the right thing.

trails cannot stop an agent from doing the wrong thing without rules that make it rigid. It
guides instead.
