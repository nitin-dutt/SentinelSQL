import os
import tempfile

# These must be set before `app` is imported anywhere: they redirect the audit log and the HITL
# run store to throwaway files (tests used to write into the real audit.db) and define the test tokens.
_TEST_TMP_DIR = tempfile.mkdtemp(prefix="sentinel-tests-")
# The demo database too: tests used to read whatever sentinel.db sat in the repo root (which is not tracked in git, so a clean
# checkout, and therefore CI, had none). Each session now builds its own seeded copy in the temp folder and never touches the real one.
os.environ["ACTIVE_DB"] = "sqlite"
os.environ["DATABASE_URL_SQLITE"] = "sqlite:///" + os.path.join(_TEST_TMP_DIR, "demo.db").replace(os.sep, "/")
os.environ["DATABASE_URL"] = ""
os.environ["READONLY_DATABASE_URL"] = ""
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["AUDIT_DB_PATH"] = os.path.join(_TEST_TMP_DIR, "audit.db")
os.environ["HITL_DB_PATH"] = os.path.join(_TEST_TMP_DIR, "hitl_runs.db")
os.environ["TRACE_DB_PATH"] = os.path.join(_TEST_TMP_DIR, "traces.db")
os.environ["CONVERSATION_DB_PATH"] = os.path.join(_TEST_TMP_DIR, "conversations.db")
for _name in ("LLM_PRICES_JSON", "LLM_PRICE_INPUT_PER_MTOK", "LLM_PRICE_OUTPUT_PER_MTOK", "TRACING_ENABLED", "INTENT_MODE", "SQL_CANDIDATES",
              "ANALYST_CLARIFY", "ANALYST_EXPLORE"):
    os.environ.pop(_name, None)
os.environ["AUTH_TOKENS"] = ",".join([
    "test-employee-token:emp-1:employee",
    "test-employee2-token:emp-2:employee",
    "test-admin-token:admin-1:admin",
    "test-admin2-token:admin-2:admin",
    "test-mcp-token:mcp-client-1:external_ai",
    "test-mcp2-token:mcp-client-2:external_ai",
])
os.environ.pop("HITL_ALLOW_SELF_APPROVAL", None)
# Embeddings in tests are the deterministic offline hash: no network, no API quota burned, no flakiness.
os.environ["EMBEDDING_PROVIDER"] = "offline"

import pytest  # noqa: E402
from unittest.mock import patch  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    os.environ["ACTIVE_DB"] = "sqlite"
    from app.db.database import init_db
    from app.runtime import reset_runtime_state
    reset_runtime_state()
    init_db()                       # creates and seeds the 21-table demo database in the temp folder
    yield
    reset_runtime_state()


@pytest.fixture
def mock_llm_validation():
    """
    Mock the UniversalLLM so tests don't require an active Ollama or Gemini instance.
    """
    with patch("app.utils.llm_factory.UniversalLLM.invoke") as mock_invoke:
        mock_invoke.return_value = "100"  # High confidence
        yield mock_invoke
