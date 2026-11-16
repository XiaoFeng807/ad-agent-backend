-- Independently reconstruct requested window totals and coverage directly from the pinned artifact.
-- Parameters: source_plan_id, start_date, end_date, excluded_date (NULL in ordinary cases).
WITH RECURSIVE
params(source_plan_id, start_date, end_date, excluded_date) AS (VALUES (?, ?, ?, ?)),
days(day) AS (
    SELECT start_date FROM params
    UNION ALL
    SELECT date(day, '+1 day') FROM days, params WHERE day < end_date
)
SELECT COUNT(*) AS expected_days,
       COUNT(m.metric_date) AS observed_days,
       GROUP_CONCAT(CASE WHEN m.metric_date IS NULL THEN days.day END, ',') AS missing_dates,
       CASE WHEN COUNT(*) = COUNT(m.metric_date) THEN SUM(m.impressions) END AS impressions,
       CASE WHEN COUNT(*) = COUNT(m.metric_date) THEN SUM(m.clicks) END AS clicks,
       CASE WHEN COUNT(*) = COUNT(m.metric_date)
            THEN 100.0 * SUM(m.clicks) / NULLIF(SUM(m.impressions), 0) END AS ctr_pct
FROM days CROSS JOIN params p
LEFT JOIN plan_daily_metrics m ON m.metric_date = days.day
    AND m.source_plan_id = p.source_plan_id AND days.day IS NOT p.excluded_date;
