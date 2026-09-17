"""Stream URL protocol and TikTok fallback regression tests."""

import importlib.util
import os
import signal
import sys
import tempfile
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


class ChromiumCleanupTest(unittest.TestCase):
    def _proc(self):
        proc = mock.Mock()
        proc.pid = 1234
        proc.args = ["chromium", "--headless=new"]
        proc.communicate.return_value = ("", "")
        return proc

    def test_process_group_is_terminated(self):
        proc = self._proc()
        with (
            mock.patch.object(tiktok_fallback.os, "killpg") as killpg,
            mock.patch.object(
                tiktok_fallback, "_wait_for_process_group_exit", return_value=True
            ),
        ):
            tiktok_fallback._terminate_process_group(proc, grace=2)
        killpg.assert_called_once_with(1234, signal.SIGTERM)

    def test_snap_profile_parent_is_shared(self):
        with (
            mock.patch.object(tiktok_fallback.Path, "home", return_value=Path("/home/test")),
            mock.patch.object(tiktok_fallback.Path, "mkdir") as mkdir,
            mock.patch.object(tiktok_fallback, "_remove_stale_chromium_profiles"),
        ):
            parent = tiktok_fallback._chromium_profile_parent("/snap/bin/chromium")
        self.assertEqual(parent, "/home/test/snap/chromium/common/chromium-headless")
        mkdir.assert_called_once_with(mode=0o700, parents=True, exist_ok=True)

    def test_stale_cleanup_preserves_active_and_recent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            stale = parent / "bili-tiktok-chromium-stale"
            active = parent / "bili-tiktok-chromium-active"
            recent = parent / "bili-tiktok-chromium-recent"
            unrelated = parent / "tiktok-chromium-other-project"
            for path in (stale, active, recent, unrelated):
                path.mkdir()
            os.utime(stale, (100, 100))
            os.utime(active, (100, 100))
            os.utime(recent, (950, 950))
            with (
                mock.patch.object(tiktok_fallback.time, "time", return_value=1000),
                mock.patch.object(
                    tiktok_fallback,
                    "_active_chromium_profiles",
                    return_value={str(active)},
                ),
            ):
                tiktok_fallback._remove_stale_chromium_profiles(parent, max_age=100)
            self.assertFalse(stale.exists())
            self.assertTrue(active.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())

    def test_successful_probe_always_reaps_group(self):
        proc = self._proc()
        proc.communicate.return_value = ('<script id="SIGI_STATE">{}</script>', "")
        with (
            mock.patch.object(tiktok_fallback.shutil, "which", return_value="/usr/bin/chrome"),
            mock.patch.object(tiktok_fallback.tempfile, "TemporaryDirectory") as temp_dir,
            mock.patch.object(tiktok_fallback.subprocess, "Popen", return_value=proc),
            mock.patch.object(tiktok_fallback, "_terminate_process_group") as terminate,
            mock.patch.object(
                tiktok_fallback, "_install_termination_handlers", return_value={}
            ),
            mock.patch.object(tiktok_fallback, "_restore_signal_handlers"),
        ):
            temp_dir.return_value.__enter__.return_value = "/tmp/profile"
            result = tiktok_fallback._get_stream_url_with_browser("example")
        self.assertIsNone(result)
        terminate.assert_called_once_with(proc)
        temp_dir.assert_called_once_with(prefix="bili-tiktok-chromium-", dir=None)

    def test_stop_signal_is_forwarded_to_browser_group(self):
        proc = self._proc()
        handlers = {}

        def save_handler(signum, handler):
            handlers[signum] = handler

        with (
            mock.patch.object(tiktok_fallback.signal, "getsignal", return_value=signal.SIG_DFL),
            mock.patch.object(tiktok_fallback.signal, "signal", side_effect=save_handler),
            mock.patch.object(tiktok_fallback.os, "killpg") as killpg,
        ):
            previous = tiktok_fallback._install_termination_handlers(proc)
            with self.assertRaises(SystemExit):
                handlers[signal.SIGTERM](signal.SIGTERM, None)

        self.assertEqual(set(previous), {signal.SIGTERM, signal.SIGINT, signal.SIGQUIT})
        killpg.assert_called_once_with(proc.pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
