"""Scoped application publication with fresh Git workspaces and bounded CAS retries."""
from contextlib import contextmanager
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


class DataChanged(Exception):
    pass


@contextmanager
def publication_checkout(f, label):
    checkout = Path(tempfile.mkdtemp(prefix=label+'-checkout-', dir=f.ROOT))
    try:
        yield checkout
    finally:
        journal = f.ROOT/(label+'-publication.json')
        record = json.loads(journal.read_text()) if journal.exists() else {}
        if record.get('status') == 'push_pending':
            record['retained_checkout'] = checkout.name
            f.record(journal.name, record)
        else:
            shutil.rmtree(checkout)


def git_failure(error):
    """Return only a fixed diagnostic, never subprocess arguments or credentials."""
    text = (error.stderr or b'')
    if isinstance(text, bytes):
        text = text.decode(errors='replace')
    if 'already exists' in text and 'branch' in text:
        return 'GitWorkspaceConflict', '旧发布目录中已有 data 分支'
    if any(word in text.lower() for word in ('authentication failed', 'could not read username', 'permission denied')):
        return 'GitAuthenticationFailed', 'GitHub 身份验证失败，请检查登录状态'
    if any(word in text.lower() for word in ('resolve host', 'timed out', 'failed to connect', 'connection reset')):
        return 'GitConnectionFailed', '连接 GitHub 失败，请稍后重试'
    return 'GitCommandFailed', 'Git 操作失败，检查点已保留'


def publish_snapshot(f, database, before, base, label, *, receipt=False):
    journal = f.ROOT/(label+'-publication.json')
    assert not journal.exists(), 'Existing publication journal; no replay'
    f.scope(before, database, receipt=receipt)
    f.validate_database(str(database))
    f.durable_backup(database, label+'-candidate')
    with publication_checkout(f, label) as checkout:
        f.command(['git', 'init', '-q', str(checkout)])
        f.command(['git', 'fetch', '-q', '--depth=1', f.URL, base], cwd=checkout, env=f.auth_env())
        f.command(['git', 'checkout', '-q', '-b', 'data', 'FETCH_HEAD'], cwd=checkout)
        shutil.rmtree(checkout/'data')
        f.shard_database(str(database), str(checkout/'data'), f.key)
        restored = f.ROOT/(label+'-roundtrip.db')
        f.reassemble_database(f.load_index(checkout/'data/icourse-index.enc', f.key), checkout/'data/shards', restored, f.key)
        f.verify_equivalent(str(database), str(restored))
        f.scope(before, restored, receipt=receipt)
        f.command(['git', 'add', '-A', 'data'], cwd=checkout)
        paths = f.command(['git', 'diff', '--cached', '--name-only'], cwd=checkout).decode().splitlines()
        assert paths and all(p.startswith('data/') and p.endswith('.enc') for p in paths)
        message = 'chore: record authorized local lecture mail receipt' if receipt else 'chore: publish locally recognized new lecture'
        f.command(['git', '-c', 'user.name=local-history-refresh', '-c', 'user.email=local-history-refresh@users.noreply.github.com',
                   'commit', '-q', '-m', message], cwd=checkout)
        commit = f.command(['git', 'rev-parse', 'HEAD'], cwd=checkout).decode().strip()

        def remote():
            return f.command(['git', 'ls-remote', '--heads', f.URL, 'refs/heads/data'], env=f.auth_env()).decode().split()[0]

        if remote() != base:
            raise DataChanged('Concurrent data update before push')
        record = {'status': 'push_pending', 'base': base, 'commit': commit, 'course_id': f.CID, 'sub_id': f.SID,
                  'scope_verified': True, 'encrypted_roundtrip_verified': True}
        f.record(journal.name, record)
        try:
            f.command(['git', 'push', f.URL, 'HEAD:refs/heads/data'], cwd=checkout, env=f.auth_env())
        except subprocess.CalledProcessError as error:
            head = remote()
            if head != commit:
                stderr = error.stderr or b''
                if isinstance(stderr, bytes):
                    stderr = stderr.decode(errors='replace')
                if '[rejected]' in stderr and any(s in stderr for s in ('(fetch first)', '(non-fast-forward)')):
                    record.update(status='rejected', reason='concurrent_data_update')
                    f.record(journal.name, record)
                    journal.rename(journal.with_name(label+'-rejected-'+commit+'.json'))
                    raise DataChanged('Confirmed non-fast-forward rejection') from None
                raise RuntimeError('Push outcome unconfirmed; retain journal for reconciliation') from None
        assert remote() == commit, 'Remote changed after push; retain journal for reconciliation'
        readback = f.ROOT/(label+'-readback.db')
        assert f.load_remote(readback) == commit
        f.verify_equivalent(str(database), str(readback))
        f.scope(before, readback, receipt=receipt)
        f.durable_backup(readback, label+'-verified-readback')
        record.update(status='published', remote_readback_verified=True)
        f.record(journal.name, record)
        return commit


def publish_notes(f, fresh, meta, overwrite, build, status):
    for attempt in range(1, 4):
        status(status='publishing', publication_phase='merge_latest', merge_attempt=attempt)
        before = f.ROOT/'publish-before.db'
        base = f.load_remote(before)
        assert base
        f.durable_backup(before, 'remote-before-'+base)
        candidate, replaced = build(before, fresh, meta, overwrite)
        if replaced:
            f.atomic(f.OUT/'replaced-cloud-summary.md', ('# 被本机版本替换的云端笔记\n\n'+replaced['summary']+'\n').encode())
            status(overwrote_remote=True, replaced_summary_sha256=f.sha(replaced['summary'].encode()),
                   remote_backup='recovery/remote-before-'+base+'.db.enc')
        try:
            return publish_snapshot(f, candidate, before, base, 'notes'), replaced
        except DataChanged:
            if attempt == 3:
                raise RuntimeError('ConcurrentDataUpdatesExhausted') from None
