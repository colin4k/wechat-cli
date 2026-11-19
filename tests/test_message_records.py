"""消息记录构建测试 — 仅使用合成消息，不接触真实微信数据。"""

import os
import tempfile
import unittest
from datetime import datetime

from wechat_cli.core.message_records import (
    build_record,
    decompress_content,
    format_msg_type,
    render_block,
    render_line,
    split_msg_type,
)

LINK_XML = (
    '<msg><appmsg appid="" sdkver="0">'
    '<title>Solodit 检查清单详解</title>'
    '<des>矿工攻击即验证者利用智能合约</des>'
    '<type>5</type>'
    '<url>http://mp.weixin.qq.com/s?__biz=abc</url>'
    '</appmsg></msg>'
)

FILE_XML = (
    '<msg><appmsg appid="" sdkver="0">'
    '<title>季度报告.pdf</title>'
    '<type>6</type>'
    '<appattach><totallen>2048</totallen><fileext>pdf</fileext></appattach>'
    '<md5>deadbeef</md5>'
    '</appmsg></msg>'
)

CHANNEL_XML = (
    '<msg><appmsg appid="" sdkver="0">'
    '<title>当前版本不支持展示该内容，请升级至最新版本。</title>'
    '<type>51</type>'
    '<finderFeed>'
    '<objectId>12345678901234567890</objectId>'
    '<username>v2_0000000000000000@finder</username>'
    '</finderFeed>'
    '</appmsg></msg>'
)


TIME = datetime.fromtimestamp(1749191381).strftime('%Y-%m-%d %H:%M')


def make_ctx(username='wxid_peer', display='张三', is_group=False):
    return {'username': username, 'display_name': display, 'is_group': is_group}


def display_name_fn(username, names):
    return names.get(username, username)


def build(row, ctx=None, names=None, id_to_username=None, **kwargs):
    ctx = ctx or make_ctx()
    names = names if names is not None else {}
    id_to_username = id_to_username if id_to_username is not None else {}
    return build_record(
        row, ctx, names, id_to_username, display_name_fn, **kwargs
    )


class TypeHelperTest(unittest.TestCase):
    def test_split_msg_type(self):
        self.assertEqual(split_msg_type(49), (49, 0))
        # 高 32 位是子类型
        self.assertEqual(split_msg_type((57 << 32) | 49), (49, 57))
        self.assertEqual(split_msg_type('bad'), (0, 0))

    def test_format_msg_type(self):
        self.assertEqual(format_msg_type(1), '文本')
        self.assertEqual(format_msg_type(49), '链接/文件')
        self.assertEqual(format_msg_type(9999), 'type=9999')


class DecompressTest(unittest.TestCase):
    def test_plain_text_passes_through(self):
        self.assertEqual(decompress_content('hello', 0), 'hello')

    def test_bytes_are_decoded(self):
        self.assertEqual(decompress_content('你好'.encode('utf-8'), 0), '你好')


class BuildRecordTest(unittest.TestCase):
    def test_text_message(self):
        record = build((1, 1, 1749191381, 7, '今天开会', 0))
        self.assertEqual(record['time'], TIME)
        self.assertEqual(record['timestamp'], 1749191381)
        self.assertEqual(record['msg_type'], '文本')
        self.assertEqual(record['text'], '今天开会')
        self.assertNotIn('url', record)
        self.assertNotIn('app_type', record)

    def test_sender_is_resolved_from_name2id(self):
        record = build(
            (1, 1, 1749191381, 7, 'hi', 0),
            id_to_username={7: 'wxid_sender'},
            names={'wxid_sender': '李四'},
        )
        self.assertEqual(record['sender'], '李四')

    def test_link_message_exposes_url(self):
        record = build((1, 49, 1749191381, 7, LINK_XML, 0))
        self.assertEqual(record['msg_type'], '链接/文件')
        self.assertEqual(record['app_type'], '链接')
        self.assertEqual(record['title'], 'Solodit 检查清单详解')
        self.assertEqual(record['description'], '矿工攻击即验证者利用智能合约')
        self.assertEqual(record['url'], 'http://mp.weixin.qq.com/s?__biz=abc')
        self.assertEqual(record['text'], '[链接] Solodit 检查清单详解')

    def test_file_message_exposes_metadata(self):
        record = build((1, 49, 1749191381, 7, FILE_XML, 0))
        self.assertEqual(record['app_type'], '文件')
        self.assertEqual(record['title'], '季度报告.pdf')
        self.assertEqual(record['file_size'], 2048)
        self.assertEqual(record['file_ext'], 'pdf')
        self.assertEqual(record['md5'], 'deadbeef')
        self.assertEqual(record['text'], '[文件] 季度报告.pdf')
        # 没有本地路径时不应有 url
        self.assertNotIn('url', record)

    def test_file_message_url_uses_local_path_when_media_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, 'db_storage')
            os.makedirs(db_dir)
            file_dir = os.path.join(tmp, 'msg', 'file', '2025-06')
            os.makedirs(file_dir)
            target = os.path.join(file_dir, '季度报告.pdf')
            with open(target, 'w', encoding='utf-8') as f:
                f.write('x')

            record = build((1, 49, 1749191381, 7, FILE_XML, 0),
                           db_dir=db_dir, resolve_media=True)
            self.assertEqual(record['media_path'], target)
            self.assertTrue(record['url'].startswith('file://'))
            self.assertIn('%E5%AD%A3%E5%BA%A6%E6%8A%A5%E5%91%8A.pdf', record['url'])

    def test_file_message_hides_local_path_without_media_flag(self):
        """默认输出不能带本机绝对路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, 'db_storage')
            os.makedirs(db_dir)
            file_dir = os.path.join(tmp, 'msg', 'file', '2025-06')
            os.makedirs(file_dir)
            with open(os.path.join(file_dir, '季度报告.pdf'), 'w', encoding='utf-8') as f:
                f.write('x')

            record = build((1, 49, 1749191381, 7, FILE_XML, 0), db_dir=db_dir)
            self.assertNotIn('media_path', record)
            self.assertNotIn('url', record)
            self.assertEqual(record['text'], '[文件] 季度报告.pdf')

    def test_group_sender_prefix_is_stripped(self):
        ctx = make_ctx('123@chatroom', '工作群', is_group=True)
        record = build((1, 1, 1749191381, 7, '王五:\n大家好', 0), ctx=ctx)
        self.assertEqual(record['text'], '大家好')

    def test_channel_message_uses_specific_kind(self):
        record = build((1, 49, 1749191381, 7, CHANNEL_XML, 0))
        self.assertEqual(record['app_type'], '视频号')
        self.assertEqual(record['object_id'], '12345678901234567890')
        self.assertTrue(record['text'].startswith('[视频号]'))
        # 视频号没有可跳转的 url
        self.assertNotIn('url', record)

    def test_search_record_includes_chat(self):
        record = build((1, 1, 1749191381, 7, 'hi', 0), include_chat=True)
        self.assertEqual(record['chat'], '张三')
        self.assertEqual(record['chat_username'], 'wxid_peer')

    def test_unparseable_content_falls_back(self):
        record = build((1, 49, 1749191381, 7, 'not xml', 0))
        self.assertEqual(record['text'], '[链接/文件]')
        self.assertNotIn('url', record)


class RenderTest(unittest.TestCase):
    def test_render_line(self):
        record = build((1, 1, 1749191381, 7, '今天开会', 0),
                       id_to_username={7: 'wxid_sender'}, names={'wxid_sender': '李四'})
        self.assertEqual(render_line(record), f'[{TIME}] 李四: 今天开会')

    def test_render_line_without_sender(self):
        record = build((1, 1, 1749191381, 7, '今天开会', 0))
        self.assertEqual(render_line(record), f'[{TIME}] 今天开会')

    def test_render_block_appends_url(self):
        record = build((1, 49, 1749191381, 7, LINK_XML, 0))
        block = render_block(record)
        self.assertIn('\n  http://mp.weixin.qq.com/s?__biz=abc', block)

    def test_render_block_includes_chat_when_asked(self):
        record = build((1, 1, 1749191381, 7, 'hi', 0), include_chat=True)
        self.assertEqual(
            render_line(record, include_chat=True), f'[{TIME}] [张三] hi'
        )

    def test_render_block_does_not_duplicate_path_already_in_text(self):
        record = build((1, 3, 1749191381, 7, 'x', 0))
        record['media_path'] = '/tmp/a.dat'
        record['text'] = '[图片] /tmp/a.dat'
        self.assertEqual(render_block(record), f'[{TIME}] [图片] /tmp/a.dat')


if __name__ == '__main__':
    unittest.main()
