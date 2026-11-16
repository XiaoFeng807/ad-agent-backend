"""Check TASK-024 archived, allowlisted SDK inputs and actual tool calls."""
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
r = json.loads((p / 'task24.json').read_text(encoding='utf-8'))
s = json.loads((p / 'server.json').read_text(encoding='utf-8'))
assert r['ok'] and len(s['sdk_requests']) == 4
out = []
for index, item in enumerate(s['sdk_requests']):
    name = item['tools'][0]['function']['name']
    assert item['status'] == 200
    queries = json.loads(item['input_messages'][1]['content'])['required_queries']
    calls = [json.loads(c['function']['arguments']) for c in item['response_calls'] if c['function']['name'] == name]
    key = lambda x: json.dumps(x, sort_keys=True)
    assert sorted(map(key, calls)) == sorted(map(key, queries))
    assert all(set(q) == ({'plan_id', 'start_date', 'end_date'} if name == 'get_plan_metrics_by_date' else {'query'}) for q in queries)
    out.append({'turn': index // 2 + 1, 'tool': name, 'queries': queries, 'issued': calls})
(p / 'verified-sdk.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('verified 4 SDK inputs and issued calls')
