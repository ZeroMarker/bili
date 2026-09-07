"""live.py 单元测试（纯函数 + mock 网络，不做真实请求）。

被测模块来源见 ``live.py`` 模块文档（上游 Zarosmm/obs-bilibili-stream，GPL-2.0）。
"""

import hashlib
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import live as bili_live  # noqa: E402


class AppsignTest(unittest.TestCase):
    def test_sorts_params_and_appends_sign(self):
        signed = bili_live.appsign([("b", "2"), ("a", "1")], "K", "S")
        query = "a=1&b=2&appkey=K"
        expect = f"{query}&sign={hashlib.md5((query + 'S').encode()).hexdigest()}"
        self.assertEqual(signed, expect)

    def test_sign_is_md5_of_query_plus_secret(self):
        signed = bili_live.appsign([("room_id", "123")], bili_live.APP_KEY, bili_live.APP_SECRET)
        head, sign = signed.rsplit("&sign=", 1)
        self.assertIn("appkey=aae92bc66f3edfab", head)
        self.assertEqual(sign, hashlib.md5((head + bili_live.APP_SECRET).encode()).hexdigest())


class CookieTest(unittest.TestCase):
    def test_extract_cookie_value(self):
        cookies = "SESSDATA=abc; bili_jct=xyz; DedeUserID=123;"
        self.assertEqual(bili_live.extract_cookie_value(cookies, "SESSDATA"), "abc")
        self.assertEqual(bili_live.extract_cookie_value(cookies, "bili_jct"), "xyz")
        self.assertEqual(bili_live.extract_cookie_value(cookies, "missing"), "")

    def test_parse_set_cookies(self):
        seen = []

        class Headers:
            def get_all_matching_headers(self, name):
                seen.append(name)
                return [
                    "Set-Cookie: SESSDATA=abc; Path=/; HttpOnly\r\n",
                    "Set-Cookie: bili_jct=xyz; Path=/\r\n",
                    "Set-Cookie: buvid3=nouse\r\n",
                ]

        self.assertEqual(
            bili_live.parse_set_cookies(Headers()),
            "SESSDATA=abc; bili_jct=xyz; buvid3=nouse",
        )
        self.assertEqual(seen, ["Set-cookie"])

    def test_extract_url_param(self):
        url = "https://x.test/cb?SESSDATA=abc&bili_jct=xyz"
        self.assertEqual(bili_live.extract_url_param(url, "SESSDATA"), "abc")
        self.assertEqual(bili_live.extract_url_param(url, "missing"), "")


class SessionTest(unittest.TestCase):
    def test_save_load_roundtrip_with_0600(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sess.json"
            bili_live.save_session(path, {"cookies": "a=b", "room_id": "1"})
            self.assertEqual(bili_live.load_session(path)["room_id"], "1")
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_load_missing_returns_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bili_live.load_session(Path(tmp) / "none.json"), {})


class StartLiveTest(unittest.TestCase):
    def test_face_auth_60024_reports_qr(self):
        calls = [
            ({"code": 0, "data": {"build": 100, "curr_version": "1.0"}}, ""),
            ({"code": 60024, "message": "need face", "data": {"qr": "https://face/qr"}}, ""),
        ]
        with mock.patch.object(bili_live, "_request", side_effect=calls):
            with self.assertRaises(bili_live.BiliError) as ctx:
                bili_live.start_live("ck", "123", "csrf", 86)
        self.assertIn("https://face/qr", str(ctx.exception))

    def test_success_returns_rtmp(self):
        calls = [
            ({"code": 0, "data": {"build": 100, "curr_version": "1.0"}}, ""),
            (
                {
                    "code": 0,
                    "data": {"rtmp": {"addr": "rtmp://live/", "code": "key123"}},
                },
                "",
            ),
        ]
        with mock.patch.object(bili_live, "_request", side_effect=calls):
            addr, code = bili_live.start_live("ck", "123", "csrf", 86)
        self.assertEqual((addr, code), ("rtmp://live/", "key123"))

    def test_form_omits_build_param(self):
        # 回归：含 build 签名必回 -3（2026-09-06 实测），表单不得带 build
        seen = {}

        def fake_request(url, *, cookies="", data=None, timeout=15):
            if data is not None:
                seen["form"] = data
            if "startLive" in url:
                return ({"code": 0, "data": {"rtmp": {"addr": "rtmp://live/", "code": "k"}}}, "")
            return ({"code": 0, "data": {"build": 11025, "curr_version": "8.5.0.11025"}}, "")

        with mock.patch.object(bili_live, "_request", side_effect=fake_request):
            bili_live.start_live("ck", "123", "csrf", 646)
        self.assertNotIn("build=", seen["form"])
        self.assertIn("area_v2=646", seen["form"])


class QrRenderTest(unittest.TestCase):
    def test_square_with_quiet_zone_and_deterministic(self):
        first = bili_live.render_qr_terminal("https://example.com")
        second = bili_live.render_qr_terminal("https://example.com")
        self.assertEqual(first, second)
        lines = first.split("\n")
        self.assertGreater(len(lines), 20)  # 版本≥1 含边框
        widths = {len(line) for line in lines}
        self.assertEqual(len(widths), 1)  # 等宽
        self.assertTrue(lines[0].strip() == "")  # 顶部静区
        self.assertTrue(lines[-1].strip() == "")  # 底部静区
        self.assertTrue(all(line.startswith("  ") and line.endswith("  ") for line in lines))  # 左右静区
        self.assertIn("██", first)


if __name__ == "__main__":
    unittest.main()
