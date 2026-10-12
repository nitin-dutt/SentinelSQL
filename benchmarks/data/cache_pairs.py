"""
Labelled question pairs for the semantic-cache safety benchmark.

(id, kind, question_a, question_b, should_hit, sql_a)
  should_hit=True   a paraphrase: the cached answer to A is also the right answer to B
  should_hit=False  B asks something different (one decisive change); serving A's answer would be wrong
  sql_a             the SQL cached for A (used by the literal-value guard; "" when it has no string filter)

Authored independently of the cache guards' implementation: each negative changes exactly one thing a user
would consider decisive, written the way people actually rephrase questions.
"""

PAIRS = [
    # ---- paraphrases: must be served from the cache -------------------------------------------------
    ("P01", "paraphrase", "How many customers do we have?", "What is the total number of customers?", True, ""),
    ("P02", "paraphrase", "Show total revenue by customer", "What is each customer's total revenue?", True, ""),
    ("P03", "paraphrase", "List the products in the Electronics category", "Show me all Electronics products", True, "SELECT name FROM products WHERE category = 'Electronics'"),
    ("P04", "paraphrase", "What is the average order amount?", "Average amount per order", True, ""),
    ("P05", "paraphrase", "Which customers have never placed an order?", "Customers with no orders", True, ""),
    ("P06", "paraphrase", "Count support tickets by priority", "How many tickets are there for each priority level?", True, ""),
    ("P07", "paraphrase", "Show open invoices", "List the invoices that are still open", True, "SELECT * FROM invoices WHERE status = 'open'"),
    ("P08", "paraphrase", "Total quantity sold per product", "How many units of each product were sold?", True, ""),
    ("P09", "paraphrase", "Which suppliers are located in Germany?", "List the suppliers based in Germany", True, "SELECT name FROM suppliers WHERE country = 'Germany'"),
    ("P10", "paraphrase", "Show the top 5 customers by revenue", "Who are our five biggest customers by revenue?", True, ""),
    ("P11", "paraphrase", "Orders placed in 2025", "Show me the orders from 2025", True, ""),
    ("P12", "paraphrase", "List shipments delivered by UPS", "Which shipments did UPS deliver?", True, "SELECT * FROM shipments WHERE carrier = 'UPS'"),
    ("P13", "paraphrase", "How many returns were there for each reason?", "Number of returns per reason", True, ""),
    ("P14", "paraphrase", "What is the total budget for 2024?", "Total budget in 2024", True, ""),
    ("P15", "paraphrase", "Show payments by method", "Payments grouped by payment method", True, ""),
    ("P16", "paraphrase", "What are the names of all departments?", "List every department name", True, ""),
    ("P17", "paraphrase", "Count the products in each category", "How many products are in each category?", True, ""),
    ("P18", "paraphrase", "Which customers are in the North region?", "Show customers from the North region", True, "SELECT name FROM customers WHERE region = 'North'"),
    ("P19", "paraphrase", "What is the maximum order amount?", "Largest order amount", True, ""),
    ("P20", "paraphrase", "List the campaigns and their budgets", "Show every campaign with its budget", True, ""),
    ("P21", "paraphrase", "How many high priority tickets are open?", "Count open tickets with high priority", True, "SELECT COUNT(*) FROM support_tickets WHERE priority = 'high' AND status = 'open'"),
    ("P22", "paraphrase", "Show the number of orders per customer", "How many orders has each customer placed?", True, ""),
    ("P23", "paraphrase", "What is the total refund amount?", "Sum of all refunds", True, ""),
    ("P24", "paraphrase", "Which products cost more than 100?", "Products priced above 100", True, ""),
    # ---- a different number ----------------------------------------------------------------------------
    ("N01", "number", "Show the top 5 customers by revenue", "Show the top 10 customers by revenue", False, ""),
    ("N02", "number", "List orders above 1000", "List orders above 2000", False, ""),
    ("N03", "number", "Revenue for the last 3 months", "Revenue for the last 6 months", False, ""),
    ("N04", "number", "Which products cost more than 50?", "Which products cost more than 500?", False, ""),
    # ---- a different year / month / quarter ----------------------------------------------------------------
    ("N05", "date", "How many orders were placed in 2024?", "How many orders were placed in 2025?", False, ""),
    ("N06", "date", "Revenue in January", "Revenue in March", False, ""),
    ("N07", "date", "Show sales for Q1", "Show sales for Q2", False, ""),
    ("N08", "date", "What is the total budget for 2023?", "What is the total budget for 2024?", False, ""),
    # ---- opposite comparison ---------------------------------------------------------------------------------
    ("N09", "comparison", "Customers with more than 3 orders", "Customers with fewer than 3 orders", False, ""),
    ("N10", "comparison", "Orders above the average amount", "Orders below the average amount", False, ""),
    ("N11", "comparison", "Shipments delivered before February", "Shipments delivered after February", False, ""),
    ("N12", "comparison", "Products priced over 100", "Products priced under 100", False, ""),
    # ---- different aggregate -------------------------------------------------------------------------------------
    ("N13", "aggregate", "Show the average order amount", "Show the total order amount", False, ""),
    ("N14", "aggregate", "What is the maximum order amount?", "What is the minimum order amount?", False, ""),
    ("N15", "aggregate", "Count the orders per customer", "Sum the order amounts per customer", False, ""),
    ("N16", "aggregate", "Average ticket count per customer", "Total ticket count per customer", False, ""),
    # ---- a different named entity (capitalised) ----------------------------------------------------------------------
    ("N17", "entity", "How many customers are in the North region?", "How many customers are in the South region?", False, "SELECT COUNT(*) FROM customers WHERE region = 'North'"),
    ("N18", "entity", "Which suppliers are located in Germany?", "Which suppliers are located in France?", False, "SELECT name FROM suppliers WHERE country = 'Germany'"),
    ("N19", "entity", "List shipments delivered by UPS", "List shipments delivered by FedEx", False, "SELECT * FROM shipments WHERE carrier = 'UPS'"),
    ("N20", "entity", "List the products in the Electronics category", "List the products in the Furniture category", False, "SELECT name FROM products WHERE category = 'Electronics'"),
    ("N21", "entity", "Show all orders for Alice", "Show all orders for Bob", False, "SELECT * FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE c.name = 'Alice'"),
    # ---- a different entity typed in lower case (only the cached SQL reveals it) ------------------------------------------------
    ("N22", "entity_lowercase", "customers in the north region", "customers in the south region", False, "SELECT name FROM customers WHERE region = 'north'"),
    ("N23", "entity_lowercase", "tickets with high priority", "tickets with low priority", False, "SELECT * FROM support_tickets WHERE priority = 'high'"),
    ("N24", "entity_lowercase", "shipments by ups", "shipments by dhl", False, "SELECT * FROM shipments WHERE carrier = 'ups'"),
    # ---- negation / opposite state -------------------------------------------------------------------------------------------------
    ("N25", "negation", "Show orders that have shipped", "Show orders that have not shipped", False, ""),
    ("N26", "negation", "Customers with orders", "Customers without orders", False, ""),
    ("N27", "negation", "Show open tickets", "Show closed tickets", False, "SELECT * FROM support_tickets WHERE status = 'open'"),
    ("N28", "negation", "Products that were returned", "Products that were never returned", False, ""),
    # ---- ranking direction ----------------------------------------------------------------------------------------------------------------
    ("N29", "ranking", "Show the highest paid orders", "Show the lowest paid orders", False, ""),
    ("N30", "ranking", "Top customers by spend", "Bottom customers by spend", False, ""),
    ("N31", "ranking", "Show the newest orders", "Show the oldest orders", False, ""),
    # ---- relative time window --------------------------------------------------------------------------------------------------------------
    ("N32", "relative_time", "Orders from this month", "Orders from last month", False, ""),
    ("N33", "relative_time", "Tickets opened today", "Tickets opened yesterday", False, ""),
    ("N34", "relative_time", "Revenue this year", "Revenue last year", False, ""),
    # ---- a different subject entirely -------------------------------------------------------------------------------------------------------
    ("N35", "subject", "How many customers do we have?", "How many suppliers do we have?", False, ""),
    ("N36", "subject", "List all departments", "List all campaigns", False, ""),
    ("N37", "subject", "Show the budget by department", "Show the budget by campaign", False, ""),
    ("N38", "subject", "Count orders per customer", "Count tickets per customer", False, ""),
]
