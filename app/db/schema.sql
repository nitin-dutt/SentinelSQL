-- Customers table
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE,
    region VARCHAR(100)
);


-- Orders table
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    amount NUMERIC(10, 2),
    order_date DATE,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    UNIQUE(customer_id, amount, order_date)
);


-- Products table
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    name VARCHAR(255),
    category VARCHAR(100),
    price NUMERIC(10, 2)
);


-- Salaries table (Confidential / Tier-1 Sensitive Table for HITL Governance Testing)
CREATE TABLE IF NOT EXISTS salaries (
    employee_id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_name VARCHAR(255) NOT NULL,
    salary NUMERIC(12, 2) NOT NULL,
    department VARCHAR(100),
    UNIQUE(employee_name, department)
);


-- Sample customers
INSERT OR IGNORE INTO customers (name, email, region) VALUES
('Alice', 'alice@example.com', 'North'),
('Bob', 'bob@example.com', 'South'),
('Charlie', 'charlie@example.com', 'West'),
('Diana', 'diana@example.com', 'East');

-- Sample orders
INSERT OR IGNORE INTO orders (customer_id, amount, order_date) VALUES
(1, 1200.00, '2025-01-05'),
(1, 3400.00, '2025-01-18'),
(2, 800.00, '2025-01-12'),
(3, 5600.00, '2025-01-20'),
(4, 2300.00, '2025-01-25');

-- Sample products
INSERT OR IGNORE INTO products (product_id, name, category, price) VALUES
(1, 'Laptop', 'Electronics', 999.99),
(2, 'Desk Chair', 'Furniture', 199.50),
(3, 'Coffee Maker', 'Appliances', 49.99),
(4, 'Wireless Mouse', 'Electronics', 29.99);

-- Sample salaries
INSERT OR IGNORE INTO salaries (employee_name, salary, department) VALUES
('Eve Adams', 145000.00, 'Security Engineering'),
('Alice Smith', 125000.00, 'Engineering'),
('Bob Jones', 98000.00, 'Marketing');

