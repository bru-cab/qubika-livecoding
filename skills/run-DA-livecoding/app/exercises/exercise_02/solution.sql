SELECT c.name, o.order_date, o.order_usd_amount
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.order_usd_amount > 500
ORDER BY o.order_usd_amount DESC;
