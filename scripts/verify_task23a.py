"""Independently recount the real three-day trace from the public SQLite aggregate."""
import json
from fractions import Fraction
from pathlib import Path
import sqlite3

root = Path(__file__).resolve().parents[1]
report_path = root / 'logs/task23a-real-retry/report.json'
report = json.loads(report_path.read_text(encoding='utf-8'))
for recorded in report['reports']:
    server = json.loads((root / recorded['dir'] / 'server.json').read_text(encoding='utf-8'))
    issued = [json.loads(call['function']['arguments']) for request in server['sdk_requests']
              for call in request['response_calls'] if call['function']['name'] == 'get_plan_metrics_by_date']
    canonical = lambda rows: sorted((row['plan_id'], row['start_date'], row['end_date']) for row in rows)
    assert canonical(issued) == canonical(recorded.get('queries', []))
    assert all(row['tool_choice'] == 'auto' for row in server['sdk_requests'])
case = next(item for item in report['reports'] if item['name'] == 'three')
evidence = case['events'][1]
mapping = {101: '32009848', 102: '27589150', 103: '13289562'}
results = []
with sqlite3.connect((root / 'data/plan_metrics/criteo.sqlite3').as_uri() + '?mode=ro', uri=True) as conn:
    for candidate in evidence['comparison']['candidates']:
        plan = candidate['plan_id']
        totals = []
        for window in ('baseline', 'current'):
            metric = candidate['metrics'][window]
            rows = conn.execute('SELECT metric_date, impressions, clicks FROM plan_daily_metrics '
                                'WHERE source_plan_id=? AND metric_date BETWEEN ? AND ? ORDER BY metric_date',
                                (mapping[plan], metric['start_date'], metric['end_date'])).fetchall()
            impressions, clicks = sum(r[1] for r in rows), sum(r[2] for r in rows)
            assert len(rows) == 3 and (impressions, clicks) == (metric['impressions'], metric['clicks'])
            totals.append({'start_date': metric['start_date'], 'end_date': metric['end_date'],
                           'impressions': impressions, 'clicks': clicks})
        base, current = (Fraction(t['clicks'], t['impressions']) for t in totals)
        decline = (base-current)/base*100
        assert abs(float(decline)-candidate['calculation']['relative_decline_pct']) < 1e-10
        results.append({'plan_id': plan, 'source_plan_id': mapping[plan], 'windows': totals,
                        'relative_decline_pct': float(decline)})
assert results == sorted(results, key=lambda row: row['relative_decline_pct'], reverse=True)
output = report_path.parent / 'independent-recount.json'
output.write_text(json.dumps({'trace_id': case['trace_id'], 'results': results}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(output)
