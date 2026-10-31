"""Bridge between the macOS app and the existing local recognition backend.

Every command prints exactly one JSON object on stdout.  Secrets, signed media
URLs, campus responses and private logs are never printed: failures expose only
an exception type name.  The bridge reuses the protected runtime (active source,
shared Python environment, wrapper templates, encrypted Store) and never edits
existing control files except the app-owned markers it creates.
"""
import argparse, contextlib, hashlib, json, os, re, shutil, signal, sqlite3, subprocess, sys, time
from datetime import datetime
from pathlib import Path

os.umask(0o077)
HERE = Path(__file__).resolve().parent
# The app passes the project root (the folder holding work/ and outputs/); the
# fallback supports running the bridge from a checkout next to that folder.
BASE = Path(os.environ.get('ICOURSE_PROJECT_ROOT') or HERE.parents[1]).resolve()
WORK = BASE/'work'
OUTPUTS = BASE/'outputs'
RUNTIME = WORK/'local-runtime'
PY = WORK/'qwen-local-asr-test-env/bin/python'
STATE = WORK/'local-app'
TEMPLATES = HERE/'templates'
LAUNCH = WORK/'local-latest-launch.json'
WORKER_PATTERN = 'python.*-m scripts.local_history_refresh.* run'
ENV = dict(os.environ, DYLD_FALLBACK_LIBRARY_PATH='/opt/homebrew/lib', PYTHONUNBUFFERED='1')
# Stages that only touch local audio/GPU; interrupting a cloud request instead
# leaves an uncertain call that the backend refuses to replay automatically.
LOCAL_STAGES = {'starting', 'authentication', 'source_selection', 'acquisition', 'vad', 'asr'}
PENDING = ('running', 'starting', 'paused', 'failed', 'awaiting_content_review', 'awaiting_review',
           'awaiting_publication', 'publishing', 'published', 'delivered', 'completed', 'blocked_resource')
ID = re.compile(r'^\d{1,12}$')
FIGURE_REF = re.compile(r'!\[([^\]\n]*)\]\(#icourse-figure-([0-9a-f]{64})\)')
EXPORTED_REF = re.compile(r'!\[([^\]\n]*)\]\(([^)\n]*/figures/([0-9a-f]{64})\.jpg)\)')


class Refused(Exception):
    """A deliberate, user-facing refusal; its message is safe to show."""


def out(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False, allow_nan=False))
    sys.stdout.flush()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path); tmp = path.with_name(path.name+'.app-tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2)); tmp.replace(path)


def active_source():
    info = read_json(RUNTIME/'active-source.json') or {}
    source = Path(info.get('path', RUNTIME/'repo')).resolve()
    source.relative_to(RUNTIME.resolve())
    if not (source/'.git').exists():
        raise Refused('当前源码目录缺少 Git 信息')
    return source


def key():
    cfg = json.loads((active_source()/'.local-history-refresh/settings.json').read_text())
    return Path(cfg['key_file']).read_text().strip()


def store(root):
    source = str(active_source())
    if source not in sys.path:
        sys.path.insert(0, source)
    from scripts.local_history.storage import Store
    return Store(Path(root), key())


def pid_alive(pid, needle=None):
    if not isinstance(pid, int) or pid <= 0:
        return False
    p = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=,command='], capture_output=True, text=True)
    if p.returncode or p.stdout.lstrip().startswith('Z'):
        return False
    return needle is None or needle in p.stdout


def worker_active():
    return subprocess.run(['pgrep', '-f', WORKER_PATTERN], stdout=subprocess.DEVNULL).returncode == 0


def safe_code(value):
    return str(value) if value is not None and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}', str(value)) else None


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


# ------------------------------------------------------------------ terms

def term_for(course_id, date, known):
    """Official term code from data (e.g. 2026-20271), else inferred from the lecture date."""
    if known.get(course_id):
        return known[course_id]
    try:
        y, m = int(date[:4]), int(date[5:7])
    except (TypeError, ValueError):
        return 'unknown'
    if m >= 8: return f'{y}-{y+1}1'
    if m == 1: return f'{y-1}-{y}1'
    return f'{y-1}-{y}2'


def term_label(code):
    m = re.fullmatch(r'(\d{4})-(\d{4})(1|2|暑期)', code or '')
    if not m:
        return '其他'
    part = {'1': '秋季学期', '2': '春季学期', '暑期': '暑期'}[m[3]]
    return f'{m[1]}–{m[2]} {part}'


def known_terms():
    s = notes_store()
    return (s.read('index.enc').get('course_terms') or {}) if s.exists('index.enc') else {}


# ---------------------------------------------------------------- queue status

def queue_dirs():
    rows = [p for p in WORK.glob('local-latest-new-*') if p.is_dir() and (p/'targets.json').exists()]
    return sorted(rows, key=lambda p: p.stat().st_mtime, reverse=True)


def lecture_status(item, crypto, queue_entry=None):
    control = Path(item['control']); sid, cid = item['sub_id'], item['course_id']
    status = read_json(control/'execution-status.json') or dict(queue_entry or {}) or {'status': 'pending'}
    row = {'course_id': cid, 'sub_id': sid, 'course_title': item.get('course_title'), 'date': item.get('date'),
           'title': item.get('title'), 'control': str(control), 'output': item.get('output'),
           'status': safe_code(status.get('status')) or 'pending', 'phase': safe_code(status.get('phase')),
           'error_type': safe_code(status.get('error_type')), 'data_commit': safe_code(status.get('data_commit')),
           'email_status': safe_code(status.get('email_status')), 'cleanup_complete': status.get('cleanup_complete') is True,
           'saved_blocks': number(status.get('saved_blocks')), 'planned_blocks': number(status.get('planned_blocks'))}
    tag = cid+'-'+sid+'.enc'; lecture_dir = control/('lecture-'+sid)
    if crypto and (lecture_dir/tag).is_file():
        try:
            saved = crypto(lecture_dir).read(tag); spec = saved.get('spec', {})
            coverage = saved.get('recognition_coverage') or {}
            row.update(stage=safe_code(saved.get('stage')), saved_blocks=len(saved.get('rows', [])),
                       planned_blocks=len(spec.get('plan', {}).get('blocks', [])) or row['planned_blocks'],
                       audio_seconds=number(spec.get('audio_seconds')),
                       missing_seconds=number(coverage.get('missing_seconds')),
                       checkpoint_time=(lecture_dir/tag).stat().st_mtime)
            if row['status'] == 'running':
                row['runtime_error_type'] = None
            else:
                row['runtime_error_type'] = safe_code(saved.get('last_error_type'))
        except Exception as e:
            row['checkpoint_error'] = type(e).__name__
    if row['status'] in ('running', 'starting'):
        row['worker_alive'] = pid_alive(status.get('worker_pid')) or pid_alive(status.get('pid'))
    if (control/'review-ready.json').exists() and not (control/'editorial-approval.json').exists() \
            and row['status'] not in ('completed', 'delivered'):
        row['status'] = 'awaiting_content_review'
    publication = read_json(control/'app-publication-status.json')
    if publication:
        row['publication'] = {k: publication.get(k) for k in ('status', 'error_type', 'notes_commit', 'data_commit',
                                                               'email_status', 'cleanup_complete', 'dry_run')}
    perf = read_json(control/'performance-latest.json')
    if perf:
        row['performance'] = {k: number(perf.get(k)) for k in ('time', 'gpu_active_percent', 'system_watts',
                                                               'gpu_watts', 'ram_gib', 'swap_gib')}
    row['safe_to_pause'] = (row.get('stage') or row.get('phase') or 'starting') in LOCAL_STAGES
    return row


def queue_snapshot(root, crypto):
    q = read_json(root/'queue-status.json') or {'status': 'not_started'}
    targets = read_json(root/'targets.json') or []
    pid = q.get('pid')
    lectures = [lecture_status(t, crypto, (q.get('lectures') or {}).get(t['sub_id']) if isinstance(q.get('lectures'), dict) else None) for t in targets]
    result = {'control': str(root), 'name': root.name, 'status': safe_code(q.get('status')) or 'not_started',
              'started': number(q.get('started')), 'finished': number(q.get('finished')),
              'current_sub_id': safe_code(q.get('current_sub_id')),
              'coordinator_alive': pid_alive(pid, 'app_queue.py') or pid_alive(pid, 'run_queue.py') or pid_alive(pid, 'resume_queue.py'),
              'pause_requested': (root/'app-pause-request.json').exists(), 'app_managed': (root/'app_queue.py').exists(),
              'error_type': safe_code(q.get('error_type')), 'lectures': lectures}
    if result['status'] == 'running' and not result['coordinator_alive']:
        result['status'] = 'interrupted'
    return result


def history(root, sub_id, seconds=3600):
    control = next((Path(t['control']) for t in read_json(root/'targets.json', []) if t['sub_id'] == sub_id), None)
    if not control or not (control/'performance.jsonl').exists():
        return []
    path = control/'performance.jsonl'; rows = []
    with path.open('rb') as f:
        size = path.stat().st_size; f.seek(max(0, size-768*1024))
        if size > 768*1024:
            f.readline()
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if time.time()-r.get('time', 0) <= seconds:
                rows.append({k: number(r.get(k)) for k in ('time', 'gpu_active_percent', 'system_watts', 'gpu_watts',
                                                            'ram_gib', 'swap_gib', 'saved_blocks', 'planned_blocks')})
    return rows


def cmd_status(args):
    crypto = None
    try:
        store(WORK)  # import check only
        crypto = store
    except Exception as e:
        crypto_error = type(e).__name__
    else:
        crypto_error = None
    queues = queue_dirs()
    launch = read_json(LAUNCH) or {}
    chosen = Path(args.queue).resolve() if args.queue else None
    if chosen is None and launch.get('control') and Path(launch['control']).is_dir():
        chosen = Path(launch['control'])
    snapshots = []
    for root in queues[:args.limit]:
        snapshots.append(queue_snapshot(root, crypto if root == chosen or args.decrypt_all else None))
    if chosen and all(Path(s['control']) != chosen for s in snapshots) and (chosen/'targets.json').exists():
        snapshots.insert(0, queue_snapshot(chosen, crypto))
    info = read_json(RUNTIME/'active-source.json') or {}
    out({'ok': True, 'time': time.time(), 'latest_control': str(chosen) if chosen else None,
         'queues': snapshots, 'worker_active': worker_active(), 'crypto_error': crypto_error,
         'source': {'path': info.get('path'), 'sha': safe_code(info.get('sha'))},
         'free_disk_gib': shutil.disk_usage(WORK).free/2**30,
         'history': history(chosen, args.history_sub, 3600) if chosen and args.history_sub else []})


# ----------------------------------------------------------- scan & start

def local_controls(cid, sid):
    rows = []
    for p in sorted(WORK.glob('local-latest-'+cid+'-'+sid+'-*')):
        st = read_json(p/'execution-status.json') or {}
        rows.append({'control': str(p), 'status': safe_code(st.get('status')) or ('prepared' if (p/'selection.json').exists() else 'unknown')})
    return rows


def summarize_inventory(control):
    inv = read_json(control/'inventory.json')
    if not inv:
        status = read_json(control/'inventory-status.json') or {}
        return {'ok': False, 'control': str(control), 'error_type': safe_code(status.get('error_type')) or 'InventoryMissing'}
    started = (control/'execution-started.json').exists() or (control/'targets.json').exists()

    def lecture(r, **extra):
        row = {k: r.get(k) for k in ('course_id', 'course_title', 'sub_id', 'title', 'date', 'available', 'has_summary',
                                     'deleted', 'session_selected', 'local_not_needed', 'local_pending')}
        row['local_controls'] = local_controls(r['course_id'], r['sub_id'])
        row.update(extra)
        return row
    ready = []
    for r in inv.get('unrecognized_available', []):
        existing = [x for x in local_controls(r['course_id'], r['sub_id']) if x['status'] not in ('unknown',)]
        ready.append(lecture(r, selectable=not existing and not started, local_existing=bool(existing)))
    deferred = [lecture(r, resource_error=safe_code(r.get('resource_error'))) for r in inv.get('deferred_unavailable', [])]
    courses = []
    for c in inv.get('courses', []):
        courses.append({'course_id': c['course_id'], 'course_title': c['course_title'],
                        'lectures': [{k: r.get(k) for k in ('sub_id', 'title', 'date', 'available', 'has_summary', 'deleted',
                                                             'session_selected', 'local_not_needed', 'local_pending')}
                                     for r in c['lectures'] if r.get('date', '') <= inv.get('today', '9999')][:12]})
    return {'ok': True, 'control': str(control), 'today': inv.get('today'), 'mode': safe_code(inv.get('mode')),
            'baseline_revision': safe_code(inv.get('baseline_revision')), 'started': started,
            'scanned_at': (control/'inventory.json').stat().st_mtime, 'ready': ready, 'deferred': deferred, 'courses': courses}


def cleanup_inventory(control):
    """Encrypted baseline was saved and read back by inventory.py; verify and drop plaintext."""
    plain = control/'baseline-inventory.db'
    if not plain.exists():
        return
    s = store(control/'recovery')
    if s.read_bytes('inventory-baseline.db.enc') != plain.read_bytes():
        raise Refused('库存备份校验不一致，已保留明文供检查')
    with contextlib.closing(sqlite3.connect(plain.as_uri()+'?mode=ro', uri=True)) as c:
        if c.execute('pragma integrity_check').fetchone()[0] != 'ok':
            raise Refused('库存数据库完整性检查失败')
    plain.unlink()
    for p in control.glob('*private.log'):
        p.unlink()
    shutil.rmtree(control/'inventory-scratch', ignore_errors=True)
    write_json(control/'inventory-cleanup.json', {'status': 'complete', 'inventory_baseline_encrypted': True,
                                                   'sqlite_integrity_verified': True,
                                                   'plaintext_inventory_and_private_log_removed': True,
                                                   'by': 'local-app'})


def source_identity():
    source = active_source()
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source, text=True).strip()
    if dirty:
        raise Refused('当前源码有未提交改动，不能冻结新的识别计划')
    return source, sha


def cmd_scan(args):
    if worker_active():
        pass  # scanning is read-only; the new inventory still marks running lectures as pending
    source, sha = source_identity()
    stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
    control = WORK/('local-latest-new-'+stamp)
    control.mkdir(mode=0o700)
    (control/'repo').symlink_to(source, target_is_directory=True)
    write_json(control/'source.json', {'path': str(source), 'sha': sha, 'reason': 'local app: user checks subscribed new playback'})
    shutil.copyfile(TEMPLATES/'inventory.py', control/'inventory.py')
    try:
        p = subprocess.run([str(PY), str(control/'inventory.py')], cwd=BASE, env=ENV, capture_output=True, timeout=args.timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        code = 'timeout'
    if code != 0:
        status = read_json(control/'inventory-status.json') or {}
        out({'ok': False, 'control': str(control), 'error_type': 'InventoryTimeout' if code == 'timeout' else (safe_code(status.get('error_type')) or 'InventoryFailed')})
        return
    cleanup_inventory(control)
    write_json(STATE/'last-scan.json', {'control': str(control), 'time': time.time()})
    out(summarize_inventory(control))


def cmd_inventory(args):
    if args.control:
        control = Path(args.control).resolve()
    else:
        last = read_json(STATE/'last-scan.json') or {}
        if not last.get('control') or not Path(last['control']).is_dir():
            out({'ok': False, 'error_type': 'NoScanYet'}); return
        control = Path(last['control'])
    control.relative_to(WORK.resolve())
    out(summarize_inventory(control))


def render_template(text, row, run_name, mode):
    substitutions = {'@@RUN_NAME@@': run_name, '@@COURSE_ID@@': row['course_id'], '@@SUB_ID@@': row['sub_id'],
                     '@@DAY@@': row['date'], '@@COURSE_TITLE@@': row['course_title'].replace('\\', '\\\\').replace("'", "\\'")}
    for k, v in substitutions.items():
        text = text.replace(k, v)
    return text.replace("'--campus-mode','direct'", "'--campus-mode',"+repr(mode))


def cmd_start(args):
    control = Path(args.control).resolve(); control.relative_to(WORK.resolve())
    inv = read_json(control/'inventory.json')
    if not inv or inv.get('status') != 'complete':
        raise Refused('扫描结果不完整，请重新检查新回放')
    if (control/'execution-started.json').exists() or (control/'targets.json').exists():
        raise Refused('这次扫描已经启动过队列，请重新检查新回放')
    if worker_active():
        raise Refused('已有本地识别进程在运行，不能同时加载第二个模型')
    wanted = [x for x in args.sub_ids.split(',') if x]
    if not wanted or not all(ID.match(x) for x in wanted):
        raise Refused('没有选择课次')
    ready = {r['sub_id']: r for r in inv.get('unrecognized_available', [])}
    missing = [x for x in wanted if x not in ready]
    if missing:
        raise Refused('所选课次不在本次可识别列表中')
    for sid in wanted:
        if local_controls(ready[sid]['course_id'], sid):
            raise Refused('课次 '+sid+' 已有本地任务，请在识别进度中续跑，避免重复识别')
    source, sha = source_identity()
    if sha != inv['source_sha'] or Path(os.readlink(control/'repo')).resolve() != source:
        raise Refused('扫描后源码已变化，请重新检查新回放')
    stamp = control.name.removeprefix('local-latest-new-')
    # newest first, the same order prepare_and_start.py uses
    selected = sorted((dict(ready[s]) for s in wanted), key=lambda x: (x['date'], int(x['sub_id'])), reverse=True)
    prepared = []
    for row in selected:
        cid, sid, day = row['course_id'], row['sub_id'], row['date']
        r = WORK/f'local-latest-{cid}-{sid}-{stamp}'; output = OUTPUTS/r.name
        r.mkdir(mode=0o700); output.mkdir(mode=0o700, exist_ok=True)
        (r/'repo').symlink_to(source, target_is_directory=True)
        for name in ['finalize.py', 'deliver_corrected.py', 'cleanup_completed.py', 'read_progress.py']:
            text = render_template((RUNTIME/'templates'/name).read_text(), row, r.name, inv['mode'])
            compile(text, str(r/name), 'exec'); (r/name).write_text(text)
        for name in ['app_run_once.py', 'app_publish.py']:
            text = render_template((TEMPLATES/name).read_text(), row, r.name, inv['mode'])
            compile(text, str(r/name), 'exec'); (r/name).write_text(text)
        (r/'queue.env').write_text('SUMMARY_FIGURES=true\nGITHUB_REPOSITORY=Lucasuiii/iCourseNotes\nCOURSE_IDS='+cid+'\n')
        write_json(r/'selection.json', row); shutil.copyfile(control/'source.json', r/'source.json')
        cmd = [str(PY), '-m', 'scripts.local_history_refresh', '--env-file', str(r/'queue.env'), '--run-dir', str(r/('lecture-'+sid)),
               '--campus-mode', inv['mode'], '--automatic-terms', '--mlx-batch-size', '2', '--review-scope', 'theory']
        for suffix, name in [(['doctor'], 'doctor-private.log'), (['plan', '--new-lecture', sid, '--course-id', cid, '--date', day], 'plan-private.log')]:
            with (r/name).open('wb') as log:
                code = subprocess.run(cmd+suffix, cwd=source, env=ENV, stdout=log, stderr=subprocess.STDOUT).returncode
            if code:
                raise Refused('课次 '+sid+' 准备失败（'+name.split('-')[0]+'），未启动识别')
        write_json(r/'preparation-result.json', {'doctor_passed': True, 'plan_created': True, 'inference_calls': 0, 'data_writes': 0,
                                                'mail_calls': 0, 'common_source': str(source), 'prepared_by': 'local-app'})
        files = {n: hashlib.sha256((r/n).read_bytes()).hexdigest() for n in ['app_run_once.py', 'app_publish.py', 'finalize.py', 'read_progress.py']}
        write_json(r/'control-files.json', files)
        row.update(control=str(r), output=str(output)); prepared.append(row)
        (output/'start-report.md').write_text('# 本机识别启动\n\n'+day+'《'+row['course_title']+'》（'+cid+'/'+sid+'）。\n\n由本地应用启动：单 bf16 模型、双块文本解码，保留 AAC/课件 OCR/作业视觉/精选图/摘要与理论审查及一次修订；完成后停在待审核，不自动发布或发邮件。\n')
    write_json(control/'targets.json', prepared)
    shutil.copyfile(TEMPLATES/'app_queue.py', control/'app_queue.py')
    shutil.copyfile(RUNTIME/'templates/read_queue.py', control/'read_progress.py')
    pid = launch_queue(control, [])
    write_json(LAUNCH, {'control': str(control), 'started': time.time(), 'scope': 'local app: user-selected new lectures; newest-first',
                        'ready_lectures': len(prepared), 'deferred_lectures': len(inv.get('deferred_unavailable', []))})
    out({'ok': True, 'started': True, 'pid': pid, 'control': str(control),
         'lectures': [{'date': x['date'], 'course_title': x['course_title'], 'sub_id': x['sub_id']} for x in prepared]})


def launch_queue(control, extra):
    with (control/'coordinator-private.log').open('ab') as log:
        child = subprocess.Popen([str(PY), str(control/'app_queue.py')]+extra, cwd=BASE, env=ENV,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    write_json(control/('launch-result.json' if not extra else 'app-resume-launch.json'),
               {'pid': child.pid, 'started': time.time(), 'single_model': True, 'batch_size': 2, 'args': extra,
                'shared_environment': str(PY), 'independent_review_before_publication': True})
    return child.pid


# ----------------------------------------------------------- pause & resume

def queue_control(path):
    control = Path(path).resolve(); control.relative_to(WORK.resolve())
    if not (control/'targets.json').exists():
        raise Refused('不是识别队列目录')
    return control


def cmd_pause(args):
    control = queue_control(args.control)
    if not (control/'app_queue.py').exists():
        raise Refused('该队列由旧流程启动，应用只能查看，不能暂停')
    q = read_json(control/'queue-status.json') or {}
    if not pid_alive(q.get('pid'), 'app_queue.py'):
        raise Refused('队列当前没有在运行')
    current = next((t for t in read_json(control/'targets.json', []) if t['sub_id'] == q.get('current_sub_id')), None)
    snapshot = lecture_status(current, store) if current else {}
    if args.after_current:
        write_json(control/'app-pause-request.json', {'requested': time.time(), 'mode': 'after_current'})
        out({'ok': True, 'pause_requested': True, 'signalled_worker': False, 'after_current': True}); return
    if (snapshot.get('phase') == 'content_validation'):
        raise Refused('正在导出审核材料，几秒后即完成，请稍候再暂停')
    if not snapshot.get('safe_to_pause') and not args.force:
        out({'ok': False, 'needs_confirmation': True, 'stage': snapshot.get('stage') or snapshot.get('phase'),
             'message': '当前处于云端阶段（'+str(snapshot.get('stage') or snapshot.get('phase'))+'），立即暂停会让进行中的请求记为不明确，续跑时后端会拒绝自动重放。建议等待其完成。'})
        return
    write_json(control/'app-pause-request.json', {'requested': time.time(), 'stage': snapshot.get('stage')})
    status = (read_json(Path(current['control'])/'execution-status.json') or {}) if current else {}
    worker = status.get('worker_pid')
    signalled = False
    if pid_alive(worker, 'scripts.local_history_refresh'):
        os.kill(worker, signal.SIGINT); signalled = True
    out({'ok': True, 'pause_requested': True, 'signalled_worker': signalled, 'stage': snapshot.get('stage')})


def cmd_resume(args):
    control = queue_control(args.control)
    if not (control/'app_queue.py').exists():
        raise Refused('该队列由旧流程启动，请使用原有续跑脚本')
    q = read_json(control/'queue-status.json') or {}
    if pid_alive(q.get('pid'), 'app_queue.py'):
        raise Refused('队列仍在运行')
    if worker_active():
        raise Refused('已有本地识别进程在运行，不能同时加载第二个模型')
    (control/'app-pause-request.json').unlink(missing_ok=True)
    extra = ['--resume'] + (['--retry-failed'] if args.retry_failed else [])
    pid = launch_queue(control, extra)
    out({'ok': True, 'resumed': True, 'pid': pid, 'retry_failed': args.retry_failed})


# ------------------------------------------------------------- notes

def notes_store():
    STATE.mkdir(mode=0o700, exist_ok=True)
    return store(STATE/'notes-cache')


def figure_payload(raw_meta):
    try:
        state = json.loads(raw_meta) if raw_meta else {}
    except ValueError:
        return {'status': 'invalid', 'figures': []}
    figs = []
    for f in state.get('figures', []) or []:
        if isinstance(f, dict) and re.fullmatch(r'[0-9a-f]{64}', str(f.get('id'))) and f.get('mime') == 'image/jpeg':
            figs.append({'id': f['id'], 'mime': 'image/jpeg', 'data': f.get('data', ''), 'caption': f.get('caption'),
                         'seconds': number(f.get('seconds'))})
    return {'status': safe_code(state.get('status')), 'error_type': safe_code(state.get('error_type')), 'figures': figs}


def review_payload(raw):
    try:
        audit = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(audit, dict):
        return None
    return {'status': safe_code(audit.get('status')), 'issues': len(audit.get('issues', []) or [])}


def provider_env():
    """Load the configured private env files for GitHub/data access; nothing is printed."""
    source = str(active_source())
    if source not in sys.path:
        sys.path.insert(0, source)
    os.chdir(source)
    from scripts.local_history_refresh import load_env
    cfg = json.loads((active_source()/'.local-history-refresh/settings.json').read_text())
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        for path in cfg['env_files']:
            load_env(path)
    os.environ.update(GITHUB_REPOSITORY='Lucasuiii/iCourseNotes', DB_ENCRYPTION_KEY=key())


def remote_lecture(cid, sid):
    """Read one lecture from the current data branch; plaintext is removed before returning."""
    provider_env()
    from scripts.production_db import load_remote
    STATE.mkdir(mode=0o700, exist_ok=True)
    tmp = STATE/'compare-tmp'; shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(mode=0o700)
    try:
        db = tmp/'remote.db'
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            revision = load_remote(db)
        with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as c:
            c.row_factory = sqlite3.Row
            r = c.execute('select * from lectures where sub_id=?', (sid,)).fetchone()
            figs = c.execute('select value from meta where key=?', ('summary_figures:'+sid,)).fetchone()
        if not r:
            return revision, None
        if str(r['course_id']) != cid:
            raise Refused('云端同编号课次属于其他课程，不能处理')
        summary = r['summary'] or ''
        return revision, {'has_summary': bool(summary), 'deleted': bool(r['deleted_at']), 'emailed': bool(r['emailed_at']),
                          'summary_sha256': hashlib.sha256(summary.encode()).hexdigest() if summary else None,
                          'summary_chars': len(summary), 'transcript_chars': len(r['transcript'] or ''),
                          'summary_model': safe_code((r['summary_model'] or '').split(' ')[0]) if r['summary_model'] else None,
                          'processed_at': safe_code(r['processed_at']),
                          'figures': len(figure_payload(figs[0] if figs else None)['figures'])}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_notes_sync(args):
    provider_env()
    from scripts.production_db import load_remote
    STATE.mkdir(mode=0o700, exist_ok=True)
    tmp = STATE/'notes-sync-tmp'; shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(mode=0o700)
    try:
        db = tmp/'remote.db'
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            revision = load_remote(db)
        s = notes_store(); index = []
        with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as c:
            c.row_factory = sqlite3.Row
            titles = {r['course_id']: r['title'] for r in c.execute('select course_id,title from courses')}
            # The latest catalog term seen for each subscribed course.
            terms = {}
            for r in c.execute('select a.course_id, a.term from all_courses a join courses c using(course_id) order by a.last_seen_at'):
                if re.fullmatch(r'\d{4}-\d{4}(1|2|暑期)', r['term'] or ''):
                    terms[str(r['course_id'])] = r['term']
            meta = {r['key']: r['value'] for r in c.execute("select key,value from meta where key like 'summary_figures:%' or key like 'summary_review:%'")}
            for r in c.execute('select * from lectures where summary is not null and summary != "" and deleted_at is null order by date desc'):
                sid = str(r['sub_id']); cid = str(r['course_id'])
                figures = figure_payload(meta.get('summary_figures:'+sid))
                term = term_for(cid, r['date'], terms)
                payload = {'sub_id': sid, 'course_id': cid, 'course_title': titles.get(cid) or cid, 'date': r['date'],
                           'term': term, 'term_label': term_label(term),
                           'title': r['sub_title'], 'summary': r['summary'], 'transcript': r['transcript'] or '',
                           'summary_model': r['summary_model'], 'emailed': bool(r['emailed_at']), 'figures': figures,
                           'review': review_payload(meta.get('summary_review:'+sid)), 'source': 'data', 'revision': revision}
                s.save('lecture-'+sid+'.enc', payload)
                index.append({k: payload[k] for k in ('sub_id', 'course_id', 'course_title', 'date', 'title', 'emailed', 'term', 'term_label')} |
                             {'figures': len(figures['figures']), 'transcript_chars': len(payload['transcript']),
                              'summary_chars': len(payload['summary'])})
        s.save('index.enc', {'revision': revision, 'synced_at': time.time(), 'lectures': index, 'course_terms': terms})
        keep = {'lecture-'+x['sub_id']+'.enc' for x in index} | {'index.enc'}
        for p in (STATE/'notes-cache').glob('lecture-*.enc'):
            if p.name not in keep:
                p.unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out({'ok': True, 'revision': revision, 'lectures': len(index)})


def local_lectures():
    rows = []; terms = known_terms()
    for p in sorted(WORK.glob('local-latest-[0-9]*-[0-9]*-*')):
        sel = read_json(p/'selection.json'); st = read_json(p/'execution-status.json')
        if not sel or not st:
            continue
        status = st.get('status')
        if (p/'review-ready.json').exists() and status not in ('completed', 'delivered', 'publishing', 'publication_failed'):
            status = 'awaiting_content_review'
        if status not in ('awaiting_content_review', 'awaiting_publication', 'completed', 'delivered', 'publishing', 'publication_failed'):
            continue
        output = OUTPUTS/p.name
        has_candidate = (p/('lecture-'+sel['sub_id'])/(sel['course_id']+'-'+sel['sub_id'])/'candidate.db').exists()
        if not has_candidate and not (output/'summary.md').exists():
            continue
        publication = read_json(p/'app-publication-status.json') or {}
        approval = read_json(p/'app-approval.json')
        term = term_for(sel['course_id'], sel['date'], terms)
        rows.append({'sub_id': sel['sub_id'], 'course_id': sel['course_id'], 'course_title': sel['course_title'],
                     'date': sel['date'], 'title': sel.get('title'), 'control': str(p), 'status': safe_code(status),
                     'term': term, 'term_label': term_label(term),
                     'data_commit': safe_code(st.get('data_commit')), 'source': 'local',
                     'approved': bool(approval), 'approved_at': approval.get('approved_at') if approval else None,
                     'app_managed': (p/'app_publish.py').exists(),
                     'publication_status': safe_code(publication.get('status')),
                     'publication_error': safe_code(publication.get('error_type')),
                     'publication_conflict': 'already processed' in str(publication.get('detail') or '') or 'changed since comparison' in str(publication.get('detail') or ''),
                     'notes_commit': safe_code(publication.get('notes_commit')),
                     'email_status': safe_code(publication.get('email_status') or st.get('email_status')),
                     'updated': publication.get('updated') or st.get('updated')})
    return rows


def cmd_notes_list(args):
    s = notes_store()
    index = s.read('index.enc') if s.exists('index.enc') else {'lectures': [], 'revision': None, 'synced_at': None}
    published = {x['sub_id'] for x in index['lectures']}
    local = [x for x in local_lectures() if x['sub_id'] not in published or x['status'] not in ('completed', 'delivered')]
    out({'ok': True, 'revision': safe_code(index.get('revision')), 'synced_at': index.get('synced_at'),
         'lectures': index['lectures'], 'local': local})


def local_note(control):
    control = Path(control).resolve(); control.relative_to(WORK.resolve())
    sel = read_json(control/'selection.json'); cid, sid = sel['course_id'], sel['sub_id']
    output = OUTPUTS/control.name
    candidate = control/('lecture-'+sid)/(cid+'-'+sid)/'candidate.db'
    term = term_for(cid, sel['date'], known_terms())
    payload = {'sub_id': sid, 'course_id': cid, 'course_title': sel['course_title'], 'date': sel['date'],
               'title': sel.get('title'), 'source': 'local', 'control': str(control), 'term': term, 'term_label': term_label(term)}
    if candidate.exists():
        with contextlib.closing(sqlite3.connect(candidate.as_uri()+'?mode=ro', uri=True)) as c:
            row = c.execute('select summary,transcript,summary_model from lectures where sub_id=?', (sid,)).fetchone()
            meta = dict(c.execute('select key,value from meta where key in (?,?,?)',
                                  ('summary_figures:'+sid, 'summary_review:'+sid, 'qwen_pipeline:'+sid)).fetchall())
        payload.update(summary=row[0] or '', transcript=row[1] or '', summary_model=row[2],
                       figures=figure_payload(meta.get('summary_figures:'+sid)), review=review_payload(meta.get('summary_review:'+sid)))
        try:
            q = json.loads(meta.get('qwen_pipeline:'+sid) or '{}')
            cov = q.get('recognition_coverage') or {}
            payload['coverage'] = {'missing_seconds': number(cov.get('missing_seconds')), 'complete': cov.get('complete')}
        except ValueError:
            pass
        recovery = control/'recovery'/'app-figure-recovery.enc'
        if recovery.exists():
            # Figures recovered after a rejected selection: same reviewed prose, figures inserted.
            artifact = store(control/'recovery').read('app-figure-recovery.enc')
            if artifact.get('base_summary') == payload['summary']:
                fs = artifact['figure_state']
                payload.update(summary=artifact['summary'], figures=figure_payload(json.dumps(fs)),
                               figure_recovery={'figures': len(fs['figures']),
                                                'dropped': len(fs.get('recovery', {}).get('dropped', []))})
        payload['summary_sha256'] = hashlib.sha256(payload['summary'].encode()).hexdigest()
    else:
        # Already cleaned: rebuild the portable export into the same figure-ref shape.
        text = (output/'summary.md').read_text(); figs = []
        def swap(m):
            path = Path(m[2])
            if path.is_file() and path.resolve().is_relative_to(output.resolve()):
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() == m[3]:
                    import base64
                    figs.append({'id': m[3], 'mime': 'image/jpeg', 'data': base64.b64encode(raw).decode(), 'caption': m[1]})
            return '![%s](#icourse-figure-%s)' % (m[1], m[3])
        text = EXPORTED_REF.sub(swap, text)
        payload.update(summary=text, transcript=(output/'transcript.md').read_text().removeprefix('# 课堂整理转录\n\n') if (output/'transcript.md').exists() else '',
                       figures={'status': 'complete', 'figures': figs}, review=None, exported_copy=True)
    asr = output/'asr-transcript.md'
    payload['asr_transcript'] = asr.read_text() if asr.exists() else ''
    status = read_json(control/'execution-status.json') or {}
    payload['status'] = safe_code(status.get('status'))
    if (control/'review-ready.json').exists() and payload['status'] not in ('completed', 'delivered'):
        payload['status'] = 'awaiting_content_review'
    payload['blocks'] = {'saved': number(status.get('saved_blocks')), 'planned': number(status.get('planned_blocks'))}
    payload['publication'] = read_json(control/'app-publication-status.json')
    approval = read_json(control/'app-approval.json')
    payload['approved'] = bool(approval) and approval.get('summary_sha256') == payload.get('summary_sha256')
    payload['publishable'] = (candidate.exists() and payload['status'] == 'awaiting_content_review'
                              and not (control/'finalization-started.json').exists()
                              and not (control/'notes-publication.json').exists()
                              and not (control/'app-publication-started.json').exists())
    if not payload['publishable']:
        payload['publish_block_reason'] = ('已发布或已开始发布' if (control/'notes-publication.json').exists() or (control/'app-publication-started.json').exists()
                                           else '已保留云端版本' if (control/'app-superseded.json').exists()
                                           else '已由旧流程开始交付' if (control/'finalization-started.json').exists()
                                           else '候选数据已清理' if not candidate.exists() else '当前状态不可发布')
    return payload


def cmd_note(args):
    if args.control:
        out({'ok': True, 'note': local_note(args.control)}); return
    if not ID.match(args.sub_id or ''):
        raise Refused('课次编号无效')
    s = notes_store(); name = 'lecture-'+args.sub_id+'.enc'
    if not s.exists(name):
        raise Refused('本地缓存中没有这堂笔记，请先同步')
    note = s.read(name)
    # Local ASR transcripts exist only on this Mac for locally recognised lectures.
    asr = sorted(OUTPUTS.glob('local-latest-'+note['course_id']+'-'+args.sub_id+'-*/asr-transcript.md'))
    note['asr_transcript'] = asr[-1].read_text() if asr else ''
    out({'ok': True, 'note': note})


# ------------------------------------------------- approval, push, remote

def lecture_control(path):
    control = Path(path).resolve(); control.relative_to(WORK.resolve())
    if not (control/'selection.json').exists():
        raise Refused('不是本地课次目录')
    return control


def cmd_approve(args):
    """Local review decision only; nothing leaves this Mac until `push`."""
    control = lecture_control(args.control)
    if not (control/'app_publish.py').exists():
        raise Refused('该课次不是由本地应用启动，请沿用原有交付流程')
    note = local_note(control)
    if not note['publishable']:
        raise Refused(note.get('publish_block_reason') or '当前不可审核通过')
    if args.summary_sha256 != note.get('summary_sha256'):
        raise Refused('内容已变化，请重新阅读后再审核')
    checks = [c for c in json.loads(args.checks) if isinstance(c, str) and c.strip()]
    if len(checks) < 4:
        raise Refused('至少需要完成四项审核确认')
    write_json(control/'app-approval.json', {'summary_sha256': args.summary_sha256, 'checks': checks,
                                             'notes': (args.notes or '')[:2000], 'approved_at': time.time()})
    out({'ok': True, 'approved': True})


def cmd_unapprove(args):
    control = lecture_control(args.control)
    if (control/'app-publication-started.json').exists() or (control/'app-publication-request.json').exists():
        raise Refused('已经开始推送，不能撤回审核')
    (control/'app-approval.json').unlink(missing_ok=True)
    out({'ok': True, 'approved': False})


def reset_failed_attempt(control):
    """A failed push that never reached GitHub (no notes journal) may be retried once the user
    has chosen again; earlier attempt files are archived, never deleted."""
    status = read_json(control/'app-publication-status.json') or {}
    if status.get('status') != 'failed' or (control/'notes-publication.json').exists():
        raise Refused('已经开始或完成推送，不会重复推送')
    n = 1
    while (control/f'app-attempt-{n}').exists():
        n += 1
    archive = control/f'app-attempt-{n}'; archive.mkdir(mode=0o700)
    for name in ('app-publication-started.json', 'app-publication-request.json', 'app-publication-status.json', 'editorial-approval.json'):
        if (control/name).exists():
            (control/name).rename(archive/name)
    st = read_json(control/'execution-status.json') or {}
    st.update(status='awaiting_content_review', phase='independent_review', error_type=None, updated=time.time())
    write_json(control/'execution-status.json', st)


def cmd_push(args):
    control = lecture_control(args.control)
    if not (control/'app_publish.py').exists():
        raise Refused('该课次不是由本地应用启动，缺少应用发布脚本')
    if not args.dry_run and (control/'app-publication-started.json').exists():
        reset_failed_attempt(control)
    if not args.dry_run:
        sel = read_json(control/'selection.json')
        _, remote = remote_lecture(sel['course_id'], sel['sub_id'])
        if remote and remote['has_summary'] and remote['summary_sha256'] != args.overwrite_remote:
            out({'ok': False, 'conflict': True, 'message': '云端已经有这堂课的笔记，请先选择保留云端或用本机覆盖'}); return
    approval = read_json(control/'app-approval.json')
    note = local_note(control)
    if not note['publishable']:
        raise Refused(note.get('publish_block_reason') or '当前不可推送')
    if args.dry_run and not approval:
        # The read-only check may run before review; it never pushes or mails.
        approval = {'summary_sha256': note.get('summary_sha256'), 'checks': [], 'notes': ''}
    if not approval:
        raise Refused('请先在笔记中审核通过')
    if approval['summary_sha256'] != note.get('summary_sha256'):
        raise Refused('审核后内容已变化，请重新审核')
    request = dict(approval, email=bool(args.email), dry_run=bool(args.dry_run), requested=time.time())
    if args.overwrite_remote:
        if not re.fullmatch(r'[0-9a-f]{64}', args.overwrite_remote):
            raise Refused('覆盖确认无效')
        # Bound to the exact cloud summary the user compared; a newer cloud edit refuses the push.
        request['overwrite_remote_sha256'] = args.overwrite_remote
    name = 'app-dry-run-request.json' if args.dry_run else 'app-publication-request.json'
    if not args.dry_run and (control/name).exists():
        raise Refused('已经提交过推送请求，不会重复推送')
    write_json(control/name, request)
    cmd = [str(PY), str(control/'app_publish.py'), '--request', name]
    if args.dry_run:
        p = subprocess.run(cmd, cwd=BASE, env=ENV, capture_output=True, timeout=900)
        result = read_json(control/'app-dry-run-result.json') or {'status': 'failed', 'error_type': 'DryRunNoResult'}
        out({'ok': p.returncode == 0, 'dry_run': True, 'result': result}); return
    with (control/'app-publication-private.log').open('ab') as log:
        child = subprocess.Popen(cmd, cwd=BASE, env=ENV, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    out({'ok': True, 'started': True, 'pid': child.pid})


def cmd_compare(args):
    """Is this lecture already in data? Shows both versions so the user can choose."""
    control = lecture_control(args.control)
    note = local_note(control)
    revision, remote = remote_lecture(note['course_id'], note['sub_id'])
    figs = (note.get('figures') or {}).get('figures') or []
    out({'ok': True, 'revision': safe_code(revision), 'conflict': bool(remote and remote['has_summary']), 'remote': remote,
         'local': {'summary_sha256': note.get('summary_sha256'), 'summary_chars': len(note.get('summary') or ''),
                   'transcript_chars': len(note.get('transcript') or ''), 'figures': len(figs),
                   'review': (note.get('review') or {}).get('status'), 'figures_status': (note.get('figures') or {}).get('status')}})


def cmd_keep_remote(args):
    """Keep the cloud note: the local result is set aside, never written to data."""
    control = lecture_control(args.control)
    if (control/'app-publication-started.json').exists():
        reset_failed_attempt(control)
    sel = read_json(control/'selection.json')
    revision, remote = remote_lecture(sel['course_id'], sel['sub_id'])
    if not (remote and remote['has_summary']):
        raise Refused('云端目前没有这堂课的笔记，无需保留云端版本')
    write_json(control/'app-superseded.json', {'kept_remote': True, 'remote_revision': revision,
                                               'remote_summary_sha256': remote['summary_sha256'], 'decided_at': time.time()})
    st = read_json(control/'execution-status.json') or {}
    st.update(status='superseded_by_remote', phase='complete', updated=time.time())
    write_json(control/'execution-status.json', st)
    out({'ok': True, 'kept_remote': True})


def cmd_remote(args):
    """Cheap remote check: the data branch head versus the local notes cache."""
    provider_env()
    from scripts.production_db import auth_env
    url = 'https://github.com/'+os.environ['GITHUB_REPOSITORY']+'.git'
    refs = subprocess.run(['git', 'ls-remote', '--heads', url, 'refs/heads/data'], env=auth_env(),
                          capture_output=True, text=True, timeout=60)
    if refs.returncode:
        raise Refused('无法连接 GitHub（git ls-remote 失败）')
    head = refs.stdout.split()[0] if refs.stdout.strip() else None
    s = notes_store()
    index = s.read('index.enc') if s.exists('index.enc') else {}
    out({'ok': True, 'repository': os.environ['GITHUB_REPOSITORY'], 'remote_head': safe_code(head),
         'cached_revision': safe_code(index.get('revision')), 'synced_at': index.get('synced_at'),
         'up_to_date': bool(head) and head == index.get('revision'), 'checked_at': time.time(),
         'local': local_lectures()})


# ------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('status'); s.add_argument('--queue'); s.add_argument('--limit', type=int, default=6)
    s.add_argument('--history-sub'); s.add_argument('--decrypt-all', action='store_true')
    s = sub.add_parser('scan'); s.add_argument('--timeout', type=int, default=900)
    s = sub.add_parser('inventory'); s.add_argument('--control')
    s = sub.add_parser('start'); s.add_argument('--control', required=True); s.add_argument('--sub-ids', required=True)
    s = sub.add_parser('pause'); s.add_argument('--control', required=True); s.add_argument('--force', action='store_true')
    s.add_argument('--after-current', action='store_true')
    s = sub.add_parser('resume'); s.add_argument('--control', required=True); s.add_argument('--retry-failed', action='store_true')
    sub.add_parser('notes-sync'); sub.add_parser('notes-list')
    s = sub.add_parser('note'); s.add_argument('--sub-id'); s.add_argument('--control')
    s = sub.add_parser('approve'); s.add_argument('--control', required=True); s.add_argument('--summary-sha256', required=True)
    s.add_argument('--checks', required=True); s.add_argument('--notes')
    s = sub.add_parser('unapprove'); s.add_argument('--control', required=True)
    s = sub.add_parser('push'); s.add_argument('--control', required=True); s.add_argument('--email', action='store_true')
    s.add_argument('--dry-run', action='store_true'); s.add_argument('--overwrite-remote')
    s = sub.add_parser('compare'); s.add_argument('--control', required=True)
    s = sub.add_parser('keep-remote'); s.add_argument('--control', required=True)
    sub.add_parser('remote')
    args = p.parse_args()
    try:
        {'status': cmd_status, 'scan': cmd_scan, 'inventory': cmd_inventory, 'start': cmd_start, 'pause': cmd_pause,
         'resume': cmd_resume, 'notes-sync': cmd_notes_sync, 'notes-list': cmd_notes_list, 'note': cmd_note,
         'approve': cmd_approve, 'unapprove': cmd_unapprove, 'push': cmd_push, 'remote': cmd_remote,
         'compare': cmd_compare, 'keep-remote': cmd_keep_remote}[args.command](args)
    except Refused as e:
        out({'ok': False, 'message': str(e)})
    except Exception as e:
        # Only the type is shown: upstream messages can carry URLs or credentials.
        out({'ok': False, 'error_type': type(e).__name__})


if __name__ == '__main__':
    main()
