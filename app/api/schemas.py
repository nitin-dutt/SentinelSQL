from pydantic import BaseModel
from typing import Optional, List, Dict, Any


# -------- Query Submission --------

class QueryRequest(BaseModel):
    query: str


class QueryResponse(BaseModel):
    status: str  # completed | requires_human | rejected
    sql: Optional[str] = None
    results: Optional[List[Dict[str, Any]]] = None
    run_id: Optional[str] = None
    reason: Optional[str] = None


# -------- HITL Decision --------

class HITLRequest(BaseModel):
    run_id: str
    decision: str  # approve | edit | reject
    edited_sql: Optional[str] = None


class HITLResponse(BaseModel):
    status: str  # completed | rejected
    sql: Optional[str] = None
    results: Optional[List[Dict[str, Any]]] = None
    reason: Optional[str] = None
