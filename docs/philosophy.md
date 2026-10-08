# trails design philosophy

trails is a lab notebook for agents. It records what was learned, links it to what it was
built on, and points to the code and data behind it. It has no say in how the work itself is
done.

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
ordinary records. Each path has an optional label and access instructions such as which AWS
profile reads an `s3://` URI. Registering a project creates nothing at those paths. Records may
belong to zero, one or several projects, and membership changes append events without revising
record content.

## 2. trails writes only to its own data

trails changes nothing on disk outside its own data directory: the database, its lock files,
its model cache, and its own git store of captured code. It never creates, edits, moves or
deletes your files, indexes, branches, refs, ignore files or folders.

Capturing code reads your repository and writes every new git object into trails' own store,
then copies in any objects it borrowed, so a capture keeps working after your repository is
rewritten, garbage-collected or deleted. Your repository is not written to.

When the right next step is a change to your files, such as adding a line to `.gitignore`,
trails prints the exact command and leaves running it to the agent or the user.

The one exception is a write the agent asks for by naming the target. `export` writes one
record to the file path the agent passes, and refuses to replace an existing file unless the
agent also passes `overwrite`. The line trails does not cross is a side effect: a change to
any file, folder or repository the agent did not name in that call.

## 3. trails stores, links and presents. It does not judge

trails computes nothing about the meaning of a record. It derives no verdicts or statuses and
runs no statistics. No language model runs inside any tool. Everything it prints comes from
stored data or from fixed templates, so a reader can trace every word to its source.

Data is referenced by location, a local path or an `s3://` URI, together with whatever identity
the agent records for it, such as a pipeline's manifest or the partitions read. trails does not
hash, copy or interpret datasets.

## 4. History is never rewritten by trails

Records are append-only. Correcting a record supersedes it, and a record that turns out to be
wrong is retired. Both stay readable, and the reason for each change is stored with it.

trails has no operation that deletes data. Removing something that must not exist at all, such
as a leaked secret, requires a manual database-history rewrite carried out by the user or by an
agent on the user's instruction.

## 5. The store refuses only what it cannot store

A write is refused only when the input is malformed or would overwrite an existing record.
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
