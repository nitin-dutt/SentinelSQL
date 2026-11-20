"""Auth headers for the tokens defined in tests/conftest.py."""


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


EMPLOYEE = bearer("test-employee-token")    # emp-1, role employee
EMPLOYEE2 = bearer("test-employee2-token")  # emp-2, role employee
ADMIN = bearer("test-admin-token")          # admin-1, role admin
ADMIN2 = bearer("test-admin2-token")        # admin-2, role admin
MCP = bearer("test-mcp-token")              # mcp-client-1, role external_ai
MCP2 = bearer("test-mcp2-token")            # mcp-client-2, role external_ai


# ---------------------------------------------------------------- stand-in pipelines (see app/answering.py)
# A pipeline is a callable: request state in, final state out. These replace the model; everything after the model is real.

from contextlib import contextmanager  # noqa: E402


def pipeline_writing(sql: str):
    """
    A pipeline whose "model" always writes `sql`. The governed run that follows is the real one (policy gate, review
    hold, execution on the test database, masking, audit record). `.states` keeps every request state it was given.
    """
    from app.graph.workflow import governed_run_node

    def pipeline(state):
        pipeline.states.append(dict(state))
        return governed_run_node({**state, "sql": sql})
    pipeline.states = []
    return pipeline


def pipeline_stopping(reason: str, error: bool = False):
    """A pipeline that stops before any SQL exists: a refusal, or with error=True an outage of the model or schema."""
    def pipeline(state):
        pipeline.states.append(dict(state))
        return {**state, "stopped": {"status": "error" if error else "blocked", "reason": reason}}
    pipeline.states = []
    return pipeline


@contextmanager
def use_pipeline(pipeline):
    """Makes POST /query and POST /chat use `pipeline`. Starts from an empty semantic cache, so the pipeline is really reached."""
    from app.answering import get_pipeline
    from app.cache.semantic_cache import get_semantic_cache
    from app.main import app
    get_semantic_cache().clear()
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    try:
        yield pipeline
    finally:
        app.dependency_overrides.pop(get_pipeline, None)
