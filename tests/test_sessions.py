"""HTTP boundary tests against a separate server and temporary database."""
import http.cookiejar
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import auth
import main as m


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / 'sessions.sqlite3'
        cls.password = secrets.token_urlsafe(24)
        with patch.object(m, 'DB_PATH', cls.db):
            m.init_state()
            auth.create_account('employee', cls.password, 'employee', 'E0001')
            auth.create_account('hr', cls.password, 'hr')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.url = f'http://127.0.0.1:{cls.port}'
        env = {**os.environ, 'CAREER_QUEST_DB': str(cls.db), 'OPENAI_API_KEY': ''}
        cls.server = subprocess.Popen([str(m.ROOT / '.venv/Scripts/python.exe'), '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', str(cls.port)],
                                      cwd=m.ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.url + '/api/health', timeout=.2).close()
                return
            except (OSError, urllib.error.URLError):
                time.sleep(.05)
        cls.server.terminate()
        cls.server.wait()
        raise RuntimeError('Isolated test server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.temp.cleanup()

    def setUp(self):
        self.cookies = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, data=None, headers=None, method=None):
        headers = headers or {}
        if data is not None:
            headers = {'Content-Type': 'application/json', **headers}
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
        try:
            response = self.client.open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response), response.headers

    def login(self, username='employee'):
        code, data, headers = self.request('/api/auth/login', {'username': username, 'password': self.password})
        self.assertEqual(code, 200)
        self.csrf = data['csrf_token']
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=strict', headers['Set-Cookie'])
        return data

    def test_headers_cannot_grant_access(self):
        for path in ['/api/hr', '/api/profile/E0001', '/api/recommendations/E0001', '/api/demo']:
            code, _, _ = self.request(path, headers={'X-Demo-Role': 'hr', 'X-Employee-Id': 'E0001'})
            self.assertEqual(code, 401, path)

    def test_employee_cannot_impersonate_hr_or_another_employee(self):
        self.login()
        self.assertEqual(self.request('/api/profile/E0001')[0], 200)
        spoof = {'X-Demo-Role': 'hr', 'X-Employee-Id': 'E0002', 'X-CSRF-Token': self.csrf}
        for path in ['/api/hr', '/api/profile/E0002', '/api/recommendations/E0002']:
            self.assertEqual(self.request(path, headers=spoof)[0], 403)
        self.assertEqual(self.request('/api/complete', {'employee_id': 'E0002', 'event_id': 'EV_005'}, spoof)[0], 403)
        self.assertEqual(self.request('/api/import', {}, spoof)[0], 403)

    def test_hr_preview_is_read_only(self):
        self.login('hr')
        self.assertEqual(self.request('/api/hr')[0], 200)
        self.assertEqual(self.request('/api/profile/E0001')[0], 200)
        self.assertEqual(self.request('/api/recommendations/E0001')[0], 200)
        self.assertEqual(self.request('/api/complete', {'employee_id': 'E0001', 'event_id': 'EV_005'}, {'X-CSRF-Token': self.csrf})[0], 403)

    def test_csrf_and_cross_origin_rejected(self):
        self.login()
        self.assertEqual(self.request('/api/auth/logout', {})[0], 403)
        self.assertEqual(self.request('/api/auth/logout', {}, {'X-CSRF-Token': self.csrf, 'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.request('/api/auth/session')[0], 200)

    def test_logout_revokes_cookie_even_when_replayed(self):
        self.login()
        token = next(iter(self.cookies)).value
        self.assertEqual(self.request('/api/auth/logout', {}, {'X-CSRF-Token': self.csrf})[0], 200)
        self.assertEqual(self.request('/api/profile/E0001', headers={'Cookie': f'{auth.COOKIE}={token}'})[0], 401)

    def test_session_expiry(self):
        self.login()
        token = next(iter(self.cookies)).value
        with patch.object(m, 'DB_PATH', self.db), m.connect() as conn:
            conn.execute('UPDATE sessions SET expires=0 WHERE token_hash=?', (auth.token_hash(token),))
        self.assertEqual(self.request('/api/auth/session')[0], 401)

    def test_credentials_and_login_origin(self):
        code, _, _ = self.request('/api/auth/login', {'username': 'employee', 'password': 'wrong'})
        self.assertEqual(code, 401)
        code, _, _ = self.request('/api/auth/login', {'username': 'hr', 'password': self.password}, {'Origin': 'https://other.example'})
        self.assertEqual(code, 403)

    def test_no_password_or_token_stored_in_plaintext(self):
        self.login()
        token = next(iter(self.cookies)).value
        with patch.object(m, 'DB_PATH', self.db), m.connect() as conn:
            account = conn.execute('SELECT password_hash FROM accounts WHERE username=\'employee\'').fetchone()[0]
            row = conn.execute('SELECT token_hash FROM sessions WHERE token_hash=?', (auth.token_hash(token),)).fetchone()
        self.assertNotEqual(account, self.password)
        self.assertIsNotNone(row)
        self.assertNotEqual(row[0], token)
        self.assertEqual(self.request('/api/profile/E0001')[2]['Cache-Control'], 'no-store')

    def test_openai_key_hr_only_and_csrf(self):
        self.assertEqual(self.request('/api/settings/openai')[0], 401)
        self.login()
        self.assertEqual(self.request('/api/settings/openai')[0], 403)
        self.assertEqual(self.request('/api/settings/openai', {'api_key': 'sk-not-a-real-key-for-testing'}, {'X-CSRF-Token': self.csrf})[0], 403)

    def test_openai_key_applies_without_echo_and_can_be_removed(self):
        self.login('hr')
        key = 'sk-not-a-real-key-for-testing'
        self.assertEqual(self.request('/api/settings/openai', {'api_key': key})[0], 403)
        headers = {'X-CSRF-Token': self.csrf}
        try:
            code, result, _ = self.request('/api/settings/openai', {'api_key': key}, headers)
            self.assertEqual(code, 200)
            self.assertTrue(result['configured'])
            self.assertNotIn(key, json.dumps(result))
            code, status, _ = self.request('/api/settings/openai')
            self.assertTrue(status['configured'])
            self.assertNotIn(key, json.dumps(status))
            self.assertEqual(self.request('/api/settings/openai', {'api_key': 'bad'}, headers)[0], 400)
            self.assertTrue(self.request('/api/settings/openai')[1]['configured'])
            with patch.object(m, 'DB_PATH', self.db), m.connect() as conn:
                self.assertNotIn(key, '\n'.join(conn.iterdump()))
        finally:
            code, result, _ = self.request('/api/settings/openai', headers=headers, method='DELETE')
            self.assertEqual(code, 200)
            self.assertFalse(result['configured'])


if __name__ == '__main__':
    unittest.main()
