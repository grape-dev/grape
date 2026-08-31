import sys, json
sys.path.insert(0, "src")
from src.session_manager import db

# 获取 user_accounts 表结构
cols = db.execute("SHOW COLUMNS FROM user_accounts").fetchall()
col_names = [c['Field'] for c in cols] if isinstance(cols[0], dict) else [c[0] for c in cols]
print("user_accounts 列:", col_names)

# 获取华为云凭据
rows = db.execute("SELECT * FROM user_accounts WHERE id=1").fetchall()
for r in rows:
    d = r if isinstance(r, dict) else dict(zip(col_names, r))
    for k, v in d.items():
        if v and ('ak' in k.lower() or 'sk' in k.lower() or 'access' in k.lower() or 'secret' in k.lower() or 'huawei' in k.lower() or 'hw' in k.lower()):
            sv = str(v)
            if len(sv) > 20:
                print(f"  {k} = {sv[:8]}...{sv[-4:]} (len={len(sv)})")
            else:
                print(f"  {k} = {sv}")
