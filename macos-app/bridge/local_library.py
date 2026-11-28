"""Persistent local recording notes, independent of the replaceable data cache."""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import time
from datetime import date

from PIL import Image

SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
TERM = re.compile(r'^\d{4}-\d{4}(1|2|暑期)$')
IMAGE = re.compile(r'!\[([^\n]*)\]\((figures/[^)\n]+)\)')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_index(storage):
    return storage.read('index.enc') if storage.exists('index.enc') else {'schema': 1, 'collections': {}}


def lectures(storage):
    return [row for collection in read_index(storage)['collections'].values()
            for row in collection['lectures']]


def note(storage, sub_id):
    with storage.lock():
        for collection in read_index(storage)['collections'].values():
            if any(row['sub_id'] == sub_id for row in collection['lectures']):
                return storage.read(collection['bundle'])['notes'][sub_id]
    raise ValueError('ImportedLectureNotFound')


def import_manifest(storage, manifest_path, term_label):
    manifest = json.loads(Path(manifest_path).read_text())
    ident = manifest['collection_id']
    if manifest.get('schema') != 1 or not SLUG.fullmatch(ident) or not TERM.fullmatch(manifest['term']):
        raise ValueError('InvalidLocalCollection')
    course_title = manifest['course_title'].strip()
    if not course_title:
        raise ValueError('MissingCourseTitle')
    root = Path(manifest['root']).resolve(strict=True)
    rows, notes = [], {}
    entries = manifest['lectures']
    if not entries or [item['order'] for item in entries] != list(range(1, len(entries) + 1)):
        raise ValueError('NonContiguousLectureOrder')
    file_ids = [item['file_id'] for item in entries]
    if len(set(file_ids)) != len(file_ids) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', x) for x in file_ids):
        raise ValueError('InvalidLocalLectureIdentity')
    for item in entries:
        folder = (root / item['folder']).resolve(strict=True)
        folder.relative_to(root)

        def read_file(name):
            path = (folder / name).resolve(strict=True)
            path.relative_to(root)
            return path.read_bytes()

        summary_raw = read_file('summary.md')
        summary = summary_raw.decode()
        review = json.loads(read_file('content-review.json'))
        if (review.get('status') != 'locally_reviewed' or review.get('missing_intervals')
                or review.get('blocks') != review.get('planned_blocks')
                or digest(summary_raw) != review.get('summary_sha256')):
            raise ValueError('UnreviewedLocalNote')
        expected = {Path(f['path']).name: f for f in review['figures']}
        figures = []

        def image_reference(match):
            raw = read_file(match[2])
            witness = expected.get(Path(match[2]).name)
            if not witness or digest(raw) != witness['sha256']:
                raise ValueError('ReviewedFigureChanged')
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != 'JPEG':
                    raise ValueError('UnsupportedLocalFigure')
                image.verify()
            sha = digest(raw)
            figures.append({'id': sha, 'mime': 'image/jpeg', 'data': base64.b64encode(raw).decode(),
                            'caption': match[1], 'seconds': witness['seconds']})
            return f'![{match[1]}](#icourse-figure-{sha})'

        summary = IMAGE.sub(image_reference, summary)
        if len(figures) != len(expected) or len({f['id'] for f in figures}) != len(figures):
            raise ValueError('FigureReferenceMismatch')
        sid = f'archive-{ident}-{item["file_id"]}'
        lecture_date = item.get('date', '')
        date_verified = item.get('date_verified') is True
        if lecture_date:
            date.fromisoformat(lecture_date)
            if not date_verified or not item.get('date_evidence'):
                raise ValueError('UnverifiedLectureDate')
        display_title = item.get('display_title')
        if display_title is None:
            display_title = item['title'] if lecture_date else f'第{item["order"]:02d}段 · {item["title"]}'
        if not isinstance(display_title, str) or not display_title.strip():
            raise ValueError('MissingLectureDisplayTitle')
        payload = {
            'sub_id': sid, 'course_id': 'archive-' + ident, 'course_title': course_title,
            'date': lecture_date, 'title': display_title,
            'term': manifest['term'], 'term_label': term_label(manifest['term']),
            'source': 'archive', 'order': item['order'], 'order_inferred': not date_verified,
            'date_verified': date_verified,
            'summary': summary, 'transcript': read_file('transcript.md').decode(),
            'asr_transcript': read_file('asr-transcript.md').decode(),
            'figures': {'status': 'complete', 'figures': figures},
            'review': {'status': 'reviewed_locally'},
            'coverage': {'complete': True, 'missing_seconds': 0},
            'provenance': {'source_file_id': item['file_id'], 'source_file': item['source_file'],
                           'summary_sha256': digest(summary_raw),
                           'date_evidence': item.get('date_evidence'),
                           'review': review, 'imported_at': time.time()}
        }
        notes[sid] = payload
        rows.append({k: payload[k] for k in ('sub_id', 'course_id', 'course_title', 'date', 'title',
                                            'term', 'term_label', 'source', 'order')} |
                    {'emailed': False, 'figures': len(figures), 'summary_chars': len(summary),
                     'transcript_chars': len(payload['transcript'])})
    bundle = {'schema': 1, 'collection_id': ident, 'notes': notes}
    raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()
    name = 'collection-' + digest(raw) + '.enc'
    with storage.lock():
        index = read_index(storage)
        previous = index['collections'].get(ident)
        storage.save(name, bundle)
        if storage.read(name) != bundle:
            raise ValueError('ImportedNotesReadbackMismatch')
        index['collections'][ident] = {'bundle': name, 'lectures': rows}
        storage.save('index.enc', index)
        if previous and previous['bundle'] != name:
            (storage.root / previous['bundle']).unlink(missing_ok=True)
    return {'lectures': len(rows), 'figures': sum(r['figures'] for r in rows),
            'term': manifest['term'], 'term_label': term_label(manifest['term']),
            'course_title': course_title}
