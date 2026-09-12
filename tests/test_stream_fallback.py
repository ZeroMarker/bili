"""Stream URL protocol and TikTok fallback regression tests."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import get_stream
import tiktok_fallback

spec = importlib.util.spec_from_file_location("webui_app_stream", PROJECT_ROOT / "webui/app.py")
webui_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webui_app)


class StreamProtocolTest(unittest.TestCase):
    def test_get_stream_accepts_http_and_rtmp_protocols(self):
        for url in ("http://example/live", "https://example/live", "rtmp://example/live", "rtmps://example/live"):
            with self.subTest(url=url):
                self.assertTrue(get_stream.is_stream_url(url))
        self.assertFalse(get_stream.is_stream_url("file:///tmp/video"))

    def test_webcast_accepts_rtmp_only_response(self):
        response = mock.Mock()
        response.json.return_value = {
            "status_code": 0,
            "data": {"status": 2, "stream_url": {"rtmp_pull_url": "rtmp://example/live"}},
        }
        with mock.patch.object(tiktok_fallback, "_request_with_retry", return_value=response):
            self.assertEqual(
                tiktok_fallback.check_live_via_webcast_api(mock.Mock(), "123"),
                "rtmp://example/live",
            )

    def test_webui_probe_accepts_rtmp_output(self):
        result = mock.Mock(stdout="diagnostic\nrtmp://example/live\n", returncode=0)
        with mock.patch.object(webui_app, "run", return_value=result):
            self.assertEqual(webui_app.probe_tiktok("demo"), "rtmp://example/live")


if __name__ == "__main__":
    unittest.main()
