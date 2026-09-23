import asyncio
import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

import main as m
from auth import Principal

EMPLOYEE = Principal("employee", "employee", "E0001")
HR = Principal("hr", "hr", None)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(m, 'DB_PATH', Path(self.temp.name) / 'test.sqlite3')
        self.db.start()
        m.init_state()
        self.employee = copy.deepcopy(m.STATE['employees']['E0001'])

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

    def record(self, event='EV_005', day='2026-09-20', **overrides):
        return dict(record_id='TEST_RECORD', employee_id=self.employee['employee_id'],
                    event_id=event, date=day, due_date='', status='completed',
                    completion_pct='100', score='', feedback_rating='', assigned_by='self', **overrides)

    def new_profile(self):
        emp = copy.deepcopy(self.employee)
        emp['employee_id'] = 'CONTROL_NEW'
        return emp

    def test_skill_cap_does_not_reduce_existing_skill(self):
        self.employee['skills']['SK_SYSTEM_DESIGN'] = 5
        m.STATE['history'] = [self.record()]
        self.assertEqual(m.progress(self.employee)['levels']['SK_SYSTEM_DESIGN'], 5)

    def test_review_boundary_future_and_missing_skills(self):
        self.employee['skills'] = {}
        self.employee['last_review_date'] = '2026-09-10'
        m.STATE['history'] = [self.record(day=day) for day in ['2026-09-09', '2026-09-10', '2026-09-11', '2026-10-02']]
        progress = m.progress(self.employee)
        self.assertEqual(progress['levels']['SK_SYSTEM_DESIGN'], 1)
        self.assertEqual(progress['levels']['SK_PYTHON'], 0)
        self.assertEqual(progress['completed_since_review'], 1)

    def test_import_invalid_numbers_produces_errors(self):
        for field in ['completion_pct', 'score', 'feedback_rating']:
            row = self.record()
            row[field] = 'invalid'
            self.assertTrue(any(field in error for error in m.validate_import([], [row])))

    def test_malformed_profiles_dates_goals_and_types(self):
        for invalid in [None, [], 42, 'profile']:
            self.assertTrue(m.validate_import([invalid], []))
        for field, value in [('last_review_date', 'bad'), ('last_review_date', '2027-01-01'),
                             ('career_goal', {'target_role': 'Backend Engineer', 'target_grade': 'Unknown'}),
                             ('employee_id', []), ('preferred_language', 'xx'), ('skills', {'SK_PYTHON': True})]:
            emp = self.new_profile()
            emp[field] = value
            self.assertTrue(m.validate_import([emp], []), field)

    def test_history_status_and_future_date(self):
        row = self.record(day='2026-10-02')
        self.assertTrue(m.validate_import([], [row]))
        row = self.record()
        row['completion_pct'] = '0'
        self.assertTrue(m.validate_import([], [row]))

    def test_failed_import_is_atomic(self):
        emp = self.new_profile()
        upload = UploadFile(filename='profiles.json', file=io.BytesIO(json.dumps({'employees': [emp, None]}).encode()))
        before = len(m.STATE['employees'])
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(m.import_data(upload, None, HR))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertTrue(caught.exception.detail['errors'])
        self.assertEqual(len(m.STATE['employees']), before)
        with m.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM uploads').fetchone()[0], 0)

    def test_successful_import_persists_and_updates_hr(self):
        emp = self.new_profile()
        upload = UploadFile(filename='profiles.json', file=io.BytesIO(json.dumps({'employees': [emp]}).encode()))
        result = asyncio.run(m.import_data(upload, None, HR))
        self.assertEqual(result['employees_imported'], 1)
        m.init_state()
        self.assertIn(emp['employee_id'], m.STATE['employees'])
        self.assertEqual(m.hr_summary(HR)['employee_count'], 201)

    def test_all_hr_deficits_included(self):
        self.assertEqual(len(m.hr_summary(HR)['competency_gaps']), 60)

    def test_profile_does_not_wait_for_llm(self):
        with patch.object(m, 'llm_select', side_effect=AssertionError('Profile must not call LLM')):
            result = m.get_profile('E0001', 'ru', EMPLOYEE)
        self.assertNotIn('recommendations', result)
        self.assertIn('progress', result)

    def test_role_and_employee_boundaries(self):
        for role in [None, EMPLOYEE]:
            with self.assertRaises(HTTPException):
                m.hr_summary(role)
        with self.assertRaises(HTTPException):
            m.get_profile('E0002', 'ru', EMPLOYEE)
        with self.assertRaises(HTTPException):
            asyncio.run(m.get_recommendations('E0002', 'ru', EMPLOYEE))
        with self.assertRaises(HTTPException):
            asyncio.run(m.import_data(None, None, EMPLOYEE))

    def test_filters_and_repeat_exception(self):
        self.employee['grade'] = 'Middle'
        self.employee['skills'] = {s['skill_id']: 2 for s in m.STATE['skills']['skills']}
        m.STATE['history'] = []
        base = copy.deepcopy(m.STATE['events']['EV_036'])
        base.update(target_roles=[self.employee['role']], target_grades=['Middle'], mandatory=False,
                    format='online', upcoming_sessions=['2026-10-10'], prerequisites={},
                    develops_skills=[{'skill_id': 'SK_SYSTEM_DESIGN', 'gain': 1, 'max_level': 5}])
        variants = {'EV_036': {}, 'DONE': {}, 'MANDATORY': {'mandatory': True},
                    'ROLE': {'target_roles': ['wrong']}, 'GRADE': {'target_grades': ['Lead']},
                    'PREREQ': {'prerequisites': {'SK_PYTHON': 5}}, 'EXPIRED': {'upcoming_sessions': ['2026-09-30']},
                    'SELF_PACED': {'format': 'self_paced', 'upcoming_sessions': []}}
        m.STATE['events'] = {eid: {**base, **changes, 'event_id': eid} for eid, changes in variants.items()}
        m.STATE['history'] = [self.record(event=eid, day='2025-01-01') for eid in ['EV_036', 'DONE']]
        self.assertEqual({c['event']['event_id'] for c in m.eligible_candidates(self.employee)}, {'EV_036', 'SELF_PACED'})

    def test_completion_uses_snapshot_and_survives_restart(self):
        employee = m.STATE['employees']['E0001']
        candidates = m.eligible_candidates(employee)
        self.assertTrue(candidates)
        event = candidates[0]['event']['event_id']
        result = m.complete_activity(m.CompleteRequest(employee_id='E0001', event_id=event), EMPLOYEE)
        self.assertEqual(m.STATE['history'][-1]['date'], '2026-10-01')
        m.init_state()
        self.assertEqual(m.progress(m.STATE['employees']['E0001']), result['progress'])

    def test_model_failure_falls_back_with_multiple_reasons(self):
        with patch.object(m, 'llm_select', side_effect=RuntimeError('Invalid model response')):
            result = m.recommendations(self.employee, 'ru')
        self.assertEqual(result['source'], 'rules_fallback')
        self.assertTrue(result['recommendations'])
        self.assertTrue(all(len(set(r['factors'])) >= 3 for r in result['recommendations']))

    def test_explanations_include_verified_levels_and_history_counts(self):
        m.STATE['history'] = []
        candidate = m.eligible_candidates(self.employee)[0]
        candidate['history_counts'] = {'completed': 2, 'no_show': 3, 'declined': 1, 'dropped': 1}
        evidence = m.recommendation_evidence(candidate, self.employee)
        self.assertTrue(evidence['skill_effects'])
        for effect in evidence['skill_effects']:
            self.assertEqual(effect['after_completion'] - effect['current'], effect['effective_gain'])
            self.assertLessEqual(effect['after_completion'], effect['max_level'])
        for language in ['ru', 'kk', 'en']:
            text = m.factor_copy('next_grade_gap', language, candidate['event'], candidate, self.employee)
            for effect in evidence['skill_effects']:
                self.assertIn(f"(+{effect['effective_gain']})", text)
            history = m.factor_copy('history_fit', language, candidate['event'], candidate, self.employee)
            self.assertIn('3', history)
            self.assertIn('2', history)

    def test_real_parser_validates_model_selection(self):
        event_id = m.eligible_candidates(self.employee)[0]['event']['event_id']
        for selected, expected_source in [(event_id, 'ai'), ('UNKNOWN_EVENT', 'rules_fallback')]:
            model_content = {'recommendations': [{'event_id': selected, 'factor_keys': {'progress':'next_grade_gap','activity':'activity_fit','history':'history_fit','priority':None}}]}
            response = io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(model_content)}}]}).encode())
            with patch.dict(m.os.environ, {'OPENAI_API_KEY': 'test-key'}), patch('urllib.request.urlopen', return_value=response):
                result = m.recommendations(self.employee, 'en')
            self.assertEqual(result['source'], expected_source)
            self.assertNotIn('UNKNOWN_EVENT', [r['event_id'] for r in result['recommendations']])

    def test_slow_model_has_deadline_and_does_not_block_profile(self):
        def slow_model(*args):
            time.sleep(9)
            raise TimeoutError('Simulated provider delay')

        async def scenario():
            start = time.perf_counter()
            task = asyncio.create_task(m.get_recommendations('E0001', 'ru', EMPLOYEE))
            await asyncio.sleep(0.05)
            profile_start = time.perf_counter()
            profile = m.get_profile('E0001', 'ru', EMPLOYEE)
            self.assertLess(time.perf_counter() - profile_start, 2)
            self.assertIn('progress', profile)
            result = await task
            self.assertLess(time.perf_counter() - start, 10)
            self.assertEqual(result['source'], 'rules_fallback')

        with patch.object(m, 'llm_select', side_effect=slow_model):
            asyncio.run(scenario())

    def test_three_control_profiles(self):
        profiles = json.loads((Path(__file__).parent / 'fixtures/employees.json').read_text(encoding='utf-8'))['employees']
        history = m.parse_upload('history.csv', (Path(__file__).parent / 'fixtures/activity_history.csv').read_bytes())
        self.assertEqual(m.validate_import(profiles, history), [])
        m.STATE['employees'].update({p['employee_id']: p for p in profiles})
        m.STATE['history'].extend(history)
        for employee in profiles:
            result = m.recommendations(employee, 'ru', use_llm=False)
            self.assertTrue(result['recommendations'], employee['employee_id'])
            if employee['employee_id'] == 'CONTROL_CRITICAL':
                self.assertIn('critical_gap', result['recommendations'][0]['factors'])
                self.assertNotEqual(result['recommendations'][0]['event_id'], 'EV_036')
            elif employee['employee_id'] == 'CONTROL_REPEAT':
                self.assertIn('EV_036', [r['event_id'] for r in result['recommendations']])


if __name__ == '__main__':
    unittest.main()
