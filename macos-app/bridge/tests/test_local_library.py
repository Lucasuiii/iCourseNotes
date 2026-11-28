"""Imported notes survive cache replacement and retain reviewed assets/order."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

spec = importlib.util.spec_from_file_location('local_library', Path(__file__).parents[1] / 'local_library.py')
library = importlib.util.module_from_spec(spec)
spec.loader.exec_module(library)


class DiskStore:
    def __init__(self, root):
        self.root = root
        root.mkdir()

    def exists(self, name):
        return (self.root / name).exists()

    def read(self, name):
        return json.loads((self.root / name).read_text())

    def save(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    @contextlib.contextmanager
    def lock(self):
        yield


class LocalLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = DiskStore(self.root / 'library')
        self.folder = self.root / 'F01'
        (self.folder / 'figures').mkdir(parents=True)
        image = io.BytesIO()
        Image.new('RGB', (16, 16), 'white').save(image, format='JPEG')
        raw = image.getvalue()
        (self.folder / 'figures' / 'original.jpg').write_bytes(raw)
        self.summary = '# 笔记\n\n$$Ax=b$$\n\n![原图](figures/original.jpg)'
        (self.folder / 'summary.md').write_text(self.summary)
        (self.folder / 'transcript.md').write_text('完整整理转录')
        (self.folder / 'asr-transcript.md').write_text('完整原始转录')
        self.review = {'status': 'locally_reviewed', 'blocks': 2, 'planned_blocks': 2,
                       'missing_intervals': [], 'summary_sha256': hashlib.sha256(self.summary.encode()).hexdigest(),
                       'figures': [{'path': 'figures/original.jpg', 'sha256': hashlib.sha256(raw).hexdigest(), 'seconds': 60}]}
        (self.folder / 'content-review.json').write_text(json.dumps(self.review))
        self.manifest = {'schema': 1, 'collection_id': 'example-2025-fall', 'term': '2025-20261',
                         'course_title': '课程', 'root': str(self.root),
                         'lectures': [{'order': 1, 'file_id': 'F01', 'folder': 'F01',
                                       'source_file': 'original.mp4', 'title': '主题'}]}
        self.path = self.root / 'manifest.json'

    def run_import(self):
        self.path.write_text(json.dumps(self.manifest))
        return library.import_manifest(self.storage, self.path, lambda code: '2025–2026 秋季学期')

    def test_roundtrip_and_repeat_without_duplicates(self):
        self.run_import()
        self.run_import()
        rows = library.lectures(self.storage)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['term'], '2025-20261')
        payload = library.note(self.storage, rows[0]['sub_id'])
        self.assertEqual(payload['date'], '')
        self.assertEqual(payload['source'], 'archive')
        self.assertEqual(payload['asr_transcript'], '完整原始转录')
        self.assertIn('#icourse-figure-', payload['summary'])
        self.assertEqual(payload['figures']['figures'][0]['seconds'], 60)
        self.assertEqual(len(list(self.storage.root.glob('collection-*.enc'))), 1)

    def test_bad_image_leaves_existing_library_unchanged(self):
        self.run_import()
        before = library.read_index(self.storage)
        (self.folder / 'figures' / 'original.jpg').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(before, library.read_index(self.storage))

    def test_course_order_must_be_contiguous(self):
        self.manifest['lectures'][0]['order'] = 2
        with self.assertRaises(ValueError):
            self.run_import()

    def test_summary_change_rejected(self):
        (self.folder / 'summary.md').write_text('changed')
        with self.assertRaises(ValueError):
            self.run_import()

    def test_unknown_id_cannot_read_arbitrary_path(self):
        self.run_import()
        with self.assertRaises(ValueError):
            library.note(self.storage, '../../other')

    def test_library_does_not_depend_on_cloud_cache(self):
        self.run_import()
        # Cloud synchronization replaces its sibling notes-cache, not this store.
        cache = self.root / 'notes-cache'
        cache.mkdir()
        (cache / 'index.enc').write_text('{"lectures": []}')
        self.assertEqual(len(library.lectures(self.storage)), 1)

    def test_verified_date_is_preserved_and_order_is_confirmed(self):
        self.manifest['lectures'][0].update(date='2025-09-15', date_verified=True,
                                            display_title='2025-09-15第1-3节',
                                            date_evidence={'basis': 'exact playback metadata match'})
        self.run_import()
        row = library.lectures(self.storage)[0]
        payload = library.note(self.storage, row['sub_id'])
        self.assertEqual(payload['date'], '2025-09-15')
        self.assertFalse(payload['order_inferred'])
        self.assertIn('2025-09-15', row['title'])
        self.assertEqual(row['title'], '2025-09-15第1-3节')
        self.assertNotIn('第01段', row['title'])

    def test_unverified_date_cannot_be_displayed_as_confirmed(self):
        self.manifest['lectures'][0]['date'] = '2025-09-15'
        with self.assertRaises(ValueError):
            self.run_import()


if __name__ == '__main__':
    unittest.main()
