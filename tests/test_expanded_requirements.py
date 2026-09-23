"""Coverage for visible completed history, HR participation and three-factor reasons."""
import asyncio
import copy
import csv
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

import main as m
from auth import Principal


EMPLOYEE = Principal('employee', 'employee', 'E0001')
HR = Principal('hr', 'hr', None)
GAP_OR_GOAL = {'next_grade_gap', 'current_grade_gap', 'career_goal'}


class ExpandedRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(m, 'DB_PATH', Path(self.temp.name) / 'expanded.sqlite3')
        self.db.start()
        m.init_state()

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

    def assert_three_factors(self, recommendation):
        keys = set(recommendation['factors'])
        self.assertGreaterEqual(len(keys), 3)
        self.assertIn('activity_fit', keys)
        self.assertIn('history_fit', keys)
        self.assertTrue(keys & GAP_OR_GOAL)
        self.assertEqual(len(recommendation['reasons']), len(recommendation['factors']))
        self.assertTrue(all(text.strip() for text in recommendation['reasons']))

    def test_all_200_profiles_fallback_reasons_have_three_distinct_factors(self):
        self.assertEqual(len(m.STATE['employees']), 200)
        recommendations_count = 0
        for employee in m.STATE['employees'].values():
            with self.subTest(employee_id=employee['employee_id']):
                result = m.recommendations(employee, 'ru', use_llm=False)
                self.assertLessEqual(len(result['recommendations']), 3)
                for recommendation in result['recommendations']:
                    self.assert_three_factors(recommendation)
                    recommendations_count += 1
        self.assertGreater(recommendations_count, 0)

    def test_absent_history_is_explicit_and_not_invented(self):
        m.STATE['history'] = []
        employee = m.STATE['employees']['E0001']
        for language in ['ru', 'kk', 'en']:
            result = m.recommendations(employee, language, use_llm=False)
            self.assertTrue(result['recommendations'])
            for recommendation in result['recommendations']:
                self.assert_three_factors(recommendation)
                self.assertFalse(any(recommendation['evidence']['history_counts'].values()))
                text = recommendation['reasons'][recommendation['factors'].index('history_fit')]
                if language == 'ru':
                    self.assertRegex(text.lower(), r'нет|отсутств|не было|ещ[её] не')
                elif language == 'en':
                    self.assertRegex(text.lower(), r'no |not |without|unavailable')
                else:
                    self.assertIn('жоқ', text.lower())

    def test_model_parser_requires_three_distinct_categories(self):
        employee = m.STATE['employees']['E0001']
        candidate = m.eligible_candidates(employee)[0]
        gap = next(key for key in m.eligible_factor_keys(candidate, employee) if key in GAP_OR_GOAL)
        cases = [
            ([gap, 'activity_fit'], 'rules_fallback'),
            ([gap, 'activity_fit', 'activity_fit'], 'rules_fallback'),
            ([gap, 'critical_gap', 'activity_fit'], 'rules_fallback'),
            ([gap, 'activity_fit', 'history_fit'], 'ai'),
        ]
        for keys, expected_source in cases:
            with self.subTest(keys=keys):
                content = {'recommendations': [{'event_id': candidate['event']['event_id'], 'factor_keys': keys}]}
                response = io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(content)}}]}).encode())
                with patch.dict(m.os.environ, {'OPENAI_API_KEY': 'test-key'}), patch('urllib.request.urlopen', return_value=response) as provider:
                    result = m.recommendations(employee, 'en')
                self.assertEqual(result['source'], expected_source)
                request = provider.call_args.args[0]
                payload = json.loads(request.data)
                factors_schema = payload['response_format']['json_schema']['schema']['properties']['recommendations']['items']['properties']['factor_keys']
                self.assertGreaterEqual(factors_schema['minItems'], 3)
                for recommendation in result['recommendations']:
                    self.assert_three_factors(recommendation)

    def test_completed_history_excludes_other_people_future_and_unfinished(self):
        employee = m.STATE['employees']['E0001']
        employee['last_review_date'] = '2026-09-15'
        base = dict(employee_id='E0001', event_id='EV_036', due_date='', status='completed',
                    completion_pct='100', score='', feedback_rating='', assigned_by='self')
        m.STATE['history'] = [
            {**base, 'record_id': 'BEFORE', 'date': '2026-09-14'},
            {**base, 'record_id': 'REVIEW_DAY', 'date': '2026-09-15'},
            {**base, 'record_id': 'AFTER', 'date': '2026-09-16'},
            {**base, 'record_id': 'FUTURE', 'date': '2026-10-02'},
            {**base, 'record_id': 'OTHER', 'date': '2026-09-16', 'employee_id': 'E0002'},
            {**base, 'record_id': 'UNFINISHED', 'date': '2026-09-16', 'status': 'dropped', 'completion_pct': '20'},
        ]
        profile = m.get_profile('E0001', 'ru', EMPLOYEE)
        records = {row['record_id']: row for row in profile['completed_activities']}
        self.assertEqual(set(records), {'BEFORE', 'REVIEW_DAY', 'AFTER'})
        self.assertFalse(records['BEFORE']['counted_since_review'])
        self.assertFalse(records['REVIEW_DAY']['counted_since_review'])
        self.assertTrue(records['AFTER']['counted_since_review'])
        for row in records.values():
            self.assertEqual(row['status'], 'completed')
            self.assertEqual(row['event_id'], 'EV_036')
            for field in ['title', 'description', 'type', 'format', 'date']:
                self.assertTrue(row[field])
        self.assertEqual(m.get_profile('E0001', 'ru', HR)['completed_activities'], profile['completed_activities'])
        with self.assertRaises(HTTPException) as denied:
            m.get_profile('E0001', 'ru', Principal('other', 'employee', 'E0002'))
        self.assertEqual(denied.exception.status_code, 403)

    def test_completion_appears_in_profile_history_and_survives_restart(self):
        employee = m.STATE['employees']['E0001']
        event_id = m.eligible_candidates(employee)[0]['event']['event_id']
        before = {row['record_id'] for row in m.get_profile('E0001', 'ru', EMPLOYEE)['completed_activities']}
        m.complete_activity(m.CompleteRequest(employee_id='E0001', event_id=event_id), EMPLOYEE)
        m.init_state()
        added = [row for row in m.get_profile('E0001', 'ru', EMPLOYEE)['completed_activities'] if row['record_id'] not in before]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['event_id'], event_id)
        self.assertTrue(added[0]['counted_since_review'])

    def test_hr_without_next_step_matches_actual_candidate_absence(self):
        expected = {employee['employee_id'] for employee in m.STATE['employees'].values() if not m.eligible_candidates(employee)}
        result = m.hr_summary(HR)
        rows = result['employees_without_next_step']
        self.assertEqual({row['employee_id'] for row in rows}, expected)
        self.assertEqual(len(rows), len(expected))
        for row in rows:
            employee = m.STATE['employees'][row['employee_id']]
            self.assertEqual(row['name'], employee['full_name'])
            self.assertEqual(row['role'], employee['role'])
            self.assertEqual(row['grade'], employee['grade'])
            self.assertIsInstance(row['has_skill_gaps'], bool)
            self.assertTrue(row['reason_codes'])
            self.assertIsInstance(row['blocked_counts'], dict)
            self.assertTrue(all(isinstance(value, int) and value >= 0 for value in row['blocked_counts'].values()))

    def test_participation_matches_history_totals_and_unique_people(self):
        result = m.hr_summary(HR)
        rows = result['activity_participation']
        self.assertEqual(result['as_of_date'], '2026-10-01')
        self.assertEqual(len(rows), 40)
        self.assertEqual({row['event_id'] for row in rows}, set(m.STATE['events']))
        visible_history = [record for employee in m.STATE['employees'].values() for record in m.employee_history(employee['employee_id'])]
        self.assertEqual(sum(row['total_records'] for row in rows), len(visible_history))
        for row in rows:
            history = [record for record in visible_history if record['event_id'] == row['event_id']]
            expected_statuses = Counter(record['status'] for record in history)
            self.assertEqual(row['total_records'], len(history))
            self.assertEqual(row['unique_employees'], len({record['employee_id'] for record in history}))
            self.assertEqual({key: value for key, value in row['status_counts'].items() if value}, dict(expected_statuses))
            self.assertEqual(sum(segment['total_records'] for segment in row['segments']), row['total_records'])
            self.assertEqual(sum(segment['unique_employees'] for segment in row['segments']), row['unique_employees'])
            expected_segments = {(m.STATE['employees'][record['employee_id']]['role'], m.STATE['employees'][record['employee_id']]['grade']) for record in history}
            self.assertEqual({(segment['role'], segment['grade']) for segment in row['segments']}, expected_segments)
            for segment in row['segments']:
                records = [record for record in history if (m.STATE['employees'][record['employee_id']]['role'], m.STATE['employees'][record['employee_id']]['grade']) == (segment['role'], segment['grade'])]
                self.assertEqual(segment['total_records'], len(records))
                self.assertEqual(segment['unique_employees'], len({record['employee_id'] for record in records}))
                self.assertEqual({key: value for key, value in segment['status_counts'].items() if value}, dict(Counter(record['status'] for record in records)))

    def test_new_hr_analytics_are_unavailable_to_employee(self):
        with self.assertRaises(HTTPException) as denied:
            m.hr_summary(EMPLOYEE)
        self.assertEqual(denied.exception.status_code, 403)

    def test_import_wrong_role_or_prerequisite_rolls_back_entire_upload(self):
        base = m.STATE['employees']['E0001']
        previous_grade = m.GRADE_ORDER[max(0, m.GRADE_ORDER.index(base['grade']) - 1)]
        wrong_role = next(event for event in m.STATE['events'].values()
                          if not event['mandatory'] and base['role'] not in event['target_roles'])
        prerequisite = next(event for event in m.STATE['events'].values()
                            if not event['mandatory'] and base['role'] in event['target_roles']
                            and {base['grade'], previous_grade}.intersection(event['target_grades'])
                            and any(level > 0 for level in event['prerequisites'].values()))
        before_employees = set(m.STATE['employees'])
        before_history = list(m.STATE['history'])
        for event in [wrong_role, prerequisite]:
            with self.subTest(event_id=event['event_id']):
                employee = copy.deepcopy(base)
                employee.update(employee_id='IMPORT_INVALID_ELIGIBILITY', skills={}, last_review_date='2026-09-01')
                record = dict(record_id='IMPORT_BAD_RECORD', employee_id=employee['employee_id'],
                              event_id=event['event_id'], date='2026-09-30', due_date='',
                              status='completed', completion_pct='100', score='', feedback_rating='', assigned_by='self')
                history_csv = io.StringIO(newline='')
                writer = csv.DictWriter(history_csv, fieldnames=m.HISTORY_FIELDS)
                writer.writeheader()
                writer.writerow(record)
                profiles = UploadFile(filename='employees.json', file=io.BytesIO(json.dumps({'employees': [employee]}).encode()))
                history = UploadFile(filename='activity_history.csv', file=io.BytesIO(history_csv.getvalue().encode()))
                with self.assertRaises(HTTPException) as rejected:
                    asyncio.run(m.import_data(profiles, history, HR))
                self.assertEqual(rejected.exception.status_code, 422)
                self.assertTrue(any(record['record_id'] in error for error in rejected.exception.detail['errors']))
                self.assertEqual(set(m.STATE['employees']), before_employees)
                self.assertEqual(m.STATE['history'], before_history)
                with m.connect() as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM uploads').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM activity_history').fetchone()[0], 0)

    def test_participation_includes_zero_activity_events(self):
        m.STATE['history'] = []
        rows = m.hr_summary(HR)['activity_participation']
        self.assertEqual(len(rows), len(m.STATE['events']))
        for row in rows:
            self.assertEqual(row['total_records'], 0)
            self.assertEqual(row['unique_employees'], 0)
            self.assertFalse(any(row['status_counts'].values()))
            self.assertEqual(row['segments'], [])


if __name__ == '__main__':
    unittest.main()
