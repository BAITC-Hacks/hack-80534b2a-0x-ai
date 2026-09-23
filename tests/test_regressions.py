"""Regression coverage for dataset semantics and recommendation personalization."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import main as m
from auth import Principal


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(m, 'DB_PATH', Path(self.temp.name) / 'regressions.sqlite3')
        self.db.start()
        m.init_state()
        self.employee = m.STATE['employees']['E0001']
        self.employee.update(hire_date='2024-01-01', last_review_date='2026-09-01')
        m.STATE['history'] = []

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

    def record(self, **overrides):
        row = dict(record_id='REG_1', employee_id='E0001', event_id='EV_005',
                   date='2026-09-20', due_date='', status='completed',
                   completion_pct='100', score='', feedback_rating='', assigned_by='self')
        row.update(overrides)
        return row

    def event(self, event_id, skill='SK_SYSTEM_DESIGN', **overrides):
        event = copy.deepcopy(m.STATE['events']['EV_005'])
        event.update(event_id=event_id, mandatory=False, type='course', format='self_paced',
                     target_roles=[self.employee['role']], target_grades=['Junior', 'Middle', 'Senior', 'Lead'],
                     prerequisites={}, upcoming_sessions=[],
                     develops_skills=[dict(skill_id=skill, gain=1, max_level=5)])
        event.update(overrides)
        return event

    def configure_targets(self):
        self.employee.update(role='Backend Engineer', grade='Middle', skills={},
                             career_goal={'target_role': 'Frontend Engineer', 'target_grade': 'Senior'})
        for profile in m.STATE['skills']['role_profiles']:
            if profile['role'] == 'Backend Engineer' and profile['grade'] in ('Senior', 'Lead'):
                profile.update(required_skills={'SK_SYSTEM_DESIGN': 3}, critical_skills=['SK_SYSTEM_DESIGN'])
            if profile['role'] == 'Frontend Engineer' and profile['grade'] == 'Senior':
                profile.update(required_skills={'SK_TYPESCRIPT': 3}, critical_skills=['SK_TYPESCRIPT'])

    def test_repeated_voluntary_completion_rejected_in_batch(self):
        first = self.record()
        second = self.record(record_id='REG_2', date='2026-09-21')
        self.assertTrue(m.validate_import([], [first, second]))
        self.assertTrue(m.validate_import([], [second, first]), 'Validation must use dates, not CSV row order')

    def test_repeated_completion_rejected_against_existing_history(self):
        m.STATE['history'] = [self.record()]
        self.assertTrue(m.validate_import([], [self.record(record_id='REG_2', date='2026-09-21')]))

    def test_noncompleted_participation_after_completion_rejected(self):
        for status, percent in [('in_progress', '20'), ('dropped', '20'), ('declined', '0')]:
            with self.subTest(status=status):
                first = self.record()
                second = self.record(record_id='REG_2', date='2026-09-21', status=status,
                                     completion_pct=percent, assigned_by='manager')
                self.assertTrue(m.validate_import([], [first, second]))

    def test_club_repeat_is_allowed(self):
        first = self.record(event_id='EV_036')
        second = self.record(record_id='REG_2', event_id='EV_036', date='2026-09-21')
        self.assertEqual(m.validate_import([], [first, second]), [])
        m.STATE['history'] = [first]
        self.assertEqual(m.validate_import([], [second]), [])

    def test_annual_mandatory_repeat_preserves_dataset_compatibility(self):
        first = self.record(event_id='EV_001', assigned_by='hr')
        second = self.record(record_id='REG_2', event_id='EV_001', assigned_by='hr', date='2026-09-21')
        self.assertEqual(m.validate_import([], [first, second]), [])

    def test_no_show_requires_scheduled_event(self):
        event = next(e for e in m.STATE['events'].values() if e['format'] == 'self_paced')
        self.assertTrue(m.validate_import([], [self.record(event_id=event['event_id'], status='no_show', completion_pct='0')]))

    def test_overdue_requires_mandatory_event_and_past_due_date(self):
        base = self.record(event_id='EV_001', status='overdue', completion_pct='0', assigned_by='hr', due_date='2026-09-30')
        # For self-paced courses date is enrollment: it legitimately precedes due_date.
        self.assertEqual(m.validate_import([], [base]), [])
        for change in [{'event_id': 'EV_005'}, {'due_date': ''}, {'due_date': '2026-10-02'}]:
            with self.subTest(change=change):
                self.assertTrue(m.validate_import([], [{**base, **change}]))

    def test_declined_requires_manager_or_hr_assignment(self):
        row = self.record(status='declined', completion_pct='0')
        self.assertTrue(m.validate_import([], [row]))
        for assignment in ['manager', 'hr']:
            self.assertEqual(m.validate_import([], [{**row, 'assigned_by': assignment}]), [])

    def test_history_cannot_precede_hire(self):
        self.assertTrue(m.validate_import([], [self.record(date='2023-12-31')]))

    def test_snapshot_review_disables_completion_without_writing(self):
        self.employee['last_review_date'] = m.STATE['skills']['meta']['as_of_date']
        principal = Principal('employee', 'employee', 'E0001')
        profile = m.get_profile('E0001', 'ru', principal)
        self.assertFalse(profile['progress']['completion_allowed'])
        with m.connect() as connection:
            before = connection.execute('SELECT count(*) FROM activity_history').fetchone()[0]
        with self.assertRaises(HTTPException) as caught:
            m.complete_activity(m.CompleteRequest(employee_id='E0001', event_id='EV_005'), principal)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(m.STATE['history'], [])
        with m.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM activity_history').fetchone()[0], before)

    def test_career_goal_is_event_specific_and_includes_goal_only_skills(self):
        self.configure_targets()
        events = [self.event('REG_CURRENT'), self.event('REG_GOAL', skill='SK_TYPESCRIPT')]
        m.STATE['events'] = {event['event_id']: event for event in events}
        candidates = {c['event']['event_id']: c for c in m.eligible_candidates(self.employee)}
        self.assertIn('REG_GOAL', candidates, 'An eligible goal-only activity should be considered')
        self.assertGreater(candidates['REG_GOAL']['goal_match'], candidates['REG_CURRENT']['goal_match'])
        self.assertEqual(candidates['REG_CURRENT']['goal_match'], 0)

    def test_career_goal_uses_effective_gain_with_cap(self):
        self.configure_targets()
        self.employee['skills']['SK_TYPESCRIPT'] = 2
        event = self.event('REG_CAPPED', skill='SK_TYPESCRIPT', develops_skills=[dict(skill_id='SK_TYPESCRIPT', gain=2, max_level=2)])
        m.STATE['events'] = {event['event_id']: event}
        self.assertEqual(m.eligible_candidates(self.employee), [])

    def test_similar_history_needs_shared_skill_and_type_or_format(self):
        self.configure_targets()
        definitions = [
            self.event('REG_CANDIDATE'),
            self.event('REG_SAME_TYPE', format='online'),
            self.event('REG_SAME_FORMAT', type='mentoring'),
            self.event('REG_OTHER_SKILL', skill='SK_TYPESCRIPT'),
            self.event('REG_OTHER_CONTEXT', type='workshop', format='offline'),
        ]
        m.STATE['events'] = {event['event_id']: event for event in definitions}
        m.STATE['history'] = [self.record(record_id=f'REG_{index}', event_id=event['event_id'],
                                         status='dropped', completion_pct='20')
                              for index, event in enumerate(definitions[1:])]
        candidate = next(c for c in m.eligible_candidates(self.employee) if c['event']['event_id'] == 'REG_CANDIDATE')
        self.assertEqual(candidate['history_counts'].get('dropped', 0), 2)

    def test_lead_receives_candidates_for_current_grade_gaps(self):
        self.configure_targets()
        self.employee.update(grade='Lead', career_goal=None)
        event = self.event('REG_LEAD')
        m.STATE['events'] = {event['event_id']: event}
        self.assertIn('REG_LEAD', [c['event']['event_id'] for c in m.eligible_candidates(self.employee)])


if __name__ == '__main__':
    unittest.main()
