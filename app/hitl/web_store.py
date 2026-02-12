from typing import Dict
from uuid import uuid4

# In-memory store for paused LangGraph states
_RUN_STORE: Dict[str, dict] = {}


def save_run(state: dict) -> str:
    """
    Save paused graph state and return run_id
    """
    run_id = str(uuid4())
    _RUN_STORE[run_id] = state
    return run_id


def get_run(run_id: str) -> dict:
    """
    Retrieve paused graph state
    """
    if run_id not in _RUN_STORE:
        raise KeyError(f"Run ID not found: {run_id}")
    return _RUN_STORE[run_id]


def delete_run(run_id: str) -> None:
    """
    Cleanup after completion or rejection
    """
    _RUN_STORE.pop(run_id, None)
