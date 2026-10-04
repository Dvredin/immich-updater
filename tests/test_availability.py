"""Availability notifications: pure transitions plus bounded local HTTP transport."""
import contextlib
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
import availability_monitor as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.state={};self.persist=Mock();self.sender=Mock(return_value={'message_id':123,'chat_id':1,'topic_id':2})
    def check(self,okay=False,now=0):
        return monitor.check(self.state,{'healthy':okay,'reason':'test-only'},url='https://photos.example.invalid',
                             sender=self.sender,persist=self.persist,now=now)
    def test_healthy_is_silent(self):
        self.check(True);self.sender.assert_not_called();self.assertEqual(self.state['failures'],0)
    def test_transient_short_failure_does_not_alert(self):
        self.check(now=0);self.check(now=60);self.check(True,now=120);self.sender.assert_not_called()
    def test_three_failures_send_one_then_deduplicate(self):
        for now in (0,60,120,180,240):self.check(now=now)
        self.sender.assert_called_once();self.assertTrue(self.state['notified'])
    def test_pending_persisted_before_remote_send(self):
        observed=[]
        self.sender.side_effect=lambda text:(observed.append(bool(self.state.get('pending'))) or {'message_id':123})
        for now in (0,60,120):self.check(now=now)
        self.assertEqual(observed,[True]);self.assertGreater(self.persist.call_count,2)
    def test_failed_delivery_retries_without_losing_event(self):
        self.sender.side_effect=RuntimeError('test-only delivery unavailable')
        for now in (0,60,120,180,600):self.check(now=now)
        self.assertEqual(self.sender.call_count,1);self.assertIn('pending',self.state)
        self.sender.side_effect=None
        self.check(now=720);self.assertEqual(self.sender.call_count,2);self.assertTrue(self.state['notified'])
    def test_obsolete_pending_discarded_after_recovery(self):
        self.sender.side_effect=RuntimeError('test-only')
        for now in (0,60,120):self.check(now=now)
        self.check(True,now=180);self.assertNotIn('pending',self.state)
        self.sender.assert_called_once()
    def test_recovery_rearms_without_recovery_spam(self):
        for now in (0,60,120):self.check(now=now)
        self.check(True,now=180)
        for now in (240,300,360):self.check(now=now)
        self.assertEqual(self.sender.call_count,2)
    def test_dedup_survives_state_reload(self):
        for now in (0,60,120):self.check(now=now)
        self.state=json.loads(json.dumps(self.state));self.check(now=180)
        self.sender.assert_called_once()
    def test_no_secret_exception_text_stored(self):
        self.sender.side_effect=RuntimeError('DO-NOT-LOG-TEST-CANARY')
        for now in (0,60,120):self.check(now=now)
        self.assertNotIn('DO-NOT-LOG-TEST-CANARY',json.dumps(self.state))
    def test_private_state_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory(dir=os.getenv('TMPDIR')) as root:
            path=Path(root)/'state.json';monitor.save(path,{'test_only':True})
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            link=Path(root)/'link';link.symlink_to(path)
            with self.assertRaises(RuntimeError):monitor.save(link,{})
    def test_credentialled_url_rejected(self):
        with self.assertRaises(ValueError):monitor.probe('https://test:secret@example.invalid')
    def test_real_local_http_transport(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'{"res":"pong"}')
            def log_message(self,format,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:self.assertTrue(monitor.probe('http://127.0.0.1:'+str(server.server_port))['healthy'])
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

if __name__=='__main__':unittest.main()
