"""Auth headers for the tokens defined in tests/conftest.py."""


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


EMPLOYEE = bearer("test-employee-token")    # emp-1, role employee
EMPLOYEE2 = bearer("test-employee2-token")  # emp-2, role employee
ADMIN = bearer("test-admin-token")          # admin-1, role admin
ADMIN2 = bearer("test-admin2-token")        # admin-2, role admin
MCP = bearer("test-mcp-token")              # mcp-client-1, role external_ai
MCP2 = bearer("test-mcp2-token")            # mcp-client-2, role external_ai
