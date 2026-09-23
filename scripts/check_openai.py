"""Opt-in live OpenAI acceptance check. Does not write to the application DB."""
import csv
import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main as app


def main():
    parser = argparse.ArgumentParser(description='Verify live OpenAI recommendations on three control profiles.')
    parser.add_argument('--prompt-key', action='store_true', help='Prompt privately for an API key if absent from the environment.')
    args = parser.parse_args()
    if args.prompt_key and not os.getenv('OPENAI_API_KEY'):
        try:
            key = getpass.getpass('OpenAI API key (hidden input): ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nKey entry cancelled.')
            return 2
        if key:
            os.environ['OPENAI_API_KEY'] = key
    if not os.getenv('OPENAI_API_KEY'):
        print('OPENAI_API_KEY is missing. Run .\\.venv\\Scripts\\python.exe scripts\\check_openai.py --prompt-key')
        return 2
    skills = app.load_json('skills.json')
    profiles = json.loads((ROOT / 'tests/fixtures/employees.json').read_text(encoding='utf-8'))['employees']
    history = list(csv.DictReader((ROOT / 'tests/fixtures/activity_history.csv').read_text(encoding='utf-8').splitlines()))
    app.STATE.update(skills=skills, events={e['event_id']: e for e in app.load_json('events.json')['events']},
                     employees={e['employee_id']: e for e in profiles}, history=history)
    report = {'timestamp': datetime.now(timezone.utc).isoformat(), 'model': os.getenv('OPENAI_MODEL', 'gpt-4o-mini'),
              'live_provider': 'OpenAI', 'profiles': [], 'passed': True}
    for employee in profiles:
        entry = {'profile': employee['employee_id'], 'passed': False}
        start = time.perf_counter()
        try:
            candidates = app.eligible_candidates(employee)
            selected = app.llm_select(employee, candidates, employee['preferred_language'])
            elapsed = time.perf_counter() - start
            event_ids = [candidate['event']['event_id'] for candidate, _ in selected]
            checks = {'one_to_three': 1 <= len(selected) <= 3,
                      'under_ten_seconds': elapsed < 10,
                      'multiple_factors': all(len(set(factors)) >= 2 for _, factors in selected),
                      'eligible_events': set(event_ids) <= {c['event']['event_id'] for c in candidates}}
            if employee['employee_id'] == 'CONTROL_CRITICAL':
                checks['critical_skill_first'] = bool(selected and 'SK_SYSTEM_DESIGN' in selected[0][0]['covered'] and 'critical_gap' in selected[0][1])
            if employee['employee_id'] == 'CONTROL_REPEAT':
                checks['repeat_exception'] = 'EV_036' in event_ids
            entry.update(elapsed_seconds=round(elapsed, 3), events=event_ids, checks=checks, passed=all(checks.values()))
        except app.OpenAISelectionError as exc:
            entry.update(error=str(exc), elapsed_seconds=round(time.perf_counter() - start, 3))
        except Exception:
            entry.update(error='invalid_provider_response', elapsed_seconds=round(time.perf_counter() - start, 3))
        report['profiles'].append(entry)
        report['passed'] = report['passed'] and entry['passed']
        print(json.dumps(entry, ensure_ascii=True))
    output = ROOT / 'openai-verification.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS' if report['passed'] else 'FAIL', '- report: openai-verification.json')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
