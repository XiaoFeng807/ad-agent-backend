WITH RECURSIVE
  calendar(window, day, last) AS (
    VALUES ('baseline', :baseline_start, :baseline_end), ('current', :current_start, :current_end)
    UNION ALL SELECT window, date(day, '+1 day'), last FROM calendar WHERE day < last
  ),
  daily AS (
    SELECT p.sku, s.version, window, day, json_extract(s.series, '$."' || day || '"') AS units
    FROM listing_test_products p
    LEFT JOIN listing_test_sales s USING(shop_id, marketplace_id, sku)
    CROSS JOIN calendar
    WHERE p.shop_id = :shop AND p.marketplace_id = :market
  ),
  totals AS (
    SELECT sku, window, count(units) AS covered,
           CASE WHEN count(units) = count(*) THEN sum(units) END AS units,
           json_group_array(day) FILTER (WHERE units IS NULL) AS missing,
           json_group_array(json_object('date', day, 'units', units) ORDER BY day) AS days
    FROM daily GROUP BY sku, window
  ),
  sales AS (
    SELECT b.sku, b.units AS baseline, c.units AS current, b.days AS baseline_days, c.days AS current_days,
           c.covered AS current_covered, b.missing AS baseline_missing, c.missing AS current_missing,
           round(100.0 * (c.units - b.units) / nullif(b.units, 0), 6) AS pct,
           coalesce(b.units > 0 AND 100 * (b.units - c.units) >= :threshold * b.units, 0) AS selected
    FROM totals b JOIN totals c USING(sku) WHERE b.window='baseline' AND c.window='current'
  ),
  linked AS (
    SELECT o.*, l.sku AS mapped
    FROM raw_report o LEFT JOIN search_term_amazon_row_links l
      ON l.shop_id = :shop AND l.marketplace_id = :market AND l.report_sha256 = o.sha256
      AND l.sheet=o.sheet AND l.line=o.line AND l.campaign_id=o.campaign AND l.ad_group_id=o.ad_group
    WHERE o.day BETWEEN :current_start AND :current_end
  ),
  metrics AS (
    SELECT s.sku, o.term, o.campaign, o.ad_group, o.sha256, count(*) AS row_count,
           json_group_array(o.line ORDER BY o.line) AS lines,
           CASE WHEN count(o.mapped) = count(*) THEN 'product' ELSE 'ad_group' END AS attribution,
           sum(CAST(o.impressions AS INTEGER)) AS impressions, sum(CAST(o.clicks AS INTEGER)) AS clicks,
           sum(CAST(o.orders AS INTEGER)) AS orders, sum(CAST(round(100 * o.spend) AS INTEGER)) AS spend_cents,
           sum(CAST(round(100 * o.sales) AS INTEGER)) AS sales_cents
    FROM sales s JOIN linked o ON s.selected=1 AND (o.mapped=s.sku OR o.mapped IS NULL)
    WHERE EXISTS (SELECT 1 FROM search_term_amazon_membership m
         WHERE m.shop_id=:shop AND m.marketplace_id=:market AND m.campaign_id=o.campaign AND m.ad_group_id=o.ad_group
         AND o.day BETWEEN m.start_date AND m.end_date AND m.sku=s.sku)
    GROUP BY s.sku, o.term, o.campaign, o.ad_group, o.sha256
  ),
  facts AS (
    SELECT f.sku, f.version AS facts_version, json_extract(j.value, '$.statement') AS capacity
    FROM listing_test_facts f, json_each(f.facts) j
    WHERE f.shop_id=:shop AND f.marketplace_id=:market AND json_extract(j.value, '$.fact_id')='capacity'
  )
SELECT s.*, m.term, m.campaign, m.ad_group, m.sha256, m.lines, m.attribution, m.impressions, m.clicks, m.orders,
       m.spend_cents, m.sales_cents, f.capacity, f.facts_version, p.version AS title_version, p.title,
       CASE WHEN m.term IS NULL THEN NULL
            WHEN m.attribution='ad_group' OR m.impressions<100 OR m.clicks<20 THEN 'observe'
            WHEN f.capacity IS NULL THEN 'missing'
            WHEN f.capacity LIKE '%Capacity: 8 L.%' THEN 'review'
            WHEN f.capacity LIKE '%Capacity: 12 L.%' AND p.title NOT LIKE '%12 L%' AND m.orders>=2
                 AND 100*m.orders >= 10*m.clicks THEN 'title' END AS action
FROM sales s LEFT JOIN metrics m USING(sku) LEFT JOIN facts f USING(sku)
JOIN listing_test_products p ON p.shop_id=:shop AND p.marketplace_id=:market AND p.sku=s.sku
ORDER BY s.sku;
