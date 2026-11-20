"""
Evaluation data for the analyst chat (benchmarks/suites/analyst.py), over the demo company database.

CONVERSATIONS  each is a first question followed by follow-ups that only make sense with the conversation
               ("now only the North region", "and by month"). Every turn has a gold query that says what a correct
               answer returns. A follow-up asked WITHOUT the conversation cannot be answered, which is the baseline.
CLARIFY        questions labelled by whether a careful analyst would have to ask before answering. The ambiguous ones
               leave out a choice that neither the schema nor the glossary settles; the clear ones include near-misses
               (a glossary term such as "VIP customers", or wording that sounds vague but has one reasonable reading).

No question touches a sensitive table, so every turn can be answered without human review. Every gold query is run
against the demo database by tests/test_analyst_benchmark.py (it must execute and return rows).
"""

CONVERSATIONS = [
    ("C01", [
        ("How many orders has each customer placed? Show the customer name.",
         "SELECT c.name, COUNT(o.order_id) FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name"),
        ("now show the total amount instead of the count",
         "SELECT c.name, SUM(o.amount) FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name"),
        ("only for customers in the North region",
         "SELECT c.name, SUM(o.amount) FROM customers c JOIN orders o ON o.customer_id = c.customer_id WHERE c.region = 'North' GROUP BY c.customer_id, c.name"),
    ]),
    ("C02", [
        ("What is the total order amount?", "SELECT SUM(amount) FROM orders"),
        ("break that down by customer region",
         "SELECT c.region, SUM(o.amount) FROM orders o JOIN customers c ON c.customer_id = o.customer_id GROUP BY c.region"),
        ("and how many orders is that per region?",
         "SELECT c.region, COUNT(o.order_id) FROM orders o JOIN customers c ON c.customer_id = o.customer_id GROUP BY c.region"),
    ]),
    ("C03", [
        ("List the names and prices of products in the Electronics category",
         "SELECT name, price FROM products WHERE category = 'Electronics'"),
        ("which of those cost more than 100?",
         "SELECT name, price FROM products WHERE category = 'Electronics' AND price > 100"),
        ("what is their average price?",
         "SELECT AVG(price) FROM products WHERE category = 'Electronics' AND price > 100"),
    ]),
    ("C04", [
        ("How many support tickets are there for each priority?",
         "SELECT priority, COUNT(*) FROM support_tickets GROUP BY priority"),
        ("only the ones that are not closed",
         "SELECT priority, COUNT(*) FROM support_tickets WHERE status <> 'closed' GROUP BY priority"),
        ("split that by status as well",
         "SELECT priority, status, COUNT(*) FROM support_tickets WHERE status <> 'closed' GROUP BY priority, status"),
    ]),
    ("C05", [
        ("How many shipments has each carrier handled?", "SELECT carrier, COUNT(*) FROM shipments GROUP BY carrier"),
        ("which carrier handled the most?",
         "SELECT carrier FROM shipments GROUP BY carrier ORDER BY COUNT(*) DESC LIMIT 1"),
        ("list that carrier's shipments with their status",
         "SELECT shipment_id, status FROM shipments WHERE carrier = (SELECT carrier FROM shipments GROUP BY carrier ORDER BY COUNT(*) DESC LIMIT 1)"),
    ]),
    ("C06", [
        ("How many units of each product were sold? Show the product name.",
         "SELECT p.name, SUM(oi.quantity) FROM order_items oi JOIN products p ON p.product_id = oi.product_id GROUP BY p.product_id, p.name"),
        ("group it by product category instead",
         "SELECT p.category, SUM(oi.quantity) FROM order_items oi JOIN products p ON p.product_id = oi.product_id GROUP BY p.category"),
        ("and the revenue per category, using quantity times unit price",
         "SELECT p.category, SUM(oi.quantity * oi.unit_price) FROM order_items oi JOIN products p ON p.product_id = oi.product_id GROUP BY p.category"),
    ]),
    ("C07", [
        ("List all campaigns with their channel and budget", "SELECT name, channel, budget FROM campaigns"),
        ("just the email ones", "SELECT name, channel, budget FROM campaigns WHERE channel = 'email'"),
        ("what is their combined budget?", "SELECT SUM(budget) FROM campaigns WHERE channel = 'email'"),
    ]),
    ("C08", [
        ("How many invoices are there for each status?", "SELECT status, COUNT(*) FROM invoices GROUP BY status"),
        ("show the total invoiced amount per status too",
         "SELECT status, COUNT(*), SUM(total_amount) FROM invoices GROUP BY status"),
        ("which status has the largest total?",
         "SELECT status FROM invoices GROUP BY status ORDER BY SUM(total_amount) DESC LIMIT 1"),
    ]),
]

# (id, question, should the assistant ask first?, the choice that is missing or why it is clear)
CLARIFY = [
    ("A01", "Who are our top customers?", True, "top by what: total amount or number of orders, and how many"),
    ("A02", "Show me the recent orders", True, "no period given"),
    ("A03", "Which products are doing well?", True, "no measure: units sold, revenue or returns"),
    ("A04", "Compare the regions", True, "no measure to compare"),
    ("A05", "Which campaigns performed best?", True, "no measure: budget, web events or something else"),
    ("A06", "List the big orders", True, "no threshold for big"),
    ("A07", "How are the carriers doing?", True, "no measure: shipment count, delivery time or status"),
    ("A08", "Show the worst suppliers", True, "no measure of worst"),
    ("N01", "How many customers are in the North region?", False, "fully specified"),
    ("N02", "What is the largest single order amount?", False, "fully specified"),
    ("N03", "Who are our VIP customers?", False, "VIP is defined in the glossary"),
    ("N04", "What is the average order value?", False, "defined in the glossary"),
    ("N05", "List the five most expensive products with their prices", False, "measure and count are given"),
    ("N06", "How many shipments has each carrier handled?", False, "fully specified"),
    ("N07", "Which customers have opened more than one support ticket?", False, "fully specified"),
    ("N08", "What is the total refund amount for each product name?", False, "fully specified"),
]
