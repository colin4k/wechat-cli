"""appmsg 解析 — 从 49 型（链接/文件）消息的 XML 中提取结构化信息。

微信把链接、文件、小程序、视频号、引用等非文本消息统一存成 base_type=49 的
"appmsg" 消息，内容是一段 XML。本模块负责把这段 XML 还原成结构化字段，
尤其是可直接跳转的 url。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional
from urllib.parse import quote, urlsplit

from .xmlsafe import parse_xml

# 被引用消息摘要的最大长度
_REFER_TEXT_MAX_LEN = 160
# 引用嵌套上限，防止异常数据把递归拖长
_MAX_QUOTE_DEPTH = 3

# 微信的占位/降级页：域名 + 路径前缀。用 (域名, 路径) 精确匹配，
# 避免把路径里恰好含有这些字样的真实链接一并误杀。
# 注意 support.weixin.qq.com/cgi-bin/mmsupport-bin/addchatroombyinvite
# 是真实的「邀请进群」链接，不在列表里。
_PLACEHOLDER_PATHS = (
    ('mp.weixin.qq.com', '/mp/waerrpage'),
    ('support.weixin.qq.com', '/cgi-bin/mmsupport-bin/readtemplate'),
    ('support.weixin.qq.com', '/cgi-bin/readtemplate'),
    ('support.weixin.qq.com', '/security/readtemplate'),
    ('support.weixin.qq.com', '/update/'),
)

# 可用地址的协议白名单。微信有些消息只写了相对路径（如 "index.html"），
# 那不是能打开的地址。
_USABLE_URL_SCHEMES = ('http', 'https', 'weixin', 'wxwork')

APP_TYPE_LABELS = {
    5: '链接',
    6: '文件',
    19: '合并转发',
    33: '小程序',
    36: '小程序',
    44: '小程序',
    51: '视频号',
    57: '引用',
    62: '短视频',
    2000: '红包',
    2001: '红包',
}
DEFAULT_KIND = '链接/文件'

# 小程序类：微信写的是占位页，需要用 appid 拼深链
MINIAPP_TYPES = (33, 36, 44)
FILE_APP_TYPE = 6
PRODUCT_APP_TYPE = 36
QUOTE_APP_TYPE = 57
_DEEP_LINK_APP_TYPES = (33, 44)


@dataclass(frozen=True)
class AppMessage:
    """一条 appmsg 消息的结构化内容。"""

    app_type: int
    kind: str
    title: str = ''
    description: str = ''
    url: Optional[str] = None
    source: Optional[str] = None
    file_size: Optional[int] = None
    file_ext: Optional[str] = None
    md5: Optional[str] = None
    appid: Optional[str] = None
    pagepath: Optional[str] = None
    object_id: Optional[str] = None
    feed_username: Optional[str] = None
    refer_name: Optional[str] = None
    refer_text: Optional[str] = None

    def with_type(self, app_type: int) -> "AppMessage":
        """返回替换过 app_type 的新实例（不修改原对象）。"""
        return replace(
            self,
            app_type=app_type,
            kind=APP_TYPE_LABELS.get(app_type, DEFAULT_KIND),
        )


def collapse_text(text: Optional[str]) -> str:
    """把连续空白折叠成单个空格并去掉首尾空白。"""
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def is_placeholder_url(url: Optional[str]) -> bool:
    """判断 url 是否是微信的占位/降级页。"""
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = parts.netloc.lower()
    path = parts.path
    return any(
        host == marker_host and path.startswith(marker_path)
        for marker_host, marker_path in _PLACEHOLDER_PATHS
    )


def normalize_url(url: Optional[str]) -> Optional[str]:
    """把协议相对地址（//host/path）补成 https。"""
    if url and url.startswith('//'):
        return 'https:' + url
    return url


def is_usable_url(url: Optional[str]) -> bool:
    """判断 url 是否是一个能打开的绝对地址。"""
    url = normalize_url(url)
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in _USABLE_URL_SCHEMES:
        return False
    return not is_placeholder_url(url)


def build_mini_program_url(
    appid: Optional[str],
    pagepath: Optional[str] = '',
    username: Optional[str] = '',
) -> Optional[str]:
    """构造小程序深链。appid 缺失时退回到公众号 username。"""
    if appid:
        url = f'weixin://dl/business/?appid={quote(appid, safe="")}'
        path, _, query = (pagepath or '').partition('?')
        if path:
            url += f'&path={quote(path.lstrip("/"), safe="/")}'
        if query:
            url += f'&query={quote(query, safe="")}'
        return url
    if username:
        return f'weixin://dl/business/?username={quote(username, safe="@")}'
    return None


def _parse_int(value: Optional[str], fallback=None):
    try:
        return int((value or '').strip())
    except (TypeError, ValueError):
        return fallback


def _child_text(node, tag: str) -> str:
    if node is None:
        return ''
    return collapse_text(node.findtext(tag))


def _raw_child_text(node, tag: str) -> str:
    """取原始文本（保留换行），用于内嵌 XML 片段。"""
    if node is None:
        return ''
    return node.findtext(tag) or ''


def _resolve_url(app_type, raw_url, appid, pagepath, mini_username, inner):
    """按类型挑选最可靠的 url。

    - 文件(6)：没有可访问地址，返回 None，由上层用本地路径生成 file://
    - 引用(57)：取被引用消息里的 url
    - 小程序(33/44)：微信写的是占位页，改用 appid 拼深链
    - 商品(36)：优先真实商品链接，否则退回深链
    - 其余：直接用 <url>，但排除占位页与相对路径
    """
    if app_type == FILE_APP_TYPE:
        return None

    if app_type == QUOTE_APP_TYPE:
        if inner is not None and inner.url:
            return inner.url
        return normalize_url(raw_url) if is_usable_url(raw_url) else None

    if app_type in _DEEP_LINK_APP_TYPES:
        deep_link = build_mini_program_url(appid, pagepath, mini_username)
        if deep_link:
            return deep_link
        return normalize_url(raw_url) if is_usable_url(raw_url) else None

    if app_type == PRODUCT_APP_TYPE:
        if is_usable_url(raw_url):
            return normalize_url(raw_url)
        return build_mini_program_url(appid, pagepath, mini_username)

    return normalize_url(raw_url) if is_usable_url(raw_url) else None


def _refer_summary(refer, inner):
    """引用消息里被引用内容的摘要。

    被引用的可能是一段 XML（原文消息），这时用它的标题代替原始 XML。
    """
    if refer is None:
        return None
    raw = _raw_child_text(refer, 'content')
    if not raw:
        return None
    text = ''
    if inner is not None:
        text = inner.title or inner.description
    if not text:
        text = collapse_text(raw)
    if not text:
        return None
    if len(text) > _REFER_TEXT_MAX_LEN:
        text = text[:_REFER_TEXT_MAX_LEN] + '...'
    return text


def _parse_appmsg_element(appmsg, depth: int):
    """从 <appmsg> 元素里抽出各分组字段。"""
    weapp = appmsg.find('weappinfo')
    source = _child_text(appmsg, 'sourceusername') or None

    finder = appmsg.find('finderFeed')
    attach = appmsg.find('appattach')
    refer = appmsg.find('refermsg')

    # 被引用的原文可能是一段 XML（原文消息），递归解析一次拿到标题。
    # 深度检查放在递归调用之前，避免异常数据把递归拖长。
    inner = None
    if refer is not None and depth < _MAX_QUOTE_DEPTH:
        inner = parse_appmsg(_raw_child_text(refer, 'content'), depth=depth + 1)

    return {
        'app_type': _parse_int(appmsg.findtext('type'), 0),
        'title': _child_text(appmsg, 'title'),
        'description': _child_text(appmsg, 'des'),
        'raw_url': _child_text(appmsg, 'url'),
        'source': source,
        'md5': _child_text(appmsg, 'md5') or None,
        'appid': _child_text(weapp, 'appid') or None,
        'pagepath': _child_text(weapp, 'pagepath') or None,
        'mini_username': _child_text(weapp, 'username') or source,
        'object_id': _child_text(finder, 'objectId') or None,
        'feed_username': _child_text(finder, 'username') or None,
        'file_size': _parse_int(_child_text(attach, 'totallen')) if attach is not None else None,
        'file_ext': _child_text(attach, 'fileext') or None,
        'refer_name': _child_text(refer, 'displayname') or None,
        'refer_text': _refer_summary(refer, inner),
        'inner': inner,
    }


def parse_appmsg(content: Optional[str], depth: int = 0) -> Optional[AppMessage]:
    """解析 appmsg XML，返回结构化内容；不是 appmsg 或解析失败时返回 None。"""
    root = parse_xml(content)
    if root is None:
        return None

    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return None

    fields = _parse_appmsg_element(appmsg, depth)
    url = _resolve_url(
        fields['app_type'], fields['raw_url'], fields['appid'],
        fields['pagepath'], fields['mini_username'], fields['inner'],
    )

    return AppMessage(
        app_type=fields['app_type'],
        kind=APP_TYPE_LABELS.get(fields['app_type'], DEFAULT_KIND),
        title=fields['title'],
        description=fields['description'],
        url=url,
        source=fields['source'],
        file_size=fields['file_size'],
        file_ext=fields['file_ext'],
        md5=fields['md5'],
        appid=fields['appid'],
        pagepath=fields['pagepath'],
        object_id=fields['object_id'],
        feed_username=fields['feed_username'],
        refer_name=fields['refer_name'],
        refer_text=fields['refer_text'],
    )
