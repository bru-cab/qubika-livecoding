SELECT c.name, COUNT(*) AS order_count
FROM orders o
JOIN customers c ON c.id = o.customer_id
GROUP BY c.name
HAVING COUNT(*) > 3
ORDER BY order_count DESC;
