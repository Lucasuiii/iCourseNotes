import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from src.ai.course_glossary import course_terms, terminology_reference


class CourseGlossaryTests(unittest.TestCase):
    def test_normalized_course_title_and_no_cross_course_leakage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'glossary.json'
            path.write_text(json.dumps([
                {'courses':['示例课程甲Ⅰ'],'terms':['示例术语甲']},
                {'courses':['示例课程（乙）'],'terms':['示例术语乙']}
            ],ensure_ascii=False))
            with patch.dict('os.environ',{'COURSE_GLOSSARY_PATH':str(path)}):
                self.assertEqual(course_terms('示例课程甲 I'),['示例术语甲'])
                self.assertEqual(course_terms('示例课程 (乙)'),['示例术语乙'])
                self.assertEqual(course_terms('其他课程'),[])
                self.assertEqual(terminology_reference('其他课程'),'')

    def test_encrypted_glossary_roundtrip_authentication_and_private_override(self):
        from src.ai.course_glossary import encrypt_glossary, decrypt_glossary
        entries=[{'courses':['私密测试课程'],'terms':['测试术语']}]
        password='unit-test-key-'+'x'*32
        sealed=encrypt_glossary(entries,password)
        self.assertNotIn('私密测试课程'.encode(),sealed)
        self.assertEqual(decrypt_glossary(sealed,password),entries)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'glossary.enc';path.write_bytes(sealed)
            with patch.dict('os.environ',{'DB_ENCRYPTION_KEY':password}):
                self.assertEqual(course_terms('私密测试课程',path),['测试术语'])
            with patch.dict('os.environ',{'DB_ENCRYPTION_KEY':'wrong-key-'+'y'*32}):
                self.assertEqual(course_terms('私密测试课程',path),[])
            path.write_bytes(sealed[:-1]+bytes([sealed[-1]^1]))
            with self.assertRaises(ValueError):decrypt_glossary(path.read_bytes(),password)
            local=Path(tmp)/'private.json';local.write_text(json.dumps(entries))
            with patch.dict('os.environ',{'COURSE_GLOSSARY_PATH':str(local),'DB_ENCRYPTION_KEY':''}):
                self.assertEqual(course_terms('私密测试课程'),['测试术语'])

    def test_default_encrypted_config_and_local_plaintext_precedence(self):
        from src.ai.course_glossary import encrypt_glossary
        password='test-private-glossary-'+'z'*32
        with tempfile.TemporaryDirectory() as tmp:
            private=Path(tmp)/'private.json';encrypted=Path(tmp)/'course.enc'
            encrypted.write_bytes(encrypt_glossary([{'courses':['示例课程'],'terms':['云端术语']}],password))
            with patch('src.ai.course_glossary.PRIVATE_GLOSSARY_PATH',private), \
                 patch('src.ai.course_glossary.ENCRYPTED_GLOSSARY_PATH',encrypted), \
                 patch.dict('os.environ',{'DB_ENCRYPTION_KEY':password,'COURSE_GLOSSARY_PATH':''}):
                self.assertEqual(course_terms('示例课程'),['云端术语'])
                private.write_text(json.dumps([{'courses':['示例课程'],'terms':['本机术语']}]))
                self.assertEqual(course_terms('示例课程'),['本机术语'])

    def test_invalid_entries_are_filtered_and_terms_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            path.write_text(json.dumps([{"courses": ["A"], "terms":
                ["矩阵", "矩阵", "", 12, "<injection>", "换\n行", "x" * 41]
                + [f"词{i}" for i in range(100)]}]), encoding="utf-8")
            terms = course_terms("A", path)
            self.assertEqual(terms[0], "矩阵")
            self.assertEqual(len(terms), 30)
            self.assertNotIn("<injection>", terms)
            self.assertEqual(terms.count("矩阵"), 1)

    def test_bad_or_missing_config_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            self.assertEqual(course_terms("A", path), [])
            path.write_text("broken json", encoding="utf-8")
            self.assertEqual(course_terms("A", path), [])
            path.write_text('{}', encoding="utf-8")
            self.assertEqual(course_terms("A", path), [])
