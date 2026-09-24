"""QC định kỳ cho baseline_gate_v2. Chỉ đọc SQLite; không tự sửa/bịa dữ liệu."""

from crawlers.facebook.crawl_facebook import parse_comment_tooltip


def check(store, sample_size=20):
    issues=[]
    checks=[
      ("non_accepted_context", "SELECT COUNT(*) n FROM comments c LEFT JOIN contexts x ON x.platform=c.platform AND x.context_id=c.context_id WHERE x.context_id IS NULL OR x.verdict!='accept'"),
      ("empty_text", "SELECT COUNT(*) n FROM comments WHERE trim(comment_text)=''"),
      ("facebook_unresolved_metadata", "SELECT COUNT(*) n FROM comments c JOIN contexts x ON x.platform=c.platform AND x.context_id=c.context_id WHERE c.platform='facebook' AND x.metadata_resolved=0"),
      ("reply_parent_inconsistent", "SELECT COUNT(*) n FROM comments WHERE comment_type='reply' AND (parent_comment_id IS NULL OR parent_comment_id='') AND parent_unresolved=0"),
    ]
    for kind,sql in checks:
        count=store.conn.execute(sql).fetchone()['n']
        if count: issues.append({'severity':'critical','kind':kind,'count':count})
    timestamp_rows = store.conn.execute(
        "SELECT posted_at_raw, posted_at FROM comments WHERE platform='facebook'"
    ).fetchall()
    invalid_timestamps = sum(
        1 for row in timestamp_rows
        if not row['posted_at_raw']
        or parse_comment_tooltip(row['posted_at_raw']) != row['posted_at']
        or not (row['posted_at'] or '').endswith('+07:00')
    )
    if invalid_timestamps:
        issues.append({'severity':'critical','kind':'facebook_invalid_comment_timestamp','count':invalid_timestamps})
    rows=store.conn.execute("SELECT platform,comment_id,context_id,substr(comment_text,1,200) text FROM comments ORDER BY RANDOM() LIMIT ?",(sample_size,)).fetchall()
    return {'ok':not issues,'issues':issues,'samples':[dict(r) for r in rows]}
