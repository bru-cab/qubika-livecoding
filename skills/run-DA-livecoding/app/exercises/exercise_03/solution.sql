SELECT c.country, SUM(o.order_usd_amount) AS total_completed_usd
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE c.country IN ('Brazil', 'Argentina')
  AND o.status = 'completed'
GROUP BY c.country;
