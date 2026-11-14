"""Probe: does `write(repos=...)` holding the store lock during code capture delay readers?

For each repository, in two fresh scratch databases under one base directory: time a write that
captures the repository with nothing else running; then, in the second database, time a reader
(`trails search`) alone, and the same reader started 0.3 s after a capturing write began. A fresh
database per measurement keeps the second capture from reusing the first one's objects. Prints
one JSON line per repository. Usage:

    python probes/lock_capture_probe.py <scratch base dir> <repo> [<repo> ...]
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TRAILS = str(Path(sys.executable).parent / "trails")
READER = [TRAILS, "search", "--query", "probe"]


def env_for(db: Path) -> dict[str, str]:
    return {**os.environ, "TRAILS_DB": str(db)}


def timed(args: list[str], db: Path) -> tuple[float, int]:
    start = time.monotonic()
    done = subprocess.run(
        args, capture_output=True, text=True, timeout=600, check=False, env=env_for(db)
    )
    return time.monotonic() - start, done.returncode


def writer(repo: str) -> list[str]:
    return [TRAILS, "write", "--kind", "note", "--title", "lock probe", "--repos", repo]


def main() -> None:
    base = Path(sys.argv[1])
    for n, repo in enumerate(sys.argv[2:]):
        db_a, db_b = base / f"{n}-alone", base / f"{n}-concurrent"
        timed(READER, db_a)  # creates the database
        timed(READER, db_b)
        write_alone, write_rc = timed(writer(repo), db_a)
        reader_alone, _ = timed(READER, db_b)
        start = time.monotonic()
        w = subprocess.Popen(
            writer(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_for(db_b)
        )
        time.sleep(0.3)
        reader_during, read_rc = timed(READER, db_b)
        w.communicate(timeout=600)
        print(
            json.dumps(
                {
                    "repo": repo,
                    "write_alone_s": round(write_alone, 2),
                    "write_rc": write_rc,
                    "write_concurrent_s": round(time.monotonic() - start, 2),
                    "write_concurrent_rc": w.returncode,
                    "reader_alone_s": round(reader_alone, 2),
                    "reader_during_write_s": round(reader_during, 2),
                    "reader_rc": read_rc,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
