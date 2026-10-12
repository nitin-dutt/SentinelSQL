"""Run metadata: everything needed to know exactly what produced a result file."""

import hashlib
import os
import platform
import subprocess
import sys
import time
from typing import Any, Dict, Iterable

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def package_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "unknown"


def run_metadata(dataset_files: Iterable[str] = (), extra: Dict[str, Any] = None) -> Dict[str, Any]:
    """Timestamp, code revision, environment, model identifiers and dataset hashes."""
    dirty = _git("status", "--porcelain")
    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_head": _git("rev-parse", "--short", "HEAD") or "none",
        "git_working_tree_dirty": bool(dirty),
        "git_changed_files": len([l for l in dirty.splitlines() if l.strip()]),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "logical_cpus": os.cpu_count(),
        "llm_model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        "embedding_model": "models/gemini-embedding-001",
        "packages": {p: package_version(p) for p in ("sqlglot", "fastapi", "langchain-google-genai", "langgraph", "numpy")},
        "datasets": {os.path.relpath(p, ROOT).replace(os.sep, "/"): sha256_file(p) for p in dataset_files if os.path.exists(p)},
        **(extra or {}),
    }
