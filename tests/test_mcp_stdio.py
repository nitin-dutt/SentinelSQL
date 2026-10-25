"""The MCP server must actually start over stdio and serve its tools (the unit tests call the functions directly and never start it)."""

import os
import subprocess
import sys

import pytest

pytest.importorskip("mcp")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_the_stdio_server_starts_and_every_governance_check_passes():
    env = {**os.environ, "PYTHONUTF8": "1"}
    done = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "mcp_smoke.py")], capture_output=True, text=True, timeout=120,
                          cwd=ROOT, env=env)
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-800:]
    assert "ALL CHECKS PASSED" in done.stdout
