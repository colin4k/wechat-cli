"""安全 XML 解析 — 处理来自微信消息体的 XML。

消息体是外部数据，可能含有 NUL 填充、非法字符引用或 DTD 实体声明。
本模块统一做清洗和防护，供 appmsg 与通话消息共用。
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional

# 体积上限：正常 appmsg 只有几 KB，超过这个量级必然不是消息
XML_MAX_LEN = 1_000_000

# 内部 DTD 实体声明会被 expat 展开（billion laughs），直接拒绝
_XML_UNSAFE_RE = re.compile(r'<!DOCTYPE|<!ENTITY', re.IGNORECASE)

# 微信在消息体尾部补的 NUL 填充字节，会让整篇 XML 解析失败
_NULL_BYTE_RE = re.compile('\x00')

# 字符引用（&#10; / &#xA;），用来剔除 XML 1.0 不允许的码点：
# &#0;-&#8;、&#11;-&#31;、&#xFFFE;/&#xFFFF; 等会让 expat 抛
# "reference to invalid character number"，整条消息连同里面的 url 一起被丢弃。
_CHAR_REF_RE = re.compile(r'&#(?:[xX]([0-9a-fA-F]+)|([0-9]+));')


def _is_valid_xml_char(code):
    """XML 1.0 允许的字符码点。"""
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _strip_invalid_char_refs(text):
    def replace(match):
        hex_digits, dec_digits = match.group(1), match.group(2)
        try:
            code = int(hex_digits, 16) if hex_digits else int(dec_digits)
        except (TypeError, ValueError):
            return ''
        return match.group(0) if _is_valid_xml_char(code) else ''

    return _CHAR_REF_RE.sub(replace, text)


def sanitize_xml(text: Optional[str]) -> str:
    """清洗消息体：去掉 NUL 填充字节与非法字符引用。"""
    if not text:
        return ''
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
    if not isinstance(text, str):
        return ''
    cleaned = _NULL_BYTE_RE.sub('', text)
    cleaned = _strip_invalid_char_refs(cleaned)
    return cleaned.strip()


def _candidates(text: str):
    """优先整体解析；失败时丢弃首个 '<' 之前的纯文本前缀再试一次。

    只有当前缀里没有任何尖括号时才允许回退——否则说明这段内容本身就
    含 XML 片段，切掉前半段只会解析出一个并不完整的假消息。
    """
    yield text
    start = text.find('<')
    if start > 0 and '>' not in text[:start] and '<' not in text[:start]:
        yield text[start:]


def parse_xml(content: Optional[str]):
    """把消息体解析成 Element；输入不安全、超长或解析失败时返回 None。

    这是唯一允许调用 ET.fromstring 的入口：务必在解析前完成清洗与防护检查。
    """
    if not content:
        return None

    text = sanitize_xml(content)
    if not text or len(text) > XML_MAX_LEN:
        return None
    if _XML_UNSAFE_RE.search(text):
        return None

    for candidate in _candidates(text):
        try:
            return ET.fromstring(candidate)
        except ET.ParseError:
            continue
    return None
