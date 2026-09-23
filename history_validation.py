"""Eligibility checks for voluntary history, after structural import validation.

Role history is not available: the dataset contract uses the employee's current
role and either current or immediately previous grade. Historical sessions are
not checked against the catalog's upcoming sessions.

Skill prerequisites are checkable only strictly after last_review_date. Skills
on that date are a snapshot, not evidence of skills before an older activity.
Only completed activities strictly after the review and strictly before the
activity date contribute gains. Within-day ordering is unknown, so same-day
completions do not establish prerequisites for each other. Earlier completions
are never replayed on top of the review snapshot.
"""
from collections import defaultdict
from datetime import date

GRADES = ('Junior', 'Middle', 'Senior', 'Lead')


def _date(value):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def validate_history_eligibility(employees: dict, event_catalog: dict,
                                 existing_history: list, incoming_history: list,
                                 snapshot: str) -> list[str]:
    """Return record-specific errors; do not mutate employees or history.

    The caller must separately validate schema, identifiers, dates and numeric
    skill values. Unknown references/dates are skipped here to avoid duplicating
    those structural errors. Only incoming rows are subject to eligibility
    checks; existing completions supply evidence of post-review skill gains.
    """
    snapshot_day = _date(snapshot)
    if snapshot_day is None:
        return []
    timelines = defaultdict(list)
    for incoming, records in ((False, existing_history), (True, incoming_history)):
        for row in records:
            if not isinstance(row, dict):
                continue
            eid, event_id = row.get('employee_id'), row.get('event_id')
            if not isinstance(eid, str) or not isinstance(event_id, str):
                continue
            day = _date(row.get('date'))
            if eid in employees and event_id in event_catalog and day and day <= snapshot_day:
                timelines[eid].append((day, incoming, row))

    errors = []
    for eid, records in timelines.items():
        employee = employees[eid]
        grade = employee.get('grade')
        permitted_grades = {grade}
        if grade in GRADES and GRADES.index(grade) > 0:
            permitted_grades.add(GRADES[GRADES.index(grade) - 1])
        review = _date(employee.get('last_review_date'))
        levels = dict(employee.get('skills', {}))
        by_day = defaultdict(list)
        for day, incoming, row in records:
            by_day[day].append((incoming, row))
        for day in sorted(by_day):
            for incoming, row in by_day[day]:
                event = event_catalog[row['event_id']]
                if not incoming or event.get('mandatory'):
                    continue
                label = row.get('record_id', 'Запись истории')
                if employee.get('role') not in event.get('target_roles', []):
                    errors.append(f'{label}: мероприятие {row["event_id"]} не подходит для роли сотрудника')
                if not permitted_grades.intersection(event.get('target_grades', [])):
                    errors.append(f'{label}: мероприятие {row["event_id"]} не подходит для текущего или предыдущего грейда')
                if review and day > review:
                    for skill, minimum in event.get('prerequisites', {}).items():
                        actual = levels.get(skill, 0)
                        if actual < minimum:
                            errors.append(f'{label}: не выполнена предпосылка {skill}: '
                                          f'до события уровень {actual}, требуется {minimum}')
            # Apply a date group only after every prerequisite on that date was
            # checked. Saturating gain updates match the application's progress.
            if review and day > review:
                for _, row in by_day[day]:
                    if row.get('status') != 'completed':
                        continue
                    for gain in event_catalog[row['event_id']].get('develops_skills', []):
                        sid = gain['skill_id']
                        before = levels.get(sid, 0)
                        levels[sid] = max(before, min(gain['max_level'], before + gain['gain']))
    return errors
