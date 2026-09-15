import pymysql, json

db = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD', database='grape_test_mys', charset='utf8mb4')
c = db.cursor(pymysql.cursors.DictCursor)
c.execute("SELECT seq, payload FROM events WHERE seq=1736")
r = c.fetchone()
if r:
    payload = json.loads(r['payload']) if r['payload'] else {}
    print("=== payload keys ===")
    print(list(payload.keys()))

    obj_attrs = payload.get('object_attributes', {})
    body = obj_attrs.get('body', '') or obj_attrs.get('description', '') or ''
    print(f"\n=== body length: {len(body)} ===")
    print(body[:3000])

    # 找附件相关字段
    attachments = payload.get('attachments') or obj_attrs.get('attachments')
    print(f"\n=== attachments field: {attachments} ===")

    # 全文搜 attachment
    payload_str = json.dumps(payload, ensure_ascii=False)
    if 'attachment' in payload_str.lower():
        idx = payload_str.lower().find('attachment')
        print("\n=== payload contains attachment ===")
        print(payload_str[max(0,idx-50):idx+200])
    else:
        print("\n=== payload no attachment keyword ===")

db.close()
