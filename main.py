from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("CAREER_QUEST_DB", str(ROOT / "career_quest.sqlite3")))
GRADE_ORDER = ["Junior", "Middle", "Senior", "Lead"]
HISTORY_FIELDS = ["record_id", "employee_id", "event_id", "date", "due_date", "status", "completion_pct", "score", "feedback_rating", "assigned_by"]
STATE: dict[str, Any] = {}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=8)
    conn.row_factory = sqlite3.Row
    return conn


def load_json(name: str) -> dict[str, Any]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def init_state() -> None:
    STATE.update(
        employees={row["employee_id"]: row for row in load_json("employees.json")["employees"]},
        events={row["event_id"]: row for row in load_json("events.json")["events"]},
        skills=load_json("skills.json"),
        history=list(csv.DictReader((DATA / "activity_history.csv").open(encoding="utf-8-sig", newline=""))),
    )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS uploads (kind TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,item_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS activity_history (record_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        for row in conn.execute("SELECT item_id,payload FROM uploads WHERE kind='employee'"):
            STATE["employees"][row["item_id"]] = json.loads(row["payload"])
        for row in conn.execute("SELECT payload FROM activity_history"):
            STATE["history"].append(json.loads(row["payload"]))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_state()
    yield


app = FastAPI(title="Career Quest", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def role_required(role: str, header: str | None) -> None:
    if header != role:
        raise HTTPException(status_code=403, detail="Доступ запрещён для этой роли")


def employee_required(employee_id: str, role: str | None, header_employee: str | None) -> dict[str, Any]:
    role_required("employee", role)
    # Demo identity is explicit. In a production deployment this header must be replaced by SSO identity.
    if not header_employee or header_employee != employee_id:
        raise HTTPException(status_code=403, detail="Можно открыть только собственный профиль")
    employee = STATE["employees"].get(employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    return employee


def as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def current_grade_profile(employee: dict[str, Any], next_grade: bool = False) -> dict[str, Any] | None:
    role_profiles = STATE["skills"].get("role_profiles", [])
    grade = employee["grade"]
    if next_grade:
        idx = GRADE_ORDER.index(grade) if grade in GRADE_ORDER else -1
        if idx < 0 or idx == len(GRADE_ORDER) - 1:
            return None
        grade = GRADE_ORDER[idx + 1]
    return next((p for p in role_profiles if p["role"] == employee["role"] and p["grade"] == grade), None)


def employee_history(employee_id: str) -> list[dict[str, Any]]:
    return [row for row in STATE["history"] if row["employee_id"] == employee_id]


def progress(employee: dict[str, Any]) -> dict[str, Any]:
    latest_review = as_date(employee.get("last_review_date"))
    levels = {skill["skill_id"]: int(employee.get("skills", {}).get(skill["skill_id"], 0)) for skill in STATE["skills"]["skills"]}
    events = STATE["events"]
    completed = sorted((r for r in employee_history(employee["employee_id"]) if r.get("status") == "completed" and (not latest_review or (as_date(r.get("date")) and as_date(r["date"]) > latest_review))), key=lambda r: r.get("date", ""))
    for record in completed:
        event = events.get(record["event_id"])
        if not event:
            continue
        for gain in event.get("develops_skills", []):
            skill_id = gain["skill_id"]
            levels[skill_id] = min(int(gain["max_level"]), levels.get(skill_id, 0) + int(gain["gain"]))
    target = current_grade_profile(employee, next_grade=True)
    current = current_grade_profile(employee)
    requirements = target or (current or {}).get("required_skills", {})
    critical = set((target or current or {}).get("critical_skills", []))
    skill_lookup = {s["skill_id"]: s for s in STATE["skills"]["skills"]}
    gaps = [{"skill_id": key, "name": skill_lookup.get(key, {}).get("name", key), "level": levels.get(key, 0), "required": required, "gap": max(0, required - levels.get(key, 0)), "critical": key in critical} for key, required in requirements.items()]
    return {"levels": levels, "gaps": sorted(gaps, key=lambda x: (not x["critical"], -x["gap"], x["name"])), "target_grade": next((p["grade"] for p in [target] if p), None), "at_top_grade": target is None, "critical_skills": list(critical), "completed_since_review": len(completed)}


def eligible_candidates(employee: dict[str, Any]) -> list[dict[str, Any]]:
    state = progress(employee)
    current_level = state["levels"]
    target = current_grade_profile(employee, next_grade=True)
    requirements = (target or {}).get("required_skills", {})
    critical = set((target or {}).get("critical_skills", []))
    history = employee_history(employee["employee_id"])
    completed_ids = {r["event_id"] for r in history if r.get("status") == "completed"}
    by_type: dict[str, Counter] = defaultdict(Counter)
    for row in history:
        event = STATE["events"].get(row["event_id"])
        if event:
            by_type[event.get("type", "")][row.get("status", "")] += 1
    result = []
    for event in STATE["events"].values():
        if event.get("mandatory") or employee["role"] not in event.get("target_roles", []) or employee["grade"] not in event.get("target_grades", []):
            continue
        if event["event_id"] in completed_ids and event["event_id"] != "EV_036":
            continue
        if any(current_level.get(skill, 0) < minimum for skill, minimum in event.get("prerequisites", {}).items()):
            continue
        # Date availability is evaluated against the dataset snapshot, not the host clock.
        as_of = as_date(STATE["skills"].get("meta", {}).get("as_of_date")) or date.today()
        sessions = event.get("upcoming_sessions", [])
        if event.get("format") != "self_paced" and not any((as_date(day) and as_date(day) >= as_of) for day in sessions):
            continue
        impact = 0.0
        covered = []
        for item in event.get("develops_skills", []):
            skill = item["skill_id"]
            gap = max(0, int(requirements.get(skill, current_level.get(skill, 0))) - current_level.get(skill, 0))
            if gap:
                weight = 4 if skill in critical else 1
                amount = min(gap, int(item["gain"]), max(0, int(item["max_level"]) - current_level.get(skill, 0)))
                impact += amount * weight
                covered.append(skill)
        if impact <= 0:
            continue
        counts = by_type[event.get("type", "")]
        preference = min(2, counts["completed"]) - min(3, counts["no_show"] + counts["declined"] + counts["dropped"])
        goal = employee.get("career_goal") or {}
        goal_match = int(bool(target and goal.get("target_role") == employee["role"] and GRADE_ORDER.index(goal.get("target_grade", "Junior")) >= GRADE_ORDER.index(target["grade"])))
        result.append({"event": event, "impact": impact, "covered": covered, "goal_match": goal_match, "history_signal": preference, "score": impact + 2 * goal_match + preference})
    return sorted(result, key=lambda row: (-row["score"], row["event"]["event_id"]))


def eligible_factor_keys(candidate: dict[str, Any], employee: dict[str, Any]) -> list[str]:
    keys = ["next_grade_gap", "activity_fit"]
    if any(s in progress(employee)["critical_skills"] for s in candidate["covered"]):
        keys.append("critical_gap")
    if candidate["goal_match"]:
        keys.append("career_goal")
    if candidate["history_signal"]:
        keys.append("history_fit")
    return keys


def factor_copy(key: str, lang: str, event: dict[str, Any], candidate: dict[str, Any], employee: dict[str, Any]) -> str:
    gaps = progress(employee)["gaps"]
    names = {s["skill_id"]: s["name"] for s in STATE["skills"]["skills"]}
    covered_names = ", ".join(names.get(s, s) for s in candidate["covered"][:2])
    lines = {
        "ru": {
            "critical_gap": f"Развивает критичный для следующего грейда навык: {covered_names}.",
            "next_grade_gap": f"Помогает закрыть разрыв до требований следующего грейда: {covered_names}.",
            "activity_fit": f"Подходит по роли и уровню; развивает: {covered_names}.",
            "career_goal": "Соответствует указанной карьерной цели.",
            "history_fit": "Учтена история похожих активностей при выборе следующего шага.",
        },
        "kk": {
            "critical_gap": f"Келесі деңгей үшін маңызды дағдыны дамытады: {covered_names}.",
            "next_grade_gap": f"Келесі деңгей талаптарына жету алшақтығын қысқартуға көмектеседі: {covered_names}.",
            "activity_fit": f"Рөл мен деңгейге сәйкес; дамытатын дағдылар: {covered_names}.",
            "career_goal": "Көрсетілген мансап мақсатына сәйкес келеді.",
            "history_fit": "Келесі қадамды таңдағанда ұқсас іс-шаралар тарихы ескерілді.",
        },
        "en": {
            "critical_gap": f"Builds a skill marked critical for the next grade: {covered_names}.",
            "next_grade_gap": f"Helps close a gap against next-grade requirements: {covered_names}.",
            "activity_fit": f"Fits the role and grade; develops: {covered_names}.",
            "career_goal": "Matches the stated career goal.",
            "history_fit": "Participation in similar activities informed this next-step choice.",
        },
    }
    return lines.get(lang, lines["en"]).get(key, "")


def llm_select(employee: dict[str, Any], candidates: list[dict[str, Any]], lang: str) -> list[tuple[dict[str, Any], list[str]]]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not candidates:
        raise RuntimeError("OpenAI API key is not configured")
    import urllib.request

    choices = [{"event_id": c["event"]["event_id"], "title": c["event"]["title"], "type": c["event"]["type"], "duration_hours": c["event"]["duration_hours"], "skills": c["covered"], "factors": eligible_factor_keys(c, employee), "score": c["score"], "history_signal": c["history_signal"]} for c in candidates]
    schema = {"type": "object", "properties": {"recommendations": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {"event_id": {"type": "string", "enum": [c["event"]["event_id"] for c in candidates]}, "factor_keys": {"type": "array", "minItems": 2, "items": {"type": "string", "enum": ["critical_gap", "next_grade_gap", "career_goal", "activity_fit", "history_fit"]}}}, "required": ["event_id", "factor_keys"], "additionalProperties": False}}}, "required": ["recommendations"], "additionalProperties": False}
    payload = {"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "temperature": 0.1, "response_format": {"type": "json_schema", "json_schema": {"name": "career_recommendations", "strict": True, "schema": schema}}, "messages": [{"role": "system", "content": "Choose up to 3 distinct suitable voluntary development activities from the supplied eligible candidates. Use multiple factors, especially critical next-grade gaps and participation history. Never invent facts. Return only the required JSON."}, {"role": "user", "content": json.dumps({"language": lang, "profile": {"role": employee["role"], "grade": employee["grade"], "career_goal": employee.get("career_goal"), "gaps": progress(employee)["gaps"]}, "history": [{"event_id": r["event_id"], "status": r.get("status"), "date": r.get("date")} for r in employee_history(employee["employee_id"])], "candidates": choices}, ensure_ascii=False)}]}
    request = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=8) as response:
        response_data = json.loads(response.read())
    content = json.loads(response_data["choices"][0]["message"]["content"])
    lookup = {c["event"]["event_id"]: c for c in candidates}
    selected = []
    seen = set()
    for item in content.get("recommendations", []):
        event_id, factors = item.get("event_id"), item.get("factor_keys", [])
        if event_id not in lookup or event_id in seen or len(set(factors)) < 2:
            continue
        allowed = set(eligible_factor_keys(lookup[event_id], employee))
        factors = [factor for factor in dict.fromkeys(factors) if factor in allowed]
        if len(factors) < 2:
            continue
        selected.append((lookup[event_id], factors))
        seen.add(event_id)
    if content.get("recommendations") and not selected:
        raise RuntimeError("Invalid LLM recommendation response")
    return selected[:3]


def recommendations(employee: dict[str, Any], lang: str) -> dict[str, Any]:
    candidates = eligible_candidates(employee)
    source = "ai"
    try:
        selected = llm_select(employee, candidates, lang)
        if not selected and candidates:
            raise RuntimeError("LLM returned no valid recommendations")
    except Exception:
        source = "rules_fallback"
        selected = [(c, eligible_factor_keys(c, employee)) for c in candidates[:3]]
    result = []
    for candidate, keys in selected:
        event = candidate["event"]
        result.append({"event_id": event["event_id"], "title": event["title"], "description": event["description"], "type": event["type"], "format": event["format"], "duration_hours": event["duration_hours"], "upcoming_sessions": event.get("upcoming_sessions", []), "reasons": [factor_copy(key, lang, event, candidate, employee) for key in keys], "factors": keys, "develops_skills": event.get("develops_skills", [])})
    return {"source": source, "recommendations": result}


class CompleteRequest(BaseModel):
    employee_id: str
    event_id: str


def localized(lang: str, ru: str, kk: str, en: str) -> str:
    return {"ru": ru, "kk": kk, "en": en}.get(lang, en)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "dataset_date": STATE.get("skills", {}).get("meta", {}).get("as_of_date", "unknown")}


@app.get("/api/demo")
def demo_info() -> dict[str, Any]:
    return {"employee_id": os.getenv("DEMO_EMPLOYEE_ID", "E0001"), "employee_name": STATE["employees"].get(os.getenv("DEMO_EMPLOYEE_ID", "E0001"), {}).get("full_name", "Demo employee"), "as_of_date": STATE["skills"]["meta"]["as_of_date"], "employee_count": len(STATE["employees"])}


@app.get("/api/profile/{employee_id}")
def get_profile(employee_id: str, lang: str = "ru", x_demo_role: str | None = Header(None), x_employee_id: str | None = Header(None)) -> dict[str, Any]:
    employee = employee_required(employee_id, x_demo_role, x_employee_id)
    next_profile = current_grade_profile(employee, True)
    current = current_grade_profile(employee)
    skills_by_id = {s["skill_id"]: s for s in STATE["skills"]["skills"]}
    state = progress(employee)
    translated_events = recommendations(employee, lang)
    employee_public = {key: employee.get(key) for key in ["employee_id", "full_name", "department", "role", "grade", "tenure_months", "preferred_language", "career_goal", "last_review_date"]}
    return {"employee": employee_public, "progress": state, "current_requirements": (current or {}).get("required_skills", {}), "target_requirements": (next_profile or {}).get("required_skills", {}), "skills": [{**skills_by_id.get(g["skill_id"], {"skill_id": g["skill_id"]}), **g} for g in state["gaps"]], "recommendations": translated_events, "as_of_date": STATE["skills"]["meta"]["as_of_date"]}


@app.post("/api/complete")
def complete_activity(body: CompleteRequest, x_demo_role: str | None = Header(None), x_employee_id: str | None = Header(None)) -> dict[str, Any]:
    employee = employee_required(body.employee_id, x_demo_role, x_employee_id)
    event = STATE["events"].get(body.event_id)
    if not event or event.get("mandatory") or body.event_id not in {c["event"]["event_id"] for c in eligible_candidates(employee)}:
        raise HTTPException(status_code=400, detail="Эта активность сейчас недоступна")
    today = STATE["skills"]["meta"]["as_of_date"]
    record = {"record_id": f"CQ_{uuid.uuid4().hex[:12]}", "employee_id": body.employee_id, "event_id": body.event_id, "date": today, "due_date": "", "status": "completed", "completion_pct": "100", "score": "", "feedback_rating": "", "assigned_by": "self"}
    with connect() as conn:
        conn.execute("INSERT INTO activity_history(record_id,payload) VALUES (?,?)", (record["record_id"], json.dumps(record)))
    STATE["history"].append(record)
    return {"ok": True, "progress": progress(employee)}


def parse_upload(name: str, raw: bytes) -> Any:
    if len(raw) > 5_000_000:
        raise ValueError(f"{name}: файл больше 5 МБ")
    if name.endswith(".json"):
        payload = json.loads(raw.decode("utf-8-sig"))
        if "employees" not in payload or not isinstance(payload["employees"], list):
            raise ValueError("employees.json должен содержать массив employees")
        return payload["employees"]
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != HISTORY_FIELDS:
        raise ValueError("CSV должен содержать столбцы activity_history.csv в исходном порядке")
    return list(reader)


def validate_import(employees: list[dict[str, Any]], history: list[dict[str, Any]]) -> list[str]:
    errors = []
    employee_ids = set(STATE["employees"])
    event_ids = set(STATE["events"])
    seen = set()
    for i, emp in enumerate(employees, 1):
        eid = emp.get("employee_id")
        if not eid or eid in seen:
            errors.append(f"Профиль {i}: отсутствует или повторяется employee_id {eid!r}")
            continue
        seen.add(eid)
        employee_ids.add(eid)
        missing = [key for key in ("role", "grade", "skills") if key not in emp]
        if missing:
            errors.append(f"{eid}: отсутствуют поля {', '.join(missing)}")
        elif not any(p["role"] == emp["role"] and p["grade"] == emp["grade"] for p in STATE["skills"]["role_profiles"]):
            errors.append(f"{eid}: неизвестное сочетание роли и грейда")
        elif any(not isinstance(v, int) or v < 0 or v > 5 for v in emp["skills"].values()):
            errors.append(f"{eid}: уровень навыка должен быть целым числом от 0 до 5")
        elif any(k not in {s["skill_id"] for s in STATE["skills"]["skills"]} for k in emp["skills"]):
            errors.append(f"{eid}: неизвестный skill_id")
    known_records = {row["record_id"] for row in STATE["history"]}
    for i, row in enumerate(history, 1):
        rid = row.get("record_id")
        if not rid or rid in known_records:
            errors.append(f"Запись истории {i}: отсутствует или уже используется record_id {rid!r}")
        known_records.add(rid)
        if row.get("employee_id") not in employee_ids:
            errors.append(f"{rid}: неизвестный employee_id {row.get('employee_id')}")
        if row.get("event_id") not in event_ids:
            errors.append(f"{rid}: неизвестный event_id {row.get('event_id')}")
        if row.get("status") not in {"completed", "in_progress", "dropped", "no_show", "declined", "overdue"}:
            errors.append(f"{rid}: неизвестный status")
        if not as_date(row.get("date")):
            errors.append(f"{rid}: некорректная дата")
    return errors


@app.post("/api/import")
async def import_data(employees_file: UploadFile | None = File(None), history_file: UploadFile | None = File(None), x_demo_role: str | None = Header(None)) -> dict[str, Any]:
    role_required("hr", x_demo_role)
    if not employees_file and not history_file:
        raise HTTPException(status_code=400, detail="Загрузите employees.json и/или activity_history.csv")
    try:
        employees = parse_upload(employees_file.filename or "employees.json", await employees_file.read()) if employees_file else []
        history = parse_upload(history_file.filename or "activity_history.csv", await history_file.read()) if history_file else []
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    errors = validate_import(employees, history)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors[:100], "total_errors": len(errors)})
    try:
        with connect() as conn:
            for emp in employees:
                conn.execute("INSERT INTO uploads(kind,item_id,payload) VALUES('employee',?,?)", (emp["employee_id"], json.dumps(emp)))
            for row in history:
                conn.execute("INSERT INTO activity_history(record_id,payload) VALUES(?,?)", (row["record_id"], json.dumps(row)))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Идентификаторы уже были загружены") from exc
    for emp in employees:
        STATE["employees"][emp["employee_id"]] = emp
    STATE["history"].extend(history)
    return {"ok": True, "employees_imported": len(employees), "history_imported": len(history)}


@app.get("/api/hr")
def hr_summary(x_demo_role: str | None = Header(None)) -> dict[str, Any]:
    role_required("hr", x_demo_role)
    gaps = defaultdict(lambda: {"employees": 0, "total_gap": 0, "critical_count": 0})
    slipping = []
    for employee in STATE["employees"].values():
        profile = progress(employee)
        for gap in profile["gaps"]:
            if gap["gap"]:
                key = gap["skill_id"]
                gaps[key]["employees"] += 1
                gaps[key]["total_gap"] += gap["gap"]
                gaps[key]["critical_count"] += int(gap["critical"])
        recent = [r for r in employee_history(employee["employee_id"]) if r.get("status") in {"no_show", "declined", "dropped"} and (as_date(STATE["skills"]["meta"]["as_of_date"]) - (as_date(r.get("date")) or date.min)).days <= 183]
        if len(recent) >= 2:
            slipping.append({"employee_id": employee["employee_id"], "name": employee.get("full_name"), "role": employee["role"], "grade": employee["grade"], "recent_opt_outs": len(recent), "latest_date": max(r.get("date", "") for r in recent)})
    skill_map = {s["skill_id"]: s for s in STATE["skills"]["skills"]}
    weak = [{"skill_id": key, "name": skill_map.get(key, {}).get("name", key), **value} for key, value in gaps.items()]
    weak.sort(key=lambda row: (-row["critical_count"], -row["total_gap"], row["name"]))
    return {"competency_gaps": weak[:20], "employees_at_risk": sorted(slipping, key=lambda r: (-r["recent_opt_outs"], r["name"])), "employee_count": len(STATE["employees"]), "history_count": len(STATE["history"])}


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="web")
