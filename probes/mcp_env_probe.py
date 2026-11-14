"""Probe: what an MCP server launched by a harness can learn about its session.

Run by a harness as a stdio MCP server. On its one tool call it writes its environment,
parent pid chain and the MCP client info to probes/out/<harness>-<pid>.json.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from fastmcp import Context, FastMCP

OUT = Path(__file__).parent / "out"
mcp = FastMCP("probe")


def _ancestors() -> list[dict[str, str]]:
    chain = []
    pid = os.getpid()
    for _ in range(8):
        line = subprocess.run(
            ["ps", "-o", "ppid=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not line:
            break
        ppid, _, command = line.partition(" ")
        chain.append({"pid": str(pid), "ppid": ppid.strip(), "command": command[:200]})
        pid = int(ppid)
        if pid <= 1:
            break
    return chain


@mcp.tool
async def probe(ctx: Context) -> str:
    """Record what this server can see about its session."""
    params = ctx.session.client_params
    info = params.clientInfo.model_dump() if params else None
    data = {
        "client_info": info,
        "env": {
            k: v
            for k, v in os.environ.items()
            if any(s in k.upper() for s in ("SESSION", "CLAUDE", "KIRO", "TRANSCRIPT", "AGENT"))
        },
        "ancestors": _ancestors(),
        "argv": sys.argv,
    }
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{(info or {}).get('name', 'unknown')}-{os.getpid()}.json"
    path.write_text(json.dumps(data, indent=2))
    return str(path)


if __name__ == "__main__":
    mcp.run()
