import sqlite3,json,os,hashlib
from datetime import datetime,timezone
from pathlib import Path
from app.errors import DomainError

SCHEMA=(
    'CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")',
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)',
    # 补卡原始记录只追加：同一 (run_id, correction_id) 保留首次写入，后续重算不会覆盖原件。
    'CREATE TABLE IF NOT EXISTS corrections (run_id TEXT NOT NULL, correction_id TEXT NOT NULL, employee TEXT, shift_day TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (run_id, correction_id))',
    # 每次成功计算留一份结果快照；切换口径失败时回到上一份，不会丢弃旧版证据。
    'CREATE TABLE IF NOT EXISTS run_revisions (run_id TEXT NOT NULL, revision INTEGER NOT NULL, signature TEXT NOT NULL, basis TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (run_id, revision))',
    # 复核决定只追加，不覆盖历史结论。
    'CREATE TABLE IF NOT EXISTS review_log (run_id TEXT NOT NULL, seq INTEGER NOT NULL, version INTEGER NOT NULL, decision TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (run_id, seq))',
)

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    for statement in SCHEMA:db.execute(statement)
    return db

def _now():return datetime.now(timezone.utc).isoformat()

def signature(body):
    """计算证据指纹：只比较业务结论与补卡证据，忽略耗时与生成时间。"""
    material=json.dumps({key:body.get(key) for key in ('calculations','business','reconcile','segments','pricing_basis','holidays','corrections_applied','corrections_rejected')},sort_keys=True,ensure_ascii=False,default=str)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()

def _record_corrections(db,run_id,body):
    created=_now()
    for item in body.get('corrections_applied',[]):
        db.execute('INSERT OR IGNORE INTO corrections(run_id,correction_id,employee,shift_day,payload,created_at) VALUES (?,?,?,?,?,?)',
                   (run_id,item['correction_id'],item.get('employee'),item.get('shift_day'),json.dumps(item,ensure_ascii=False,sort_keys=True),created))
    # 被拒绝的补卡同样留痕，便于核对是否命中；不进入行数对账。
    for index,item in enumerate(body.get('corrections_rejected',[])):
        raw=item.get('raw') if isinstance(item.get('raw'),dict) else {}
        digest=hashlib.sha256((run_id+'|rejected|'+str(index)+'|'+json.dumps(item.get('raw'),sort_keys=True,ensure_ascii=False,default=str)).encode('utf-8')).hexdigest()[:20]
        db.execute('INSERT OR IGNORE INTO corrections(run_id,correction_id,employee,shift_day,payload,created_at) VALUES (?,?,?,?,?,?)',
                   (run_id,digest,raw.get('employee'),raw.get('shift_day'),
                    json.dumps({'status':'rejected',**item},ensure_ascii=False,sort_keys=True,default=str),created))

def _record_revision(db,run_id,body):
    digest=signature(body)
    last=db.execute('SELECT revision,signature FROM run_revisions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(run_id,)).fetchone()
    if last and last['signature']==digest:return last['revision']
    revision=(last['revision']+1) if last else 1
    db.execute('INSERT INTO run_revisions(run_id,revision,signature,basis,body,created_at) VALUES (?,?,?,?,?,?)',
               (run_id,revision,digest,body.get('pricing_basis'),json.dumps(body,ensure_ascii=False),_now()))
    return revision

def _set_active(db,run_id,revision):
    db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('active:'+run_id,str(revision)))

def active_revision(run_id):
    with connection() as db:
        row=db.execute("SELECT value FROM meta WHERE key=?",('active:'+run_id,)).fetchone()
        return int(row['value']) if row else None

def active_signature(run_id):
    with connection() as db:
        row=db.execute("SELECT r.signature FROM run_revisions r JOIN meta m ON m.value=CAST(r.revision AS TEXT) AND m.key=? WHERE r.run_id=?",
                       ('active:'+run_id,run_id)).fetchone()
        return row['signature'] if row else None

def save(run_id, body):
    """首次保存；同一幂等键重跑直接返回已存批次，不覆盖原始结果与补卡原件。

    后续变更（补卡、休息、假期、口径）走 commit_revision，只追加修订。
    """
    with connection() as db:
        existing=db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone()
        if not existing:
            db.execute('INSERT INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
            _record_corrections(db,run_id,body)
            revision=_record_revision(db,run_id,body)
            _set_active(db,run_id,revision)
        # 无论新建还是复用，都把“最近查看”指回该批次；不改动其原始结果。
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def commit_revision(run_id, body):
    """在一个事务内追加修订、切换生效版本并把复核退回待复核。

    任一步失败整体回滚，生效版本与既有补卡原件保持原样。
    """
    with connection() as db:
        _record_corrections(db,run_id,body)
        revision=_record_revision(db,run_id,body)
        _set_active(db,run_id,revision)
        row=db.execute('SELECT version FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','批次不存在',404)
        version=row['version']+1
        db.execute('UPDATE runs SET version=?,decision=? WHERE id=?',(version,'待复核',run_id))
        seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 AS n FROM review_log WHERE run_id=?',(run_id,)).fetchone()['n']
        db.execute('INSERT INTO review_log(run_id,seq,version,decision,created_at) VALUES (?,?,?,?,?)',
                   (run_id,seq,version,'待复核（重算）',_now()))
    return revision

def _stored(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    return r

def get(run_id):
    r=_stored(run_id)
    revision=active_revision(run_id)
    if revision:
        with connection() as db:rev=db.execute('SELECT body FROM run_revisions WHERE run_id=? AND revision=?',(run_id,revision)).fetchone()
        body=json.loads(rev['body']) if rev else json.loads(r['body'])
    else:body=json.loads(r['body'])
    body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'],active_revision=revision)
    return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def corrections(run_id):
    _stored(run_id)
    with connection() as db:rows=db.execute('SELECT correction_id,employee,shift_day,payload,created_at FROM corrections WHERE run_id=? ORDER BY created_at,rowid',(run_id,)).fetchall()
    return [dict(r) for r in rows]

def revisions(run_id):
    _stored(run_id)
    with connection() as db:rows=db.execute('SELECT revision,signature,basis,created_at FROM run_revisions WHERE run_id=? ORDER BY revision',(run_id,)).fetchall()
    return [dict(r) for r in rows]

def revision_body(run_id,revision):
    _stored(run_id)
    with connection() as db:r=db.execute('SELECT body FROM run_revisions WHERE run_id=? AND revision=?',(run_id,revision)).fetchone()
    if not r:raise DomainError('NOT_FOUND','修订不存在',404)
    body=json.loads(r['body']);stored=get(run_id)
    body.update(run_id=run_id,review_version=stored['review_version'],decision=stored['decision'],active_revision=revision);return body

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
        seq=(db.execute('SELECT COALESCE(MAX(seq),0)+1 AS n FROM review_log WHERE run_id=?',(run_id,)).fetchone()['n'])
        db.execute('INSERT INTO review_log(run_id,seq,version,decision,created_at) VALUES (?,?,?,?,?)',
                   (run_id,seq,version+1,decision,_now()))
    return get(run_id)

def review_history(run_id):
    _stored(run_id)
    with connection() as db:rows=db.execute('SELECT seq,version,decision,created_at FROM review_log WHERE run_id=? ORDER BY seq',(run_id,)).fetchall()
    return [dict(r) for r in rows]
