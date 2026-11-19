"""媒体文件路径解析 — 图片/视频/语音/文件在磁盘上的位置。

微信把媒体文件存在 db_storage 同级的 msg/ 目录下，本模块负责定位它们，
并把本地路径转成 file:// URI。

消息里的文件名来自外部数据（对方客户端写的 <title>），所以拼路径时必须做
越界检查，不能让它通过 "../" 或绝对路径跳出 msg/ 目录。
"""

import hashlib
import os
from datetime import datetime
from pathlib import Path

_IMAGE_TYPE = 3
_VOICE_TYPE = 34
_VIDEO_TYPE = 43
_APPMSG_TYPE = 49
_APPMSG_FILE_TYPE = 6

# 模糊匹配时，候选名至少要有查询名的这个比例长度，避免 "a" 匹配到一切
_FUZZY_MIN_RATIO = 0.6


def msg_root(db_dir):
    """msg 目录：db_storage 的同级目录。"""
    return os.path.join(os.path.dirname(db_dir), 'msg')


def file_uri(path):
    """把本地绝对路径转成 file:// URI。"""
    if not path:
        return None
    try:
        return Path(path).as_uri()
    except ValueError:
        return None


def _safe_child(base_dir, file_name):
    """返回 base_dir 下名为 file_name 的路径；越界或非法时返回 None。

    os.path.join 对绝对路径和 ".." 没有任何约束，这里显式校验：
    文件名不得含路径分隔符，解析后的真实路径必须仍在 base_dir 内。
    """
    if not file_name or '\x00' in file_name:
        return None
    if os.path.isabs(file_name):
        return None
    if os.sep in file_name or (os.altsep and os.altsep in file_name):
        return None

    base_real = os.path.realpath(base_dir)
    target = os.path.join(base_dir, file_name)
    if os.path.commonpath([base_real, os.path.realpath(target)]) != base_real:
        return None
    return target


def _date_prefix(create_time_ts):
    """把时间戳转成 msg 目录用的 YYYY-MM；时间戳损坏时返回 None。"""
    try:
        return datetime.fromtimestamp(create_time_ts).strftime('%Y-%m')
    except (OverflowError, OSError, ValueError):
        return None


def _fuzzy_match(file_dir, file_name):
    """在目录里找与 file_name 最接近的文件，返回完整路径或 None。

    优先级：精确 > 前缀 > 后缀 > 包含；同分时按名称排序，保证结果稳定。
    """
    try:
        names = sorted(os.listdir(file_dir))
    except OSError:
        return None

    min_len = max(1, int(len(file_name) * _FUZZY_MIN_RATIO))
    best = None
    best_score = 0
    for name in names:
        if len(name) < min_len and len(file_name) < min_len:
            continue
        if name == file_name:
            score = 4
        elif name.startswith(file_name):
            score = 3
        elif name.endswith(file_name):
            score = 2
        elif file_name in name:
            score = 1
        else:
            continue
        if score > best_score:
            best, best_score = name, score
    if best is None:
        return None
    return _safe_child(file_dir, best)


def resolve_file_path(db_dir, file_name, create_time_ts, fuzzy=False):
    """解析文件消息在磁盘上的路径（msg/file/YYYY-MM/文件名）。

    先做精确匹配；fuzzy=True 时再退回模糊匹配（需要列目录，较慢）。
    """
    if not db_dir or not file_name:
        return None
    date_prefix = _date_prefix(create_time_ts)
    if not date_prefix:
        return None

    file_dir = os.path.join(msg_root(db_dir), 'file', date_prefix)
    if not os.path.isdir(file_dir):
        return None

    target = _safe_child(file_dir, file_name)
    if target and os.path.isfile(target):
        return target
    if not fuzzy:
        return None
    return _fuzzy_match(file_dir, file_name)


def _resolve_attach_path(db_dir, base_type, date_prefix, chat_username):
    """图片/语音/视频：在 msg/attach/<hash>/YYYY-MM/<Img|Video|Voice>/ 下找。"""
    root = msg_root(db_dir)
    attach_dir = os.path.join(root, 'attach')
    if not os.path.isdir(attach_dir):
        return None

    # 用 chat_username 的 MD5 定位 attach 子目录；找不到就退回全部候选
    target_hash = None
    if chat_username:
        candidate = hashlib.md5(chat_username.encode()).hexdigest()
        if os.path.isdir(os.path.join(attach_dir, candidate)):
            target_hash = candidate

    try:
        search_dirs = [target_hash] if target_hash else [
            d for d in os.listdir(attach_dir)
            if os.path.isdir(os.path.join(attach_dir, d))
        ]
    except OSError:
        return None

    sub_dir_name = {
        _IMAGE_TYPE: 'Img', _VIDEO_TYPE: 'Video', _VOICE_TYPE: 'Voice',
    }[base_type]

    for dir_name in search_dirs:
        sub_dir = os.path.join(attach_dir, dir_name, date_prefix, sub_dir_name)
        if not os.path.isdir(sub_dir):
            continue
        try:
            files = sorted(f for f in os.listdir(sub_dir) if not f.endswith('_h.dat'))
        except OSError:
            continue
        if files:
            # 无法从 XML 精确匹配到具体文件，返回首个候选
            return os.path.join(sub_dir, files[0])

    # 视频：也检查 msg/video/YYYY-MM/ 的封面图
    if base_type == _VIDEO_TYPE:
        video_dir = os.path.join(root, 'video', date_prefix)
        if os.path.isdir(video_dir):
            try:
                thumbs = sorted(f for f in os.listdir(video_dir) if f.endswith('_thumb.jpg'))
            except OSError:
                return None
            if thumbs:
                return os.path.join(video_dir, thumbs[0])
    return None


def resolve_media_path(db_dir, content, local_type, create_time_ts, chat_username=None):
    """解析媒体文件在磁盘上的路径。

    返回 (path, exists) 元组，path 为 None 表示无法解析。
    文件消息（49）由调用方用 appmsg.title 单独处理，不经过这里。
    """
    if not db_dir:
        return None, False

    base_type = local_type & 0xFFFFFFFF
    if base_type not in (_IMAGE_TYPE, _VOICE_TYPE, _VIDEO_TYPE):
        return None, False
    if not os.path.isdir(msg_root(db_dir)):
        return None, False

    date_prefix = _date_prefix(create_time_ts)
    if not date_prefix:
        return None, False

    path = _resolve_attach_path(db_dir, base_type, date_prefix, chat_username)
    if path:
        return path, True
    return None, False
