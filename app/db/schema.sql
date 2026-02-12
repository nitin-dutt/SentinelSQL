-- Customers table
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    region TEXT
);


-- Orders table
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER,
    amount REAL,
    order_date DATE,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);


-- Products table
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    name TEXT,
    category TEXT,
    price REAL
);


-- Sample customers
INSERT OR IGNORE INTO customers (name, email, region) VALUES
('Alice', 'alice@example.com', 'North'),
('Bob', 'bob@example.com', 'South'),
('Charlie', 'charlie@example.com', 'West'),
('Diana', 'diana@example.com', 'East');

-- Sample orders
INSERT OR IGNORE INTO orders (customer_id, amount, order_date) VALUES
(1, 1200, '2025-01-05'),
(1, 3400, '2025-01-18'),
(2, 800, '2025-01-12'),
(3, 5600, '2025-01-20'),
(4, 2300, '2025-01-25');
