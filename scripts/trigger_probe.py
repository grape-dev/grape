#!/usr/bin/env python3
"""Manually trigger local probe + save on 0001."""
import socket
from src.runtime.local_probe import probe_local_engines
from src.runtime.registry import discover_runtimes
from src.runtime.agent import _scan_local_skills_with_detail
from src.user import db as user_db

hostname = socket.gethostname()
print(f"hostname: {hostname}")
probed = [r for r in probe_local_engines(discover_runtimes()) if r.get("installed")]
print(f"probed: {len(probed)} engines")
for r in probed:
    r["local_skills_detail"] = []
    r["local_skills"] = []
    print(f"  {r['name']} v{r.get('version','')} healthy={r.get('healthy')}")
user_db.save_agent_runtimes(probed, host_name=hostname)
print(f"saved {len(probed)} runtimes with host_name={hostname}")
