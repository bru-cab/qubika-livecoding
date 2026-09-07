CREATE TABLE customers (
    id INTEGER,
    name VARCHAR,
    country VARCHAR,
    signup_date DATE
);

CREATE TABLE orders (
    id INTEGER,
    customer_id INTEGER,
    order_date DATE,
    status VARCHAR,
    order_usd_amount DECIMAL(10,2)
);
