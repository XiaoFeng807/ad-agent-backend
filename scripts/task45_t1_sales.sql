WITH RECURSIVE days(window_name, day, end_day) AS (
    SELECT 'baseline', :baseline_start, :baseline_end
    UNION ALL SELECT 'current', :current_start, :current_end
    UNION ALL
    SELECT window_name, date(day, '+1 day'), end_day FROM days WHERE day < end_day
), points AS (
    SELECT s.sku, s.version, d.window_name, d.day,
           json_extract(s.series, '$."' || d.day || '"') units
    FROM listing_test_sales s CROSS JOIN days d
    WHERE s.shop_id=:shop_id AND s.marketplace_id=:marketplace_id
), totals AS (
    SELECT sku, version, window_name, COUNT(units) covered,
           CASE WHEN COUNT(units)=COUNT(*) THEN SUM(units) END units,
           json_group_array(day) FILTER (WHERE units IS NULL) missing_days
    FROM points GROUP BY sku, version, window_name
), changes AS (
    SELECT b.sku, b.version, b.units baseline_units, c.units current_units,
           b.covered baseline_covered, c.covered current_covered,
           b.missing_days baseline_missing, c.missing_days current_missing,
           c.units-b.units delta,
           100.0*(c.units-b.units)/NULLIF(b.units,0) change_pct
    FROM totals b JOIN totals c USING (sku, version)
    WHERE b.window_name='baseline' AND c.window_name='current'
)
SELECT *,
       CASE WHEN COUNT(change_pct) OVER ()=COUNT(*) OVER () AND change_pct<0
              AND COUNT(*) OVER (PARTITION BY change_pct)=1
              AND change_pct=MIN(change_pct) OVER () THEN 1 ELSE 0 END selected
FROM changes ORDER BY sku;
