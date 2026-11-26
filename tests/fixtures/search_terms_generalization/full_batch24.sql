-- Inputs are the frozen official-field CSV rows and the seeded membership/row-link evidence.
-- csv_rows uses CSV header names projected to the columns below. Blank sheet is CSV.
WITH owned AS (
    SELECT r.*,
           CASE
               WHEN (SELECT count(*) FROM membership m WHERE m.ad_group = r.ad_group) = 1
               THEN (SELECT sku FROM membership m WHERE m.ad_group = r.ad_group)
               ELSE l.sku
           END AS sku
    FROM csv_rows r
    LEFT JOIN row_links l ON l.line = r.line AND l.ad_group = r.ad_group
), grouped AS (
    SELECT campaign, ad_group, lower(term) AS term, min(line) AS first_line,
           group_concat(line ORDER BY line) AS lines,
           CASE WHEN count(sku) = count(*) AND min(sku) = :sku AND max(sku) = :sku
                THEN 'product' ELSE 'ad_group' END AS attribution,
           sum(impressions) AS impressions, sum(clicks) AS clicks, sum(purchases) AS orders,
           sum(round(spend * 100)) AS spend_cents, sum(round(sales * 100)) AS sales_cents
    FROM owned
    GROUP BY campaign, ad_group, lower(term)
)
SELECT row_number() OVER (ORDER BY first_line) AS item_id, *,
       impressions < 100 AS failed_impressions,
       clicks < 20 AS failed_clicks,
       orders < 2 AS failed_orders,
       clicks = 0 OR 100 * orders < 10 * clicks AS failed_cvr
FROM grouped
ORDER BY first_line;
