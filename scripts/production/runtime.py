"""Formal pilot queue -> bounded ASR jobs -> LectureRunner -> scoped publication.

Private payloads are authenticated to run/task/stage. No baseline or test slots.
The CLI emits counts and exception types only, never classroom content.
"""
from __future__ import annotations
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from scripts import sharded_qwen_pilot as shards
from src.pipeline.qwen_plan import build_audio_plan, validate_plan, validate_result, fingerprint, MAX_TASKS
from scripts.parallel_courses import configured_courses
from scripts.production_db import load_remote, snapshot, lecture_snapshot, merge_lecture, publish
from src.data.database import Database
from src.data.checkpoint_database import CheckpointDatabase

from src.runtime.audio_preparation import (PREPARE_STREAM_TIMEOUT, PREPARE_IDLE_TIMEOUT,
    collect_decode_diagnostics, validate_prepared_audio)
_POOL_RUNS = set()


def root():
    return shards.root()


def out(name):
    directory = root()/'out'
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory/name


def decode(path, role):
    return shards.unseal(Path(path), role, slot=0 if role == 'queue' else None)


def encode(files, role, path):
    shards.seal(files, role, Path(path), slot=0 if role == 'queue' else None)
    if role == 'prepared' and os.environ.get('POOL_ARTIFACTS') == 'true':
        spec = read_json(files['specification.json'])
        thin = {'mode': spec['mode'], 'plan': spec.get('plan', {})}
        shards.seal({'specification.json': shards.encoded(thin)}, 'worker-plan', out('worker-plan.enc'))
        # Workers need original blocks, not another whole audio/database/PPT copy.
        worker_files = {n: b for n, b in files.items()
                        if n == 'specification.json' or re.fullmatch(r'chunk-[0-9]+\.flac|completed-[0-9]+\.json', n)}
        shards.seal(worker_files, 'prepared', out('worker-input')/'prepared.enc')


def read_json(blob):
    return json.loads(blob)


def encode_audio_chunks(source, plan, files, destination):
    from scripts.production import preparation
    return preparation.encode_audio_chunks(sys.modules[__name__], source, plan, files, destination)


def failure_code(error):
    # Whitelisted internal reasons only; provider/media exceptions may contain
    # private URLs or credentials and must never be printed verbatim.
    from scripts.coordination_transport import CoordinationError
    if isinstance(error, CoordinationError): return 'coordination_failure'
    reasons = {
        'Qwen chunk reached its token budget': 'qwen_token_budget',
        'Qwen recognition incomplete; missing audio intervals': 'qwen_missing_intervals',
        'Incomplete Qwen audio block': 'qwen_incomplete_block',
        'Shared worker time budget reached': 'worker_deadline',
        'Qwen shard timeout': 'worker_deadline',
        'Queue conflict retry budget exhausted': 'queue_conflicts',
        'A later finalization lost its quota checkpoint; automatic refund forbidden': 'quota_checkpoint_lost',
        'Prior finalization has no quota checkpoint; fresh review forbidden': 'quota_checkpoint_missing',
        'Prior finalization quota is unknown; fresh review forbidden': 'quota_checkpoint_missing',
        'Recovery artifact is expired or ambiguous': 'recovery_expired',
        'Required recovery artifact is absent': 'recovery_missing',
        'No playable production audio': 'audio_startup_failed',
        'Production audio is incomplete': 'incomplete_audio',
        'Production AAC packet coverage is incomplete': 'incomplete_audio',
        'Production AAC timeline is incomplete': 'incomplete_audio',
        'Production audio has read or decode errors': 'audio_decode_errors',
        'Production audio diagnostics are incomplete': 'audio_diagnostics_incomplete',
        'Production audio has invalid sample metadata': 'audio_sample_metadata_invalid',
        'Subscribed course enumeration incomplete': 'course_enumeration_failed',
        'Audio preparation deadline exceeded': 'preparation_deadline',
        'Audio preparation stalled': 'preparation_stalled',
        'Encrypted bundle too large': 'bundle_size_limit',
        'Prepared bundle exceeds total size limit': 'bundle_size_limit',
        'Prepared bundle has too many files': 'bundle_file_limit',
        'Retained preparation failed; explicit repair required': 'retained_preparation_failed',
        'Isolated validation produced no transcript or summary': 'validation_empty_output',
        'All planned ASR shards must finish before finalization': 'incomplete_shards',
        'Shard incomplete; summary forbidden': 'incomplete_shards',
        'Publication conflict retry budget exhausted; encrypted delta retained': 'publication_conflicts',
    }
    return reasons.get(str(error), 'missing_stage_input' if isinstance(error, FileNotFoundError) else 'stage_failure')


def artifact(name, target, *, run=None, required=False, _direct=False):
    """API failure or expiry is not equivalent to a confirmed absent checkpoint."""
    run = run or os.environ['GITHUB_RUN_ID']
    repo = os.environ['GITHUB_REPOSITORY']
    from scripts.coordination_transport import read_json as coordination_json
    pages = coordination_json(lambda: subprocess.check_output(['gh', 'api', '--paginate', '--slurp',
        f'repos/{repo}/actions/runs/{run}/artifacts?per_page=100'], stderr=subprocess.PIPE, timeout=120))
    if any(a['name'] == 'qwen-production-pool-audit' for page in pages for a in page['artifacts']):
        _POOL_RUNS.add(str(run))
    found = [a for page in pages for a in page['artifacts'] if a['name'] == name]
    if not found:
        pooled = os.environ.get('POOL_ARTIFACTS') == 'true' or any(a['name'] == 'qwen-production-pool-audit'
                            for page in pages for a in page['artifacts'])
        if pooled and not _direct and name not in ('qwen-production-queue', 'qwen-production-pool-audit'):
            from scripts.production_pool import artifact_runs
            for source in artifact_runs(str(run), name, allow_legacy=str(run) != os.environ['GITHUB_RUN_ID'] and str(run) not in _POOL_RUNS):
                if artifact(name, target, run=source, _direct=True): return True
        if required: raise ValueError('Required recovery artifact is absent')
        return False
    if len(found) != 1 or found[0]['expired']:
        raise ValueError('Recovery artifact is expired or ambiguous')
    shards.command(['gh', 'run', 'download', run, '--repo', repo, '--name', name, '--dir', str(target)])
    return True


def write_outputs(**values):
    shards.outputs(**values)


def asr_cost(material):
    import math
    from src.ai.qwen_transcriber import MODEL, REVISION
    rows = material.get('full_chunks', [])
    known = [r for r in rows if isinstance(r.get('decode_seconds'), (int, float))
             and math.isfinite(r['decode_seconds']) and r['decode_seconds'] > 0]
    if len(known) < 5: return None
    audio = sum(r['end']-r['start'] for r in known)
    seconds = sum(r['decode_seconds'] for r in known)
    if audio <= 0 or not math.isfinite(seconds): return None
    return {'model': MODEL, 'revision': REVISION, 'audio_seconds': audio,
            'decode_seconds': seconds, 'blocks': len(known)}


def historical_asr_cost(db, course, date):
    from scripts.production import recovery
    return recovery.historical_asr_cost(sys.modules[__name__], db, course, date)


def last_finalization_attempt(run, slot, *, prior_only=False):
    from scripts.production import recovery
    return recovery.last_finalization_attempt(sys.modules[__name__], run, slot, prior_only=prior_only)


def validate_checkpoint_age(saved, run, slot, *, prior_only=False):
    from scripts.production import recovery
    return recovery.validate_checkpoint_age(sys.modules[__name__], saved, run, slot, prior_only=prior_only)


def validation_course():
    from scripts.production import planning
    return planning.validation_course(sys.modules[__name__])


def validation_rank():
    from scripts.production import planning
    return planning.validation_rank(sys.modules[__name__])


def validation_ranks():
    from scripts.production import planning
    return planning.validation_ranks(sys.modules[__name__])


def validation_before_date():
    from scripts.production import planning
    return planning.validation_before_date(sys.modules[__name__])


def validation_on_date():
    from scripts.production import planning
    return planning.validation_on_date(sys.modules[__name__])


def validation_source_queue(source):
    from scripts.production import planning
    return planning.validation_source_queue(sys.modules[__name__], source)


def validation_selection_queue(source):
    from scripts.production import planning
    return planning.validation_selection_queue(sys.modules[__name__], source)


def validate_frozen_terms(frozen, course, lecture):
    from scripts.production import planning
    return planning.validate_frozen_terms(sys.modules[__name__], frozen, course, lecture)


def latest_validation_task(client, db, course, *, today=None, rank=1, before_date='', on_date=''):
    from scripts.production import planning
    return planning.latest_validation_task(sys.modules[__name__], client, db, course, today=today, rank=rank, before_date=before_date, on_date=on_date)


def plan():
    from scripts.production import planning
    return planning.plan(sys.modules[__name__])


def task_files():
    from scripts.production import preparation
    return preparation.task_files(sys.modules[__name__])


def read_preparation():
    from scripts.production import preparation
    return preparation.read_preparation(sys.modules[__name__])


def shared_local_checkpoints(plan):
    from scripts.production import recognition
    return recognition.shared_local_checkpoints(sys.modules[__name__], plan)


def shared_results(plan, *, require_complete=True):
    from scripts.production import recognition
    return recognition.shared_results(sys.modules[__name__], plan, require_complete=require_complete)


def freeze_course_terms(db, course, title, sub_id):
    from scripts.production import preparation
    return preparation.freeze_course_terms(sys.modules[__name__], db, course, title, sub_id)


def recover_preparation(db, course, sub_id):
    from scripts.production import preparation
    return preparation.recover_preparation(sys.modules[__name__], db, course, sub_id)


def retain_prepared_audio(handle, specification, files):
    from scripts.production import preparation
    return preparation.retain_prepared_audio(sys.modules[__name__], handle, specification, files)


def prepare_audio_stream(transcriber, handle, specification):
    from scripts.production import preparation
    return preparation.prepare_audio_stream(sys.modules[__name__], transcriber, handle, specification)


def preparation_failure_audit(specification, files, error, *, saved=False, secondary=(), fallback=False, full_counts=None):
    from scripts.production import preparation
    return preparation.preparation_failure_audit(sys.modules[__name__], specification, files, error, saved=saved, secondary=secondary, fallback=fallback, full_counts=full_counts)


def preserve_preparation_failure(db, sub_id, specification, files, handle, error):
    from scripts.production import preparation
    return preparation.preserve_preparation_failure(sys.modules[__name__], db, sub_id, specification, files, handle, error)


def prepare():
    from scripts.production import preparation
    return preparation.prepare(sys.modules[__name__])


def worker():
    from scripts.production import recognition
    return recognition.worker(sys.modules[__name__])


def gather():
    from scripts.production import finalization
    return finalization.gather(sys.modules[__name__])


def publish_result():
    from scripts.production import publishing
    return publishing.publish_result(sys.modules[__name__])


def deliver():
    from scripts.production import publishing
    return publishing.deliver(sys.modules[__name__])


def persist_pool_failures():
    from scripts.production import publishing
    return publishing.persist_pool_failures(sys.modules[__name__])


def finalize():
    from scripts.production import publishing
    return publishing.finalize(sys.modules[__name__])


def main():
    os.environ['QWEN_PRODUCTION_TASK'] = 'true'
    mode = sys.argv[1]
    if mode == 'clean':
        shutil.rmtree(root(), ignore_errors=True); return
    # Existing components log private course names. Keep their output inside
    # the process; only the enclosing workflow gets sanitized completion info.
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            {'plan': plan, 'prepare': prepare, 'worker': worker, 'gather': gather,
             'publish': publish_result, 'deliver': deliver, 'finalize': finalize}[mode]()
    except Exception as error:
        from scripts.coordination_transport import diagnostic
        audit = {'mode': mode if mode in ('plan', 'prepare', 'worker', 'gather', 'publish', 'deliver', 'finalize') else 'unknown',
                 'error_code': failure_code(error)}
        if diagnostic(error): audit['coordination'] = diagnostic(error)
        if isinstance(getattr(error, 'auth_failure_diagnostics', None), dict):
            from src.api.webvpn import authentication_failure
            audit['authentication'] = authentication_failure(error)
            audit['error_code'] = audit['authentication']['failure']
        try: out('pipeline-failure.json').write_bytes(shards.encoded(audit))
        except Exception: pass  # Preserve the processing exception on a full disk.
        raise
    print(f'Production pilot {mode} completed', flush=True)


def cli():
    try: main()
    except Exception as error:
        if len(sys.argv) > 1 and sys.argv[1] == 'prepare':
            try:
                if not out('prepare-failure.json').exists():
                    preparation_failure_audit({}, {}, error)
            except Exception:
                pass
        print(f'Production pilot failed ({type(error).__name__}, {failure_code(error)}); private details withheld', flush=True)
        raise SystemExit(1)
