import os
import tempfile

# These must be set before `app` is imported anywhere: they redirect the audit log and the HITL
# run store to throwaway files (tests used to write into the real audit.db) and define the test tokens.
_TEST_TMP_DIR = tempfile.mkdtemp(prefix="sentinel-tests-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_TEST_TMP_DIR, "audit.db")
os.environ["HITL_DB_PATH"] = os.path.join(_TEST_TMP_DIR, "hitl_runs.db")
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
    yield


@pytest.fixture
def mock_llm_validation():
    """
    Mock the UniversalLLM so tests don't require an active Ollama or Gemini instance.
    """
    with patch("app.utils.llm_factory.UniversalLLM.invoke") as mock_invoke:
        mock_invoke.return_value = "100"  # High confidence
        yield mock_invoke
