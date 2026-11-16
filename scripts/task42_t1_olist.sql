WITH RECURSIVE
days(window, day, end_day) AS (
    SELECT 'baseline', :baseline_start, :baseline_end
    UNION ALL SELECT 'current', :current_start, :current_end
    UNION ALL SELECT window, date(day, '+1 day'), end_day FROM days WHERE day < end_day
),
selected AS (
    SELECT o.day, o.id
    FROM raw_orders o JOIN raw_items i ON i.id = o.id
    WHERE i.seller = :seller AND i.product = :product
      AND o.status IN (:status1, :status2)
),
daily AS (
    SELECT d.window, d.day, count(s.id) units, count(DISTINCT s.id) orders
    FROM days d LEFT JOIN selected s ON s.day = d.day
    GROUP BY d.window, d.day
),
totals AS (
    SELECT window, sum(units) units, sum(orders) orders FROM daily GROUP BY window
)
SELECT window, day, units, orders, NULL units_pct, NULL orders_pct FROM daily
UNION ALL
SELECT window, NULL, units, orders, NULL, NULL FROM totals
UNION ALL
SELECT 'change', NULL, c.units-b.units, c.orders-b.orders,
       100.0*(c.units-b.units)/nullif(b.units,0), 100.0*(c.orders-b.orders)/nullif(b.orders,0)
FROM totals b CROSS JOIN totals c WHERE b.window='baseline' AND c.window='current'
ORDER BY window, day;
