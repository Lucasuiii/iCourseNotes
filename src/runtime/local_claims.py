"""Claims that a Mac is recognising a lecture locally, so the daily cloud run skips it.

The macOS app writes ``local_claim:<course_id>:<sub_id>`` into the encrypted
data ``meta`` table when it starts a lecture. A claim expires on its own, so an
abandoned local run never blocks the cloud for long. Claims carry no lecture
content, only timestamps.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

PREFIX = 'local_claim:'
CLAIM_HOURS = 48


def key(course_id, sub_id) -> str:
    return f'{PREFIX}{course_id}:{sub_id}'


def make(course_id, sub_id, *, now=None, hours=CLAIM_HOURS) -> tuple[str, str]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    value = {'claimed_at': now.isoformat(), 'expires_at': (now + timedelta(hours=hours)).isoformat(),
             'source': 'local-app'}
    return key(course_id, sub_id), json.dumps(value, sort_keys=True)


def is_active(value, *, now=None) -> bool:
    """True only for a well-formed claim whose expiry is still ahead."""
    if not value:
        return False
    try:
        expires = datetime.fromisoformat(json.loads(value)['expires_at'])
    except (ValueError, TypeError, KeyError):
        return False
    if expires.tzinfo is None:
        return False
    return expires > (now or datetime.now(timezone.utc))


def newer(candidate, current) -> bool:
    """Merge rule: the claim made later wins; malformed values never replace a valid one."""
    def stamp(value):
        try:
            return datetime.fromisoformat(json.loads(value)['claimed_at'])
        except (ValueError, TypeError, KeyError):
            return None
    new, old = stamp(candidate), stamp(current) if current else None
    if new is None or new.tzinfo is None:
        return False
    return old is None or old.tzinfo is None or new > old


def without_claimed(tasks, lookup, *, now=None):
    """Drop queue tasks (course, title, lecture) that a local run has claimed."""
    kept, skipped = [], 0
    for task in tasks:
        if is_active(lookup(key(task[0], task[2]['sub_id'])), now=now):
            skipped += 1
        else:
            kept.append(task)
    return kept, skipped
