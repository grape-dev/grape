import urllib.request, json, urllib.parse

base = 'https://api.gitcode.com/api/v5'

# Try milestone with form-encoded AND proper Content-Type header
print('=== CREATE MILESTONE (form-encoded with content-type) ===')
form_data = urllib.parse.urlencode({'title': 'v1.0.0'}).encode('utf-8')
headers = {
    'Authorization': 'Bearer XmMqZP6LJ89XpPkAL2YYm8yY',
    'Content-Type': 'application/x-www-form-urlencoded'
}
req = urllib.request.Request(f'{base}/repos/Jason7602/grape/milestones', data=form_data, headers=headers, method='POST')
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode('utf-8')
        print('HTTP Status:', resp.status)
        print(json.dumps(json.loads(body), indent=2, ensure_ascii=False)[:500])
except urllib.error.HTTPError as e:
    print('HTTP', e.code, ':', e.reason)
    err_body = e.read().decode('utf-8')
    print(err_body[:500])
print()

# Also try with different param names for milestone
for field_name in ['title', 'name', 'milestone', 'subject', 'summary']:
    form_data = urllib.parse.urlencode({field_name: 'v1.0.0'}).encode('utf-8')
    req = urllib.request.Request(f'{base}/repos/Jason7602/grape/milestones', data=form_data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            print(f'Milestone with "{field_name}": HTTP {resp.status} -> SUCCESS')
            print(json.dumps(json.loads(body), indent=2, ensure_ascii=False)[:300])
            break  # Stop if one works
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8')
        print(f'Milestone with "{field_name}": HTTP {e.code} -> {err_body[:150]}')
