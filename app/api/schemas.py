from pydantic import BaseModel
from typing import Optional, List, Dict, Any


# -------- Query Submission --------

class QueryRequest(BaseModel):
    query: str


class QueryResponse(BaseModel):
    status: str  # completed | requires_human | rejected | error
    sql: Optional[str] = None
    results: Optional[Dict[str, Any]] = None
    run_id: Optional[str] = None
    reason: Optional[str] = None
    confidence_score: Optional[int] = None
    cached: Optional[bool] = False
    latency_ms: Optional[float] = None
    # Set when the answer came from the semantic cache: how it matched, so the UI can show it
    cache_match_type: Optional[str] = None       # exact | semantic
    cache_similarity: Optional[float] = None
    cache_original_query: Optional[str] = None   # the earlier question whose answer is being reused


# -------- HITL Decision --------

class HITLRequest(BaseModel):
    run_id: str
    decision: str  # approve | edit | reject
    edited_sql: Optional[str] = None
    save_as_template: bool = False


class HITLResponse(BaseModel):
    status: str  # completed | rejected
    sql: Optional[str] = None
    results: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
