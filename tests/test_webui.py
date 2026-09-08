"""WebUI regression tests: no real services, credentials or upstream requests."""
import http.client
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('webui_app', Path(__file__).resolve().parents[1] / 'webui/app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class WebUITest(unittest.TestCase):
    def test_first_sync_private_atomic_and_shell_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config'
            session = root / 'session.json'
            code = "test'\"$NOT_EXPANDED`false`&value"
            session.write_text(json.dumps({'rtmp_addr': 'rtmp://example/', 'rtmp_code': code}))
            output = config / 'push.env'
            with mock.patch.object(app, 'CONFIG_DIR', config), mock.patch.object(app, 'PUSH_ENV', output), mock.patch.object(app, 'SESSION_FILE', session):
                app.sync_push_env()
                app.sync_push_env()
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.stat().st_mode & 0o777, 0o700)
            result = subprocess.run(['bash', '-c', 'source "$1"; printf "%s" "$BILIBILI_PUSH_CODE"', 'test', str(output)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, code)
            self.assertEqual(list(config.iterdir()), [output])

    def test_concurrent_controls_rejected_and_lock_released(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def block(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timeout')
        def stop():
            try:
                app.stop_all()
            except Exception as exc:
                errors.append(exc)
        with mock.patch.object(app, '_ctl', side_effect=block), mock.patch.object(app, 'status', return_value={}):
            worker = threading.Thread(target=stop)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(app.ControlBusy):
                    app.set_mode({'mode': 'live', 'target': 'test'})
                with self.assertRaises(app.ControlBusy):
                    app.room({'action': 'stop'})
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(errors)
        with self.assertRaises(ValueError):
            app.set_mode({'mode': 'invalid'})
        self.assertFalse(app.CONTROL_LOCK.locked())

    def test_failed_stop_never_starts_new_mode(self):
        with mock.patch.object(app, 'ensure_room_live'), mock.patch.object(app, '_write_env'), mock.patch.object(app, '_ctl') as ctl, mock.patch.object(app, '_wait_inactive', return_value=False):
            with self.assertRaises(RuntimeError):
                app.set_mode({'mode': 'live', 'target': 'demo', 'force': True})
        ctl.assert_called_once_with('disable', '--now', app.REPLAY_UNIT)

    def test_deactivating_is_not_stopped(self):
        with mock.patch.object(app, '_show', side_effect=[{'active': 'deactivating'}, {'active': 'inactive'}]) as show, mock.patch('time.sleep'):
            self.assertTrue(app._wait_inactive(app.LIVE_UNIT))
            self.assertEqual(show.call_count, 2)

    def test_http_validation_and_busy_response(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(app.Handler, 'log_message'), mock.patch.object(app, 'stop_all', return_value={'ok': True}) as stop:
                for body in ('[]', 'null', '"text"'):
                    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
                    conn.request('POST', '/api/stop', body, {'Content-Type': 'application/json'})
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 400)
                    self.assertIn('error', json.loads(resp.read()))
                    conn.close()
                stop.assert_not_called()
            with mock.patch.object(app.Handler, 'log_message'), mock.patch.object(app, 'stop_all', side_effect=app.ControlBusy('busy')):
                conn = http.client.HTTPConnection(*server.server_address, timeout=2)
                conn.request('POST', '/api/stop', '{}')
                resp = conn.getresponse()
                self.assertEqual(resp.status, 409)
                resp.read()
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
