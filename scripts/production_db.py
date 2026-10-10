"""Fail-closed encrypted storage and optimistic, forward-only pilot publication."""
from __future__ import annotations
import base64
from contextlib import closing
import gzip
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from scripts.validate_db import validate_database
from src.data.database import Database


def command(args, *, cwd=None, env=None, input=None):
    return subprocess.run(args, cwd=cwd, env=env, input=input, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180).stdout


def auth_env():
    env = dict(os.environ)
    token = os.environ.get('GH_TOKEN', '')
    if token:
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+base64.b64encode(('x-access-token:'+token).encode()).decode())
    return env


def snapshot(db, path):
    """Back up through SQLite, including committed WAL pages."""
    path = Path(path)
    if path.exists():
        path.unlink()
    with db._lock, closing(sqlite3.connect(path)) as target:
        db.conn.backup(target)
        target.commit()
    validate_database(str(path))
    return path.read_bytes()


def lecture_snapshot(db, path, course_id, sub_id):
    snapshot(db, path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('DELETE FROM ppt_pages WHERE sub_id != ?', (str(sub_id),))
        conn.execute('DELETE FROM lectures WHERE sub_id != ?', (str(sub_id),))
        conn.execute('DELETE FROM courses WHERE course_id != ?', (str(course_id),))
        conn.execute('DELETE FROM all_courses')
        from src.pipeline.history_refresh import scope_keys
        allowed = scope_keys(str(course_id), str(sub_id))
        conn.execute('DELETE FROM meta WHERE key NOT IN (?, ?, ?, ?)', allowed)
    validate_database(str(path))
    return Path(path).read_bytes()


def load_remote(destination):
    """Confirm absence or decode the exact existing data commit; never reset on errors."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = 'https://github.com/'+os.environ['GITHUB_REPOSITORY']+'.git'
    env = auth_env()
    refs = command(['git', 'ls-remote', '--heads', url, 'refs/heads/data'], env=env).decode().strip()
    if not refs:
        db = Database(str(destination)); db.conn.close()
        return None
    revision = refs.split()[0]
    with tempfile.TemporaryDirectory() as tmp:
        command(['git', 'init', '-q', tmp])
        command(['git', 'fetch', '-q', '--depth=1', url, revision], cwd=tmp, env=env)
        paths = command(['git', 'ls-tree', '-r', '--name-only', 'FETCH_HEAD', 'data/'], cwd=tmp).decode().splitlines()
        if 'data/icourse-index.enc' in paths:
            from src.data.sharder import load_index, reassemble_database
            storage = Path(tmp)/'storage'; (storage/'shards').mkdir(parents=True)
            index = storage/'icourse-index.enc'
            index.write_bytes(command(['git', 'show', 'FETCH_HEAD:data/icourse-index.enc'], cwd=tmp))
            for item in paths:
                if item.startswith('data/shards/'):
                    (storage/'shards'/Path(item).name).write_bytes(command(['git', 'show', 'FETCH_HEAD:'+item], cwd=tmp))
            password = os.environ['DB_ENCRYPTION_KEY']
            reassemble_database(load_index(index, password), storage/'shards', destination, password)
        else:
            name = next((n for n in ('data/icourse.db.gz.enc', 'data/icourse.db.enc') if n in paths), None)
            if not name:
                raise ValueError('Existing data branch has no recognized database')
            blob = Path(tmp)/'legacy.enc'; blob.write_bytes(command(['git', 'show', 'FETCH_HEAD:'+name], cwd=tmp))
            passwords = [(os.environ['DB_ENCRYPTION_KEY'], True)]
            legacy = ((os.environ.get('STUID') or os.environ.get('StuId', ''))
                      +(os.environ.get('UISPSW') or os.environ.get('UISPsw', ''))
                      +os.environ.get('DASHSCOPE_API_KEY', '')+os.environ.get('SMTP_PASSWORD', ''))
            if legacy:
                passwords.append((legacy, False))
            for password, modern in passwords:
                try:
                    args = ['openssl', 'enc', '-aes-256-cbc', '-d', '-pbkdf2']
                    if modern: args += ['-iter', '100000']
                    raw = command(args+['-in', str(blob), '-pass', 'stdin'], input=(password+'\n').encode())
                    destination.write_bytes(gzip.decompress(raw) if '.gz.' in name else raw)
                    validate_database(str(destination))
                    break
                except (subprocess.CalledProcessError, ValueError, sqlite3.DatabaseError, OSError):
                    destination.unlink(missing_ok=True)
            else:
                raise ValueError('Existing database could not be decoded')
    validate_database(str(destination))
    return revision


def merge_lecture(delta, remote, course_id, sub_id):
    """Preserve newer histories, tombstones and mail receipts; advance this lesson only."""
    from scripts.merge_db import merge
    validate_database(str(delta)); validate_database(str(remote))
    with sqlite3.connect(delta) as local, sqlite3.connect(remote) as target:
        local.row_factory = target.row_factory = sqlite3.Row
        rows = local.execute('SELECT * FROM lectures').fetchall()
        if len(rows) != 1 or str(rows[0]['sub_id']) != str(sub_id) or str(rows[0]['course_id']) != str(course_id):
            raise ValueError('Publication delta crosses the selected lecture scope')
        if any(str(r[0]) != str(course_id) for r in local.execute('SELECT course_id FROM courses')):
            raise ValueError('Publication delta crosses course scope')
        if any(str(r[0]) != str(sub_id) for r in local.execute('SELECT sub_id FROM ppt_pages')):
            raise ValueError('Publication delta crosses PPT scope')
        from src.pipeline.history_refresh import scope_keys
        if any(r[0] not in scope_keys(str(course_id), str(sub_id))
               for r in local.execute('SELECT key FROM meta')):
            raise ValueError('Publication delta crosses checkpoint scope')
        previous = target.execute('SELECT * FROM lectures WHERE sub_id=?', (str(sub_id),)).fetchone()
        if previous and str(previous['course_id']) != str(course_id):
            raise ValueError('Remote lecture belongs to another course')
        protected = bool(previous and (previous['summary'] or previous['processed_at'] or previous['deleted_at']
                         or (previous['retry_generation'] or 0) > (rows[0]['retry_generation'] or 0)))
        if protected:
            # Copy protected row so the existing additive merger cannot replace
            # a newer published summary with an earlier worker snapshot.
            previous = dict(previous)
            if previous['summary'] == rows[0]['summary'] and not previous['deleted_at']:
                previous['emailed_at'] = previous['emailed_at'] or rows[0]['emailed_at']
                previous['failure_notified_at'] = previous['failure_notified_at'] or rows[0]['failure_notified_at']
            names = list(previous); assignments = ','.join('"'+n+'"=?' for n in names)
            local.execute('UPDATE lectures SET '+assignments+' WHERE sub_id=?', [previous[n] for n in names]+[str(sub_id)])
            local.execute('DELETE FROM meta'); local.execute('DELETE FROM ppt_pages')
    merge(str(delta), str(remote))
    if not protected:
        with sqlite3.connect(remote) as conn:
            conn.execute('ATTACH DATABASE ? AS delta', (str(delta),))
            conn.execute('''UPDATE ppt_pages SET text=d.text, ocr_status=d.ocr_status,
                         ocr_at=d.ocr_at, dhash=d.dhash FROM delta.ppt_pages d
                         WHERE ppt_pages.sub_id=d.sub_id AND ppt_pages.page_num=d.page_num
                         AND ppt_pages.ocr_status IN ('pending','failed') AND d.ocr_status!='pending'
                         AND NOT EXISTS (SELECT 1 FROM lectures l WHERE l.sub_id=d.sub_id AND l.deleted_at IS NOT NULL)''')
            for key, value in conn.execute('SELECT key,value FROM delta.meta').fetchall():
                if key in ('qwen_pipeline:'+str(sub_id), 'summary_figures:'+str(sub_id), 'summary_review:'+str(sub_id)):
                    conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key, value))
    validate_database(str(remote))


def publish(delta, course_id=None, sub_id=None):
    """Normal fast-forward pushes only; on conflict refresh and reapply the scoped delta."""
    from src.data.sharder import shard_database
    from scripts.parallel_courses import bundle_key
    bundle_key(os.environ['DB_ENCRYPTION_KEY'])
    url = 'https://github.com/'+os.environ['GITHUB_REPOSITORY']+'.git'
    for attempt in range(8):
        with tempfile.TemporaryDirectory(prefix='icourse-publish-') as tmp:
            tmp = Path(tmp); merged = tmp/'merged.db'
            revision = load_remote(merged)
            # merge_lecture may protect/migrate its source: retry against a fresh copy.
            local = tmp/'delta.db'; shutil.copyfile(delta, local)
            if course_id is None:
                from scripts.merge_db import merge
                with sqlite3.connect(local) as conn:
                    if any(conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]
                           for t in ('courses', 'lectures', 'ppt_pages')):
                        raise ValueError('Catalog delta contains classroom state')
                merge(str(local), str(merged))
            else:
                merge_lecture(local, merged, course_id, sub_id)
            checkout = tmp/'checkout'
            command(['git', 'init', '-q', str(checkout)])
            if revision:
                command(['git', 'fetch', '-q', '--depth=1', url, revision], cwd=checkout, env=auth_env())
                command(['git', 'checkout', '-q', '-b', 'data', 'FETCH_HEAD'], cwd=checkout)
            else:
                command(['git', 'checkout', '-q', '-b', 'data'], cwd=checkout)
            shutil.rmtree(checkout/'data', ignore_errors=True)
            shard_database(str(merged), str(checkout/'data'), os.environ['DB_ENCRYPTION_KEY'])
            command(['git', 'add', '-A', 'data'], cwd=checkout)
            if not command(['git', 'diff', '--cached', '--name-only'], cwd=checkout).strip():
                return
            command(['git', '-c', 'user.name=github-actions[bot]', '-c',
                     'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                     'commit', '-q', '-m', 'chore: publish encrypted Qwen lecture result'], cwd=checkout)
            try:
                command(['git', 'push', url, 'HEAD:refs/heads/data'], cwd=checkout, env=auth_env())
                return
            except subprocess.CalledProcessError:
                # Retry only a confirmed optimistic concurrency conflict.
                current = command(['git', 'ls-remote', '--heads', url, 'refs/heads/data'], env=auth_env()).decode().strip()
                current = current.split()[0] if current else None
                if current == revision:
                    raise
        time.sleep(min(2**attempt, 10))
    raise RuntimeError('Publication conflict retry budget exhausted; encrypted delta retained')
