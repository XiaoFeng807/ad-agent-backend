WITH RECURSIVE
days(window, day, end_day) AS (
    SELECT 'baseline', :baseline_start, :baseline_end
    UNION ALL SELECT 'current', :current_start, :current_end
    UNION ALL SELECT window, date(day, '+1 day'), end_day FROM days WHERE day < end_day
),
points AS (
    SELECT d.window, d.day, s.version, json_extract(s.series, '$."' || d.day || '"') units
    FROM days d CROSS JOIN listing_test_sales s
    WHERE s.shop_id=:shop_id AND s.marketplace_id=:marketplace_id AND s.sku=:sku
),
totals AS (
    SELECT window, count(units) covered, CASE WHEN count(units)=count(*) THEN sum(units) END units
    FROM points GROUP BY window
)
SELECT p.*, t.covered, t.units total, c.units-b.units delta,
       round(100.0*(c.units-b.units)/nullif(b.units,0),6) relative_pct
FROM points p JOIN totals t USING(window)
JOIN totals b ON b.window='baseline' JOIN totals c ON c.window='current'
ORDER BY p.window,p.day;
