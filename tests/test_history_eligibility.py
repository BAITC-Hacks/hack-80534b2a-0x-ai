import copy
import csv
import json
from pathlib import Path
import unittest

from history_validation import validate_history_eligibility

ROOT = Path(__file__).resolve().parents[1]


class HistoryEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.employees = {'E': {'role': 'Engineer', 'grade': 'Senior',
                               'skills': {'SK': 1}, 'last_review_date': '2026-09-01'}}
        self.events = {
            'BASE': {'target_roles': ['Engineer'], 'target_grades': ['Middle'],
                     'prerequisites': {}, 'develops_skills': [{'skill_id': 'SK', 'gain': 1, 'max_level': 3}]},
            'ADV': {'target_roles': ['Engineer'], 'target_grades': ['Senior'],
                    'prerequisites': {'SK': 2}, 'develops_skills': []},
        }

    def row(self, event='ADV', day='2026-09-10', status='completed', rid='R'):
        return {'record_id': rid, 'employee_id': 'E', 'event_id': event, 'date': day, 'status': status}

    def validate(self, incoming, existing=None):
        return validate_history_eligibility(self.employees, self.events, existing or [], incoming, '2026-10-01')

    def test_wrong_role_is_rejected_even_before_review(self):
        self.employees['E']['role'] = 'Designer'
        errors = self.validate([self.row('BASE', '2026-08-01')])
        self.assertEqual(len(errors), 1)
        self.assertIn('R:', errors[0])
        self.assertIn('роли', errors[0])

    def test_only_current_or_immediately_previous_grade_allowed(self):
        self.assertEqual(self.validate([self.row('BASE')]), [])
        self.events['BASE']['target_grades'] = ['Senior']
        self.assertEqual(self.validate([self.row('BASE')]), [])
        for forbidden in ('Junior', 'Lead'):
            self.events['BASE']['target_grades'] = [forbidden]
            self.assertIn('грейда', self.validate([self.row('BASE')])[0])

    def test_prerequisites_use_earlier_existing_or_incoming_completions(self):
        base = self.row('BASE', '2026-09-09', rid='BASE_R')
        advanced = self.row()
        self.assertEqual(self.validate([advanced], [base]), [])
        self.assertEqual(self.validate([advanced, base]), [])
        self.assertIn('предпосылка', self.validate([advanced])[0])

    def test_same_day_future_pre_review_and_noncompleted_do_not_grant_skills(self):
        advanced = self.row()
        for day, status in (('2026-09-10', 'completed'), ('2026-09-11', 'completed'),
                            ('2026-09-01', 'completed'), ('2026-08-31', 'completed'),
                            ('2026-09-09', 'in_progress')):
            with self.subTest(day=day, status=status):
                base = self.row('BASE', day, status, rid='BASE_R')
                self.assertIn('предпосылка', self.validate([advanced], [base])[0])

    def test_skills_at_or_before_review_are_unknown_not_reconstructed(self):
        for day in ('2026-08-31', '2026-09-01'):
            self.assertEqual(self.validate([self.row(day=day)]), [])

    def test_absent_skill_is_zero_and_gain_respects_max_level(self):
        self.employees['E']['skills'] = {}
        base = self.row('BASE', '2026-09-09', rid='BASE_R')
        self.events['BASE']['develops_skills'][0].update(gain=10, max_level=1)
        self.assertIn('уровень 1', self.validate([self.row()], [base])[0])

    def test_low_cap_never_lowers_existing_level(self):
        self.employees['E']['skills']['SK'] = 3
        self.events['BASE']['develops_skills'][0]['max_level'] = 1
        self.assertEqual(self.validate([self.row()], [self.row('BASE', '2026-09-09')]), [])

    def test_mandatory_records_are_outside_voluntary_eligibility_check(self):
        self.events['ADV']['mandatory'] = True
        self.employees['E']['role'] = 'Designer'
        self.assertEqual(self.validate([self.row()]), [])

    def test_inputs_are_not_mutated(self):
        incoming = [self.row('BASE', '2026-09-09'), self.row()]
        before = copy.deepcopy((self.employees, self.events, incoming))
        self.validate(incoming)
        self.assertEqual((self.employees, self.events, incoming), before)

    def test_starter_and_acceptance_fixtures_are_compatible(self):
        def read_json(path, key):
            return json.loads(path.read_text(encoding='utf-8'))[key]

        def read_csv(path):
            with path.open(encoding='utf-8-sig', newline='') as stream:
                return list(csv.DictReader(stream))

        employees = {e['employee_id']: e for e in read_json(ROOT / 'data/employees.json', 'employees')}
        events = {e['event_id']: e for e in read_json(ROOT / 'data/events.json', 'events')}
        baseline = read_csv(ROOT / 'data/activity_history.csv')
        self.assertEqual(validate_history_eligibility(employees, events, [], baseline, '2026-10-01'), [])
        fixtures = {e['employee_id']: e for e in read_json(ROOT / 'tests/fixtures/employees.json', 'employees')}
        incoming = read_csv(ROOT / 'tests/fixtures/activity_history.csv')
        self.assertEqual(validate_history_eligibility({**employees, **fixtures}, events, baseline, incoming, '2026-10-01'), [])


if __name__ == '__main__':
    unittest.main()
