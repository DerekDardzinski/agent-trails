"""Whether an artifact or a captured commit can be read now.

Read-only, and local only: these functions read local files and trails' own capture store,
and never fetch a remote location. An s3:// URI, a query or free text is reported `unresolvable`, which
says the check was not run, rather than `absent`, which says it ran and found nothing.

Nothing here raises for a missing or unreadable file: that is a status.
"""

from __future__ import annotations

import hashlib
import re
import socket
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from trails.models import ArtifactRef, CodeCapture, ProjectPath

_HASH_CHUNK_BYTES = 64 * 1024
# A URI scheme such as s3:// or https://. A Windows drive letter would match too, which is
# immaterial: this store runs where fcntl exists.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
NOT_LOCAL = "not checked: location is not a local path"


class Reachability(BaseModel):
    """What a reachability check found."""

    status: Literal["reachable", "changed", "absent", "referenced", "unresolvable"]
    path: str | None  # the local path checked, when there is one
    detail: str  # one sentence: what was checked and what was found


def reachability(ref: ArtifactRef, *, verify: bool) -> Reachability:
    """Report whether an artifact can be read now, without reading anything remote.

    A local file: absent when missing; with `verify` and a recorded digest, reachable when the
    sha256 matches and changed when it does not; otherwise reachable on existence alone, and
    the detail says the digest was not compared. A local directory: referenced, not checked.
    Any non-local location (s3://, a query, free text): unresolvable, detail says
    "not checked: location is not a local path". Never raises for a missing or unreadable
    file; that is a status.
    """
    local = _local_path(ref.location)
    if local is None:
        return Reachability(status="unresolvable", path=None, detail=NOT_LOCAL)
    return _check_file(local, ref.digest if verify else None, compared=verify or ref.digest is None)


def code_reachability(capture: CodeCapture, store: Path) -> Reachability:
    """reachable when captured_sha resolves as a commit in the capture store; absent when the
    store or the commit is missing. Never raises."""
    sha = capture.captured_sha
    if not store.is_dir():
        return Reachability(
            status="absent", path=str(store), detail=f"capture store {store} does not exist"
        )
    try:
        completed = subprocess.run(
            ["git", f"--git-dir={store}", "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return Reachability(status="absent", path=str(store), detail=f"git could not run: {exc}")
    if completed.returncode == 0:
        return Reachability(
            status="reachable", path=str(store), detail=f"commit {sha} resolves in {store}"
        )
    return Reachability(
        status="absent", path=str(store), detail=f"commit {sha} does not resolve in {store}"
    )


def _local_path(location: str) -> Path | None:
    """The location as a local path, or None when it is a URI, a query or free text.

    Local means absolute or `~`-relative with no URI scheme. A relative path is not local,
    because the directory it was relative to was not recorded.
    """
    if _SCHEME.match(location) or "\n" in location:
        return None
    if location.startswith("~"):
        try:
            return Path(location).expanduser()
        except RuntimeError:
            # `~name` with no such user, or free text that happens to start with a tilde.
            return None
    if location.startswith("/"):
        return Path(location)
    return None


def _check_file(path: Path, digest: str | None, *, compared: bool) -> Reachability:
    try:
        if path.is_dir():
            # A directory is present, so "absent" would send a reader looking for a file; it
            # is data referenced by location, and nothing about its contents is checked.
            return Reachability(
                status="referenced",
                path=str(path),
                detail=f"{path} is a directory; referenced, not checked",
            )
        if not path.is_file():
            return Reachability(status="absent", path=str(path), detail=f"{path} does not exist")
        if digest is None:
            detail = (
                f"{path} exists; no digest was recorded to compare"
                if compared
                else f"{path} exists; its digest is compared by show detail provenance and by lint"
            )
            return Reachability(status="reachable", path=str(path), detail=detail)
        found = _sha256(path)
    except OSError as exc:
        return Reachability(status="absent", path=str(path), detail=f"{path} cannot be read: {exc}")
    if found == _hex(digest):
        return Reachability(
            status="reachable", path=str(path), detail=f"{path} exists and its sha256 matches"
        )
    return Reachability(
        status="changed",
        path=str(path),
        detail=f"{path} exists but its sha256 is {found}, not the recorded {_hex(digest)}",
    )


def _hex(digest: str) -> str:
    return digest.removeprefix("sha256:")


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


PathState = Literal["exists", "missing", "not checked"]


def is_uri(location: str) -> bool:
    """Whether `location` starts with a URI scheme such as s3://."""
    return _SCHEME.match(location) is not None


def path_state(item: ProjectPath) -> PathState:
    """Whether a project path exists here: not checked for a URI or a path on another host."""
    if is_uri(item.path) or (item.host and item.host != socket.gethostname()):
        return "not checked"
    try:
        return "exists" if Path(item.path).expanduser().exists() else "missing"
    except (OSError, RuntimeError):
        return "missing"
