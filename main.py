from __future__ import annotations

import csv
import asyncio
import io
import json
import os
import sqlite3
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Depends, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
import auth
from auth import Principal, require_session

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("CAREER_QUEST_DB", str(ROOT / "career_quest.sqlite3")))
GRADE_ORDER = ["Junior", "Middle", "Senior", "Lead"]
HISTORY_FIELDS = ["record_id", "employee_id", "event_id", "date", "due_date", "status", "completion_pct", "score", "feedback_rating", "assigned_by"]
STATE: dict[str, Any] = {}


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=8)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def load_json(name: str) -> dict[str, Any]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def init_state() -> None:
    employees = load_json("employees.json")["employees"]
    events = load_json("events.json")["events"]
    STATE.update(
        employees={},
        events={row["event_id"]: row for row in events},
        skills=load_json("skills.json"),
        history=[],
    )
    history = parse_upload("activity_history.csv", (DATA / "activity_history.csv").read_bytes())
    skill_ids = {s["skill_id"] for s in STATE["skills"]["skills"]}
    if len(STATE["events"]) != len(events) or len(skill_ids) != len(STATE["skills"]["skills"]):
        raise ValueError("Повторные идентификаторы в каталоге мероприятий или навыков")
    if not as_date(STATE["skills"]["meta"].get("as_of_date")):
        raise ValueError("Некорректная дата среза")
    for event in events:
        refs = set(event.get("prerequisites", {})) | {g["skill_id"] for g in event.get("develops_skills", [])}
        if not refs <= skill_ids:
            raise ValueError(f"{event['event_id']}: неизвестные навыки")
    for profile in STATE["skills"]["role_profiles"]:
        if not set(profile["required_skills"]) <= skill_ids or not set(profile["critical_skills"]) <= set(profile["required_skills"]):
            raise ValueError("Некорректные ссылки в требованиях грейда")
    errors = validate_import(employees, history)
    if errors:
        raise ValueError("Ошибки стартового набора: " + "; ".join(errors[:100]))
    STATE["employees"] = {row["employee_id"]: row for row in employees}
    STATE["history"] = history
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS uploads (kind TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,item_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS activity_history (record_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        for row in conn.execute("SELECT item_id,payload FROM uploads WHERE kind='employee'"):
            STATE["employees"][row["item_id"]] = json.loads(row["payload"])
        for row in conn.execute("SELECT payload FROM activity_history"):
            STATE["history"].append(json.loads(row["payload"]))
    auth.init_storage(connect)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_state()
    yield


app = FastAPI(title="Career Quest", version="1.0.0", lifespan=lifespan)
app.include_router(auth.router)


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def role_required(role: str, principal: Principal) -> None:
    if not isinstance(principal, Principal) or principal.role != role:
        raise HTTPException(status_code=403, detail="Доступ запрещён для этой роли")


def employee_required(employee_id: str, principal: Principal, allow_hr: bool = False) -> dict[str, Any]:
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail="Войдите в аккаунт")
    if not (allow_hr and principal.role == "hr"):
        role_required("employee", principal)
        if principal.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Можно открыть только собственный профиль")
    employee = STATE["employees"].get(employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    return employee


def as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
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
    snapshot = as_date(STATE["skills"]["meta"]["as_of_date"])
    return [row for row in STATE["history"] if row["employee_id"] == employee_id and as_date(row.get("date")) and as_date(row["date"]) <= snapshot]


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
            before = levels.get(skill_id, 0)
            levels[skill_id] = max(before, min(int(gain["max_level"]), before + int(gain["gain"])))
    target = current_grade_profile(employee, next_grade=True)
    current = current_grade_profile(employee)
    requirements = (target or current or {}).get("required_skills", {})
    critical = set((target or current or {}).get("critical_skills", []))
    skill_lookup = {s["skill_id"]: s for s in STATE["skills"]["skills"]}
    gaps = [{"skill_id": key, "name": skill_lookup.get(key, {}).get("name", key), "level": levels.get(key, 0), "required": int(required), "gap": max(0, int(required) - levels.get(key, 0)), "critical": key in critical} for key, required in requirements.items()]
    return {"levels": levels, "gaps": sorted(gaps, key=lambda x: (not x["critical"], -x["gap"], x["name"])), "target_grade": next((p["grade"] for p in [target] if p), None), "at_top_grade": target is None, "critical_skills": list(critical), "completed_since_review": len(completed), "improved_skill_count": sum(level > employee.get("skills", {}).get(sid, 0) for sid, level in levels.items())}


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
                if amount > 0:
                    covered.append(skill)
        if impact <= 0:
            continue
        counts = by_type[event.get("type", "")]
        preference = min(2, counts["completed"]) - min(3, counts["no_show"] + counts["declined"] + counts["dropped"])
        goal = employee.get("career_goal") or {}
        goal_match = int(bool(target and goal.get("target_role") == employee["role"] and GRADE_ORDER.index(goal.get("target_grade", "Junior")) >= GRADE_ORDER.index(target["grade"])))
        result.append({"event": event, "impact": impact, "covered": covered, "goal_match": goal_match, "history_signal": preference, "history_counts": dict(counts), "score": impact + 2 * goal_match + preference})
    return sorted(result, key=lambda row: (-row["score"], row["event"]["event_id"]))


def eligible_factor_keys(candidate: dict[str, Any], employee: dict[str, Any]) -> list[str]:
    keys = ["next_grade_gap", "activity_fit"]
    if any(s in progress(employee)["critical_skills"] for s in candidate["covered"]):
        keys.append("critical_gap")
    if candidate["goal_match"]:
        keys.append("career_goal")
    if candidate.get("history_counts"):
        keys.append("history_fit")
    return keys


def recommendation_evidence(candidate: dict[str, Any], employee: dict[str, Any]) -> dict[str, Any]:
    current = progress(employee)
    gaps = {g["skill_id"]: g for g in current["gaps"]}
    effects = []
    for gain in candidate["event"].get("develops_skills", []):
        sid = gain["skill_id"]
        if sid not in candidate["covered"]:
            continue
        before = current["levels"].get(sid, 0)
        after = max(before, min(gain["max_level"], before + gain["gain"]))
        effects.append({"skill_id": sid, "name": gaps[sid]["name"], "current": before,
                        "required": gaps[sid]["required"], "after_completion": after,
                        "effective_gain": after - before, "max_level": gain["max_level"],
                        "critical": gaps[sid]["critical"]})
    return {"target_grade": current["target_grade"], "skill_effects": effects,
            "similarity_basis": "activity_type", "history_counts": candidate.get("history_counts", {}),
            "activity_type": candidate["event"]["type"], "as_of_date": STATE["skills"]["meta"]["as_of_date"]}


def factor_copy(key: str, lang: str, event: dict[str, Any], candidate: dict[str, Any], employee: dict[str, Any]) -> str:
    evidence = recommendation_evidence(candidate, employee)
    effects = evidence["skill_effects"]
    critical = ", ".join(e["name"] for e in effects if e["critical"])
    grade = evidence["target_grade"]
    counts = evidence["history_counts"]
    completed, skipped, declined, dropped = (counts.get(k, 0) for k in ("completed", "no_show", "declined", "dropped"))
    goal = employee.get("career_goal") or {}
    role, level = employee["role"], employee["grade"]
    effect_ru = "; ".join(f"{e['name']}: {e['current']} из {e['required']}; после выполнения — {e['after_completion']} (+{e['effective_gain']})" for e in effects)
    effect_kk = "; ".join(f"{e['name']}: қазір {e['current']}, талап {e['required']}; аяқтаған соң — {e['after_completion']} (+{e['effective_gain']})" for e in effects)
    effect_en = "; ".join(f"{e['name']}: {e['current']} of {e['required']}; after completion: {e['after_completion']} (+{e['effective_gain']})" for e in effects)
    lines = {
        "ru": {
            "next_grade_gap": f"Для {grade}: {effect_ru}.",
            "critical_gap": f"Для перехода на {grade} обязателен требуемый уровень: {critical}.",
            "activity_fit": f"Доступно для {role}, {level}; предпосылки выполнены. Длительность — {event['duration_hours']} ч.",
            "career_goal": f"Поддерживает вашу цель: {goal.get('target_role')}, {goal.get('target_grade')}.",
            "history_fit": f"В истории активностей того же типа: завершено — {completed}, пропущено — {skipped}, отказов — {declined}, прекращено — {dropped}. Эти данные учтены при выборе; пропуски не запрещают участие.",
        },
        "kk": {
            "next_grade_gap": f"{grade} үшін: {effect_kk}.",
            "critical_gap": f"{grade} деңгейіне өту үшін талап етілетін деңгейге жетуі міндетті дағдылар: {critical}.",
            "activity_fit": f"{role}, {level} үшін қолжетімді; алғышарттар орындалған. Ұзақтығы — {event['duration_hours']} сағ.",
            "career_goal": f"Мансап мақсатыңызға көмектеседі: {goal.get('target_role')}, {goal.get('target_grade')}.",
            "history_fit": f"Осы типтегі іс-шаралар тарихы: аяқталған — {completed}, қатыспаған — {skipped}, бас тартқан — {declined}, тоқтатқан — {dropped}. Бұл деректер таңдауда ескерілді; қатыспау қайта қатысуға тыйым салмайды.",
        },
        "en": {
            "next_grade_gap": f"For {grade}: {effect_en}.",
            "critical_gap": f"Reaching the required level in these skills is mandatory for {grade}: {critical}.",
            "activity_fit": f"Eligible for {role}, {level}; prerequisites met. Duration: {event['duration_hours']} h.",
            "career_goal": f"Supports your goal: {goal.get('target_role')}, {goal.get('target_grade')}.",
            "history_fit": f"History for the same activity type: {completed} completed, {skipped} no-shows, {declined} declined, {dropped} dropped. These counts informed selection; missed activities do not prevent participation.",
        },
    }
    return lines.get(lang, lines["en"]).get(key, "")


class OpenAISelectionError(RuntimeError):
    """Safe diagnostic code; never includes provider bodies or credentials."""


def llm_select(employee: dict[str, Any], candidates: list[dict[str, Any]], lang: str) -> list[tuple[dict[str, Any], list[str]]]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise OpenAISelectionError("missing_api_key")
    if not candidates:
        return []
    import urllib.error
    import urllib.request

    choices = [{"event_id": c["event"]["event_id"], "title": c["event"]["title"], "type": c["event"]["type"], "duration_hours": c["event"]["duration_hours"], "skills": c["covered"], "factors": eligible_factor_keys(c, employee), "score": c["score"], "history_signal": c["history_signal"]} for c in candidates]
    schema = {"type": "object", "properties": {"recommendations": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {"event_id": {"type": "string", "enum": [c["event"]["event_id"] for c in candidates]}, "factor_keys": {"type": "array", "minItems": 2, "items": {"type": "string", "enum": ["critical_gap", "next_grade_gap", "career_goal", "activity_fit", "history_fit"]}}}, "required": ["event_id", "factor_keys"], "additionalProperties": False}}}, "required": ["recommendations"], "additionalProperties": False}
    payload = {"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "temperature": 0.1, "response_format": {"type": "json_schema", "json_schema": {"name": "career_recommendations", "strict": True, "schema": schema}}, "messages": [{"role": "system", "content": "Choose up to 3 distinct suitable voluntary development activities from the supplied eligible candidates. Use multiple factors, especially critical next-grade gaps and participation history. Never invent facts. Return only the required JSON."}, {"role": "user", "content": json.dumps({"language": lang, "profile": {"role": employee["role"], "grade": employee["grade"], "career_goal": employee.get("career_goal"), "gaps": progress(employee)["gaps"]}, "history": [{"event_id": r["event_id"], "status": r.get("status"), "date": r.get("date")} for r in employee_history(employee["employee_id"])], "candidates": choices}, ensure_ascii=False)}]}
    request = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response_data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise OpenAISelectionError(f"provider_http_{exc.code}") from None
    except (TimeoutError, urllib.error.URLError):
        raise OpenAISelectionError("provider_connection_or_timeout") from None
    choice = response_data["choices"][0]
    if choice.get("finish_reason", "stop") != "stop" or choice["message"].get("refusal"):
        raise OpenAISelectionError("provider_refusal_or_incomplete_response")
    content = json.loads(choice["message"]["content"])
    lookup = {c["event"]["event_id"]: c for c in candidates}
    selected = []
    seen = set()
    items = content.get("recommendations", [])
    if not isinstance(items, list) or not 1 <= len(items) <= 3:
        raise OpenAISelectionError("invalid_recommendation_count")
    for item in items:
        event_id, factors = item.get("event_id"), item.get("factor_keys", [])
        if event_id not in lookup or event_id in seen or len(set(factors)) < 2:
            raise OpenAISelectionError("invalid_event_or_factors")
        allowed = set(eligible_factor_keys(lookup[event_id], employee))
        if not set(factors) <= allowed:
            raise OpenAISelectionError("unsupported_factors")
        factors = list(dict.fromkeys(factors))
        if len(factors) < 2:
            continue
        selected.append((lookup[event_id], factors))
        seen.add(event_id)
    if content.get("recommendations") and not selected:
        raise RuntimeError("Invalid LLM recommendation response")
    return selected[:3]


def recommendations(employee: dict[str, Any], lang: str, use_llm: bool = True) -> dict[str, Any]:
    candidates = eligible_candidates(employee)
    source = "ai"
    try:
        if not use_llm:
            raise RuntimeError("Rules requested")
        selected = llm_select(employee, candidates, lang)
        if not selected and candidates:
            raise RuntimeError("LLM returned no valid recommendations")
    except Exception:
        source = "rules_fallback"
        selected = [(c, eligible_factor_keys(c, employee)) for c in candidates[:3]]
    result = []
    for candidate, keys in selected:
        keys = list(dict.fromkeys(["next_grade_gap", *keys]))
        event = candidate["event"]
        result.append({"event_id": event["event_id"], "title": event["title"], "description": event["description"], "type": event["type"], "format": event["format"], "duration_hours": event["duration_hours"], "upcoming_sessions": event.get("upcoming_sessions", []), "reasons": [factor_copy(key, lang, event, candidate, employee) for key in keys], "factors": keys, "evidence": recommendation_evidence(candidate, employee), "develops_skills": event.get("develops_skills", [])})
    return {"source": source, "recommendations": result}


class CompleteRequest(BaseModel):
    employee_id: str
    event_id: str


class OpenAIKeyRequest(BaseModel):
    api_key: SecretStr


@app.get("/api/settings/openai")
def openai_settings(principal: Principal = Depends(require_session)):
    role_required("hr", principal)
    return {"configured": bool(os.getenv("OPENAI_API_KEY")), "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini")}


@app.post("/api/settings/openai")
def save_openai_key(body: OpenAIKeyRequest, principal: Principal = Depends(require_session)):
    role_required("hr", principal)
    key = body.api_key.get_secret_value().strip()
    if not 20 <= len(key) <= 4096 or not key.startswith("sk-") or any(char.isspace() for char in key):
        raise HTTPException(400, "Некорректный формат API-ключа")
    os.environ["OPENAI_API_KEY"] = key
    return openai_settings(principal)


@app.delete("/api/settings/openai")
def remove_openai_key(principal: Principal = Depends(require_session)):
    role_required("hr", principal)
    os.environ.pop("OPENAI_API_KEY", None)
    return openai_settings(principal)


def localized(lang: str, ru: str, kk: str, en: str) -> str:
    return {"ru": ru, "kk": kk, "en": en}.get(lang, en)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "dataset_date": STATE.get("skills", {}).get("meta", {}).get("as_of_date", "unknown")}


@app.get("/api/demo")
def demo_info(principal: Principal = Depends(require_session)) -> dict[str, Any]:
    return {"employee_id": principal.employee_id, "as_of_date": STATE["skills"]["meta"]["as_of_date"]}


@app.get("/api/profile/{employee_id}")
def get_profile(employee_id: str, lang: str = "ru", principal: Principal = Depends(require_session)) -> dict[str, Any]:
    employee = employee_required(employee_id, principal, allow_hr=True)
    next_profile = current_grade_profile(employee, True)
    current = current_grade_profile(employee)
    skills_by_id = {s["skill_id"]: s for s in STATE["skills"]["skills"]}
    state = progress(employee)
    employee_public = {key: employee.get(key) for key in ["employee_id", "full_name", "department", "role", "grade", "tenure_months", "preferred_language", "career_goal", "last_review_date"]}
    return {"employee": employee_public, "progress": state, "current_requirements": (current or {}).get("required_skills", {}), "target_requirements": (next_profile or {}).get("required_skills", {}), "skills": [{**skills_by_id.get(g["skill_id"], {"skill_id": g["skill_id"]}), **g} for g in state["gaps"]], "as_of_date": STATE["skills"]["meta"]["as_of_date"]}


@app.get("/api/recommendations/{employee_id}")
async def get_recommendations(employee_id: str, lang: str = "ru", principal: Principal = Depends(require_session)) -> dict[str, Any]:
    employee = employee_required(employee_id, principal, allow_hr=True)
    try:
        return await asyncio.wait_for(asyncio.to_thread(recommendations, employee, lang), timeout=8.5)
    except asyncio.TimeoutError:
        return recommendations(employee, lang, use_llm=False)


@app.post("/api/complete")
def complete_activity(body: CompleteRequest, principal: Principal = Depends(require_session)) -> dict[str, Any]:
    employee = employee_required(body.employee_id, principal)
    event = STATE["events"].get(body.event_id)
    if not event or event.get("mandatory") or body.event_id not in {c["event"]["event_id"] for c in eligible_candidates(employee)}:
        raise HTTPException(status_code=400, detail="Эта активность сейчас недоступна")
    before = progress(employee)
    snapshot = as_date(STATE["skills"]["meta"]["as_of_date"]) or date.today()
    activity_day = snapshot.isoformat()
    record = {"record_id": f"CQ_{uuid.uuid4().hex[:12]}", "employee_id": body.employee_id, "event_id": body.event_id, "date": activity_day, "due_date": "", "status": "completed", "completion_pct": "100", "score": "", "feedback_rating": "", "assigned_by": "self"}
    with connect() as conn:
        conn.execute("INSERT INTO activity_history(record_id,payload) VALUES (?,?)", (record["record_id"], json.dumps(record)))
    STATE["history"].append(record)
    after = progress(employee)
    names = {s["skill_id"]: s["name"] for s in STATE["skills"]["skills"]}
    changes = [{"skill_id": sid, "name": names[sid], "before": before["levels"][sid], "after": value}
               for sid, value in after["levels"].items() if value != before["levels"][sid]]
    return {"ok": True, "progress": after, "changes": changes, "recorded_date": activity_day}


def parse_upload(name: str, raw: bytes) -> Any:
    if len(raw) > 5_000_000:
        raise ValueError(f"{name}: файл больше 5 МБ")
    if name.endswith(".json"):
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict) or "employees" not in payload or not isinstance(payload["employees"], list):
            raise ValueError("employees.json должен содержать массив employees")
        return payload["employees"]
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != HISTORY_FIELDS:
        raise ValueError("CSV должен содержать столбцы activity_history.csv в исходном порядке")
    return list(reader)


def validate_import(employees: list[dict[str, Any]], history: list[dict[str, Any]]) -> list[str]:
    errors = []
    snapshot = as_date(STATE["skills"]["meta"]["as_of_date"])
    employee_ids = set(STATE["employees"])
    event_ids = set(STATE["events"])
    skill_ids = {s["skill_id"] for s in STATE["skills"]["skills"]}
    role_grades = {(p["role"], p["grade"]) for p in STATE["skills"]["role_profiles"]}
    accepted = {}
    for i, emp in enumerate(employees, 1):
        label = f"Профиль {i}"
        if not isinstance(emp, dict):
            errors.append(f"{label}: запись должна быть объектом")
            continue
        eid = emp.get("employee_id")
        if not isinstance(eid, str) or not eid.strip() or eid in employee_ids:
            errors.append(f"{label}: отсутствует или повторяется employee_id {eid!r}")
            continue
        employee_ids.add(eid)
        accepted[eid] = emp
        for field in ("full_name", "department", "role", "grade"):
            if not isinstance(emp.get(field), str) or not emp[field].strip():
                errors.append(f"{eid}: {field} должен быть непустой строкой")
        role, grade = emp.get("role"), emp.get("grade")
        if not isinstance(role, str) or not isinstance(grade, str) or (role, grade) not in role_grades:
            errors.append(f"{eid}: неизвестное сочетание роли и грейда")
        skills = emp.get("skills")
        if not isinstance(skills, dict):
            errors.append(f"{eid}: skills должен быть объектом")
        else:
            for sid, level in skills.items():
                if sid not in skill_ids:
                    errors.append(f"{eid}: неизвестный skill_id {sid}")
                if type(level) is not int or not 0 <= level <= 5:
                    errors.append(f"{eid}/{sid}: уровень должен быть целым числом от 0 до 5")
        for field in ("last_review_date", "hire_date"):
            day = as_date(emp.get(field))
            if day is None or day > snapshot:
                errors.append(f"{eid}: {field} должен быть датой YYYY-MM-DD не позже даты среза")
        hired, reviewed = as_date(emp.get("hire_date")), as_date(emp.get("last_review_date"))
        if hired and reviewed and reviewed < hired:
            errors.append(f"{eid}: оценка не может быть раньше найма")
        if type(emp.get("tenure_months")) is not int or emp["tenure_months"] < 0:
            errors.append(f"{eid}: tenure_months должен быть неотрицательным целым числом")
        if emp.get("preferred_language") not in ("ru", "kk", "en"):
            errors.append(f"{eid}: preferred_language должен быть ru, kk или en")
        if emp.get("work_format") not in ("office", "hybrid", "remote"):
            errors.append(f"{eid}: неизвестный work_format")
        goal = emp.get("career_goal")
        if goal is not None:
            if (not isinstance(goal, dict) or not isinstance(goal.get("target_role"), str)
                    or not isinstance(goal.get("target_grade"), str)
                    or (goal["target_role"], goal["target_grade"]) not in role_grades):
                errors.append(f"{eid}: некорректная карьерная цель")
    all_employees = {**STATE["employees"], **accepted}
    for eid, emp in accepted.items():
        manager_id = emp.get("manager_id")
        if manager_id is not None:
            manager = all_employees.get(manager_id) if isinstance(manager_id, str) else None
            if not manager or manager_id == eid or manager.get("grade") != "Lead" or manager.get("department") != emp.get("department"):
                errors.append(f"{eid}: manager_id должен указывать на другого Lead из того же отдела")
    known_records = {row["record_id"] for row in STATE["history"]}
    status_ranges = {"completed": (100, 100), "in_progress": (0, 95), "dropped": (5, 95), "no_show": (0, 0), "declined": (0, 0), "overdue": (0, 95)}
    for i, row in enumerate(history, 1):
        if not isinstance(row, dict):
            errors.append(f"Запись истории {i}: запись должна быть объектом")
            continue
        rid = row.get("record_id")
        label = rid if isinstance(rid, str) and rid else f"Запись истории {i}"
        if not isinstance(rid, str) or not rid.strip() or rid in known_records:
            errors.append(f"{label}: отсутствует или уже используется record_id")
        else:
            known_records.add(rid)
        eid, event_id = row.get("employee_id"), row.get("event_id")
        if not isinstance(eid, str) or eid not in employee_ids:
            errors.append(f"{label}: неизвестный employee_id {eid!r}")
        if not isinstance(event_id, str) or event_id not in event_ids:
            errors.append(f"{label}: неизвестный event_id {event_id!r}")
        status = row.get("status")
        if not isinstance(status, str) or status not in status_ranges:
            errors.append(f"{label}: неизвестный status")
        day = as_date(row.get("date"))
        if day is None or day > snapshot:
            errors.append(f"{label}: date должна быть датой YYYY-MM-DD не позже даты среза")
        for field, maximum in (("completion_pct", 100), ("score", 100), ("feedback_rating", 5)):
            value = row.get(field)
            minimum = 1 if field == "feedback_rating" else 0
            if value in (None, "") and field != "completion_pct":
                continue
            try:
                if isinstance(value, bool) or isinstance(value, float):
                    raise ValueError
                number = int(value)
                if not minimum <= number <= maximum:
                    raise ValueError
                if field == "completion_pct" and isinstance(status, str) and status in status_ranges:
                    low, high = status_ranges[status]
                    if not low <= number <= high:
                        errors.append(f"{label}: completion_pct не соответствует status={status}")
            except (TypeError, ValueError):
                errors.append(f"{label}: {field} должен быть целым числом от {minimum} до {maximum}")
        if row.get("due_date") and not as_date(row.get("due_date")):
            errors.append(f"{label}: некорректный due_date")
        if row.get("assigned_by") not in ("self", "manager", "hr"):
            errors.append(f"{label}: неизвестный assigned_by")
    return errors


@app.post("/api/import")
async def import_data(employees_file: UploadFile | None = File(None), history_file: UploadFile | None = File(None), principal: Principal = Depends(require_session)) -> dict[str, Any]:
    role_required("hr", principal)
    if not employees_file and not history_file:
        raise HTTPException(status_code=400, detail="Загрузите employees.json и/или activity_history.csv")
    try:
        employees = parse_upload("employees.json", await employees_file.read()) if employees_file else []
        history = parse_upload("activity_history.csv", await history_file.read()) if history_file else []
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail={"errors": [str(exc)], "total_errors": 1}) from exc
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
def hr_summary(principal: Principal = Depends(require_session)) -> dict[str, Any]:
    role_required("hr", principal)
    gaps = defaultdict(lambda: {"employees": 0, "total_gap": 0, "critical_count": 0})
    grouped_gaps = defaultdict(lambda: defaultdict(lambda: {"employees": 0, "total_gap": 0, "critical_count": 0}))
    slipping = []
    for employee in STATE["employees"].values():
        profile = progress(employee)
        for gap in profile["gaps"]:
            if gap["gap"]:
                key = gap["skill_id"]
                gaps[key]["employees"] += 1
                gaps[key]["total_gap"] += gap["gap"]
                gaps[key]["critical_count"] += int(gap["critical"])
                segment = (employee["role"], employee["grade"])
                grouped_gaps[segment][key]["employees"] += 1
                grouped_gaps[segment][key]["total_gap"] += gap["gap"]
                grouped_gaps[segment][key]["critical_count"] += int(gap["critical"])
        recent = [r for r in employee_history(employee["employee_id"]) if r.get("status") in {"no_show", "declined", "dropped"} and (as_date(STATE["skills"]["meta"]["as_of_date"]) - (as_date(r.get("date")) or date.min)).days <= 183]
        if len(recent) >= 2:
            slipping.append({"employee_id": employee["employee_id"], "name": employee.get("full_name"), "role": employee["role"], "grade": employee["grade"], "recent_opt_outs": len(recent), "latest_date": max(r.get("date", "") for r in recent)})
    skill_map = {s["skill_id"]: s for s in STATE["skills"]["skills"]}
    weak = [{"skill_id": key, "name": skill_map.get(key, {}).get("name", key), **value} for key, value in gaps.items()]
    weak.sort(key=lambda row: (-row["critical_count"], -row["total_gap"], row["name"]))
    by_segment = []
    for (role, grade), skill_data in grouped_gaps.items():
        rows = [{"skill_id": key, "name": skill_map.get(key, {}).get("name", key), **value} for key, value in skill_data.items()]
        rows.sort(key=lambda row: (-row["critical_count"], -row["total_gap"], row["name"]))
        by_segment.append({"role": role, "grade": grade, "competency_gaps": rows})
    by_segment.sort(key=lambda item: (item["role"], GRADE_ORDER.index(item["grade"])))
    return {"competency_gaps": weak, "role_grade_gaps": by_segment, "roles": sorted({e["role"] for e in STATE["employees"].values()}), "grades": GRADE_ORDER, "employees_at_risk": sorted(slipping, key=lambda r: (-r["recent_opt_outs"], r["name"] or "")), "employee_count": len(STATE["employees"]), "history_count": len(STATE["history"])}


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="web")
