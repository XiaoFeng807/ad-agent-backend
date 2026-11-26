SELECT r.key AS ordinal,
       json_extract(r.value, '$.sheet') AS sheet,
       json_extract(r.value, '$.line') AS line,
       json_extract(r.value, '$.values.Date') AS date,
       CAST(json_extract(r.value, '$.values.Clicks') AS INTEGER) AS clicks,
       CAST(json_extract(r.value, '$.values.Impressions') AS INTEGER) AS impressions,
       CAST(json_extract(r.value, '$.values.Purchases') AS INTEGER) AS orders,
       CAST(round(100.0 * json_extract(r.value, '$.values.Spend')) AS INTEGER) AS spend_cents,
       CAST(round(100.0 * json_extract(r.value, '$.values.Sales')) AS INTEGER) AS sales_cents
FROM search_term_report_imports AS i, json_each(i.original_rows) AS r
WHERE i.import_id = :import_id AND i.sha256 = :sha256
  AND json_extract(r.value, '$.values."Campaign ID"') = :campaign
  AND json_extract(r.value, '$.values."Ad Group ID"') = :ad_group
  AND lower(json_extract(r.value, '$.values."Customer Search Term"')) = lower(:term)
  AND json_extract(r.value, '$.values.Date') BETWEEN :start_date AND :end_date
  AND upper(json_extract(r.value, '$.values.Currency')) = 'USD'
  AND json_extract(r.value, '$.normalized.attribution_days') = 7
