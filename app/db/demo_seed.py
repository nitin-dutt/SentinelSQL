"""
Extended demo dataset (alignment_plan DAT-02).

The original four demo tables (customers, orders, products, salaries) come from schema.sql and
are deliberately left untouched so existing tests and benchmark datasets keep their meaning.
This module adds 17 more tables so the demo database looks like a small company, which is what
makes Schema RAG (it only activates above 5 tables), foreign-key join discovery, sensitive-table
governance and PII masking meaningful instead of trivial.

Design rules
  * Deterministic: every value derives from a fixed seed, so two machines build identical data.
  * Idempotent: a table is only filled when it is empty, so restarts and hot reloads never
    duplicate rows (the root cause of the old 1,845-row `orders` table).
  * Portable DDL/DML: plain types and parameterised executemany, so it runs on SQLite,
    PostgreSQL and MySQL.
  * Synthetic PII only: SSNs use the 9xx range that is never issued, phone numbers use the
    fictional 555-01xx block, card numbers are the published payment-processor test numbers.
"""

import datetime
import random
from typing import Dict, List

from sqlalchemy import text

from app.utils.logger import get_logger

logger = get_logger("sentinel.demo_seed")

SEED = 20241009

# Ordered so that every table appears after the tables its foreign keys reference.
DDL: Dict[str, str] = {
    "departments": """CREATE TABLE IF NOT EXISTS departments (
        department_id INTEGER PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        cost_center VARCHAR(20),
        location VARCHAR(100)
    )""",
    "employees": """CREATE TABLE IF NOT EXISTS employees (
        employee_id INTEGER PRIMARY KEY,
        full_name VARCHAR(150) NOT NULL,
        department_id INTEGER,
        job_title VARCHAR(100),
        hire_date DATE,
        ssn VARCHAR(11),
        phone VARCHAR(30),
        personal_email VARCHAR(255),
        manager_id INTEGER,
        FOREIGN KEY (department_id) REFERENCES departments(department_id)
    )""",
    "performance_reviews": """CREATE TABLE IF NOT EXISTS performance_reviews (
        review_id INTEGER PRIMARY KEY,
        employee_id INTEGER,
        review_date DATE,
        rating INTEGER,
        notes TEXT,
        FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
    )""",
    "timesheets": """CREATE TABLE IF NOT EXISTS timesheets (
        timesheet_id INTEGER PRIMARY KEY,
        employee_id INTEGER,
        work_date DATE,
        hours NUMERIC(4, 1),
        FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
    )""",
    "budgets": """CREATE TABLE IF NOT EXISTS budgets (
        budget_id INTEGER PRIMARY KEY,
        department_id INTEGER,
        fiscal_year INTEGER,
        amount NUMERIC(12, 2),
        FOREIGN KEY (department_id) REFERENCES departments(department_id)
    )""",
    "categories": """CREATE TABLE IF NOT EXISTS categories (
        category_id INTEGER PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        description VARCHAR(255)
    )""",
    "suppliers": """CREATE TABLE IF NOT EXISTS suppliers (
        supplier_id INTEGER PRIMARY KEY,
        name VARCHAR(150) NOT NULL,
        country VARCHAR(80),
        contact_email VARCHAR(255),
        contact_phone VARCHAR(30)
    )""",
    "product_suppliers": """CREATE TABLE IF NOT EXISTS product_suppliers (
        product_id INTEGER,
        supplier_id INTEGER,
        unit_cost NUMERIC(10, 2),
        lead_time_days INTEGER,
        PRIMARY KEY (product_id, supplier_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id)
    )""",
    "order_items": """CREATE TABLE IF NOT EXISTS order_items (
        order_item_id INTEGER PRIMARY KEY,
        order_id INTEGER,
        product_id INTEGER,
        quantity INTEGER,
        unit_price NUMERIC(10, 2),
        FOREIGN KEY (order_id) REFERENCES orders(order_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""",
    "shipments": """CREATE TABLE IF NOT EXISTS shipments (
        shipment_id INTEGER PRIMARY KEY,
        order_id INTEGER,
        carrier VARCHAR(50),
        shipped_date DATE,
        delivered_date DATE,
        status VARCHAR(30),
        FOREIGN KEY (order_id) REFERENCES orders(order_id)
    )""",
    "returns": """CREATE TABLE IF NOT EXISTS returns (
        return_id INTEGER PRIMARY KEY,
        order_id INTEGER,
        product_id INTEGER,
        reason VARCHAR(255),
        refund_amount NUMERIC(10, 2),
        return_date DATE,
        FOREIGN KEY (order_id) REFERENCES orders(order_id),
        FOREIGN KEY (product_id) REFERENCES products(product_id)
    )""",
    "payment_cards": """CREATE TABLE IF NOT EXISTS payment_cards (
        card_id INTEGER PRIMARY KEY,
        customer_id INTEGER,
        card_number VARCHAR(25),
        expiry_month INTEGER,
        expiry_year INTEGER,
        FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
    )""",
    "invoices": """CREATE TABLE IF NOT EXISTS invoices (
        invoice_id INTEGER PRIMARY KEY,
        order_id INTEGER,
        issued_date DATE,
        due_date DATE,
        total_amount NUMERIC(10, 2),
        status VARCHAR(20),
        FOREIGN KEY (order_id) REFERENCES orders(order_id)
    )""",
    "payments": """CREATE TABLE IF NOT EXISTS payments (
        payment_id INTEGER PRIMARY KEY,
        invoice_id INTEGER,
        paid_date DATE,
        amount NUMERIC(10, 2),
        method VARCHAR(30),
        FOREIGN KEY (invoice_id) REFERENCES invoices(invoice_id)
    )""",
    "support_tickets": """CREATE TABLE IF NOT EXISTS support_tickets (
        ticket_id INTEGER PRIMARY KEY,
        customer_id INTEGER,
        opened_date DATE,
        status VARCHAR(20),
        priority VARCHAR(10),
        subject VARCHAR(200),
        notes TEXT,
        FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
    )""",
    "campaigns": """CREATE TABLE IF NOT EXISTS campaigns (
        campaign_id INTEGER PRIMARY KEY,
        name VARCHAR(150),
        channel VARCHAR(50),
        start_date DATE,
        end_date DATE,
        budget NUMERIC(10, 2)
    )""",
    "web_events": """CREATE TABLE IF NOT EXISTS web_events (
        event_id INTEGER PRIMARY KEY,
        customer_id INTEGER,
        campaign_id INTEGER,
        event_type VARCHAR(30),
        event_date DATE,
        FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
        FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
    )""",
}

FIRST = ["Ava", "Liam", "Noah", "Emma", "Olivia", "Elijah", "Mia", "Lucas", "Amara", "Kai", "Zoe", "Ethan",
         "Priya", "Mateo", "Sofia", "Hugo", "Lena", "Omar", "Yuki", "Nina", "Felix", "Ines", "Ravi", "Tara"]
LAST = ["Nguyen", "Okafor", "Silva", "Muller", "Haddad", "Ivanova", "Tanaka", "Brooks", "Castillo", "Dubois",
        "Kowalski", "Mehta", "Rossi", "Larsen", "Abara", "Fischer", "Moreau", "Quinn", "Singh", "Park",
        "Novak", "Reyes", "Hansen", "Walker"]
JOB_TITLES = ["Software Engineer", "Account Executive", "Support Specialist", "Financial Analyst",
              "Marketing Manager", "HR Generalist", "Data Analyst", "Operations Lead"]
CARRIERS = ["UPS", "FedEx", "DHL", "USPS"]
# Published payment-processor test card numbers (never real accounts)
TEST_CARDS = ["4111111111111111", "4012888888881881", "5555555555554444",
              "5105105105105100", "378282246310005", "6011111111111117"]


def _day(rng: random.Random, start: datetime.date, span_days: int) -> str:
    return (start + datetime.timedelta(days=rng.randrange(span_days))).isoformat()


def build_rows() -> Dict[str, List[dict]]:
    """Builds every extended table's rows from the fixed seed. Pure function: no database access."""
    rng = random.Random(SEED)
    rows: Dict[str, List[dict]] = {}

    depts = [("Engineering", "CC-100", "Berlin"), ("Sales", "CC-200", "New York"), ("Support", "CC-300", "Lisbon"),
             ("Finance", "CC-400", "London"), ("Marketing", "CC-500", "Austin"), ("People Ops", "CC-600", "Toronto")]
    rows["departments"] = [{"department_id": i + 1, "name": n, "cost_center": c, "location": l}
                           for i, (n, c, l) in enumerate(depts)]

    employees = []
    for i in range(24):
        employees.append({
            "employee_id": i + 1,
            "full_name": f"{FIRST[i]} {LAST[(i * 5) % len(LAST)]}",
            "department_id": rng.randrange(1, 7),
            "job_title": rng.choice(JOB_TITLES),
            "hire_date": _day(rng, datetime.date(2016, 1, 1), 3000),
            "ssn": f"9{rng.randrange(10, 99)}-{rng.randrange(10, 99)}-{rng.randrange(1000, 9999)}",
            "phone": f"555-010-{rng.randrange(1000, 9999)}",
            "personal_email": f"{FIRST[i].lower()}.{LAST[(i * 5) % len(LAST)].lower()}@mail.example.org",
            "manager_id": None if i < 6 else rng.randrange(1, 7),
        })
    rows["employees"] = employees

    review_notes = [
        "Exceeded targets. Reach out on {phone} to schedule the promotion discussion.",
        "Solid quarter. Prefers contact via {email}.",
        "Needs support with prioritisation; follow up next sprint.",
        "Strong collaborator across teams. Mentoring two juniors.",
        "Missed two deadlines; improvement plan agreed. HR contact {phone}.",
    ]
    reviews = []
    for i in range(40):
        emp = employees[rng.randrange(len(employees))]
        reviews.append({
            "review_id": i + 1,
            "employee_id": emp["employee_id"],
            "review_date": _day(rng, datetime.date(2023, 1, 1), 700),
            "rating": rng.randrange(1, 6),
            "notes": rng.choice(review_notes).format(phone=emp["phone"], email=emp["personal_email"]),
        })
    rows["performance_reviews"] = reviews

    rows["timesheets"] = [
        {"timesheet_id": i * 5 + d + 1, "employee_id": emp["employee_id"],
         "work_date": (datetime.date(2024, 9, 2) + datetime.timedelta(days=d)).isoformat(),
         "hours": round(rng.choice([6.0, 7.5, 8.0, 8.0, 9.0]), 1)}
        for i, emp in enumerate(employees) for d in range(5)
    ]

    rows["budgets"] = [
        {"budget_id": d["department_id"] * 10 + y - 2023, "department_id": d["department_id"],
         "fiscal_year": y, "amount": rng.randrange(80, 600) * 1000}
        for d in rows["departments"] for y in (2023, 2024)
    ]

    cats = [("Electronics", "Computers, phones and accessories"), ("Furniture", "Office and home furniture"),
            ("Appliances", "Kitchen and small appliances"), ("Accessories", "Peripherals and add-ons")]
    rows["categories"] = [{"category_id": i + 1, "name": n, "description": d} for i, (n, d) in enumerate(cats)]

    sup = [("Northwind Components", "Germany"), ("Pacific Source Ltd", "Vietnam"), ("Atlas Furnishings", "Poland"),
           ("BrightHome Supply", "Mexico"), ("Summit Peripherals", "Taiwan"), ("Keystone Metals", "Canada")]
    rows["suppliers"] = [
        {"supplier_id": i + 1, "name": n, "country": c,
         "contact_email": f"sales@{n.split()[0].lower()}.example.com", "contact_phone": f"555-010-{2000 + i * 37}"}
        for i, (n, c) in enumerate(sup)
    ]

    # products 1..4 come from schema.sql
    rows["product_suppliers"] = [
        {"product_id": p, "supplier_id": s, "unit_cost": round(rng.uniform(8, 600), 2), "lead_time_days": rng.randrange(3, 40)}
        for p in range(1, 5) for s in rng.sample(range(1, 7), 2)
    ]

    # orders 1..5 and their customers come from schema.sql: (order_id, amount)
    base_orders = {1: 1200.00, 2: 3400.00, 3: 800.00, 4: 5600.00, 5: 2300.00}
    items, item_id = [], 1
    for order_id in base_orders:
        for product_id in rng.sample(range(1, 5), rng.randrange(1, 4)):
            items.append({"order_item_id": item_id, "order_id": order_id, "product_id": product_id,
                          "quantity": rng.randrange(1, 5), "unit_price": [999.99, 199.50, 49.99, 29.99][product_id - 1]})
            item_id += 1
    rows["order_items"] = items

    rows["shipments"] = []
    for order_id in base_orders:
        shipped = datetime.date(2025, 1, 6) + datetime.timedelta(days=order_id * 3)
        delivered = shipped + datetime.timedelta(days=rng.randrange(2, 8))
        rows["shipments"].append({"shipment_id": order_id, "order_id": order_id, "carrier": rng.choice(CARRIERS),
                                  "shipped_date": shipped.isoformat(), "delivered_date": delivered.isoformat(),
                                  "status": "delivered"})

    rows["returns"] = [
        {"return_id": 1, "order_id": 2, "product_id": 3, "reason": "Arrived damaged", "refund_amount": 49.99, "return_date": "2025-02-01"},
        {"return_id": 2, "order_id": 4, "product_id": 2, "reason": "Wrong size", "refund_amount": 199.50, "return_date": "2025-02-09"},
    ]

    rows["payment_cards"] = [
        {"card_id": i + 1, "customer_id": (i % 4) + 1, "card_number": number,
         "expiry_month": rng.randrange(1, 13), "expiry_year": rng.randrange(2026, 2031)}
        for i, number in enumerate(TEST_CARDS)
    ]

    rows["invoices"] = [
        {"invoice_id": oid, "order_id": oid, "issued_date": f"2025-01-{10 + oid:02d}", "due_date": f"2025-02-{10 + oid:02d}",
         "total_amount": amount, "status": "paid" if oid != 5 else "open"}
        for oid, amount in base_orders.items()
    ]
    rows["payments"] = [
        {"payment_id": oid, "invoice_id": oid, "paid_date": f"2025-01-{20 + oid:02d}", "amount": amount,
         "method": rng.choice(["card", "bank_transfer", "card"])}
        for oid, amount in base_orders.items() if oid != 5
    ]

    ticket_notes = [
        "Customer called from {phone} about a delayed delivery.",
        "Resolved by email to {email}. Customer thanked the team.",
        "Customer verified identity with SSN {ssn}; refund escalated to finance.",
        "Duplicate charge on card {card}; refund issued.",
        "Asked for invoice copy; sent to {email}.",
    ]
    tickets = []
    for i in range(12):
        tickets.append({
            "ticket_id": i + 1, "customer_id": (i % 4) + 1, "opened_date": _day(rng, datetime.date(2025, 1, 1), 120),
            "status": rng.choice(["open", "closed", "closed", "pending"]), "priority": rng.choice(["low", "medium", "high"]),
            "subject": rng.choice(["Delivery delay", "Billing question", "Product defect", "Account access"]),
            "notes": rng.choice(ticket_notes).format(
                phone=f"(555) 010-{rng.randrange(1000, 9999)}", email=f"customer{i + 1}@mail.example.org",
                ssn=f"9{rng.randrange(10, 99)}-{rng.randrange(10, 99)}-{rng.randrange(1000, 9999)}",
                card=f"4111-1111-1111-{rng.randrange(1000, 9999)}"),
        })
    rows["support_tickets"] = tickets

    camps = [("Spring Launch", "email", 12000), ("Summer Sale", "social", 20000), ("Back to School", "search", 15000),
             ("Black Friday", "email", 30000), ("Holiday Gifts", "display", 18000)]
    rows["campaigns"] = [
        {"campaign_id": i + 1, "name": n, "channel": ch, "start_date": f"2025-{i * 2 + 1:02d}-01",
         "end_date": f"2025-{i * 2 + 2:02d}-28", "budget": b}
        for i, (n, ch, b) in enumerate(camps)
    ]
    rows["web_events"] = [
        {"event_id": i + 1, "customer_id": rng.choice([None, 1, 2, 3, 4]), "campaign_id": rng.randrange(1, 6),
         "event_type": rng.choice(["view", "click", "add_to_cart", "purchase"]), "event_date": _day(rng, datetime.date(2025, 1, 1), 300)}
        for i in range(60)
    ]
    return rows


def seed_extended_demo(connection) -> Dict[str, int]:
    """Creates the extended tables and fills the empty ones. Returns {table: rows inserted}."""
    data = build_rows()
    inserted: Dict[str, int] = {}
    for table, ddl in DDL.items():
        connection.execute(text(ddl))
        count = connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count:
            continue
        table_rows = data[table]
        cols = list(table_rows[0].keys())
        connection.execute(
            text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
            table_rows,
        )
        inserted[table] = len(table_rows)
    if inserted:
        logger.info("Seeded %d extended demo tables (%d rows)", len(inserted), sum(inserted.values()))
    return inserted
