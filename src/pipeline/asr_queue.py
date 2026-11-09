"""Immutable audio plan, atomic block claims and durable completed results.

No wall-clock lease expiry: a slow decode cannot be stolen. New Actions attempts
may reclaim old unfinished claims only after the prior attempt has ended.
"""
import copy
import math
import random
import time
import uuid
from src.pipeline.qwen_plan import fingerprint, validate_plan, validate_result, validate_block_row, incomplete_row


def initial_queue(plan, completed=()):
    validate_plan(plan)
    rows = {}
    for result in completed:
        validate_result(plan, result, result['shard_id'])
        for row in result['chunks']:
            if str(row['chunk_id']) in rows:
                raise ValueError('Duplicate seeded queue block')
            rows[str(row['chunk_id'])] = {'status': 'failed' if incomplete_row(row) else 'complete', 'result': row}
    state = {'schema': 1, 'plan_hash': fingerprint(plan), 'revision': 0,
             'blocks': {str(b['chunk_id']): rows.get(str(b['chunk_id']), {'status': 'pending'})
                        for b in plan['blocks']}}
    validate_queue(plan, state)
    return state


def validate_queue(plan, state):
    validate_plan(plan)
    if (not isinstance(state, dict) or not isinstance(state.get('blocks'), dict)
            or state.get('schema') != 1 or state.get('plan_hash') != fingerprint(plan)
            or type(state.get('revision')) is not int or state['revision'] < 0
            or set(state.get('blocks', {})) != {str(b['chunk_id']) for b in plan['blocks']}):
        raise ValueError('Queue belongs to another immutable plan')
    for block in plan['blocks']:
        record = state['blocks'][str(block['chunk_id'])]
        if not isinstance(record, dict):
            raise ValueError('Invalid queue block')
        if record.get('status') in ('complete','failed'):
            row = record.get('result', {})
            validate_block_row(block,row)
            if (record['status'] == 'failed') != incomplete_row(row):
                raise ValueError('Queue status disagrees with recognition completeness')
            if (not isinstance(row, dict) or row.get('chunk_id') != block['chunk_id'] or type(row.get('chunk_id')) is not int
                    or row.get('start') != block['start'] or row.get('end') != block['end']
                    or not isinstance(row.get('text'), str)
                    or not isinstance(row.get('decode_seconds', 0), (int, float))
                    or not math.isfinite(row.get('decode_seconds', 0)) or row.get('decode_seconds', 0) < 0):
                raise ValueError('Completed queue block changed its identity')
        elif record.get('status') == 'claimed':
            if (not isinstance(record.get('owner'), str) or not record['owner']
                    or type(record.get('attempt')) is not int or record['attempt'] <= 0
                    or not isinstance(record.get('token'), str) or len(record['token']) != 32):
                raise ValueError('Invalid queue claim')
        elif record.get('status') != 'pending':
            raise ValueError('Invalid queue status')


# Several Runners push one git ref; a lost compare-and-swap is retried after a
# randomized, growing pause so concurrent writers spread out instead of colliding
# again immediately (up to about 8 minutes in all before giving up).
CONFLICT_ATTEMPTS = 40
CONFLICT_PAUSE_CAP = 20.0


class SharedQueue:
    def __init__(self, plan, store, *, sleep=time.sleep, jitter=random.uniform):
        self.plan, self.store = plan, store
        self._sleep, self._jitter = sleep, jitter

    def change(self, operation):
        for attempt in range(CONFLICT_ATTEMPTS):
            if attempt:
                self._sleep(min(CONFLICT_PAUSE_CAP, 0.5*2**min(attempt, 6)) * self._jitter(0.5, 1.0))
            version, state = self.store.read()
            if state is None:
                raise ValueError('Queue checkpoint missing; cannot reset it in a worker')
            validate_queue(self.plan, state)
            state = copy.deepcopy(state)
            result, changed = operation(state)
            if not changed:
                return result
            state['revision'] += 1
            validate_queue(self.plan, state)
            if self.store.compare_and_swap(version, state):
                return result
        raise RuntimeError('Queue conflict retry budget exhausted')

    def claim(self, owner, attempt):
        if not isinstance(owner, str) or not owner or type(attempt) is not int or attempt <= 0:
            raise ValueError('Invalid worker identity')
        def mutate(state):
            available = [b for b in self.plan['blocks'] if
                         state['blocks'][str(b['chunk_id'])]['status'] == 'pending' or
                         (state['blocks'][str(b['chunk_id'])]['status'] == 'claimed'
                          and state['blocks'][str(b['chunk_id'])]['attempt'] < attempt)]
            if not available:
                return None, False
            block = min(available, key=lambda b: (-b['samples'], b['chunk_id']))
            token = uuid.uuid4().hex
            state['blocks'][str(block['chunk_id'])] = {
                'status': 'claimed', 'owner': owner, 'attempt': attempt, 'token': token}
            return (block, token), True
        return self.change(mutate)

    def finish(self, block_id, token, row):
        def mutate(state):
            record = state['blocks'][str(block_id)]
            if record['status'] in ('complete','failed'):
                if record['result'] == row:
                    return None, False  # Lost successful CAS response; exact idempotence.
                raise ValueError('Completed result cannot be overwritten')
            if record['status'] != 'claimed' or record['token'] != token:
                raise ValueError('Stale worker claim cannot publish a result')
            state['blocks'][str(block_id)] = {'status': 'failed' if incomplete_row(row) else 'complete', 'result': row}
            return None, True
        self.change(mutate)

    def release(self, block_id, token):
        def mutate(state):
            record = state['blocks'][str(block_id)]
            if record['status'] == 'claimed' and record['token'] == token:
                state['blocks'][str(block_id)] = {'status': 'pending'}
                return None, True
            return None, False
        self.change(mutate)

    def restore_rows(self, rows, attempt):
        """Reuse decoded local checkpoints if coordination transport failed."""
        def mutate(state):
            changed = False
            seen = set()
            for row in rows:
                n = row.get('chunk_id')
                if type(n) is not int or str(n) not in state['blocks'] or n in seen:
                    raise ValueError('Invalid dynamic local checkpoint')
                seen.add(n)
                record = state['blocks'][str(n)]
                if record['status'] in ('complete','failed'):
                    if record['result'] != row:
                        raise ValueError('Local result conflicts with committed result')
                elif record['status'] == 'claimed' and record['attempt'] >= attempt:
                    raise ValueError('Local checkpoint cannot override an active claim')
                else:
                    state['blocks'][str(n)] = {'status': 'failed' if incomplete_row(row) else 'complete', 'result': row}
                    changed = True
            return None, changed
        self.change(mutate)

    def snapshot(self):
        _, state = self.store.read()
        validate_queue(self.plan, state)
        return state

    def results(self, *, require_complete=True):
        state = self.snapshot()
        reports = []
        # Preserve existing static shard validation as an assembly adapter only;
        # those audit groups do not restrict which worker may claim each block.
        for shard in self.plan['shards']:
            rows = [state['blocks'][str(i)]['result'] for i in shard['chunk_ids']
                    if state['blocks'][str(i)]['status'] in ('complete','failed')]
            report = {'plan_hash': fingerprint(self.plan), 'shard_id': shard['shard_id'],
                      'complete': len(rows) == len(shard['chunk_ids']) and not any(incomplete_row(r) for r in rows), 'chunks': rows,
                      'seconds': sum(r.get('decode_seconds', 0) for r in rows), 'attempts': [],
                      'execution': 'shared_queue', 'queue_revision': state['revision']}
            validate_result(self.plan, report, shard['shard_id'], require_complete=require_complete)
            reports.append(report)
        return reports
