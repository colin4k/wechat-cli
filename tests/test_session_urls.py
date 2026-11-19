"""会话 url 反查测试 — 用临时 SQLite 库模拟消息表，不接触真实微信数据。"""

import hashlib
import os
import sqlite3
import tempfile
import unittest

from wechat_cli.core.session_urls import (
    _merge_fields,
    _table_name,
    _wanted_timestamps,
    attach_urls,
    collect_session_messages,
)

USERNAME = 'wxid_peer'
TS = 1749191381

LINK_XML = (
    '<msg><appmsg appid="" sdkver="0">'
    '<title>某篇文章</title><type>5</type>'
    '<url>http://mp.weixin.qq.com/s?__biz=abc</url>'
    '</appmsg></msg>'
)
FILE_XML = (
    '<msg><appmsg appid="" sdkver="0">'
    '<title>报告.pdf</title><type>6</type>'
    '</appmsg></msg>'
)

_SCHEMA = (
    "local_id INTEGER, local_type INTEGER, create_time INTEGER, "
    "message_content TEXT, WCDB_CT_message_content INTEGER"
)


class Cache:
    """最小化的解密缓存替身。"""

    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, key):
        return self._mapping.get(key)


class WantedTimestampsTest(unittest.TestCase):
    def test_only_link_base_type_is_wanted(self):
        wanted = _wanted_timestamps([
            {'username': 'a', 'timestamp': 1, 'msg_type': 49},
            {'username': 'b', 'timestamp': 2, 'msg_type': 1},
        ])
        self.assertEqual(wanted, {'a': {1}})

    def test_label_string_is_ignored(self):
        """targets 必须传原始整数 msg_type；标签字符串会被静默跳过。"""
        self.assertEqual(
            _wanted_timestamps([{'username': 'a', 'timestamp': 1, 'msg_type': '链接/文件'}]),
            {},
        )

    def test_missing_fields_are_skipped(self):
        self.assertEqual(_wanted_timestamps([{'username': 'a'}]), {})
        self.assertEqual(_wanted_timestamps([{'timestamp': 1, 'msg_type': 49}]), {})


class TableNameTest(unittest.TestCase):
    def test_table_name_is_md5_hex(self):
        name = _table_name(USERNAME)
        self.assertEqual(name, 'Msg_' + hashlib.md5(USERNAME.encode()).hexdigest())
        self.assertRegex(name, r'^Msg_[0-9a-f]{32}$')


class MergeFieldsTest(unittest.TestCase):
    def test_inserts_after_msg_type(self):
        merged = _merge_fields({'chat': 'x', 'msg_type': '链接/文件', 'z': 1},
                               {'url': 'u', 'app_type': '链接'})
        self.assertEqual(
            list(merged), ['chat', 'msg_type', 'url', 'app_type', 'z']
        )

    def test_missing_msg_type_appends(self):
        merged = _merge_fields({'chat': 'x'}, {'url': 'u'})
        self.assertEqual(merged, {'chat': 'x', 'url': 'u'})

    def test_does_not_mutate_input(self):
        item = {'msg_type': 'y'}
        extra = {'url': 'u'}
        _merge_fields(item, extra)
        self.assertEqual(item, {'msg_type': 'y'})
        self.assertEqual(extra, {'url': 'u'})


class AttachUrlsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'message_0.db')
        conn = sqlite3.connect(self.path)
        conn.execute(f"CREATE TABLE [{_table_name(USERNAME)}] ({_SCHEMA})")
        conn.execute(
            f"INSERT INTO [{_table_name(USERNAME)}] VALUES (1, 49, ?, ?, 0)",
            (TS, LINK_XML),
        )
        conn.commit()
        conn.close()
        self.cache = Cache({'message/message_0.db': self.path})

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_url_for_matching_session(self):
        results = [{'chat': '张三', 'username': USERNAME,
                    'msg_type': '链接/文件', 'last_message': '某篇文章'}]
        targets = [{'username': USERNAME, 'timestamp': TS, 'msg_type': 49}]

        enriched = attach_urls(results, targets, ['message/message_0.db'], self.cache)

        self.assertEqual(enriched[0]['url'], 'http://mp.weixin.qq.com/s?__biz=abc')
        self.assertEqual(enriched[0]['app_type'], '链接')
        self.assertNotIn('url', results[0])  # 原列表未被修改

    def test_non_link_session_is_untouched(self):
        results = [{'chat': '李四', 'username': 'wxid_other',
                    'msg_type': '文本', 'last_message': 'hi'}]
        targets = [{'username': 'wxid_other', 'timestamp': TS, 'msg_type': 1}]

        enriched = attach_urls(results, targets, ['message/message_0.db'], self.cache)
        self.assertEqual(enriched, results)

    def test_missing_db_is_tolerated(self):
        results = [{'chat': '张三', 'username': USERNAME, 'msg_type': '链接/文件'}]
        targets = [{'username': USERNAME, 'timestamp': TS, 'msg_type': 49}]

        enriched = attach_urls(results, targets, ['message/nope.db'], self.cache)
        self.assertEqual(enriched, results)

    def test_file_message_has_no_url(self):
        conn = sqlite3.connect(self.path)
        conn.execute(f"CREATE TABLE [{_table_name('wxid_file')}] ({_SCHEMA})")
        conn.execute(
            f"INSERT INTO [{_table_name('wxid_file')}] VALUES (2, 49, ?, ?, 0)",
            (TS, FILE_XML),
        )
        conn.commit()
        conn.close()

        targets = [{'username': 'wxid_file', 'timestamp': TS, 'msg_type': 49}]
        self.assertEqual(
            collect_session_messages(targets, ['message/message_0.db'], self.cache), {}
        )


if __name__ == '__main__':
    unittest.main()
