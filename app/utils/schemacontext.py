def get_schema_context() -> str:
    return """
customers(customer_id, name, email, region)
orders(order_id, customer_id, amount, order_date)
products(product_id, name, category, price)

orders.customer_id → customers.customer_id
"""