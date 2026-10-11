"""Run with python -m scripts.local_history_refresh; no automatic publication."""
from contextlib import closing
import argparse
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]


def load_env(path):
    """Literal env parser; never shell-eval credentials or log their values."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('Environment file must be a private regular file (chmod 600)')
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        name, separator, value = line.partition('=')
        if not separator or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError(f'Invalid environment assignment at line {number}')
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'"):
            parts = shlex.split(value)
            if len(parts) != 1:
                raise ValueError(f'Invalid quoted value at line {number}')
            value = parts[0]
        if not isinstance(value, str) or any(c in value for c in ('\0', '\n', '\r')):
            raise ValueError(f'Invalid environment value at line {number}')
        os.environ[name] = value


def source_identity():
    from scripts.production_db import command
    from scripts.local_history.storage import tree_hash
    return {'sha': command(['git', 'rev-parse', 'HEAD'], cwd=REPO).decode().strip(),
            'tree_hash': tree_hash(REPO, ['src/**/*.py', 'scripts/**/*.py', 'prompts/**/*', 'requirements*.txt', 'local-history.command'])}


def backend_identity(model):
    from scripts.local_history.storage import tree_hash
    return {'kind': 'mlx', 'device': 'Apple GPU', 'dtype': 'bfloat16',
            'converted_revision': None,
            'weights_hash': tree_hash(model, ['*.safetensors', '*.json', '*.txt', '*.model']),
            'versions': {name: importlib.metadata.version(name) for name in ('mlx', 'mlx-qwen3-asr', 'numpy', 'sherpa-onnx')}}


def review_identity(args):
    """Freeze the existing local CPU aligner and review dependencies."""
    if not args.cloud_review or getattr(args, 'review_scope', 'full') == 'theory':
        return None
    from scripts.local_history.storage import tree_hash
    model = args.aligner.resolve()
    if not model.is_dir() or not (model/'model.safetensors').is_file():
        raise ValueError('Local forced aligner is missing; resolve doctor before planning')
    return {'alignment_model_path': str(model),
            'weights_hash': tree_hash(model, ['*.safetensors','*.json','*.txt','*.model']),
            'versions': {name: importlib.metadata.version(name)
                         for name in ('torch','qwen-asr','soundfile','transformers')}}


def select_targets(path, revision, courses, limit=None, lecture_ids=None):
    from src.pipeline import history_refresh as policy
    from src.runtime import config
    from src.runtime.session_rules import lecture_is_selected
    if not courses:
        raise ValueError('COURSE_IDS must explicitly select the subscribed courses')
    requested = set(lecture_ids or [])
    if any(not str(sid).isascii() or not str(sid).isdigit() for sid in requested):
        raise ValueError('Invalid requested lecture ID')
    targets, skipped = [], 0
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute('SELECT * FROM lectures ORDER BY date,course_id,sub_id').fetchall():
            row = dict(row)
            if row['course_id'] not in courses:
                continue
            if requested and str(row['sub_id']) not in requested:
                continue
            try:
                policy.completed(row)
                if not lecture_is_selected(row['course_id'], row, config.COURSE_SESSION_RULES,
                        config.COURSE_SESSION_OVERRIDE_DATES, exclusions=config.COURSE_SESSION_EXCLUSIONS):
                    skipped += 1; continue
                target = policy.baseline_manifest(conn, {'course_id': row['course_id'],
                    'lecture_ids': [row['sub_id']]}, revision)['targets'][0]
            except ValueError:
                skipped += 1; continue
            targets.append(target)
    if requested and {t['sub_id'] for t in targets} != requested:
        raise ValueError('Requested lectures are unavailable, excluded or lack complete historical notes')
    if requested and limit is not None and limit < len(requested):
        raise ValueError('Limit cannot discard explicitly requested lectures')
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        raise ValueError('No complete historical notes match the subscribed selection')
    if len(targets) > 256:
        raise ValueError('Local history selection exceeds 256 lectures; split into separate plans')
    return targets, skipped


def new_preview_target(args, courses, baseline_path=None):
    """Explicit, date-bounded new-lecture preview; runtime checks campus identity."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from src.pipeline.history_refresh import numeric
    course, sub = numeric(args.course_id or ''), numeric(args.new_lecture)
    day = args.date or ''
    if (course not in courses or date.fromisoformat(day).isoformat() != day
            or day > datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            or args.lecture_id or args.limit):
        raise ValueError('Invalid new-lecture preview selection')
    if baseline_path is not None:
        with closing(sqlite3.connect(baseline_path)) as conn:
            row = conn.execute('SELECT course_id, deleted_at FROM lectures WHERE sub_id=?', (sub,)).fetchone()
        if row and (str(row[0]) != course or row[1]):
            raise ValueError('New-lecture preview is unavailable or permanently ignored')
    return {'slot': 0, 'course_id': course, 'sub_id': sub, 'date': day,
            'before_hash': None, 'preview_only': True}


def make_plan(store, args):
    from scripts.production_db import load_remote
    from scripts.local_history.storage import file_hash
    from src.runtime import config
    if store.exists('manifest.enc'):
        raise ValueError('This run already has a frozen plan; choose a different --run-dir')
    with tempfile.TemporaryDirectory(prefix='plan-', dir=store.root) as tmp:
        path = Path(tmp)/'baseline.db'
        revision = load_remote(path)
        if getattr(args, 'new_lecture', None):
            targets = [new_preview_target(args, config.COURSE_IDS, path)]; skipped = 0
        else:
            targets, skipped = select_targets(path, revision, config.COURSE_IDS, args.limit,
                                              getattr(args,'lecture_id',None))
        manifest = {'schema': 1, 'run_id': str(time.time_ns()), 'repository': os.environ['GITHUB_REPOSITORY'],
                    'baseline_revision': revision, 'targets': targets, 'source': source_identity(),
                    'model_path': str(args.model.resolve()), 'backend': backend_identity(args.model),
                    'mlx_batch_size':getattr(args,'mlx_batch_size',1),
                    'vad_sha256': file_hash(args.vad_model), 'cloud_review': args.cloud_review,
                    'review_scope': getattr(args, 'review_scope', 'theory'),
                    'review_runtime': review_identity(args),
                    'audio_acquisition': args.audio_mode,
                    'campus_access': getattr(args,'campus_mode','auto'),
                    'automatic_terms': getattr(args,'automatic_terms',False),
                    'session_rules': {k: os.environ.get(k, '') for k in ('COURSE_IDS', 'COURSE_SESSION_RULES',
                        'COURSE_SESSION_EXCLUSIONS', 'COURSE_SESSION_OVERRIDE_DATES')}}
        store.save_bytes('baseline.db.enc', path.read_bytes())
    store.save('manifest.enc', manifest)
    store.save('progress.enc', {'lectures': {}, 'status': 'planned'})
    print(f'已冻结 {len(targets)} 堂历史课次；排除/未完整 {skipped} 堂。未登录校园、未调用模型。')


def verify_resume(manifest, args):
    from scripts.local_history.storage import file_hash
    if (manifest['repository'] != os.environ['GITHUB_REPOSITORY']
            or manifest['source'] != source_identity()
            or manifest['backend'] != backend_identity(args.model)
            or manifest.get('mlx_batch_size',1) != getattr(args,'mlx_batch_size',1)
            or manifest['model_path'] != str(args.model.resolve())
            or manifest['vad_sha256'] != file_hash(args.vad_model)
            or manifest['cloud_review'] != args.cloud_review
            or manifest.get('review_scope', 'full') != getattr(args, 'review_scope', 'full')
            or manifest.get('review_runtime') != review_identity(args)
            or manifest.get('audio_acquisition') != args.audio_mode
            or manifest.get('campus_access','auto') != getattr(args,'campus_mode','auto')
            or manifest.get('automatic_terms',False) != getattr(args,'automatic_terms',False)
            or manifest['session_rules'] != {k: os.environ.get(k, '') for k in manifest['session_rules']}):
        raise ValueError('Code, model, audio acquisition, repository or session rules changed; create a new plan')


def run(store, manifest, args):
    from scripts.local_history.runtime import process, Paused, PreflightBlocked, candidate_files
    verify_resume(manifest, args)
    if doctor(args):
        raise ValueError('Local runtime is incomplete; resolve the doctor results before running')
    progress = store.read('progress.enc')
    if progress.get('status') == 'published':
        raise ValueError('This plan is already published')
    progress['status'] = 'running'; store.save('progress.enc', progress)
    deadline = time.monotonic()+args.hours*3600
    for target in manifest['targets']:
        tag = target['course_id']+'-'+target['sub_id']
        previous = progress['lectures'].get(tag, {})
        if previous.get('status') == 'complete':
            candidate_files(store, target)
            continue
        if previous.get('status') == 'failed' and not args.retry_failed:
            continue
        if time.monotonic() >= deadline:
            progress['status'] = 'paused'; break
        progress['lectures'][tag] = {'status': 'running'}
        store.save('progress.enc', progress)
        try:
            process(store, manifest, target, deadline,
                    allow_active_actions=getattr(args, 'allow_active_actions', False))
            progress['lectures'][tag] = {'status': 'complete'}
        except PreflightBlocked as error:
            progress['lectures'][tag] = {'status': 'blocked', 'reason': error.code, 'actions': error.runs}
            progress['status'] = 'blocked'
            store.save('progress.enc', progress)
            print('本地试跑停在登录之前：相关 Actions 正在运行（'+', '.join(error.runs)+'）。结束后执行相同 run 命令即可。')
            return 2
        except (Paused, KeyboardInterrupt):
            progress['lectures'][tag] = {'status': 'paused'}
            progress['status'] = 'paused'
            store.save('progress.enc', progress)
            print('进度已保存。继续时使用相同的 run 命令。'); return
        except Exception as error:
            if time.monotonic() >= deadline:
                progress['lectures'][tag] = {'status': 'paused'}
                progress['status'] = 'paused'
                store.save('progress.enc', progress)
                print('运行预算已到，检查点保留；下次 run 会继续未完成块。')
                return 0
            # Only the exception type is durable/public; campus URLs and secrets
            # can appear in upstream exception messages.
            progress['lectures'][tag] = {'status': 'failed', 'error_type': type(error).__name__}
            print(f'课次 {tag} 未完成：{type(error).__name__}；旧 data 保留。', flush=True)
        store.save('progress.enc', progress)
    if progress['status'] == 'running':
        progress['status'] = 'finished'
    store.save('progress.enc', progress)
    status(store, manifest)
    return 2 if any(r.get('status') == 'failed' for r in progress['lectures'].values()) else 0


def status(store, manifest):
    progress = store.read('progress.enc')
    counts = {}
    active = []
    blockers = []
    for target in manifest['targets']:
        tag = target['course_id']+'-'+target['sub_id']
        state = progress['lectures'].get(tag, {}).get('status', 'pending')
        counts[state] = counts.get(state, 0)+1
        if state == 'blocked':
            blockers.append(progress['lectures'][tag])
        if state == 'running' and store.exists(tag+'.enc'):
            checkpoint = store.read(tag+'.enc')
            plan = checkpoint.get('spec', {}).get('plan', {})
            active.append({'stage': checkpoint.get('stage', 'starting'),
                           'saved_blocks': len(checkpoint.get('rows', [])),
                           'planned_blocks': len(plan.get('blocks', [])),
                           'audio_seconds': checkpoint.get('spec', {}).get('audio_seconds')})
    coverage = []
    for target in manifest['targets']:
        tag = target['course_id']+'-'+target['sub_id']
        if store.exists(tag+'.enc'):
            value = store.read(tag+'.enc').get('recognition_coverage')
            if value is not None:
                coverage.append({'course_id':target['course_id'],'sub_id':target['sub_id'],**value})
    print(json.dumps({'status': progress['status'], 'total': len(manifest['targets']),
                      'lectures': counts, 'active': active, 'blockers': blockers,
                      'recognition_coverage':coverage}, ensure_ascii=False))


def review(store, manifest, completed_only=False):
    from src.pipeline import history_refresh as policy
    from scripts.local_history.runtime import candidate_files
    if any(t.get('preview_only') for t in manifest['targets']):
        return review_new_preview(store, manifest)
    from scripts.local_history.storage import atomic
    progress = store.read('progress.enc')
    approvals, artifacts, lines = [], {}, ['# 本地历史笔记覆盖预览', '', '只在本机保存；尚未覆盖正式 data。', '']
    with tempfile.TemporaryDirectory(prefix='review-', dir=store.root) as tmp:
        baseline = Path(tmp)/'baseline.db'
        atomic(baseline, store.read_bytes('baseline.db.enc'))
        with closing(sqlite3.connect(baseline)) as conn:
            for target in manifest['targets']:
                tag = target['course_id']+'-'+target['sub_id']
                if progress['lectures'].get(tag, {}).get('status') != 'complete':
                    if not completed_only:
                        raise ValueError('Some planned lectures are incomplete; use --completed-only to explicitly review a partial batch')
                    continue
                _, fresh = candidate_files(store, target)
                old = policy.lesson_state(conn, target['course_id'], target['sub_id'])
                approval = {'schema': 1, 'source_sha': manifest['source']['sha'],
                    'source_run': manifest['run_id'], 'baseline_revision': manifest['baseline_revision'],
                    'targets': [dict(target, candidate_hash=policy.digest(fresh))]}
                policy.validate_approval(approval); approvals.append(approval)
                artifacts[tag] = policy.digest(store.read(tag+'.enc'))
                title = old['lecture']['sub_title']
                from src.pipeline.recognition_coverage import missing_recognition_notice
                metadata = json.loads(next((r['value'] for r in fresh['meta']
                    if r['key']=='qwen_pipeline:'+target['sub_id']), '{}'))
                coverage = metadata.get('recognition_coverage')
                notice = ('新结果保留识别缺口，属于不完整转录。'+missing_recognition_notice(coverage)
                          if coverage and not coverage['complete'] else '新结果已通过完整识别与复核门禁。')
                lines += [f'## {target["course_id"]} / {title}', '',
                          f'课次 {target["sub_id"]}；日期 {target["date"]}', '',
                          notice, '',
                          f'转录：{len(old["lecture"]["transcript"])} → {len(fresh["lecture"]["transcript"])} 字', '',
                          '### 原笔记', '', old['lecture']['summary'], '',
                          '### 新笔记', '', fresh['lecture']['summary'], '']
    if not approvals:
        raise ValueError('No complete candidates are available for review')
    value = {'schema': 1, 'manifest_hash': policy.digest(manifest), 'approvals': approvals,
             'completed_only': completed_only, 'artifacts': artifacts}
    fingerprint = policy.digest(value)
    lines += ['## 覆盖指纹', '', '`'+fingerprint+'`', '',
              'apply 会重新核对正式 data；任何待覆盖课次变化都会拒绝本批覆盖。', '']
    atomic(store.root/'review.md', '\n'.join(lines).encode())
    store.save('approval.enc', value)
    print(f'预览已保存：{store.root / "review.md"}\n覆盖指纹：{fingerprint}')


def review_new_preview(store, manifest):
    from scripts.local_history.runtime import candidate_files
    from src.pipeline.summary_figures import export_local, PREFIX
    from scripts.local_history.storage import atomic
    progress = store.read('progress.enc')
    lines = ['# 本地新课预览', '', '本批为新课预览，没有覆盖批准或发布入口。', '']
    for target in manifest['targets']:
        tag = target['course_id']+'-'+target['sub_id']
        if progress['lectures'].get(tag, {}).get('status') != 'complete':
            raise ValueError('New-lecture preview is incomplete')
        _, fresh = candidate_files(store, target)
        figure_state = json.loads(next((r['value'] for r in fresh['meta'] if r['key'] == PREFIX+target['sub_id']), '{}'))
        from src.pipeline.summary_review import PREFIX as REVIEW_PREFIX, report
        audit = json.loads(next((r['value'] for r in fresh['meta'] if r['key'] == REVIEW_PREFIX+target['sub_id']), '{}'))
        if audit:
            atomic(store.root/'summary-review.md', report(audit).encode())
        summary = fresh['lecture']['summary']
        if figure_state:
            summary = export_local(summary, figure_state, store.root)
        atomic(store.root/'summary.md', summary.encode())
        lines += [f"## {fresh['lecture']['sub_title']} / {target['sub_id']}", '',
                  f"配图状态：{figure_state.get('status', 'unavailable')}；选用 {len(figure_state.get('figures', []))} 张。", '', summary, '']
    atomic(store.root/'review.md', '\n'.join(lines).encode())
    print(f'新课预览已保存：{store.root / "summary.md"}；未发布、未发邮件。')


def doctor(args):
    packages = ['requests', 'Crypto', 'numpy', 'openai', 'PIL', 'markdown', 'psutil', 'rapidocr_onnxruntime',
                'imagehash', 'cryptography', 'sherpa_onnx', 'mlx', 'mlx_qwen3_asr']
    missing = [p for p in packages if importlib.util.find_spec(p) is None]
    from src.runtime import config
    result = {'python': sys.executable, 'platform': sys.platform, 'missing_packages': missing,
              'ffmpeg': bool(shutil.which('ffmpeg')), 'git': bool(shutil.which('git')), 'gh': bool(shutil.which('gh')),
              'model_exists': args.model.is_dir(), 'vad_exists': args.vad_model.is_file(),
              'campus_credentials': bool(config.STUDENT_ID and config.PASSWORD),
              'database_key': len(os.environ.get('DB_ENCRYPTION_KEY', '')) >= 32,
              'subscribed_courses': len(config.COURSE_IDS),
              'summary_provider': bool(config.resolve_model_providers()),
              'cloud_review': args.cloud_review, 'free_disk_gib': round(shutil.disk_usage(args.run_dir.parent).free/1024**3, 1),
              'review_scope': getattr(args, 'review_scope', 'full'),
              'audio_acquisition': args.audio_mode,
              'campus_logins': 0, 'inference_calls': 0, 'publication': False, 'email': False}
    good = not missing and sys.platform == 'darwin' and all(result[k] for k in
        ('ffmpeg', 'git', 'gh', 'model_exists', 'vad_exists', 'campus_credentials', 'database_key', 'subscribed_courses', 'summary_provider'))
    if args.cloud_review and getattr(args, 'review_scope', 'full') == 'full':
        result['cloud_key'] = bool(config.DOUBAO_ASR_API_KEY)
        result['alignment_dependencies'] = all(importlib.util.find_spec(p) for p in ('torch', 'qwen_asr', 'soundfile'))
        result['alignment_model_exists'] = args.aligner.is_dir() and (args.aligner/'model.safetensors').is_file()
        result['homework_vision_provider'] = any(p['name']=='deepseek' for p in config.resolve_model_providers())
        good = good and all(result[k] for k in ('cloud_key','alignment_dependencies',
                                               'alignment_model_exists','homework_vision_provider'))
    result['mlx_batch_size']=getattr(args,'mlx_batch_size',1)
    if result['mlx_batch_size']==2:
        result['batch_runtime_compatible']=not missing and importlib.metadata.version('mlx-qwen3-asr')=='0.4.4'
        good=good and result['batch_runtime_compatible']
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if good else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description='本机历史笔记重做：plan → run → review → apply；不发邮件')
    parser.add_argument('--env-file', type=Path, action='append', default=[], help='私密 env 文件；可重复')
    parser.add_argument('--key-file', type=Path, help='单行数据库加密密钥文件')
    parser.add_argument('--run-dir', type=Path, default=REPO/'.local-history-refresh/current')
    parser.add_argument('--model', type=Path, default=Path.home()/'Library/Application Support/iCourseQwen/models/qwen3-asr-1.7b-bf16')
    parser.add_argument('--mlx-batch-size',type=int,choices=(1,2),default=1,
                        help='单模型原始块调度；2启用双路解码并保留单路容错')
    parser.add_argument('--vad-model', type=Path, default=REPO/'silero_vad.onnx')
    parser.add_argument('--cloud-review', action='store_true', default=True,
                        help='兼容旧命令；允许识别缺口的有界补救')
    parser.add_argument('--review-scope', choices=('theory', 'full'), default='theory',
                        help='默认只检查摘要理论正确性并保留作业截图；full恢复原逐句定位复核')
    parser.add_argument('--aligner',type=Path,
                        default=Path.home()/'Library/Application Support/iCourseQwen/models/qwen3-forced-aligner-0.6b',
                        help='已准备的本地CPU时间对齐模型，不替换MLX语音识别')
    parser.add_argument('--audio-mode', choices=['aac_auto', 'mp4'], default='aac_auto',
                        help='默认验证 AAC 音轨后解码；mp4 显式使用原获取方式')
    parser.add_argument('--campus-mode',choices=['auto','direct','webvpn'],default='auto',
                        help='运行时先做无凭据校园直连探测；不可用才使用 WebVPN，实际路径保存后续跑保持一致')
    parser.add_argument('--automatic-terms',action='store_true',
                        help='与 main 一致的可选术语学习；只采用更早课次的已核实证据，结果随候选保存')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='只检查环境；不登录、不加载模型')
    plan = commands.add_parser('plan', help='读取现有 data，冻结课次；不调用校园或模型')
    plan.add_argument('--limit', type=int, help='例如 1：先测试一堂')
    plan.add_argument('--new-lecture', help='新课ID；仅本地预览，不生成覆盖批准')
    plan.add_argument('--course-id', help='新课所属课程ID，必须在 COURSE_IDS 中')
    plan.add_argument('--date', help='新课日期 YYYY-MM-DD；运行时再次核对校园目录')
    plan.add_argument('--lecture-id',action='append',help='只重做指定历史课次，可重复；不替换成其他课次')
    execute = commands.add_parser('run', help='真实下载、识别、生成候选笔记；不发布')
    execute.add_argument('--hours', type=float, default=10, help='本次运行预算小时；阶段/块边界合作停止')
    execute.add_argument('--retry-failed', action='store_true', help='重试失败课次，保留完成块和已消耗复核额度')
    execute.add_argument('--allow-active-actions', action='store_true',
                         help='明确允许本次本地登录与正在运行的相关 Actions 并行；不取消它们')
    commands.add_parser('status', help='查看本地进度')
    preview = commands.add_parser('review', help='生成原/新笔记对照和覆盖指纹')
    preview.add_argument('--completed-only', action='store_true')
    publish = commands.add_parser('apply', help='明确覆盖正式 data；先备份、再检查、一次推送')
    publish.add_argument('--approval', required=True, help='review 输出的完整覆盖指纹')
    args = parser.parse_args(argv)
    os.umask(0o077)
    settings_path = REPO/'.local-history-refresh/settings.json'
    if settings_path.is_file():
        if settings_path.is_symlink() or settings_path.stat().st_mode & 0o077:
            raise ValueError('Local settings must be private (chmod 600)')
        settings = json.loads(settings_path.read_text())
        args.env_file = [Path(p) for p in settings.get('env_files', [])]+args.env_file
        if args.key_file is None and settings.get('key_file'):
            args.key_file = Path(settings['key_file'])
        if args.vad_model == REPO/'silero_vad.onnx' and settings.get('vad_model'):
            args.vad_model = Path(settings['vad_model'])
    for path in args.env_file:
        load_env(path)
    if args.key_file:
        if args.key_file.is_symlink() or args.key_file.stat().st_mode & 0o077:
            raise ValueError('Key file must be private (chmod 600)')
        os.environ['DB_ENCRYPTION_KEY'] = args.key_file.read_text().strip()
    for old, new in [('STUID', 'StuId'), ('UISPSW', 'UISPsw')]:
        if old in os.environ:
            os.environ[new] = os.environ[old]
    os.environ.update(AUTO_COURSE_TERMS='true' if args.automatic_terms else 'false', PUBLISH_RESULTS='false', SEND_EMAIL='false',
                      DATA_DIR=str(args.run_dir/'scratch'), SILERO_VAD_PATH=str(args.vad_model),
                      HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                      AUDIO_ACQUISITION=args.audio_mode,
                      IMAGE_WORKERS='4', OCR_MAX_WORKERS='2', OCR_MAX_TARGET='2', VIDEO_DOWNLOAD_CONCURRENCY='1')
    if not os.environ.get('GITHUB_REPOSITORY'):
        from scripts.production_db import command
        origin = command(['git', 'remote', 'get-url', 'origin'], cwd=REPO).decode().strip()
        match = re.fullmatch(r'(?:https://github.com/|git@github.com:)([\w.-]+/[\w.-]+?)(?:\.git)?', origin)
        if not match:
            raise ValueError('Set GITHUB_REPOSITORY explicitly')
        os.environ['GITHUB_REPOSITORY'] = match[1]
    from scripts.local_history.storage import Store, private_dir
    private_dir(args.run_dir.parent)
    if args.command == 'doctor':
        return doctor(args)
    if args.command == 'plan' and args.limit is not None and args.limit < 1:
        raise ValueError('--limit must be positive')
    if args.command == 'run' and (not math.isfinite(args.hours) or not 0 < args.hours <= 48):
        raise ValueError('--hours must be greater than 0 and at most 48')
    store = Store(args.run_dir, os.environ.get('DB_ENCRYPTION_KEY', ''))
    if args.command == 'status':
        # Checkpoints are atomically replaced; readers need no writer lock.
        manifest = store.read('manifest.enc')
        if manifest['repository'] != os.environ['GITHUB_REPOSITORY']:
            raise ValueError('Frozen plan belongs to a different repository')
        return status(store, manifest) or 0
    with store.lock():
        if args.command == 'plan':
            make_plan(store, args); return 0
        manifest = store.read('manifest.enc')
        if manifest['repository'] != os.environ['GITHUB_REPOSITORY']:
            raise ValueError('Frozen plan belongs to a different repository')
        if args.command == 'run':
            return run(store, manifest, args) or 0
        if args.command == 'review':
            return review(store, manifest, args.completed_only) or 0
        if any(t.get('preview_only') for t in manifest['targets']):
            raise ValueError('New-lecture previews cannot publish through historical apply')
        from scripts.local_history.publication import apply
        revision = apply(store, manifest, store.read('approval.enc'), args.approval)
        progress = store.read('progress.enc'); progress.update(status='published', published_revision=revision)
        store.save('progress.enc', progress)
        print('已覆盖经预览确认的课次；data='+revision+'；未发邮件。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('已停止；保留本地检查点。', file=sys.stderr); sys.exit(130)
    except Exception as error:
        # Never echo an upstream credential-bearing request/response exception.
        safe = str(error) if type(error) in (ValueError, RuntimeError) else type(error).__name__
        print('入口停止：'+safe, file=sys.stderr); sys.exit(1)
