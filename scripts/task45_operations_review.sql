WITH RECURSIVE days(window_name, day, end_day) AS (
    SELECT 'baseline', :baseline_start, :baseline_end
    UNION ALL SELECT 'current', date(:approved_day, '+1 day'), date(:approved_day, '+7 days')
    UNION ALL SELECT window_name, date(day, '+1 day'), end_day FROM days WHERE day < end_day
), points AS (
    SELECT window_name, day, CASE WHEN day < :review_day THEN json_extract(series, '$."' || day || '"') END units
    FROM listing_test_sales CROSS JOIN days
    WHERE shop_id=:shop AND marketplace_id=:market AND sku=:sku
), totals AS (
    SELECT window_name, min(day) start_date, max(day) end_date, count(units) covered_days,
           CASE WHEN count(units)=7 THEN sum(units) END units,
           json_group_array(json_object('date',day,'units',units) ORDER BY day) days,
           json_group_array(day ORDER BY day) FILTER (WHERE units IS NULL) missing
    FROM points GROUP BY window_name
)
SELECT b.*, c.start_date current_start, c.end_date current_end, c.covered_days current_covered,
       c.units current_units, c.days current_days, c.missing current_missing,
       c.units-b.units delta, round(100.0*(c.units-b.units)/nullif(b.units,0),6) change_pct
FROM totals b CROSS JOIN totals c WHERE b.window_name='baseline' AND c.window_name='current';
