"""消息查询 — 分表查找、分页、格式化"""

import hashlib
import re
import sqlite3
from contextlib import closing
from datetime import datetime

from .key_utils import key_path_variants
# 这些辅助函数原先定义在本模块，外部可能仍从这里导入，继续导出以保持兼容
from .message_records import (  # noqa: F401
    build_record,
    decompress_content,
    format_msg_type,
    split_msg_type,
)

# 消息类型过滤映射: 名称 -> (base_type,) 或 (base_type, sub_type)
MSG_TYPE_FILTERS = {
    'text': (1,),
    'image': (3,),
    'voice': (34,),
    'video': (43,),
    'sticker': (47,),
    'location': (48,),
    'link': (49,),
    'file': (49, 6),
    'call': (50,),
    'system': (10000,),
}
MSG_TYPE_NAMES = list(MSG_TYPE_FILTERS.keys())

_QUERY_LIMIT_MAX = 500
_HISTORY_QUERY_BATCH_SIZE = 500


# ---- 消息 DB 发现 ----

def find_msg_db_keys(all_keys):
    return sorted([
        k for k in all_keys
        if any(v.startswith("message/") for v in key_path_variants(k))
        and any(re.search(r"message_\d+\.db$", v) for v in key_path_variants(k))
    ])


def _is_safe_msg_table_name(table_name):
    return bool(re.fullmatch(r'Msg_[0-9a-f]{32}', table_name))


def _find_msg_tables_for_user(username, msg_db_keys, cache):
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    if not _is_safe_msg_table_name(table_name):
        return []
    matches = []
    for rel_key in msg_db_keys:
        path = cache.get(rel_key)
        if not path:
            continue
        conn = sqlite3.connect(path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if not exists:
                continue
            max_ct = conn.execute(f"SELECT MAX(create_time) FROM [{table_name}]").fetchone()[0] or 0
            matches.append({'db_path': path, 'table_name': table_name, 'max_create_time': max_ct})
        except Exception:
            pass
        finally:
            conn.close()
    matches.sort(key=lambda x: x['max_create_time'], reverse=True)
    return matches


# ---- SQL 查询 ----

def _build_message_filters(start_ts=None, end_ts=None, keyword='', msg_type_filter=None):
    clauses = []
    params = []
    if start_ts is not None:
        clauses.append('create_time >= ?')
        params.append(start_ts)
    if end_ts is not None:
        clauses.append('create_time <= ?')
        params.append(end_ts)
    if keyword:
        clauses.append('message_content LIKE ?')
        params.append(f'%{keyword}%')
    if msg_type_filter is not None:
        base_type = msg_type_filter[0]
        clauses.append('(local_type & 0xFFFFFFFF) = ?')
        params.append(base_type)
        if len(msg_type_filter) > 1:
            clauses.append('((local_type >> 32) & 0xFFFFFFFF) = ?')
            params.append(msg_type_filter[1])
    return clauses, params


def _query_messages(conn, table_name, start_ts=None, end_ts=None, keyword='', limit=20, offset=0, msg_type_filter=None):
    if not _is_safe_msg_table_name(table_name):
        raise ValueError(f'非法消息表名: {table_name}')
    clauses, params = _build_message_filters(start_ts, end_ts, keyword, msg_type_filter)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    sql = f"""
        SELECT local_id, local_type, create_time, real_sender_id, message_content,
               WCDB_CT_message_content
        FROM [{table_name}]
        {where_sql}
        ORDER BY create_time DESC
    """
    if limit is None:
        return conn.execute(sql, params).fetchall()
    sql += "\n        LIMIT ? OFFSET ?"
    return conn.execute(sql, (*params, limit, offset)).fetchall()


# ---- 时间解析 ----

def parse_time_value(value, field_name, is_end=False):
    value = (value or '').strip()
    if not value:
        return None
    formats = [
        ('%Y-%m-%d %H:%M:%S', False),
        ('%Y-%m-%d %H:%M', False),
        ('%Y-%m-%d', True),
    ]
    for fmt, date_only in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if date_only and is_end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"{field_name} 格式无效: {value}。支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS")


def parse_time_range(start_time='', end_time=''):
    start_ts = parse_time_value(start_time, 'start_time', is_end=False)
    end_ts = parse_time_value(end_time, 'end_time', is_end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError('start_time 不能晚于 end_time')
    return start_ts, end_ts


def validate_pagination(limit, offset=0, limit_max=_QUERY_LIMIT_MAX):
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    if limit_max is not None and limit > limit_max:
        raise ValueError(f"limit 不能大于 {limit_max}")
    if offset < 0:
        raise ValueError("offset 不能小于 0")


# ---- 聊天上下文 ----

def resolve_chat_context(chat_name, msg_db_keys, cache, decrypted_dir):
    from .contacts import resolve_username, get_contact_names
    username = resolve_username(chat_name, cache, decrypted_dir)
    if not username:
        return None
    names = get_contact_names(cache, decrypted_dir)
    display_name = names.get(username, username)
    message_tables = _find_msg_tables_for_user(username, msg_db_keys, cache)
    if not message_tables:
        return {
            'query': chat_name, 'username': username, 'display_name': display_name,
            'db_path': None, 'table_name': None, 'message_tables': [],
            'is_group': '@chatroom' in username,
        }
    primary = message_tables[0]
    return {
        'query': chat_name, 'username': username, 'display_name': display_name,
        'db_path': primary['db_path'], 'table_name': primary['table_name'],
        'message_tables': message_tables, 'is_group': '@chatroom' in username,
    }


def _iter_table_contexts(ctx):
    tables = ctx.get('message_tables') or []
    if not tables and ctx.get('db_path') and ctx.get('table_name'):
        tables = [{'db_path': ctx['db_path'], 'table_name': ctx['table_name']}]
    for table in tables:
        yield {
            'query': ctx['query'], 'username': ctx['username'], 'display_name': ctx['display_name'],
            'db_path': table['db_path'], 'table_name': table['table_name'],
            'is_group': ctx['is_group'],
        }


def _candidate_page_size(limit, offset):
    return limit + offset


def _page_ranked_entries(entries, limit, offset):
    """按时间排序后分页，返回记录列表。"""
    ordered = sorted(entries, key=lambda item: item.get('timestamp', 0), reverse=True)
    paged = ordered[offset:offset + limit]
    paged.sort(key=lambda item: item.get('timestamp', 0))
    return paged


# ---- Name2Id ----

def _load_name2id_maps(conn):
    id_to_username = {}
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return id_to_username
    for rowid, user_name in rows:
        if not user_name:
            continue
        id_to_username[rowid] = user_name
    return id_to_username


# ---- 记录构建 ----

def _build_record(row, ctx, names, id_to_username, display_name_fn,
                  resolve_media=False, db_dir=None, include_chat=False):
    return build_record(
        row, ctx, names, id_to_username, display_name_fn,
        resolve_media=resolve_media, db_dir=db_dir, include_chat=include_chat,
    )


# ---- 聊天记录查询 ----

def collect_chat_history(ctx, names, display_name_fn, start_ts=None, end_ts=None, limit=20, offset=0, msg_type_filter=None, resolve_media=False, db_dir=None):
    """返回 (记录列表, 失败列表)。"""
    collected = []
    failures = []
    candidate_limit = _candidate_page_size(limit, offset)
    batch_size = min(candidate_limit, _HISTORY_QUERY_BATCH_SIZE)

    for table_ctx in _iter_table_contexts(ctx):
        try:
            with closing(sqlite3.connect(table_ctx['db_path'])) as conn:
                id_to_username = _load_name2id_maps(conn)
                fetch_offset = 0
                before = len(collected)
                while len(collected) - before < candidate_limit:
                    rows = _query_messages(conn, table_ctx['table_name'], start_ts=start_ts, end_ts=end_ts, limit=batch_size, offset=fetch_offset, msg_type_filter=msg_type_filter)
                    if not rows:
                        break
                    fetch_offset += len(rows)
                    for row in rows:
                        try:
                            collected.append(_build_record(
                                row, table_ctx, names, id_to_username, display_name_fn,
                                resolve_media=resolve_media, db_dir=db_dir,
                            ))
                        except Exception as e:
                            failures.append(f"local_id={row[0]}: {e}")
                        if len(collected) - before >= candidate_limit:
                            break
                    if len(rows) < batch_size:
                        break
        except Exception as e:
            failures.append(f"{table_ctx['db_path']}: {e}")

    paged = _page_ranked_entries(collected, limit, offset)
    return paged, failures


# ---- 搜索查询 ----

def _is_readable(row):
    """消息体能否解压解析。压缩内容解不开时这条消息没有搜索价值。"""
    _lid, _lt, _ts, _sid, content, ct = row
    return decompress_content(content, ct) is not None


def _collect_search_entries(conn, contexts, names, keyword, display_name_fn, start_ts=None, end_ts=None, candidate_limit=20, msg_type_filter=None):
    collected = []
    failures = []
    id_to_username = _load_name2id_maps(conn)
    batch_size = candidate_limit

    for ctx in contexts:
        try:
            fetch_offset = 0
            before = len(collected)
            while len(collected) - before < candidate_limit:
                rows = _query_messages(conn, ctx['table_name'], start_ts=start_ts, end_ts=end_ts, keyword=keyword, limit=batch_size, offset=fetch_offset, msg_type_filter=msg_type_filter)
                if not rows:
                    break
                fetch_offset += len(rows)
                for row in rows:
                    if not _is_readable(row):
                        # 解压失败的压缩消息不参与搜索：既不是有效命中，
                        # 也不该占用 candidate_limit 名额
                        continue
                    try:
                        record = _build_record(
                            row, ctx, names, id_to_username, display_name_fn,
                            include_chat=True,
                        )
                    except Exception as e:
                        failures.append(f"local_id={row[0]}: {e}")
                        continue
                    text = record.get('text', '')
                    if len(text) > 300:
                        record = {**record, 'text': text[:300] + '...'}
                    collected.append(record)
                    if len(collected) - before >= candidate_limit:
                        break
                if len(rows) < batch_size:
                    break
        except Exception as e:
            failures.append(f"{ctx['display_name']}: {e}")
    return collected, failures


def collect_chat_search(ctx, names, keyword, display_name_fn, start_ts=None, end_ts=None, candidate_limit=20, msg_type_filter=None):
    collected = []
    failures = []
    contexts_by_db = {}
    for table_ctx in _iter_table_contexts(ctx):
        contexts_by_db.setdefault(table_ctx['db_path'], []).append(table_ctx)

    for db_path, db_contexts in contexts_by_db.items():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                db_entries, db_failures = _collect_search_entries(
                    conn, db_contexts, names, keyword, display_name_fn,
                    start_ts=start_ts, end_ts=end_ts, candidate_limit=candidate_limit,
                    msg_type_filter=msg_type_filter,
                )
                collected.extend(db_entries)
                failures.extend(db_failures)
        except Exception as e:
            failures.extend(f"{tc['display_name']}: {e}" for tc in db_contexts)
    return collected, failures


def search_all_messages(msg_db_keys, cache, names, keyword, display_name_fn, start_ts=None, end_ts=None, candidate_limit=20, msg_type_filter=None):
    collected = []
    failures = []
    for rel_key in msg_db_keys:
        path = cache.get(rel_key)
        if not path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                contexts = _load_search_contexts_from_db(conn, path, names)
                db_entries, db_failures = _collect_search_entries(
                    conn, contexts, names, keyword, display_name_fn,
                    start_ts=start_ts, end_ts=end_ts, candidate_limit=candidate_limit,
                    msg_type_filter=msg_type_filter,
                )
                collected.extend(db_entries)
                failures.extend(db_failures)
        except Exception as e:
            failures.append(f"{rel_key}: {e}")
    return collected, failures


def _load_search_contexts_from_db(conn, db_path, names):
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    ).fetchall()
    table_to_username = {}
    try:
        for (user_name,) in conn.execute("SELECT user_name FROM Name2Id").fetchall():
            if not user_name:
                continue
            table_hash = hashlib.md5(user_name.encode()).hexdigest()
            table_to_username[f"Msg_{table_hash}"] = user_name
    except sqlite3.Error:
        pass
    contexts = []
    for (table_name,) in tables:
        username = table_to_username.get(table_name, '')
        display_name = names.get(username, username) if username else table_name
        contexts.append({
            'query': display_name, 'username': username, 'display_name': display_name,
            'db_path': db_path, 'table_name': table_name, 'is_group': '@chatroom' in username,
        })
    return contexts


# ---- 多聊天上下文解析 ----

def resolve_chat_contexts(chat_names, msg_db_keys, cache, decrypted_dir):
    resolved = []
    unresolved = []
    missing_tables = []
    seen = set()
    for chat_name in chat_names:
        name = (chat_name or '').strip()
        if not name:
            unresolved.append('(空)')
            continue
        ctx = resolve_chat_context(name, msg_db_keys, cache, decrypted_dir)
        if not ctx:
            unresolved.append(name)
            continue
        if not ctx['message_tables']:
            missing_tables.append(ctx['display_name'])
            continue
        if ctx['username'] in seen:
            continue
        seen.add(ctx['username'])
        resolved.append(ctx)
    return resolved, unresolved, missing_tables


# ---- 聊天统计 ----

def collect_chat_stats(ctx, names, display_name_fn, start_ts=None, end_ts=None):
    """聚合统计指定聊天的消息数据。

    返回: {
        total, type_breakdown: {type_name: count},
        top_senders: [{name, count}],
        hourly: {0:N, ..., 23:N}
    }
    """
    type_map = {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
        50: '通话', 10000: '系统', 10002: '撤回',
    }

    total = 0
    type_counts = {}
    sender_counts = {}
    hourly_counts = {}

    for table_ctx in _iter_table_contexts(ctx):
        try:
            with closing(sqlite3.connect(table_ctx['db_path'])) as conn:
                id_to_username = _load_name2id_maps(conn)
                tbl = table_ctx['table_name']
                if not _is_safe_msg_table_name(tbl):
                    continue

                where_parts = []
                params = []
                if start_ts is not None:
                    where_parts.append('create_time >= ?')
                    params.append(start_ts)
                if end_ts is not None:
                    where_parts.append('create_time <= ?')
                    params.append(end_ts)
                where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ''

                # 总数 + 类型分布
                for bt, cnt in conn.execute(
                    f"SELECT (local_type & 0xFFFFFFFF), COUNT(*) FROM [{tbl}] {where_sql} GROUP BY (local_type & 0xFFFFFFFF)",
                    params
                ).fetchall():
                    label = type_map.get(bt, f'type={bt}')
                    type_counts[label] = type_counts.get(label, 0) + cnt
                    total += cnt

                # 发送者排名
                for sid, cnt in conn.execute(
                    f"SELECT real_sender_id, COUNT(*) FROM [{tbl}] {where_sql} GROUP BY real_sender_id ORDER BY COUNT(*) DESC LIMIT 20",
                    params
                ).fetchall():
                    uname = id_to_username.get(sid, str(sid))
                    if uname:
                        sender_counts[uname] = sender_counts.get(uname, 0) + cnt

                # 24小时分布
                for h, cnt in conn.execute(
                    f"SELECT cast(strftime('%H', create_time, 'unixepoch', 'localtime') as integer), COUNT(*) FROM [{tbl}] {where_sql} GROUP BY cast(strftime('%H', create_time, 'unixepoch', 'localtime') as integer)",
                    params
                ).fetchall():
                    if h is not None:
                        hourly_counts[h] = hourly_counts.get(h, 0) + cnt
        except Exception:
            pass

    top_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    top_senders = [{'name': display_name_fn(u, names), 'count': c} for u, c in top_senders]

    hourly = {h: hourly_counts.get(h, 0) for h in range(24)}

    return {
        'total': total,
        'type_breakdown': dict(sorted(type_counts.items(), key=lambda x: x[1], reverse=True)),
        'top_senders': top_senders,
        'hourly': hourly,
    }
