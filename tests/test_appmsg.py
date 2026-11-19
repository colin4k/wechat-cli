"""appmsg 解析测试 — 仅使用合成 XML，不接触真实微信数据。"""

import unittest

from wechat_cli.core.appmsg import (
    build_mini_program_url,
    is_placeholder_url,
    is_usable_url,
    parse_appmsg,
)
from wechat_cli.core.xmlsafe import sanitize_xml


def wrap(inner):
    """把 appmsg 片段包进合法的 <msg> 根节点。"""
    return f"<msg>\n<appmsg appid=\"\" sdkver=\"0\">\n{inner}\n</appmsg>\n</msg>"


LINK_XML = wrap(
    "<title><![CDATA[Solodit 检查清单详解]]></title>\n"
    "<des><![CDATA[矿工攻击即验证者利用智能合约]]></des>\n"
    "<type>5</type>\n"
    "<url><![CDATA[http://mp.weixin.qq.com/s?__biz=MjAwMDAwMDAwMA==&mid=2247538124]]></url>\n"
    "<appattach><totallen>0</totallen></appattach>\n"
    "<md5>abc123</md5>"
)

FILE_XML = wrap(
    "<title>副本资产证券化相关问题解答1202.docx</title>\n"
    "<type>6</type>\n"
    "<url></url>\n"
    "<appattach>\n"
    "  <totallen>30114</totallen>\n"
    "  <attachid>@cdn_0000000000000000</attachid>\n"
    "  <fileext>docx</fileext>\n"
    "  <cdnattachurl>0000000000000000</cdnattachurl>\n"
    "</appattach>\n"
    "<md5>0123456789abcdef0123456789abcdef</md5>"
)

MINIAPP_XML = wrap(
    "<title><![CDATA[点击进入小程序，立即领取打火机～]]></title>\n"
    "<type>33</type>\n"
    "<url><![CDATA[https://mp.weixin.qq.com/mp/waerrpage?appid=wx00000000000000aa]]></url>\n"
    "<sourceusername>gh_000000000000@app</sourceusername>\n"
    "<weappinfo>\n"
    "  <username>gh_000000000000@app</username>\n"
    "  <appid>wx00000000000000aa</appid>\n"
    "  <pagepath>pages/vending/index.html</pagepath>\n"
    "</weappinfo>"
)

CHANNEL_XML = wrap(
    "<title><![CDATA[前瞻巴菲特股东会]]></title>\n"
    "<type>51</type>\n"
    "<url><![CDATA[http://support.weixin.qq.com/cgi-bin/readtemplate?t=page/common_page__upgrade]]></url>\n"
    "<finderFeed>\n"
    "  <objectId>12345678901234567890</objectId>\n"
    "  <username>v2_0000000000000000@finder</username>\n"
    "  <nickname>腾讯财经</nickname>\n"
    "</finderFeed>"
)

# 真实微信数据里，引用消息的被引用内容用实体转义后嵌在 <content> 中
QUOTE_XML = wrap(
    "<title>全名晓得吗？</title>\n"
    "<type>57</type>\n"
    "<url></url>\n"
    "<refermsg>\n"
    "  <type>49</type>\n"
    "  <displayname>朱基刚</displayname>\n"
    "  <content>&lt;msg&gt;&lt;appmsg appid=\"\" sdkver=\"0\"&gt;"
    "&lt;title&gt;深度分析 Jito 在 Solana 的作用&lt;/title&gt;"
    "&lt;type&gt;5&lt;/type&gt;"
    "&lt;url&gt;https://example.com/jito-solana&lt;/url&gt;"
    "&lt;/appmsg&gt;&lt;/msg&gt;</content>\n"
    "</refermsg>"
)

# 另一种写法：被引用内容整段包在 CDATA 里
QUOTE_CDATA_XML = wrap(
    "<title>全名晓得吗？</title>\n"
    "<type>57</type>\n"
    "<refermsg>\n"
    "  <content><![CDATA["
    "<msg><appmsg appid=\"\" sdkver=\"0\">"
    "<title>深度分析 Jito 在 Solana 的作用</title>"
    "<type>5</type>"
    "<url>https://example.com/jito-solana</url>"
    "</appmsg></msg>"
    "]]></content>\n"
    "</refermsg>"
)

# 引用一条纯文本消息
QUOTE_TEXT_XML = wrap(
    "<title>好的</title>\n"
    "<type>57</type>\n"
    "<refermsg>\n"
    "  <type>1</type>\n"
    "  <displayname>李四</displayname>\n"
    "  <content>跟客户收的是一个点</content>\n"
    "</refermsg>"
)

PRODUCT_XML = wrap(
    "<title><![CDATA[极摩客EVO-X2迷你电脑]]></title>\n"
    "<type>36</type>\n"
    "<url><![CDATA[https://item.m.jd.com/product/10147548162123.html]]></url>\n"
    "<weappinfo>\n"
    "  <appid>wx00000000000000bb</appid>\n"
    "  <pagepath>pages/product/index.html</pagepath>\n"
    "</weappinfo>"
)


class SanitizeXmlTest(unittest.TestCase):
    def test_removes_trailing_null_bytes(self):
        self.assertEqual(sanitize_xml("<a/>\x00\x00\x00"), "<a/>")

    def test_removes_interior_null_bytes(self):
        self.assertEqual(sanitize_xml("<a\x00/>"), "<a/>")

    def test_removes_invalid_char_references(self):
        # &#11; (垂直制表符) 在 XML 1.0 中非法，会导致整篇解析失败
        self.assertNotIn("&#11;", sanitize_xml("<a>&#11;x</a>"))
        self.assertNotIn("&#xB;", sanitize_xml("<a>&#xB;x</a>"))

    def test_keeps_valid_char_references(self):
        self.assertEqual(sanitize_xml("<a>&#10;x</a>"), "<a>&#10;x</a>")


class ParseAppmsgTest(unittest.TestCase):
    def test_link_appmsg(self):
        msg = parse_appmsg(LINK_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.app_type, 5)
        self.assertEqual(msg.kind, "链接")
        self.assertEqual(msg.title, "Solodit 检查清单详解")
        self.assertEqual(msg.description, "矿工攻击即验证者利用智能合约")
        self.assertEqual(msg.url, "http://mp.weixin.qq.com/s?__biz=MjAwMDAwMDAwMA==&mid=2247538124")
        self.assertEqual(msg.md5, "abc123")

    def test_trailing_null_padding_is_tolerated(self):
        msg = parse_appmsg(LINK_XML + "\x00" * 64)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.url, "http://mp.weixin.qq.com/s?__biz=MjAwMDAwMDAwMA==&mid=2247538124")

    def test_invalid_char_reference_is_tolerated(self):
        msg = parse_appmsg(wrap("<title>含&#11;控制符</title><type>5</type><url>http://a.example/x</url>"))
        self.assertIsNotNone(msg)
        self.assertIn("含", msg.title)

    def test_file_appmsg_has_no_url_but_has_metadata(self):
        msg = parse_appmsg(FILE_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.app_type, 6)
        self.assertEqual(msg.kind, "文件")
        self.assertEqual(msg.file_size, 30114)
        self.assertEqual(msg.file_ext, "docx")
        self.assertEqual(msg.md5, "0123456789abcdef0123456789abcdef")
        self.assertIsNone(msg.url)

    def test_mini_program_placeholder_url_becomes_deep_link(self):
        msg = parse_appmsg(MINIAPP_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.app_type, 33)
        self.assertEqual(msg.kind, "小程序")
        self.assertEqual(
            msg.url,
            "weixin://dl/business/?appid=wx00000000000000aa&path=pages/vending/index.html",
        )
        self.assertEqual(msg.appid, "wx00000000000000aa")
        self.assertEqual(msg.pagepath, "pages/vending/index.html")

    def test_channel_placeholder_url_is_dropped(self):
        msg = parse_appmsg(CHANNEL_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.app_type, 51)
        self.assertEqual(msg.kind, "视频号")
        self.assertIsNone(msg.url)
        self.assertEqual(msg.object_id, "12345678901234567890")

    def test_quote_resolves_referenced_url(self):
        msg = parse_appmsg(QUOTE_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.app_type, 57)
        self.assertEqual(msg.kind, "引用")
        self.assertEqual(msg.url, "https://example.com/jito-solana")

    def test_quote_with_cdata_payload_resolves_referenced_url(self):
        msg = parse_appmsg(QUOTE_CDATA_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.url, "https://example.com/jito-solana")

    def test_quote_summary_uses_referenced_title_not_raw_xml(self):
        msg = parse_appmsg(QUOTE_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.refer_name, "朱基刚")
        self.assertEqual(msg.refer_text, "深度分析 Jito 在 Solana 的作用")
        self.assertNotIn("<", msg.refer_text)

    def test_quote_summary_keeps_plain_text_reference(self):
        msg = parse_appmsg(QUOTE_TEXT_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.refer_text, "跟客户收的是一个点")
        self.assertIsNone(msg.url)

    def test_product_keeps_real_http_url(self):
        msg = parse_appmsg(PRODUCT_XML)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.app_type, 36)
        self.assertEqual(msg.url, "https://item.m.jd.com/product/10147548162123.html")

    def test_non_appmsg_content_returns_none(self):
        self.assertIsNone(parse_appmsg("hello world"))
        self.assertIsNone(parse_appmsg(""))
        self.assertIsNone(parse_appmsg(None))

    def test_doctype_is_rejected(self):
        hostile = '<?xml version="1.0"?><!DOCTYPE msg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        self.assertIsNone(parse_appmsg(hostile + LINK_XML))

    def test_oversized_content_is_rejected(self):
        self.assertIsNone(parse_appmsg("<msg>" + "x" * 2_000_000 + "</msg>"))


class UrlHelperTest(unittest.TestCase):
    def test_placeholder_detection(self):
        self.assertTrue(is_placeholder_url("https://mp.weixin.qq.com/mp/waerrpage?appid=x"))
        self.assertTrue(
            is_placeholder_url("http://support.weixin.qq.com/cgi-bin/readtemplate?t=page/common_page__upgrade")
        )
        self.assertTrue(
            is_placeholder_url(
                "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate"
                "?t=page/common_page__upgrade&text=text001"
            )
        )
        self.assertFalse(is_placeholder_url("http://mp.weixin.qq.com/s?__biz=abc"))

    def test_group_invite_link_is_not_a_placeholder(self):
        """「邀请进群」是真实可用的地址，不能被占位页规则误伤。"""
        invite = (
            "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/addchatroombyinvite"
            "?ticket=AVd21WISPG4jlPaHiCTX8A%3D%3D"
        )
        self.assertFalse(is_placeholder_url(invite))
        self.assertTrue(is_usable_url(invite))

    def test_relative_url_is_not_usable(self):
        self.assertFalse(is_usable_url("index.html"))
        self.assertFalse(is_usable_url("pages/order/detail/detail.html?orderId=1"))

    def test_usable_url_accepts_absolute_schemes(self):
        self.assertTrue(is_usable_url("https://mp.weixin.qq.com/s?__biz=abc"))
        self.assertTrue(is_usable_url("weixin://dl/business/?appid=wxabc"))
        self.assertFalse(is_usable_url(""))

    def test_mini_program_url_prefers_appid(self):
        self.assertEqual(
            build_mini_program_url("wxabc", "pages/index.html", "gh_x@app"),
            "weixin://dl/business/?appid=wxabc&path=pages/index.html",
        )

    def test_mini_program_url_falls_back_to_username(self):
        self.assertEqual(
            build_mini_program_url(None, None, "gh_x@app"),
            "weixin://dl/business/?username=gh_x@app",
        )

    def test_mini_program_url_without_identity_is_none(self):
        self.assertIsNone(build_mini_program_url(None, None, None))



if __name__ == "__main__":
    unittest.main()
