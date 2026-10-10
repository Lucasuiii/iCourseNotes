"""Public, read-only progress: opaque slots, queue counts and verified run IDs.

Never serialize the private journal or exception text. Display failures must
not change dispatch, ownership, retry or classroom-completeness decisions.
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

COUNTS = ('total_blocks', 'completed_blocks', 'failed_blocks', 'pending_blocks',
          'claimed_blocks', 'remaining_blocks')
STAGES = {'new': '等待调度', 'prepare': '完整获取与音频校验', 'asr': '语音识别',
          'gather': '汇总与复核', 'publish': '结果校验', 'done': '完成', 'failed': '失败'}
STATUSES = {'reserved': '等待登记', 'queued': '排队', 'in_progress': '运行中',
            'waiting': '等待', 'pending': '排队', 'requested': '排队',
            'completed': '已结束'}
CONCLUSIONS = {'success', 'failure', 'cancelled', 'skipped', 'timed_out',
               'action_required', 'startup_failure', 'stale', 'neutral'}
LABELS = {'running': '进行中', 'running_with_failures': '进行中（部分课次失败）',
          'running_incomplete': '进行中（已有缺失块，整堂不完整）',
          'success': '完成', 'success_with_gaps': '完成（保留未识别片段）', 'failed': '失败', 'incomplete': '不完整',
          'controller_stopped': '控制器停止；子任务状态需核验'}


class PoolProgress:
    def __init__(self, output, *, repository='', summary=None, emit=print, clock=time.monotonic):
        self.output = Path(output)
        self.summary = Path(summary) if summary else None
        self.repository = repository if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) else ''
        self.emit = emit; self.clock = clock; self.began = clock()
        self.counts = {}; self.fingerprint = None; self.last_emit = float('-inf')

    def snapshot(self, state, works, *, final=False, error=False):
        for slot, work in works.items():
            # Copy only fixed numeric fields, never text, plans or private IDs.
            self.counts[slot] = {k: work[k] for k in COUNTS
                                if type(work.get(k)) is int and work[k] >= 0}
        courses = []
        for raw, course in state.get('courses', {}).items():
            slot = int(raw); counts = self.counts.get(slot, {})
            phase = course['phase']
            label = STAGES[phase]
            if phase == 'publish':
                label = '发布结果' if state['flags']['PUBLISH_RESULTS'] == 'true' else '隔离结果校验'
            if phase == 'failed' and counts.get('failed_blocks', 0): label = '不完整（存在缺失块）'
            if phase == 'done' and counts.get('failed_blocks', 0): label = '完成（保留未识别片段）'
            # Keep the latest ticket per stage/worker, including ended runs.
            latest = {}
            for t in state.get('tickets', []):
                if t['slot'] == slot: latest[(t['stage'], t['worker'])] = t
            children = []
            for t in latest.values():
                run = str(t.get('run') or '')
                child = {'stage': t['stage'], 'worker': t['worker'],
                         'status': t['status'],
                         'conclusion': t.get('conclusion') if t.get('conclusion') in CONCLUSIONS else None}
                if re.fullmatch(r'[0-9]+', run):
                    child['run'] = run
                    if self.repository:
                        child['url'] = f'https://github.com/{self.repository}/actions/runs/{run}'
                children.append(child)
            workers = [t for t in state.get('tickets', []) if t['slot'] == slot
                       and t['stage'] == 'asr' and t['status'] != 'completed']
            courses.append({'slot': slot, 'phase': phase, 'label': label, **counts,
                            'workers_running': sum(t['status'] == 'in_progress' for t in workers),
                            'workers_waiting': sum(t['status'] != 'in_progress' for t in workers),
                            'children': children})
        phases = [c['phase'] for c in courses]
        incomplete = any(c.get('failed_blocks', 0) and c['phase'] != 'done' for c in courses)
        retained_gaps = any(c.get('failed_blocks', 0) and c['phase'] == 'done' for c in courses)
        active = any(p not in ('done', 'failed') for p in phases)
        status = ('controller_stopped' if final and error and active
                  else 'running_incomplete' if incomplete and active
                  else 'running_with_failures' if 'failed' in phases and active
                  else 'incomplete' if incomplete
                  else 'failed' if 'failed' in phases
                  else 'controller_stopped' if final and error
                  else 'success_with_gaps' if retained_gaps and all(p == 'done' for p in phases)
                  else 'success' if courses and all(p == 'done' for p in phases) else 'running')
        run = str(state['run_id'])
        return {'schema': 1, 'parent_run_id': run if re.fullmatch('[0-9]+', run) else None,
                'status': status, 'final': final,
                'elapsed_seconds': max(0, int(self.clock()-self.began)), 'courses': courses}

    @staticmethod
    def block_text(course):
        if 'total_blocks' not in course:
            if course['phase'] == 'failed':
                children = course.get('children', [])
                return ('音频准备失败；未进入分块识别' if children and all(c['stage'] == 'prepare' for c in children)
                        else '暂无分块计数')
            return '块数待音频准备完成后确定'
        return (f"成功 {course.get('completed_blocks', 0)}/{course['total_blocks']}，"
                f"缺失 {course.get('failed_blocks', 0)}，"
                f"待领取 {course.get('pending_blocks', 0)}，"
                f"识别中 {course.get('claimed_blocks', 0)}")

    def markdown(self, snapshot):
        lines = [f"## 课堂并发进度：{LABELS[snapshot['status']]}", '',
                 '实时进度请展开“实时课堂进度”步骤日志；此摘要在步骤结束后显示。', '',
                 '| 课次 | 阶段 | 音频块 | Worker 运行 / 等待 |',
                 '| --- | --- | --- | --- |']
        for c in snapshot['courses']:
            lines.append(f"| {c['slot']+1} | {c['label']} | {self.block_text(c)} | "
                         f"{c['workers_running']} / {c['workers_waiting']} |")
        lines += ['', '成功块数仅统计完整识别；缺失块不会计为成功。没有预计完成时间。', '']
        for c in snapshot['courses']:
            links = []
            for child in c['children']:
                label = STAGES[child['stage']]
                if child['stage'] == 'asr': label += f" Worker {child['worker']+1}"
                label += '：'+STATUSES[child['status']]
                if child['conclusion']: label += ' / '+child['conclusion']
                links.append(f"[{label}]({child['url']})" if 'url' in child else label)
            if links: lines.append(f"- 课次 {c['slot']+1}："+' · '.join(links))
        return '\n'.join(lines)+'\n'

    def update(self, state, works, *, final=False, error=False):
        snapshot = self.snapshot(state, works, final=final, error=error)
        fingerprint = json.dumps({k: v for k, v in snapshot.items() if k != 'elapsed_seconds'}, sort_keys=True)
        now = self.clock()
        # Emit changes immediately; a quiet queue still gets a two-minute heartbeat.
        if fingerprint != self.fingerprint or now-self.last_emit >= 120 or final:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2)+'\n')
            lines = [f"课堂进度 · 已运行 {snapshot['elapsed_seconds']//60} 分钟 · {LABELS[snapshot['status']]}"]
            for c in snapshot['courses']:
                lines.append(f"课次 {c['slot']+1} | {c['label']} | {self.block_text(c)} | "
                             f"Worker 运行 {c['workers_running']} / 等待 {c['workers_waiting']}")
                for child in c['children']:
                    if 'url' in child:
                        worker = f" Worker {child['worker']+1}" if child['stage'] == 'asr' else ''
                        lines.append(f"  {STAGES[child['stage']]}{worker}: {STATUSES[child['status']]} "
                                     f"{child.get('conclusion') or ''} {child['url']}")
            self.emit('\n'.join(lines), flush=True)
            self.fingerprint = fingerprint; self.last_emit = now
        if final and self.summary:
            with self.summary.open('a') as handle: handle.write(self.markdown(snapshot))


def show_progress(progress, state, works, **kwargs):
    """Display is best effort; never hide the original pipeline error."""
    if progress is None: return
    try: progress.update(state, works, **kwargs)
    except Exception:
        if not getattr(progress, 'warned', False):
            progress.warned = True
            print('进度显示暂不可用；调度与检查点继续按原流程处理。', flush=True)
