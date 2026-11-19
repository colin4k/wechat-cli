"""媒体路径解析测试 — 仅使用临时目录，不接触真实微信数据。"""

import os
import tempfile
import unittest

from wechat_cli.core.media import file_uri, resolve_file_path, resolve_media_path

TS = 1749191381  # 2025-06-06


class ResolveFilePathTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.db_dir = os.path.join(self.tmp, 'db_storage')
        os.makedirs(self.db_dir)
        self.file_dir = os.path.join(self.tmp, 'msg', 'file', '2025-06')
        os.makedirs(self.file_dir)
        self.target = os.path.join(self.file_dir, '季度报告.pdf')
        with open(self.target, 'w', encoding='utf-8') as f:
            f.write('x')

    def tearDown(self):
        self._tmp.cleanup()

    def test_exact_match(self):
        self.assertEqual(
            resolve_file_path(self.db_dir, '季度报告.pdf', TS), self.target
        )

    def test_missing_file_returns_none(self):
        self.assertIsNone(resolve_file_path(self.db_dir, '不存在.pdf', TS))

    def test_rejects_absolute_path(self):
        """<title> 是外部数据，不能让它把路径指向任意文件。"""
        self.assertIsNone(resolve_file_path(self.db_dir, '/etc/passwd', TS))

    def test_rejects_parent_traversal(self):
        self.assertIsNone(
            resolve_file_path(self.db_dir, '../../../etc/passwd', TS)
        )

    def test_rejects_path_separator(self):
        self.assertIsNone(resolve_file_path(self.db_dir, 'sub/季度报告.pdf', TS))

    def test_rejects_null_byte(self):
        self.assertIsNone(resolve_file_path(self.db_dir, '季度报告\x00.pdf', TS))

    def test_fuzzy_prefers_closest_name(self):
        with open(os.path.join(self.file_dir, '季度报告v2.pdf'), 'w') as f:
            f.write('x')
        path = resolve_file_path(self.db_dir, '季度报告', TS, fuzzy=True)
        self.assertIn(os.path.basename(path), ('季度报告.pdf', '季度报告v2.pdf'))

    def test_fuzzy_does_not_match_unrelated_short_name(self):
        with open(os.path.join(self.file_dir, 'a.txt'), 'w') as f:
            f.write('x')
        # 查询名远长于目录里的无关文件，不应匹配
        self.assertIsNone(
            resolve_file_path(self.db_dir, '完全不同的长文件名.docx', TS, fuzzy=True)
        )

    def test_bad_timestamp_returns_none(self):
        self.assertIsNone(resolve_file_path(self.db_dir, '季度报告.pdf', 10 ** 15))

    def test_missing_db_dir_returns_none(self):
        self.assertIsNone(resolve_file_path(None, '季度报告.pdf', TS))


class FileUriTest(unittest.TestCase):
    def test_encodes_absolute_path(self):
        self.assertTrue(file_uri('/tmp/报告.pdf').startswith('file://'))
        self.assertIn('%E6%8A%A5%E5%91%8A.pdf', file_uri('/tmp/报告.pdf'))

    def test_empty_returns_none(self):
        self.assertIsNone(file_uri(''))
        self.assertIsNone(file_uri(None))


class ResolveMediaPathTest(unittest.TestCase):
    def test_unsupported_type_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, 'db_storage')
            os.makedirs(db_dir)
            os.makedirs(os.path.join(tmp, 'msg'))
            self.assertEqual(resolve_media_path(db_dir, 'x', 49, TS), (None, False))

    def test_image_is_found_under_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, 'db_storage')
            os.makedirs(db_dir)
            img_dir = os.path.join(tmp, 'msg', 'attach', 'abcd', '2025-06', 'Img')
            os.makedirs(img_dir)
            target = os.path.join(img_dir, '1.dat')
            with open(target, 'w') as f:
                f.write('x')
            with open(os.path.join(img_dir, '1_h.dat'), 'w') as f:
                f.write('x')

            path, exists = resolve_media_path(db_dir, '', 3, TS)
            self.assertEqual(path, target)
            self.assertTrue(exists)


if __name__ == '__main__':
    unittest.main()
