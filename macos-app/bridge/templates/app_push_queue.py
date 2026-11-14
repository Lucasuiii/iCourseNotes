"""Durable user-selected publication queue. Each item keeps its own scoped journal."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def save(path, state):
    tmp = path.with_suffix('.queue-tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(path)


def run(path):
    with (path.parent/'push-batch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
    state.update(pid=os.getpid(), status='running')
    save(path, state)
    for item in state['items']:
        item.update(status='running', started=time.time())
        save(path, state)
        args = [sys.executable, state['bridge'], 'push', '--control', item['control'],
                '--expected-summary', item['summary_sha256']]
        if state['email']:
            args.append('--email')
        if item.get('overwrite_remote_sha256'):
            args.extend(['--overwrite-remote', item['overwrite_remote_sha256']])
        try:
            result = subprocess.run(args, capture_output=True, timeout=300)
            response = json.loads(result.stdout)
            if not response.get('ok'):
                item.update(status='failed', message=response.get('message'), error_type=response.get('error_type'))
            else:
                status_path = Path(item['control'])/'app-publication-status.json'
                while True:
                    status = json.loads(status_path.read_text()) if status_path.exists() else {}
                    if status.get('status') in ('completed', 'failed'):
                        item.update(status=status['status'], error_type=status.get('error_type'),
                                    message=status.get('message'), notes_commit=status.get('notes_commit'))
                        break
                    alive = subprocess.run(['ps', '-p', str(response['pid']), '-o', 'stat='], capture_output=True, text=True)
                    if alive.returncode or alive.stdout.lstrip().startswith('Z'):
                        item.update(status='failed', error_type='PublisherExited', message='发布进程已退出，请查看该堂结果')
                        break
                    time.sleep(2)
        except Exception as error:
            item.update(status='failed', error_type=type(error).__name__)
        item['finished'] = time.time()
        save(path, state)
    state.update(status='completed' if all(i['status'] == 'completed' for i in state['items']) else 'finished_with_failures',
                 finished=time.time())
    save(path, state)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True)
    run(Path(parser.parse_args().state))
