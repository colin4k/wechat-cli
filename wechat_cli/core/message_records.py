"""消息记录构建 — 把消息行转成可 JSON 序列化的结构化对象。

一条记录包含时间、发送者、消息类型、正文，以及链接/文件类消息特有的
url、标题等字段。
"""

from datetime import datetime

import zstandard as zstd

from .appmsg import (
    DEFAULT_KIND,
    FILE_APP_TYPE,
    MINIAPP_TYPES,
    QUOTE_APP_TYPE,
    collapse_text,
    parse_appmsg,
)
from .media import file_uri, resolve_file_path, resolve_media_path
from .xmlsafe import parse_xml

_zstd_dctx = zstd.ZstdDecompressor()

BASE_TYPE_LABELS = {
    1: '文本', 3: '图片', 34: '语音', 42: '名片',
    43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
    50: '通话', 10000: '系统', 10002: '撤回',
}

_APPMSG_BASE_TYPE = 49
_TEXT_BASE_TYPE = 1
_IMAGE_BASE_TYPE = 3
_STICKER_BASE_TYPE = 47
_VOIP_BASE_TYPE = 50


# ---- 内容解压 ----

def decompress_content(content, ct):
    """解压消息体（微信 4.x 用 zstd 压缩部分字段）。"""
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
        except Exception:
            return None
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8', errors='replace')
        except Exception:
            return None
    return content


# ---- 消息类型 ----

def split_msg_type(t):
    """把 local_type 拆成 (base_type, sub_type)。"""
    try:
        t = int(t)
    except (TypeError, ValueError):
        return 0, 0
    if t > 0xFFFFFFFF:
        return t & 0xFFFFFFFF, t >> 32
    return t, 0


def format_msg_type(t):
    """base type 的中文标签。"""
    base_type, _ = split_msg_type(t)
    return BASE_TYPE_LABELS.get(base_type, f'type={t}')


# ---- 正文渲染 ----

def split_sender(content, is_group):
    """拆出群聊里的发送者前缀。"""
    if content is None:
        return '', ''
    if isinstance(content, bytes):
        return '', '(二进制内容)'
    sender = ''
    text = content
    if is_group and ':\n' in content:
        sender, text = content.split(':\n', 1)
    return sender, text


def render_voip_text(content):
    """渲染通话消息正文。"""
    if not content or '<voip' not in content:
        return None
    root = parse_xml(content)
    if root is None:
        return "[通话]"
    raw_text = collapse_text(root.findtext('.//msg') or '')
    if not raw_text:
        return "[通话]"
    status_map = {
        'Canceled': '已取消', 'Line busy': '对方忙线',
        'Call not answered': '未接听', "Call wasn't answered": '未接听',
    }
    if raw_text.startswith('Duration:'):
        duration = raw_text.split(':', 1)[1].strip()
        return f"[通话] 通话时长 {duration}" if duration else "[通话]"
    return f"[通话] {status_map.get(raw_text, raw_text)}"


def render_appmsg_text(appmsg):
    """渲染链接/文件类消息正文。"""
    if appmsg is None:
        return None
    title = appmsg.title
    if appmsg.app_type == QUOTE_APP_TYPE:
        quote = title or "[引用消息]"
        if appmsg.refer_text:
            prefix = f"回复 {appmsg.refer_name}: " if appmsg.refer_name else "回复: "
            quote += f"\n  ↳ {prefix}{appmsg.refer_text}"
        return quote
    if appmsg.app_type == FILE_APP_TYPE:
        return f"[文件] {title}" if title else "[文件]"
    if appmsg.app_type == 5:
        return f"[链接] {title}" if title else "[链接]"
    if appmsg.app_type in MINIAPP_TYPES:
        return f"[小程序] {title}" if title else "[小程序]"
    # 视频号、红包、合并转发等有专属类型的，用专属标签而不是泛化的「链接/文件」
    if appmsg.kind != DEFAULT_KIND:
        return f"[{appmsg.kind}] {title}" if title else f"[{appmsg.kind}]"
    if title:
        return f"[链接/文件] {title}"
    return "[链接/文件]"


def render_body(local_id, local_type, base_type, body, appmsg, media_path, media_exists):
    """渲染消息正文（不含时间与发送者前缀）。"""
    if base_type == _IMAGE_BASE_TYPE:
        if media_path:
            tag = f"[图片] {media_path}"
            if not media_exists:
                tag += " (文件不存在)"
        else:
            tag = f"[图片] (local_id={local_id})"
        return tag
    if base_type == _STICKER_BASE_TYPE:
        return "[表情]"
    if base_type == _VOIP_BASE_TYPE:
        return render_voip_text(body) or "[通话]"
    if base_type == _APPMSG_BASE_TYPE:
        return render_appmsg_text(appmsg) or "[链接/文件]"
    if base_type == _TEXT_BASE_TYPE:
        # 纯文本消息不加类型前缀
        return body
    label = format_msg_type(local_type)
    return f"[{label}] {body}" if body else f"[{label}]"


# ---- 发送者 ----

def resolve_sender_label(real_sender_id, sender_from_content, is_group, chat_username,
                         chat_display_name, names, id_to_username, display_name_fn):
    """解析消息发送者的显示名。"""
    sender_username = id_to_username.get(real_sender_id, '')
    if is_group:
        if sender_username and sender_username != chat_username:
            return display_name_fn(sender_username, names)
        if sender_from_content:
            return display_name_fn(sender_from_content, names)
        return ''
    if sender_username == chat_username:
        return chat_display_name
    if sender_username:
        return display_name_fn(sender_username, names)
    return ''


# ---- 记录构建 ----

def _resolve_media(local_type, base_type, body, appmsg, create_time_ts,
                   chat_username, resolve_media, db_dir):
    """返回 (media_path, media_exists)。

    本地路径只在 --media 时解析：它含本机绝对路径，默认输出不应该带上。
    """
    if not resolve_media or not db_dir:
        return None, False

    if base_type == _APPMSG_BASE_TYPE:
        # 文件消息：按 appmsg.title 去 msg/file/YYYY-MM/ 下找
        if appmsg is not None and appmsg.app_type == FILE_APP_TYPE and appmsg.title:
            path = resolve_file_path(db_dir, appmsg.title, create_time_ts, fuzzy=True)
            return path, path is not None
        return None, False

    return resolve_media_path(
        db_dir, body, local_type, create_time_ts, chat_username
    )


def _appmsg_fields(appmsg):
    """把 appmsg 里非空的附加字段抽出来。"""
    fields = {}
    if appmsg is None:
        return fields
    if appmsg.title:
        fields['title'] = appmsg.title
    if appmsg.description:
        fields['description'] = appmsg.description
    if appmsg.file_size:
        fields['file_size'] = appmsg.file_size
    if appmsg.file_ext:
        fields['file_ext'] = appmsg.file_ext
    if appmsg.md5:
        fields['md5'] = appmsg.md5
    if appmsg.appid:
        fields['appid'] = appmsg.appid
    if appmsg.pagepath:
        fields['pagepath'] = appmsg.pagepath
    if appmsg.object_id:
        fields['object_id'] = appmsg.object_id
    if appmsg.source:
        fields['source'] = appmsg.source
    return fields


def build_record(row, ctx, names, id_to_username, display_name_fn,
                 resolve_media=False, db_dir=None, include_chat=False):
    """把一条消息行转成结构化记录。"""
    local_id, local_type, create_time, real_sender_id, content, ct = row

    body = decompress_content(content, ct)
    if body is None:
        body = '(无法解压)'
    sender_hint, body = split_sender(body, ctx['is_group'])
    base_type, sub_type = split_msg_type(local_type)

    appmsg = parse_appmsg(body) if base_type == _APPMSG_BASE_TYPE else None
    if appmsg is not None and appmsg.app_type == 0 and sub_type:
        appmsg = appmsg.with_type(sub_type)

    media_path, media_exists = _resolve_media(
        local_type, base_type, body, appmsg, create_time,
        ctx['username'], resolve_media, db_dir,
    )

    text = render_body(
        local_id, local_type, base_type, body, appmsg, media_path, media_exists
    )
    sender_label = resolve_sender_label(
        real_sender_id, sender_hint, ctx['is_group'], ctx['username'],
        ctx['display_name'], names, id_to_username, display_name_fn,
    )

    url = appmsg.url if appmsg is not None else None
    if not url and media_path and appmsg is not None and appmsg.app_type == FILE_APP_TYPE:
        url = file_uri(media_path)

    record = {
        'time': datetime.fromtimestamp(create_time).strftime('%Y-%m-%d %H:%M'),
        'timestamp': create_time,
        'sender': sender_label,
        'msg_type': format_msg_type(local_type),
    }
    if appmsg is not None:
        record['app_type'] = appmsg.kind
    record.update(_appmsg_fields(appmsg))
    record['text'] = text
    if url:
        record['url'] = url
    if media_path:
        record['media_path'] = media_path
    if include_chat:
        record['chat'] = ctx['display_name']
        record['chat_username'] = ctx['username']
    return record


# ---- 文本渲染 ----

def render_line(record, include_chat=False):
    """单行文本：[时间] [聊天] 发送者: 正文"""
    line = f"[{record.get('time', '')}]"
    if include_chat and record.get('chat'):
        line += f" [{record['chat']}]"
    sender = record.get('sender')
    if sender:
        line += f" {sender}:"
    return line + f" {record.get('text', '')}"


def render_block(record, include_chat=False):
    """多行文本：单行 + 可选的本地路径 / url 行。"""
    line = render_line(record, include_chat=include_chat)
    text = record.get('text', '')
    detail = record.get('media_path') or record.get('url')
    if detail and detail not in text:
        line += f"\n  {detail}"
    return line
