"""Functional checks for demo XP; no production DB or external provider used."""
import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import main as m
import rewards
from auth import Principal


class RewardsFunctionalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(m, 'DB_PATH', Path(self.temp.name) / 'rewards.sqlite3')
        self.db_patch.start()
        m.init_state()
        self.employee = Principal('reward-check', 'employee', 'E0001')

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def row(self, event='EV_036', day='2026-09-20', **extra):
        return dict(record_id=uuid.uuid4().hex, employee_id='E0001', event_id=event,
                    date=day, status='completed', **extra)

    def test_ordinary_event_awarded_once_repeat_event_once_per_month(self):
        m.STATE['history'] = [self.row('EV_005'), self.row('EV_005', '2026-09-21'),
                              self.row(), self.row(day='2026-09-21'), self.row(day='2026-08-21')]
        wallet = m.get_rewards(self.employee)
        self.assertEqual(wallet['earned'], 300)
        self.assertEqual(m.get_rewards(self.employee), wallet)

    def test_mandatory_future_unfinished_and_other_employee_excluded(self):
        unfinished = self.row(); unfinished['status'] = 'dropped'
        other = self.row(); other['employee_id'] = 'E0002'
        m.STATE['history'] = [self.row('EV_001'), self.row(day='2026-10-02'), unfinished, other]
        self.assertEqual(m.get_rewards(self.employee)['balance'], 0)

    def test_snapshot_day_included_and_review_does_not_limit_xp(self):
        m.STATE['employees']['E0001']['last_review_date'] = '2026-09-30'
        m.STATE['history'] = [self.row(day='2026-08-01'), self.row(day='2026-10-01')]
        self.assertEqual(m.get_rewards(self.employee)['earned'], 200)

    def test_catalog_is_localized_and_demo_codes_persist(self):
        wallet = m.get_rewards(self.employee)
        self.assertEqual(len(wallet['catalog']), 6)
        for item in wallet['catalog']:
            self.assertEqual(set(item['name']), {'ru', 'kk', 'en'})
            self.assertEqual(set(item['offer']), {'ru', 'kk', 'en'})
        self.assertGreaterEqual(wallet['balance'], 150)
        result = m.redeem_reward(m.RedeemRequest(reward_id='coffee', request_id=uuid.uuid4()), self.employee)
        self.assertTrue(result['codes'][0]['code'].startswith('DEMO-NOT-VALID-'))
        m.init_state()
        self.assertEqual(m.get_rewards(self.employee), result)

    def test_goal_persists_without_spending_and_unknown_goal_is_rejected(self):
        before = m.get_rewards(self.employee)
        after = m.set_reward_goal(m.RewardRequest(reward_id='market'), self.employee)
        self.assertEqual(after['goal'], 'market')
        self.assertEqual(after['balance'], before['balance'])
        m.init_state()
        self.assertEqual(m.get_rewards(self.employee)['goal'], 'market')
        with self.assertRaises(HTTPException) as error:
            m.set_reward_goal(m.RewardRequest(reward_id='unknown'), self.employee)
        self.assertEqual(error.exception.status_code, 404)

    def test_exchange_is_idempotent_and_conflicting_retry_rejected(self):
        request_id = uuid.uuid4()
        first = m.redeem_reward(m.RedeemRequest(reward_id='coffee', request_id=request_id), self.employee)
        self.assertEqual(m.redeem_reward(m.RedeemRequest(reward_id='coffee', request_id=request_id), self.employee), first)
        with self.assertRaises(HTTPException) as error:
            m.redeem_reward(m.RedeemRequest(reward_id='books', request_id=request_id), self.employee)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(m.get_rewards(self.employee), first)

    def test_insufficient_funds_do_not_create_code_or_spend(self):
        m.STATE['history'] = []
        with self.assertRaises(HTTPException) as error:
            m.redeem_reward(m.RedeemRequest(reward_id='coffee', request_id=uuid.uuid4()), self.employee)
        self.assertEqual(error.exception.detail, 'insufficient_xp')
        self.assertEqual(m.get_rewards(self.employee)['codes'], [])
        self.assertEqual(m.get_rewards(self.employee)['spent'], 0)

    def test_exchange_and_goal_do_not_change_skills_history_or_recommendations(self):
        employee = m.STATE['employees']['E0001']
        before = copy.deepcopy(m.progress(employee))
        history = copy.deepcopy(m.STATE['history'])
        candidates = copy.deepcopy(m.eligible_candidates(employee))
        m.set_reward_goal(m.RewardRequest(reward_id='cinema'), self.employee)
        m.redeem_reward(m.RedeemRequest(reward_id='coffee', request_id=uuid.uuid4()), self.employee)
        self.assertEqual(m.progress(employee), before)
        self.assertEqual(m.STATE['history'], history)
        self.assertEqual(m.eligible_candidates(employee), candidates)

    def test_completion_and_xp_are_atomic_on_award_failure(self):
        candidate = m.eligible_candidates(m.STATE['employees']['E0001'])[0]['event']['event_id']
        before = copy.deepcopy(m.STATE['history'])
        with m.connect() as conn:
            rows_before = conn.execute('SELECT COUNT(*) FROM activity_history').fetchone()[0]
        with patch.object(rewards, 'sync', side_effect=RuntimeError('simulated XP storage failure')):
            with self.assertRaises(RuntimeError):
                m.complete_activity(m.CompleteRequest(employee_id='E0001', event_id=candidate), self.employee)
        self.assertEqual(m.STATE['history'], before)
        with m.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM activity_history').fetchone()[0], rows_before)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM quest_xp').fetchone()[0], 0)

    def test_completion_awards_xp_and_survives_restart(self):
        before = m.get_rewards(self.employee)
        candidate = next(c['event']['event_id'] for c in m.eligible_candidates(m.STATE['employees']['E0001']) if c['event']['event_id'] != 'EV_036')
        result = m.complete_activity(m.CompleteRequest(employee_id='E0001', event_id=candidate), self.employee)
        self.assertEqual(result['xp_awarded'], 100)
        m.init_state()
        self.assertEqual(m.get_rewards(self.employee)['earned'], before['earned'] + 100)

    def test_imported_profile_history_awards_xp_without_code_changes(self):
        employee = copy.deepcopy(m.STATE['employees']['E0001'])
        employee['employee_id'] = 'REWARD_CONTROL'
        history = [copy.deepcopy(r) for r in m.STATE['history'] if r['employee_id'] == 'E0001']
        for row in history:
            row['record_id'] = 'REWARD_' + row['record_id']
            row['employee_id'] = employee['employee_id']
        m.persist_import([employee], history)
        imported = Principal('imported', 'employee', employee['employee_id'])
        self.assertEqual(m.get_rewards(imported)['earned'], m.get_rewards(self.employee)['earned'])


if __name__ == '__main__':
    unittest.main()
