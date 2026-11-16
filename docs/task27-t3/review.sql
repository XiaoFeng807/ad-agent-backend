-- Parameters: baseline impressions, current impressions, baseline CTR %, current CTR %,
-- minimum impressions per window, minimum relative decline %.
WITH facts(bi, ci, bctr, cctr, min_exp, min_drop) AS (VALUES (?, ?, ?, ?, ?, ?)),
comparison AS (
    SELECT *, 100.0 * (bctr - cctr) / NULLIF(bctr, 0) AS relative_decline_pct FROM facts
)
SELECT relative_decline_pct,
       CASE WHEN relative_decline_pct IS NOT NULL THEN
           bi >= min_exp AND ci >= min_exp AND relative_decline_pct >= min_drop END AS qualifies
FROM comparison;
