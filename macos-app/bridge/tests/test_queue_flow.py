"""End-to-end queue / pause / resume test against a simulated backend.

Builds a throwaway project tree (real bridge, real app templates, the real
encrypted Store from the active source) whose `scripts.local_history_refresh`
is a fake that saves one block per 0.3 s and honours SIGINT like the real one.
Run:  work/qwen-local-asr-test-env/bin/python local-app/bridge/tests/test_queue_flow.py
"""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
REAL = APP.parent
SOURCE = Path(json.loads((REAL/'work/local-runtime/active-source.json').read_text())['path'])
PY = REAL/'work/qwen-local-asr-test-env/bin/python'

FAKE_REFRESH = r'''
import argparse, json, os, sys, time
from pathlib import Path
from scripts.local_history.storage import Store
p = argparse.ArgumentParser(); p.add_argument('--env-file'); p.add_argument('--run-dir'); p.add_argument('--campus-mode')
p.add_argument('--automatic-terms', action='store_true'); p.add_argument('--mlx-batch-size'); p.add_argument('--review-scope')
p.add_argument('command'); p.add_argument('rest', nargs='*'); a, _ = p.parse_known_args()
key = Path(json.loads(Path('.local-history-refresh/settings.json').read_text())['key_file']).read_text().strip()
s = Store(Path(a.run_dir), key); tag = os.environ['FAKE_TAG']
if a.command == 'review': sys.exit(0)
progress = s.read('progress.enc') if s.exists('progress.enc') else {'lectures': {}}
state = s.read(tag+'.enc') if s.exists(tag+'.enc') else {'stage': 'asr', 'rows': [], 'spec': {'plan': {'blocks': list(range(12))}, 'audio_seconds': 600}}
try:
    while len(state['rows']) < 12:
        time.sleep(0.3); state['rows'].append({'chunk_id': len(state['rows']), 'start': 0.0, 'end': 1.0, 'text': 'x'}); s.save(tag+'.enc', state)
    state['stage'] = 'complete'; s.save(tag+'.enc', state)
    progress['lectures'][tag] = {'status': 'complete'}; s.save('progress.enc', progress)
except KeyboardInterrupt:
    progress['lectures'][tag] = {'status': 'paused'}; progress['status'] = 'paused'; s.save('progress.enc', progress)
'''


def bridge(root, *args):
    out = subprocess.run([str(PY), str(root/'local-app/bridge/app_bridge.py'), *args], capture_output=True, text=True, cwd=root)
    return json.loads(out.stdout)


def wait(pred, timeout=40):
    end = time.time()+timeout
    while time.time() < end:
        v = pred()
        if v: return v
        time.sleep(0.3)
    raise AssertionError('timeout')


def main():
    root = Path(tempfile.mkdtemp(prefix='icourse-app-test-'))
    try:
        work = root/'work'; rt = work/'local-runtime'
        (work/'qwen-local-asr-test-env/bin').mkdir(parents=True)
        (work/'qwen-local-asr-test-env/bin/python').symlink_to(PY)
        shutil.copytree(APP/'bridge', root/'local-app/bridge')
        src = rt/'fake-source'; (src/'scripts/local_history').mkdir(parents=True); (src/'.git').mkdir()
        shutil.copy(SOURCE/'scripts/local_history/storage.py', src/'scripts/local_history/storage.py')
        (src/'scripts/__init__.py').write_text(''); (src/'scripts/local_history/__init__.py').write_text('')
        (src/'scripts/local_history_refresh.py').write_text(FAKE_REFRESH)
        (src/'src').symlink_to(SOURCE/'src', target_is_directory=True)
        (src/'.local-history-refresh').mkdir(); (root/'key').write_text('test-key-not-secret-'+'0'*32)
        (src/'.local-history-refresh/settings.json').write_text(json.dumps({'key_file': str(root/'key'), 'env_files': []}))
        (rt/'active-source.json').write_text(json.dumps({'path': str(src), 'sha': 'test'}))
        # Two prepared lectures in one queue, built the way cmd_start renders templates.
        sys.path.insert(0, str(root/'local-app/bridge')); os.environ['FAKE_TAG'] = 'x'
        q = work/'local-latest-new-test'; q.mkdir()
        targets = []
        for cid, sid in [('1', '11'), ('2', '22')]:
            r = work/f'local-latest-{cid}-{sid}-test'; r.mkdir(); (r/'repo').symlink_to(src)
            text = (APP/'bridge/templates/app_run_once.py').read_text()
            for k, v in {'@@RUN_NAME@@': r.name, '@@COURSE_ID@@': cid, '@@SUB_ID@@': sid, '@@DAY@@': '2026-10-30', '@@COURSE_TITLE@@': 'T'+cid}.items():
                text = text.replace(k, v)
            # the fake backend needs to know the tag; also skip macmon/caffeinate side effects
            text = text.replace("env=dict(os.environ,", "env=dict(os.environ,FAKE_TAG=TAG,")
            (r/'app_run_once.py').write_text(text); (r/'queue.env').write_text('')
            (r/'selection.json').write_text(json.dumps({'course_id': cid, 'sub_id': sid, 'date': '2026-10-30', 'course_title': 'T'+cid}))
            targets.append({'course_id': cid, 'sub_id': sid, 'course_title': 'T'+cid, 'date': '2026-10-30', 'control': str(r), 'output': str(root/'outputs'/r.name)})
        (q/'targets.json').write_text(json.dumps(targets)); shutil.copy(APP/'bridge/templates/app_queue.py', q/'app_queue.py')
        env = dict(os.environ, DYLD_FALLBACK_LIBRARY_PATH='/opt/homebrew/lib')
        log = open(q/'coordinator-private.log', 'ab')
        subprocess.Popen([str(PY), str(q/'app_queue.py')], cwd=root, env=env, stdout=log, stderr=log, start_new_session=True)

        def snap():
            return bridge(root, 'status', '--queue', str(q))['queues'][0]
        wait(lambda: (snap()['lectures'][0].get('saved_blocks') or 0) >= 3)
        s = snap(); assert s['status'] == 'running' and s['coordinator_alive'], s
        print('running, blocks', s['lectures'][0]['saved_blocks'], 'stage', s['lectures'][0]['stage'], 'safe', s['lectures'][0]['safe_to_pause'])
        r = bridge(root, 'pause', '--control', str(q)); assert r['ok'] and r['signalled_worker'], r
        s = wait(lambda: (lambda x: x if x['status'] == 'paused' and not x['coordinator_alive'] else None)(snap()))
        first = s['lectures'][0]; assert first['status'] == 'paused' and 3 <= first['saved_blocks'] < 12, first
        assert s['lectures'][1]['status'] == 'pending', s['lectures'][1]
        print('paused at', first['saved_blocks'], 'blocks; second lecture untouched')
        r = bridge(root, 'resume', '--control', str(q)); assert r['ok'], r
        s = wait(lambda: (lambda x: x if x['status'] == 'awaiting_content_review' and not x['coordinator_alive'] else None)(snap()), 60)
        assert all(l['status'] == 'awaiting_content_review' and l['saved_blocks'] == 12 for l in s['lectures']), s
        st = json.loads((Path(targets[0]['control'])/'execution-status.json').read_text())
        assert st['authorized_resume'] and st['resume_attempt'] == 1, st
        assert (Path(targets[0]['control'])/'app-resume-1-started.json').exists()
        # finished lectures are never rerun
        r = bridge(root, 'resume', '--control', str(q)); assert r['ok'], r
        time.sleep(3); s = snap(); assert s['status'] == 'awaiting_content_review', s
        assert not (Path(targets[0]['control'])/'app-resume-2-started.json').exists()
        assert not (Path(targets[1]['control'])/'app-resume-1-started.json').exists()
        print('resumed from checkpoint, both lectures complete (12/12), no rerun of finished lectures')
        r = bridge(root, 'pause', '--control', str(q)); assert not r['ok'], r
        print('pause refused when queue idle:', r['message'])
        print('OK')
    finally:
        if os.environ.get("KEEP"): print("kept", root)
        else: shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
