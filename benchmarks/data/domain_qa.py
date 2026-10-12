"""
Domain question set for the architectural ablation, over the 21-table demo company database.

Each question: (id, tier, question, gold_sql, needs)
  needs = set of capabilities the question depends on, used to read the ablation by subgroup:
      "glossary"  the answer hinges on a business term defined in the data dictionary (VIP, AOV, net revenue, ...)
      "joins3"    three or more tables must be joined (a schema-retrieval stress)
  tier is only a label for reporting.

The questions are phrased the way people ask them, never quoting column or table names, and every gold query
was run against the demo database to confirm it returns a sensible, non-empty answer (tests/test_benchmark_data.py).

GOLDEN_EXAMPLES are the verified question/SQL pairs loaded into the golden-query memory for the "golden memory"
configurations. They are deliberately DIFFERENT questions from the evaluation set (same families, different
filters, tables or wording) so the memory teaches a pattern and never contains an answer.
"""

QUESTIONS = [
    # ---- easy: one table ---------------------------------------------------------------------------------
    ("D01", "easy", "How many customers are in the North region?", "SELECT COUNT(*) FROM customers WHERE region = 'North'", set()),
    ("D02", "easy", "List the names and prices of products in the Electronics category", "SELECT name, price FROM products WHERE category = 'Electronics'", set()),
    ("D03", "easy", "What is the total amount of all orders?", "SELECT SUM(amount) FROM orders", set()),
    ("D04", "easy", "How many support tickets have high priority?", "SELECT COUNT(*) FROM support_tickets WHERE priority = 'high'", set()),
    ("D05", "easy", "Show the name and country of every supplier", "SELECT name, country FROM suppliers", set()),
    ("D06", "easy", "What is the largest single order amount?", "SELECT MAX(amount) FROM orders", set()),
    ("D07", "easy", "How many campaigns use the email channel?", "SELECT COUNT(*) FROM campaigns WHERE channel = 'email'", set()),
    ("D08", "easy", "List the distinct shipping carriers", "SELECT DISTINCT carrier FROM shipments", set()),
    # ---- medium: joins and aggregates ------------------------------------------------------------------------
    ("D09", "medium", "What is the total order amount for each customer? Show the customer name.",
     "SELECT c.name, SUM(o.amount) FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name", set()),
    ("D10", "medium", "How many units of each product were sold? Show the product name.",
     "SELECT p.name, SUM(oi.quantity) FROM order_items oi JOIN products p ON p.product_id = oi.product_id GROUP BY p.product_id, p.name", set()),
    ("D11", "medium", "How many shipments has each carrier handled?",
     "SELECT carrier, COUNT(*) FROM shipments GROUP BY carrier", set()),
    ("D12", "medium", "Show each department's name and its number of employees",
     "SELECT d.name, COUNT(e.employee_id) FROM departments d LEFT JOIN employees e ON e.department_id = d.department_id GROUP BY d.department_id, d.name", set()),
    ("D13", "medium", "What is the total refund amount for each product name?",
     "SELECT p.name, SUM(r.refund_amount) FROM returns r JOIN products p ON p.product_id = r.product_id GROUP BY p.product_id, p.name", set()),
    ("D14", "medium", "List every customer with the number of support tickets they have opened",
     "SELECT c.name, COUNT(t.ticket_id) FROM customers c LEFT JOIN support_tickets t ON t.customer_id = c.customer_id GROUP BY c.customer_id, c.name", set()),
    ("D15", "medium", "For each campaign, how many website events were recorded?",
     "SELECT cam.name, COUNT(w.event_id) FROM campaigns cam LEFT JOIN web_events w ON w.campaign_id = cam.campaign_id GROUP BY cam.campaign_id, cam.name", set()),
    ("D16", "medium", "What is the average performance review rating for each department name?",
     "SELECT d.name, AVG(pr.rating) FROM performance_reviews pr JOIN employees e ON e.employee_id = pr.employee_id "
     "JOIN departments d ON d.department_id = e.department_id GROUP BY d.department_id, d.name", {"joins3"}),
    ("D17", "medium", "Which suppliers provide the product named Laptop?",
     "SELECT s.name FROM suppliers s JOIN product_suppliers ps ON ps.supplier_id = s.supplier_id "
     "JOIN products p ON p.product_id = ps.product_id WHERE p.name = 'Laptop'", {"joins3"}),
    ("D18", "medium", "What is the total amount of payments received for each invoice status?",
     "SELECT i.status, SUM(p.amount) FROM invoices i JOIN payments p ON p.invoice_id = i.invoice_id GROUP BY i.status", set()),
    # ---- hard: business terms from the glossary ------------------------------------------------------------------
    ("D19", "hard", "Who are our VIP customers?",
     "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name HAVING SUM(o.amount) >= 3000", {"glossary"}),
    ("D20", "hard", "What is our net revenue?",
     "SELECT SUM(amount) - (SELECT COALESCE(SUM(refund_amount), 0) FROM returns) FROM orders", {"glossary"}),
    ("D21", "hard", "What is the average order value?", "SELECT AVG(amount) FROM orders", {"glossary"}),
    ("D22", "hard", "List the open invoices with their due dates", "SELECT invoice_id, due_date FROM invoices WHERE status = 'open'", {"glossary"}),
    ("D23", "hard", "How many escalated tickets do we have?",
     "SELECT COUNT(*) FROM support_tickets WHERE priority = 'high' AND status <> 'closed'", {"glossary"}),
    ("D24", "hard", "Which shipments were slow deliveries?",
     "SELECT shipment_id FROM shipments WHERE julianday(delivered_date) - julianday(shipped_date) > 5", {"glossary"}),
    ("D25", "hard", "List our senior employees", "SELECT full_name FROM employees WHERE hire_date < '2020-01-01'", {"glossary"}),
    ("D26", "hard", "Who are the top performers?",
     "SELECT e.full_name FROM employees e JOIN performance_reviews pr ON pr.employee_id = e.employee_id "
     "GROUP BY e.employee_id, e.full_name HAVING AVG(pr.rating) >= 4", {"glossary", "joins3"}),
    ("D27", "hard", "How many active customers do we have?",
     "SELECT COUNT(*) FROM customers WHERE customer_id IN (SELECT customer_id FROM orders)", {"glossary"}),
    # ---- hard: long join paths ----------------------------------------------------------------------------------
    ("D28", "hard", "Which product categories has each customer bought? Show the customer name and category, without duplicates.",
     "SELECT DISTINCT c.name, p.category FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
     "JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id", {"joins3"}),
    ("D29", "hard", "What is the total quantity sold for each supplier country?",
     "SELECT s.country, SUM(oi.quantity) FROM order_items oi JOIN product_suppliers ps ON ps.product_id = oi.product_id "
     "JOIN suppliers s ON s.supplier_id = ps.supplier_id GROUP BY s.country", {"joins3"}),
    ("D30", "hard", "Which customers have a high priority support ticket and have also placed an order? Show their names.",
     "SELECT DISTINCT c.name FROM customers c JOIN support_tickets t ON t.customer_id = c.customer_id "
     "JOIN orders o ON o.customer_id = c.customer_id WHERE t.priority = 'high'", {"joins3"}),
]

GOLDEN_EXAMPLES = [
    ("How many customers are in the South region?", "SELECT COUNT(*) FROM customers WHERE region = 'South'"),
    ("What is the average order amount for each customer? Show the customer name.",
     "SELECT c.name, AVG(o.amount) FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name"),
    ("Show the total units sold for each product category",
     "SELECT p.category, SUM(oi.quantity) FROM order_items oi JOIN products p ON p.product_id = oi.product_id GROUP BY p.category"),
    ("Which customers have placed more than one order?",
     "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name HAVING COUNT(*) > 1"),
    ("List shipments delivered by DHL", "SELECT shipment_id FROM shipments WHERE carrier = 'DHL'"),
    ("How many tickets are open for each priority?", "SELECT priority, COUNT(*) FROM support_tickets WHERE status = 'open' GROUP BY priority"),
    ("Show the total budget for each department name",
     "SELECT d.name, SUM(b.amount) FROM budgets b JOIN departments d ON d.department_id = b.department_id GROUP BY d.department_id, d.name"),
    ("List the employees hired after 2022", "SELECT full_name FROM employees WHERE hire_date > '2022-12-31'"),
    ("How many orders did each customer place, including customers with none?",
     "SELECT c.name, COUNT(o.order_id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name"),
    ("What is the total invoice value for each invoice status?", "SELECT status, SUM(total_amount) FROM invoices GROUP BY status"),
    ("Which suppliers provide products in the Furniture category?",
     "SELECT DISTINCT s.name FROM suppliers s JOIN product_suppliers ps ON ps.supplier_id = s.supplier_id "
     "JOIN products p ON p.product_id = ps.product_id WHERE p.category = 'Furniture'"),
    ("Which products were returned more than once?",
     "SELECT p.name FROM products p JOIN returns r ON r.product_id = p.product_id GROUP BY p.product_id, p.name HAVING COUNT(*) > 1"),
]
