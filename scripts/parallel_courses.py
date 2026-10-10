"""Manual pilot: opaque course slots, encrypted per-course deltas, one publisher."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.data.schema import SCHEMA_SQL
from scripts.validate_db import validate_database

MAX_PARALLEL = 5


def configured_courses(raw):
    values=list(dict.fromkeys(x.strip() for x in raw.split(',') if x.strip()))
    if not values or len(values)>100 or any(not x.isdigit() for x in values):
        raise ValueError('Invalid course selection')
    return values


def selected_course(raw, slot):
    courses=configured_courses(raw)
    if type(slot) is not int or not 0<=slot<len(courses):
        raise ValueError('Invalid course slot')
    return courses[slot]


def course_delta(source, target, course_id):
    """Copy only one course, never merge stale snapshots of unrelated courses."""
    validate_database(str(source))
    conn=sqlite3.connect(target)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute('ATTACH DATABASE ? AS source',(str(source),))
        with conn:
            # Name columns explicitly: migrated legacy DB column order differs.
            for table in ('courses','lectures'):
                columns=[row[1] for row in conn.execute(f'PRAGMA main.table_info({table})')]
                names=','.join('"'+name+'"' for name in columns)
                conn.execute(f'INSERT INTO {table}({names}) SELECT {names} FROM source.{table} WHERE course_id=?',(course_id,))
            columns=[row[1] for row in conn.execute('PRAGMA main.table_info(ppt_pages)')]
            names=','.join('"'+name+'"' for name in columns)
            selected=','.join('p."'+name+'"' for name in columns)
            conn.execute(f'INSERT INTO ppt_pages({names}) SELECT {selected} FROM source.ppt_pages p JOIN source.lectures l ON l.sub_id=p.sub_id WHERE l.course_id=?',(course_id,))
            conn.execute("INSERT INTO meta SELECT * FROM source.meta WHERE key IN (SELECT 'summary_review:' || sub_id FROM source.lectures WHERE course_id=?) OR key IN (SELECT 'summary_figures:' || sub_id FROM source.lectures WHERE course_id=?)", (course_id, course_id))
            prefix='auto_glossary:'+course_id+':'
            conn.execute('INSERT INTO meta SELECT * FROM source.meta WHERE substr(key,1,?)=?',(len(prefix),prefix))
    finally:
        conn.close()
    validate_database(str(target))


def bundle_key(password):
    if len(password)<32:
        raise ValueError('Missing encryption key')
    return hashlib.sha256(('icourse-parallel-v1:'+password).encode()).digest()


def seal(payload, password, run_id, slot):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce=os.urandom(12)
    aad=f'icourse-parallel-v1:{run_id}:{slot}'.encode()
    return b'ICP1'+nonce+AESGCM(bundle_key(password)).encrypt(nonce,zlib.compress(payload),aad)


def unseal(blob, password, run_id, slot):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:4]!=b'ICP1':
        raise ValueError('Invalid encrypted result')
    data=AESGCM(bundle_key(password)).decrypt(blob[4:16],blob[16:],f'icourse-parallel-v1:{run_id}:{slot}'.encode())
    decoder=zlib.decompressobj()
    result=decoder.decompress(data,100*1024*1024+1)
    if len(result)>100*1024*1024 or not decoder.eof:
        raise ValueError('Oversized or incomplete result')
    return result


def pack():
    course=selected_course(os.environ['COURSE_IDS'],int(os.environ['COURSE_SLOT']))
    if not Path('.db-source-ok').exists():
        raise ValueError('Source database not verified')
    with tempfile.TemporaryDirectory() as tmp:
        delta=Path(tmp)/'course.db'
        course_delta(Path('data/icourse.db'),delta,course)
        Path('data/course-result.enc').write_bytes(seal(delta.read_bytes(),os.environ['DB_ENCRYPTION_KEY'],
              os.environ['GITHUB_RUN_ID'],os.environ['COURSE_SLOT']))


def collect():
    from scripts.merge_db import merge
    courses=configured_courses(os.environ['COURSE_IDS'])
    received=0
    for slot,course in enumerate(courses):
        result=Path('data/results')/f'course-result-{slot}'/'course-result.enc'
        if not result.exists():
            continue
        payload=unseal(result.read_bytes(),os.environ['DB_ENCRYPTION_KEY'],os.environ['GITHUB_RUN_ID'],str(slot))
        with tempfile.TemporaryDirectory() as tmp:
            delta=Path(tmp)/'course.db';delta.write_bytes(payload)
            validate_database(str(delta))
            conn=sqlite3.connect(delta)
            try:
                if any(str(row[0])!=course for row in conn.execute('SELECT course_id FROM courses UNION SELECT course_id FROM lectures')):
                    raise ValueError('Cross-course artifact rejected')
            finally:
                conn.close()
            merge(str(delta),'data/icourse.db')
        received+=1
    if not received:
        raise ValueError('No verified course results received')
    validate_database('data/icourse.db')
    print(f'Merged {received}/{len(courses)} encrypted course result(s).')


def deliver(*, database_factory=None):
    from main import _send_email, _send_failure_notices, _in_run_scope
    from src.api.emailer import Emailer
    from src.runtime.reporter import Reporter
    from src.data.database import Database
    from src.runtime import config
    os.environ['PARALLEL_COURSE_SCOPE']='true'
    if not (config.SMTP_EMAIL and config.SMTP_PASSWORD and config.RECEIVER_EMAILS):
        raise ValueError('Mail is not configured')
    db=(database_factory or Database)('data/icourse.db')
    try:
        reporter=Reporter()
        emailer=Emailer()
        _send_email(emailer,db,reporter,[])
        _send_failure_notices(emailer,db,reporter)
        if any(_in_run_scope(row['course_id'],row) for row in db.get_unsent_lectures()):
            raise RuntimeError('Some course emails remain unsent')
    finally:
        db.conn.close()


def main():
    mode=sys.argv[1]
    if mode=='plan':
        courses=configured_courses(os.environ['COURSE_IDS'])
        with open(os.environ['GITHUB_OUTPUT'],'a') as out:
            out.write('matrix='+json.dumps({'slot':list(range(len(courses)))})+'\n')
    elif mode=='select':
        course=selected_course(os.environ['COURSE_IDS'],int(os.environ['COURSE_SLOT']))
        print('::add-mask::'+course)
        with open(os.environ['GITHUB_ENV'],'a') as out:
            out.write('SELECTED_COURSE_ID='+course+'\n')
    else:
        {'pack':pack,'collect':collect,'deliver':deliver}[mode]()


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print(f'Parallel course step failed ({type(error).__name__}); private details withheld')
        raise SystemExit(1)
