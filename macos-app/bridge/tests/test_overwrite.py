"""Overwrite rules of app_publish.build(), with a stub finalize module and SQLite fixtures.

Cloud note present: preserved unless the user chose to overwrite that exact note (by hash);
a changed cloud note or a deleted lecture is always refused; receipts are kept.
Run:  python3 local-app/bridge/tests/test_overwrite.py
"""
import hashlib, importlib.util, shutil, sqlite3, sys, tempfile
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1]/'templates/app_publish.py'
STUB = '''
import hashlib
from pathlib import Path
CID, SID, DAY = '1', '11', '2026-10-30'
OUT = Path(__file__).parent/'out'
sha = lambda raw: hashlib.sha256(raw).hexdigest()
def scope_keys(c, s): return ('qwen_pipeline:'+s, 'auto_glossary:'+c+':'+s, 'summary_figures:'+s, 'summary_review:'+s)
def validate_database(p): pass
def scope(a, b): pass
'''
SCHEMA = '''create table courses(course_id text primary key, title text);
create table lectures(sub_id text primary key, course_id text, sub_title text, date text, transcript text, summary text,
  processed_at text, emailed_at text, error_msg text, error_count integer, error_stage text, summary_model text, deleted_at text);
create table ppt_pages(sub_id text, page_num integer, created_sec integer, text text);
create table meta(key text primary key, value text);
insert into courses values ('1', 'T');'''


def load(root):
    (root/'finalize.py').write_text(STUB)
    shutil.copy(TEMPLATE, root/'app_publish.py')
    spec = importlib.util.spec_from_file_location('app_publish_under_test', root/'app_publish.py')
    module = importlib.util.module_from_spec(spec)
    sys.argv = ['x']
    spec.loader.exec_module(module)
    return module


def remote(root, summary=None, emailed=None, deleted=None):
    db = root/'before.db'; db.unlink(missing_ok=True)
    with sqlite3.connect(db) as c:
        c.executescript(SCHEMA)
        c.execute("insert into lectures values ('11','1','2026-10-30第6-8节','2026-10-30','cloud transcript',?,"
                  "'2026-10-31','"+(emailed or '')+"',null,0,null,'cloud-model',?)", (summary, deleted))
        if not emailed:
            c.execute("update lectures set emailed_at=null")
    return db


FRESH = {'lecture': {'transcript': 'local transcript', 'summary': 'LOCAL', 'summary_model': 'local', 'processed_at': 'now',
                     'error_count': 0, 'error_stage': None, 'error_msg': None, 'sub_id': '11', 'course_id': '1'},
         'ppt': []}


def expect_refused(fn, text):
    try:
        fn()
    except AssertionError as e:
        assert text in str(e), str(e)
        return
    raise AssertionError('expected refusal: '+text)


def main():
    root = Path(tempfile.mkdtemp())
    try:
        m = load(root)
        cloud = 'CLOUD NOTE'; cloud_sha = hashlib.sha256(cloud.encode()).hexdigest()
        meta = {'summary_figures:11': '{}'}

        remote(root, summary=cloud)
        expect_refused(lambda: m.build(root/'before.db', FRESH, meta), 'preserve')
        expect_refused(lambda: m.build(root/'before.db', FRESH, meta, '0'*64), 'changed since comparison')
        print('cloud note kept without an explicit, matching overwrite choice')

        remote(root, summary=cloud, emailed='2026-10-31T01:00:00Z')
        candidate, replaced = m.build(root/'before.db', FRESH, meta, cloud_sha)
        with sqlite3.connect(candidate) as c:
            summary, emailed, transcript = c.execute("select summary, emailed_at, transcript from lectures where sub_id='11'").fetchone()
        assert summary == 'LOCAL' and transcript == 'local transcript' and emailed == '2026-10-31T01:00:00Z'
        assert replaced['summary'] == cloud
        print('overwrite of the compared note replaces content and keeps the mail receipt')

        remote(root, summary=cloud, deleted='2026-10-31')
        expect_refused(lambda: m.build(root/'before.db', FRESH, meta, cloud_sha), 'removed')
        print('deleted lecture is never overwritten')

        remote(root, summary=None)
        candidate, replaced = m.build(root/'before.db', FRESH, meta)
        assert replaced is None
        print('no cloud note: normal publication')
        print('OK')
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
