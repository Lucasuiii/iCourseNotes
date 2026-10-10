"""Reviewed replacements: encrypted backup, fresh merge, one forward-only push."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from scripts.local_history.runtime import candidate_files
from scripts.local_history.storage import private_dir
from src.pipeline import history_refresh as policy


def prepare_database(store, manifest, review, database):
    """Mutates only a private scratch DB. Any failed target prevents publication."""
    if review['manifest_hash'] != policy.digest(manifest):
        raise ValueError('Reviewed manifest changed')
    paths = []
    for approval in review['approvals']:
        target = approval['targets'][0]
        original = {k:v for k,v in target.items() if k != 'candidate_hash'}
        if original not in manifest['targets']:
            raise ValueError('Reviewed target left the frozen selection')
        path, fresh = candidate_files(store, original)
        tag = target['course_id']+'-'+target['sub_id']
        if policy.digest(store.read(tag+'.enc')) != review['artifacts'][tag]:
            raise ValueError('Recognition/review checkpoint changed after preview')
        if policy.digest(fresh) != target['candidate_hash']:
            raise ValueError('Candidate changed after review')
        paths.append(path)
    changed = False
    for approval, path in zip(review['approvals'], paths):
        changed = policy.replace_batch(database, approval, {0: path}) or changed
    return changed


def apply(store, manifest, review, approval_hash):
    if any(t.get("preview_only") for t in manifest["targets"]):
        raise ValueError("New-lecture previews cannot publish through historical apply")
    from scripts.production_db import command, auth_env, load_remote
    from src.data.sharder import shard_database, load_index, reassemble_database
    if policy.digest(review) != approval_hash:
        raise ValueError('Approval fingerprint does not match the reviewed result')
    if manifest['repository'] != os.environ.get('GITHUB_REPOSITORY'):
        raise ValueError('Frozen plan belongs to a different repository')
    url = 'https://github.com/'+manifest['repository']+'.git'
    env = auth_env()
    def remote_head():
        refs = command(['git', 'ls-remote', '--heads', url, 'refs/heads/data'], env=env).decode().split()
        return refs[0] if refs else None
    with tempfile.TemporaryDirectory(prefix='apply-', dir=store.root) as tmp:
        root = private_dir(tmp)
        database = root/'merged.db'
        revision = load_remote(database)
        if revision is None:
            raise ValueError('Historical data branch disappeared')
        # Durable recovery backup exists before any replacement or push.
        store.save_bytes('backup-'+revision+'.db.enc', database.read_bytes())
        store.save('backup-'+revision+'.json.enc', {'revision': revision, 'approval_hash': approval_hash})
        if not prepare_database(store, manifest, review, database):
            return revision
        checkout = root/'checkout'
        command(['git', 'init', '-q', str(checkout)])
        command(['git', 'fetch', '-q', '--depth=1', url, revision], cwd=checkout, env=env)
        command(['git', 'checkout', '-q', '-b', 'data', 'FETCH_HEAD'], cwd=checkout)
        shutil.rmtree(checkout/'data', ignore_errors=True)
        # Preserve the latest formal subscription metadata while re-encoding;
        # this entry is authorized to replace notes, not change subscriptions.
        subscribed = os.environ.pop('SUBSCRIBED_COURSE_IDS', None)
        try:
            shard_database(str(database), str(checkout/'data'), os.environ['DB_ENCRYPTION_KEY'])
        finally:
            if subscribed is not None:
                os.environ['SUBSCRIBED_COURSE_IDS'] = subscribed
        restored = root/'verified.db'
        index = load_index(str(checkout/'data/icourse-index.enc'), os.environ['DB_ENCRYPTION_KEY'])
        reassemble_database(index, str(checkout/'data/shards'), str(restored), os.environ['DB_ENCRYPTION_KEY'])
        # Verify the entire plaintext database survived encryption/sharding.
        with closing(sqlite3.connect(database)) as left, closing(sqlite3.connect(restored)) as right:
            for table in ('courses', 'lectures', 'ppt_pages', 'all_courses', 'meta'):
                a = sorted(left.execute('SELECT * FROM '+table).fetchall(), key=repr)
                b = sorted(right.execute('SELECT * FROM '+table).fetchall(), key=repr)
                if a != b:
                    raise ValueError('Encrypted database readback differs from replacement')
        command(['git', 'add', '-A', 'data'], cwd=checkout)
        command(['git', '-c', 'user.name=local-history-refresh', '-c',
                 'user.email=local-history-refresh@users.noreply.github.com', 'commit', '-q', '-m',
                 'chore: apply reviewed local historical notes'], cwd=checkout)
        new_head = command(['git', 'rev-parse', 'HEAD'], cwd=checkout).decode().strip()
        if remote_head() != revision:
            raise ValueError('Data changed during apply; review against latest data before retrying')
        store.save('publication.enc', {'base': revision, 'commit': new_head,
                   'approval_hash': approval_hash, 'status': 'push_pending'})
        try:
            command(['git', 'push', url, 'HEAD:refs/heads/data'], cwd=checkout, env=env)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if remote_head() != new_head:
                raise RuntimeError('Push outcome unconfirmed; candidates/backup retained; inspect before retrying') from None
        if remote_head() != new_head:
            raise RuntimeError('Published head changed before verification; inspect data history')
        store.save('publication.enc', {'base': revision, 'commit': new_head,
                   'approval_hash': approval_hash, 'status': 'published'})
        return new_head
