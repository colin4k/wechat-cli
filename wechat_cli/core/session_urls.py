"""会话最后一条消息的 url 提取。

session.db 的 summary 字段只保存渲染后的纯文本预览，不含 XML，
要拿到链接/文件消息的 url，必须回消息库按 (username, create_time) 反查。
"""

import hashlib
import re
import sqlite3
from contextlib import closing

from .appmsg import parse_appmsg
from .message_records import decompress_content, split_msg_type

_LINK_BASE_TYPE = 49
_TABLE_NAME_RE = re.compile(r'Msg_[0-9a-f]{32}')


def _table_name(username):
    """会话对应的消息表名。表名只由 md5 十六进制构成，可安全拼进 SQL。"""
    return 'Msg_' + hashlib.md5(username.encode()).hexdigest()


def _wanted_timestamps(targets):
    """挑出链接/文件类会话，按 username 归拢时间戳。

    targets 里每项需要 username、timestamp 和原始 msg_type（整数）。
    """
    wanted = {}
    for target in targets:
        base_type, _ = split_msg_type(target.get('msg_type'))
        if base_type != _LINK_BASE_TYPE:
            continue
        username = target.get('username')
        timestamp = target.get('timestamp')
        if not username or not timestamp:
            continue
        wanted.setdefault(username, set()).add(timestamp)
    return wanted


def _message_at(conn, table, timestamp):
    """在 <table> 里找出该时刻能解析出 url 的 appmsg。"""
    try:
        rows = conn.execute(
            f"SELECT local_type, message_content, WCDB_CT_message_content "
            f"FROM [{table}] WHERE create_time = ?",
            (timestamp,),
        ).fetchall()
    except sqlite3.Error:
        return None

    for local_type, content, ct in rows:
        base_type, _ = split_msg_type(local_type)
        if base_type != _LINK_BASE_TYPE:
            continue
        text = decompress_content(content, ct)
        if not text:
            continue
        appmsg = parse_appmsg(text)
        if appmsg and appmsg.url:
            return appmsg
    return None


def _scan_db(conn, wanted, found):
    """在一个消息库里找出会话对应的消息并抽取 appmsg。"""
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error:
        return

    for username, timestamps in wanted.items():
        if username in found:
            continue
        table = _table_name(username)
        if table not in tables or not _TABLE_NAME_RE.fullmatch(table):
            continue
        for timestamp in sorted(timestamps):
            appmsg = _message_at(conn, table, timestamp)
            if appmsg:
                found[username] = appmsg
                break


def collect_session_messages(targets, msg_db_keys, cache):
    """返回 {username: AppMessage}，只包含能解析出 url 的会话。

    需要解密全部消息库，调用方应做成显式开关。
    """
    wanted = _wanted_timestamps(targets)
    if not wanted:
        return {}

    found = {}
    for rel_key in msg_db_keys:
        try:
            path = cache.get(rel_key)
        except (OSError, ValueError, KeyError):
            # 解密失败或密钥缺失：这是尽力而为的补充信息，跳过即可
            continue
        if not path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                _scan_db(conn, wanted, found)
        except sqlite3.Error:
            # 单个消息库损坏/未解密不应让整个命令失败
            continue
    return found


def _merge_fields(item, extra):
    """把附加字段插到 msg_type 之后，返回新字典。

    msg_type 缺失时追加到末尾。
    """
    merged = {}
    for key, value in item.items():
        merged[key] = value
        if key == 'msg_type':
            merged.update(extra)
    for key, value in extra.items():
        if key not in merged:
            merged[key] = value
    return merged


def attach_urls(results, targets, msg_db_keys, cache):
    """给会话结果补上 url / app_type 字段，返回新的结果列表。"""
    found = collect_session_messages(targets, msg_db_keys, cache)
    if not found:
        return results

    enriched = []
    for item in results:
        appmsg = found.get(item.get('username'))
        if not appmsg:
            enriched.append(item)
            continue
        extra = {'url': appmsg.url, 'app_type': appmsg.kind}
        enriched.append(_merge_fields(item, extra))
    return enriched
