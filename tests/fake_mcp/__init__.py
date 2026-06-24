"""In-repo fake MCP servers serving recorded fixtures (spec §12).

These let the connector layer, graph mappers, and (later) the agent loop run in
CI against real stdio MCP sessions without a live cluster or observability stack.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def server_command(name: str) -> str:
    """A stdio connector `endpoint` string that spawns one of the fake servers.

    e.g. server_command("k8s") -> '<python> <.../k8s_server.py>'
    """
    script = _HERE / f"{name}_server.py"
    return f"{sys.executable} {script}"
