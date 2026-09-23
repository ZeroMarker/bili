"""Stream URL protocol and TikTok fallback regression tests."""

import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    def test_rendered_sigi_extracts_video_flv_before_audio(self):
        sigi = {
            "LiveRoom": {"liveRoomUserInfo": {"liveRoom": {
                "streamData": {"pull_data": {"streams": [
                    {"url": "https://cdn.example/audio.flv?only_audio=1"},
                    {"url": "https://cdn.example/video.flv"},
                    {"url": "https://cdn.example/video.m3u8"},
                ]}
            }}}}
        }
        html = '<script id="SIGI_STATE">' + __import__("json").dumps(sigi) + "</script>"
        self.assertEqual(
            tiktok_fallback._stream_url_from_sigi(html),
            "https://cdn.example/video.flv",
        )

    def test_stale_stream_data_rejected_unless_live(self):
        """离线页（status=4）残留的陈旧 streamData 必须拒绝；直播（status=2）放行。

        陈旧 FLV 永远 404：放行会把死地址交给推流循环空转。
        """
        def html_for(live_room):
            sigi = {"LiveRoom": {"liveRoomUserInfo": {"liveRoom": live_room}}}
            return '<script id="SIGI_STATE">' + __import__("json").dumps(sigi) + "</script>"

        stale = {"status": 4, "streamData": {"flv": "https://cdn.example/stale.flv"}}
        self.assertIsNone(tiktok_fallback._stream_url_from_sigi(html_for(stale)))
        live = {"status": 2, "streamData": {"flv": "https://cdn.example/live.flv"}}
        self.assertEqual(
            tiktok_fallback._stream_url_from_sigi(html_for(live)),
            "https://cdn.example/live.flv",
        )





class BrowserdClientTest(unittest.TestCase):
    """共享渲染客户端：请求参数要送对，服务不可用必须降级为 None。"""

    def _serve(self, body: bytes):
        """启动一次性 stub 渲染服务，请求体记录在 self.captured。"""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.captured = json.loads(self.rfile.read(length))
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_probe_renders_via_service_and_extracts(self):
        sigi = {"LiveRoom": {"liveRoomUserInfo": {"liveRoom": {
            "status": 2, "streamData": {"flv": "https://cdn.example/live.flv"}
        }}}}
        body = ('<script id="SIGI_STATE">' + json.dumps(sigi) + "</script>").encode()
        server = self._serve(body)
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        with mock.patch.dict(os.environ, {"TIKTOK_BROWSERD_URL": endpoint}):
            url = tiktok_fallback._get_stream_url_with_browser("x", timeout=5)
        self.assertEqual(url, "https://cdn.example/live.flv")
        self.assertEqual(self.captured["url"], "https://www.tiktok.com/@x/live")
        self.assertEqual(self.captured["timeout"], 5)
        self.assertIn("streamData", self.captured["wait_js"])

    def test_probe_returns_none_when_service_down(self):
        with mock.patch.dict(os.environ, {"TIKTOK_BROWSERD_URL": "http://127.0.0.1:1"}):
            self.assertIsNone(
                tiktok_fallback._get_stream_url_with_browser("x", timeout=2)
            )


if __name__ == "__main__":
    unittest.main()
