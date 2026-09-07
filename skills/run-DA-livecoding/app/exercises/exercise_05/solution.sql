SELECT c.name, SUM(o.order_usd_amount) AS total_completed_usd
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.status = 'completed'
GROUP BY c.name
ORDER BY total_completed_usd DESC
LIMIT 5;
