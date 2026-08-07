import urllib.request, json

headers = {
    "Authorization": "Bearer XmMqZP6LJ89XpPkAL2YYm8yY",
    "Content-Type": "application/json"
}
base = "https://api.gitcode.com/api/v5"

# Try more field names
field_names = [
    "Title", "TITLE", "t", "subject", "Subject",
    "name", "Name", "NAME", "n",
    "milestone_name", "milestoneTitle", "milestone_name",
    "tag", "version", "release", "sprint",
    "text", "value", "label", "data",
    "content", "body", "summary", "headline",
    "issue_title", "objective", "goal",
    "plan", "task", "todo",
]

for field in field_names:
    data = json.dumps({field: "v1.0.0"}).encode("utf-8")
    req = urllib.request.Request(f"{base}/repos/Jason7602/grape/milestones", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body_resp = resp.read().decode("utf-8")
            print(f"FIELD \"{field}\": HTTP {resp.status} - SUCCESS")
            print(f"  {body_resp[:300]}")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8")[:100]
        if "must not be blank" not in err:
            print(f"FIELD \"{field}\": HTTP {e.code} - {err}")

# Also try sending empty body to see what it expects
print("\n=== TRY EMPTY BODY ===")
req = urllib.request.Request(f"{base}/repos/Jason7602/grape/milestones", data=b"{}", headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"HTTP {resp.status}: {resp.read().decode()[:300]}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:300]}")

# Send no body at all
print("\n=== TRY NO BODY ===")
import tempfile, os
# Can't send no body with POST easily, skip

# Try with query params
print("\n=== CHECK RATE LIMIT ===")
req = urllib.request.Request(f"{base}/rate_limit", headers={"Authorization": "Bearer XmMqZP6LJ89XpPkAL2YYm8yY"})
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"HTTP {resp.status}: {resp.read().decode()[:500]}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:300]}")
