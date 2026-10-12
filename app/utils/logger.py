"""
SentinelSQL logging (alignment_plan RUN-01).

All application output goes through the standard `logging` module under the `sentinel.*`
namespace and is written to **stderr**, never stdout: with the stdio MCP transport, stdout is
the JSON-RPC channel and any stray print corrupts the protocol.

The handler is encoding-safe: a console that cannot encode a character (e.g. cp1252 on a
default Windows terminal) gets a replacement character instead of raising mid-request.
Messages are plain ASCII by convention.

Configure the level with LOG_LEVEL (default INFO).
"""

import logging
import os
import sys

_CONFIGURED = False


def configure_logging() -> None:
    """Idempotent. Called when `app` is imported, so every entry point is covered."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass  # not a TextIOWrapper (e.g. replaced by a test runner)

    root = logging.getLogger("sentinel")
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    if not any(getattr(h, "_sentinel_handler", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._sentinel_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name if name.startswith("sentinel") else f"sentinel.{name}")
