-- Inputs: raw_orders, raw_items, request (one row), candidates (one row per ID).
-- Each raw Olist item line is one unit. A window includes both endpoints.
CREATE TEMP VIEW matching_lines AS
SELECT c.product, w.name AS window, o.id AS order_id, o.day, o.status, i.line AS item_id
FROM candidates c
JOIN request r
JOIN raw_items i ON i.product = c.product AND i.seller = r.seller
JOIN raw_orders o ON o.id = i.id AND o.status IN ('delivered', 'shipped')
JOIN (
    SELECT 'baseline' AS name, baseline_start AS first_day, baseline_end AS last_day FROM request
    UNION ALL SELECT 'current', current_start, current_end FROM request
) w ON o.day BETWEEN w.first_day AND w.last_day;

CREATE TEMP VIEW candidate_totals AS
SELECT c.product, c.position, EXISTS (
    SELECT 1 FROM raw_items i JOIN request r ON i.seller = r.seller WHERE i.product = c.product
) AS verified,
    count(*) FILTER (WHERE m.window = 'baseline') AS baseline_units,
    count(DISTINCT m.order_id) FILTER (WHERE m.window = 'baseline') AS baseline_orders,
    count(*) FILTER (WHERE m.window = 'current') AS current_units,
    count(DISTINCT m.order_id) FILTER (WHERE m.window = 'current') AS current_orders
FROM candidates c LEFT JOIN matching_lines m ON m.product = c.product
GROUP BY c.product, c.position;

CREATE TEMP VIEW ranked_candidates AS
SELECT *, baseline_units - current_units AS units_lost,
    100.0 * (baseline_units - current_units) / nullif(baseline_units, 0) AS decline_pct,
    rank() OVER (ORDER BY baseline_units - current_units DESC) AS rank
FROM candidate_totals WHERE verified;

CREATE TEMP VIEW selected_candidate AS
SELECT product FROM ranked_candidates
WHERE rank = 1 AND units_lost > 0
  AND (SELECT count(*) FROM ranked_candidates WHERE rank = 1) = 1
  AND NOT EXISTS (SELECT 1 FROM candidate_totals WHERE NOT verified)
  AND (SELECT wants_detail FROM request);

CREATE TEMP VIEW selected_order_items AS
SELECT m.* FROM matching_lines m JOIN selected_candidate s USING (product);
