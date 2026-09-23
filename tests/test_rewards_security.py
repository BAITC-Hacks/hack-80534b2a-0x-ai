"""Rewards security checks; all writes use temporary SQLite databases."""
import concurrent.futures
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException
import rewards
import test_sessions as sessions


class RewardsHTTPTests(unittest.TestCase):
    setUpClass = classmethod(sessions.SessionTests.setUpClass.__func__)
    tearDownClass = classmethod(sessions.SessionTests.tearDownClass.__func__)
    setUp = sessions.SessionTests.setUp
    request = sessions.SessionTests.request
    login = sessions.SessionTests.login

    def test_anonymous_and_hr_denied(self):
        self.assertEqual(self.request('/api/rewards')[0], 401)
        self.login('hr')
        self.assertEqual(self.request('/api/rewards')[0], 403)
        for endpoint in ('goal', 'redeem'):
            self.assertEqual(self.request('/api/rewards/' + endpoint,
                {'reward_id': 'coffee', 'request_id': str(uuid.uuid4())},
                {'X-CSRF-Token': self.csrf})[0], 403)

    def test_mutations_require_csrf_and_same_origin(self):
        self.login()
        for endpoint in ('goal', 'redeem'):
            body = {'reward_id': 'coffee', 'request_id': str(uuid.uuid4())}
            self.assertEqual(self.request('/api/rewards/' + endpoint, body)[0], 403)
            self.assertEqual(self.request('/api/rewards/' + endpoint, body,
                {'X-CSRF-Token': self.csrf, 'Origin': 'https://evil.example'})[0], 403)

    def test_spoofed_employee_ignored_and_no_store(self):
        self.login()
        code, own, headers = self.request('/api/rewards')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        code, spoofed, _ = self.request('/api/rewards?employee_id=E0002',
            headers={'X-Employee-Id': 'E0002', 'X-Demo-Role': 'hr'})
        self.assertEqual(code, 200)
        self.assertEqual(own, spoofed)

    def test_bad_reward_and_request_id_rejected(self):
        self.login()
        headers = {'X-CSRF-Token': self.csrf}
        self.assertEqual(self.request('/api/rewards/goal', {'reward_id': 'invalid'}, headers)[0], 404)
        self.assertEqual(self.request('/api/rewards/redeem',
            {'reward_id': 'coffee', 'request_id': 'invalid'}, headers)[0], 422)


class RewardsLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'rewards.sqlite3'
        with self.connect() as conn:
            rewards.init(conn)

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=8)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def seed(self, amount):
        with self.connect() as conn:
            conn.execute('INSERT INTO quest_xp VALUES(?,?,?,?,?)',
                ('E1', 'seed', 'EV', amount, '2026-10-01'))

    def test_awards_exclude_mandatory_future_other_and_incomplete(self):
        events = {'A': {'mandatory': False}, 'M': {'mandatory': True}, 'EV_036': {'mandatory': False}}
        rows = []
        for i, (event, day, status, employee) in enumerate([
            ('A', '2026-09-01', 'completed', 'E1'),
            ('A', '2026-09-02', 'completed', 'E1'),
            ('M', '2026-09-01', 'completed', 'E1'),
            ('EV_036', '2026-09-01', 'completed', 'E1'),
            ('EV_036', '2026-09-02', 'completed', 'E1'),
            ('EV_036', '2026-10-01', 'completed', 'E1'),
            ('EV_036', '2026-11-01', 'completed', 'E1'),
            ('A', '2026-09-01', 'completed', 'E2'),
            ('A', '2026-09-01', 'dropped', 'E1'),
        ]):
            rows.append(dict(record_id=str(i), event_id=event, date=day,
                             status=status, employee_id=employee))
        with self.connect() as conn:
            rewards.sync(conn, 'E1', rows, events, '2026-10-01')
            rewards.sync(conn, 'E1', rows, events, '2026-10-01')
            self.assertEqual(rewards.balance(conn, 'E1'), (300, 0))
            self.assertEqual(rewards.balance(conn, 'E2'), (0, 0))

    def test_idempotency_conflict_and_wallet_privacy(self):
        self.seed(300)
        request_id = str(uuid.uuid4())
        with self.connect() as conn:
            rewards.redeem(conn, 'E1', 'coffee', request_id, '2026-10-01')
            first = rewards.wallet(conn, 'E1')
            rewards.redeem(conn, 'E1', 'coffee', request_id, '2026-10-01')
            self.assertEqual(first, rewards.wallet(conn, 'E1'))
            self.assertEqual(first['balance'], 150)
            self.assertTrue(first['codes'][0]['code'].startswith('DEMO-NOT-VALID-'))
            with self.assertRaises(HTTPException) as error:
                rewards.redeem(conn, 'E1', 'books', request_id, '2026-10-01')
            self.assertEqual(error.exception.detail, 'redemption_conflict')
            self.assertEqual(rewards.wallet(conn, 'E2')['codes'], [])

    def test_parallel_transactions_cannot_overspend(self):
        self.seed(300)
        def buy(_):
            try:
                with self.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    rewards.redeem(conn, 'E1', 'coffee', str(uuid.uuid4()), '2026-10-01')
                return 'ok'
            except HTTPException as error:
                return error.detail
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(buy, range(8)))
        self.assertEqual(results.count('ok'), 2)
        self.assertEqual(results.count('insufficient_xp'), 6)
        with self.connect() as conn:
            self.assertEqual(rewards.balance(conn, 'E1'), (300, 300))

    def test_parallel_same_request_spends_once(self):
        self.seed(300)
        request_id = str(uuid.uuid4())
        def buy(_):
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                rewards.redeem(conn, 'E1', 'coffee', request_id, '2026-10-01')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(buy, range(8)))
        with self.connect() as conn:
            self.assertEqual(rewards.balance(conn, 'E1'), (300, 150))


if __name__ == '__main__':
    unittest.main()
