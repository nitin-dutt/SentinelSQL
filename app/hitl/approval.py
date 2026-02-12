def human_approval(sql_query: str) -> str | None:
    """
    Human-in-the-loop approval step.
    Returns approved (or edited) SQL, or None if rejected.
    """
    print("\n👩‍💻 HUMAN APPROVAL REQUIRED")
    print("Proposed SQL:\n")
    print(sql_query)
    print("\nOptions:")
    print("  [a] Approve as-is")
    print("  [e] Edit SQL")
    print("  [r] Reject")

    choice = input("\nYour choice (a/e/r): ").strip().lower()

    if choice == "a":
        return sql_query

    if choice == "e":
        print("\nEnter edited SQL (end with a semicolon):")
        edited_sql = input(">> ").strip()
        return edited_sql

    if choice == "r":
        print("\n❌ Query rejected by human.")
        return None

    print("\nInvalid choice. Query rejected by default.")
    return None
