from fastapi import FastAPI

from app.db.database import init_db
from app.graph.workflow import build_graph

from app.api.schemas import QueryRequest, QueryResponse
from app.hitl.web_store import save_run

from app.hitl.web_store import get_run, delete_run
from app.db.database import execute_readonly_query
from app.api.schemas import HITLRequest, HITLResponse

# -----------------------
# FastAPI App (Web)
# -----------------------

app = FastAPI(title="SentinelSQL API")


@app.on_event("startup")
def startup_event():
    init_db()


@app.post("/query", response_model=QueryResponse)
def run_query(req: QueryRequest):
    graph = build_graph()

    state = {
        "user_query": req.query,
        "intent": None,
        "sql": None,
        "validation": None,
        "results": None
    }

    final_state = graph.invoke(state)
    validation = final_state.get("validation", {})

    if validation.get("requires_human"):
        run_id = save_run(final_state)
        return QueryResponse(
            status="requires_human",
            run_id=run_id,
            sql=final_state.get("sql"),
            reason=validation.get("reason")
        )

    if not validation.get("safe"):
        return QueryResponse(
            status="rejected",
            reason=validation.get("reason")
        )

    return QueryResponse(
        status="completed",
        sql=final_state.get("sql"),
        results=final_state.get("results")
    )


    # ❌ Rejected early (destructive intent)
    if status == "rejected":
        return QueryResponse(
            status="rejected",
            reason=validation.get("reason", "Query rejected")
        )

    # ⚠️ HITL required
    if status == "requires_human":
        run_id = save_run(final_state)
        return QueryResponse(
            status="requires_human",
            run_id=run_id,
            sql=final_state.get("sql"),
            reason=validation.get("reason")
        )

    # ✅ Safe query auto-executed
    return QueryResponse(
        status="completed",
        sql=final_state.get("sql"),
        results=final_state.get("results")
    )

@app.post("/hitl/decision", response_model=HITLResponse)
def hitl_decision(req: HITLRequest):
    try:
        state = get_run(req.run_id)
    except KeyError:
        return HITLResponse(
            status="rejected",
            reason="Invalid or expired run_id"
        )

    # ❌ Human rejects the query
    if req.decision == "reject":
        delete_run(req.run_id)
        return HITLResponse(
            status="rejected",
            reason="Query rejected by human reviewer"
        )

    # ✏️ Human edits SQL
    if req.decision == "edit":
        if not req.edited_sql:
            return HITLResponse(
                status="rejected",
                reason="Edited SQL must be provided"
            )
        sql_to_execute = req.edited_sql
    else:
        # ✅ Human approves original SQL
        sql_to_execute = state.get("sql")

    try:
        results = execute_readonly_query(sql_to_execute)
    except Exception as e:
        return HITLResponse(
            status="rejected",
            reason=str(e)
        )
    finally:
        delete_run(req.run_id)

    return HITLResponse(
        status="completed",
        sql=sql_to_execute,
        results=results
    )

# -----------------------
# CLI Entry Point (Existing)
# -----------------------

def main():
    init_db()

    user_query = input("Ask a question: ")

    graph = build_graph()

    final_state = graph.invoke({
        "user_query": user_query,
        "intent": None,
        "sql": None,
        "validation": None,
        "results": None
    })

    print("\n📊 Final Results:")
    print(final_state.get("results"))


if __name__ == "__main__":
    main()
