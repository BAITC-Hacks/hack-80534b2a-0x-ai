"""Demo-only rewards. XP is separate from skill levels and has no cash value."""
import uuid
from fastapi import HTTPException

CATALOG = [
    {"id": "market", "cost": 600, "icon": "▧", "name": {"ru": "Halyk Market", "kk": "Halyk Market", "en": "Halyk Market"}, "offer": {"ru": "Демо-скидка на покупку", "kk": "Сатып алуға демо жеңілдік", "en": "Demo shopping discount"}},
    {"id": "sport", "cost": 400, "icon": "◇", "name": {"ru": "Спорт и энергия", "kk": "Спорт және қуат", "en": "Sport & energy"}, "offer": {"ru": "Гостевой визит в спортзал или бассейн", "kk": "Спортзалға немесе бассейнге бір рет бару", "en": "Gym or swimming pool guest visit"}},
    {"id": "cinema", "cost": 300, "icon": "▷", "name": {"ru": "Вечер в кино", "kk": "Кино кеші", "en": "Movie night"}, "offer": {"ru": "Демо-билет на киносеанс", "kk": "Киносеансқа демо билет", "en": "Demo cinema ticket"}},
    {"id": "coffee", "cost": 150, "icon": "☕", "name": {"ru": "Кофе-пауза", "kk": "Кофе үзілісі", "en": "Coffee break"}, "offer": {"ru": "Напиток в кофейне", "kk": "Кофеханадағы сусын", "en": "A drink at a coffee shop"}},
    {"id": "books", "cost": 250, "icon": "▤", "name": {"ru": "Новая история", "kk": "Жаңа оқиға", "en": "A new story"}, "offer": {"ru": "Электронная книга или аудиокнига", "kk": "Электронды кітап немесе аудиокітап", "en": "An ebook or audiobook"}},
    {"id": "experience", "cost": 450, "icon": "✦", "name": {"ru": "Попробовать новое", "kk": "Жаңаны байқап көру", "en": "Try something new"}, "offer": {"ru": "Творческий мастер-класс или скалодром", "kk": "Шығармашылық шеберлік сабағы немесе өрмелеу", "en": "Creative workshop or climbing session"}},
]


def init(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS quest_xp (employee_id TEXT NOT NULL, award_key TEXT NOT NULL, event_id TEXT NOT NULL, points INTEGER NOT NULL CHECK(points > 0), date TEXT NOT NULL, PRIMARY KEY(employee_id, award_key))")
    conn.execute("CREATE TABLE IF NOT EXISTS quest_redemptions (employee_id TEXT NOT NULL, request_id TEXT NOT NULL, reward_id TEXT NOT NULL, cost INTEGER NOT NULL CHECK(cost > 0), code TEXT NOT NULL UNIQUE, date TEXT NOT NULL, PRIMARY KEY(employee_id, request_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS quest_goals (employee_id TEXT PRIMARY KEY, reward_id TEXT NOT NULL)")


def sync(conn, employee_id, history, events, snapshot):
    # Imported history is trusted demo data, not proof for a real partner payout.
    for record in sorted(history, key=lambda r: (r.get('date', ''), r['record_id'])):
        event = events.get(record['event_id'])
        if record['employee_id'] != employee_id or record.get('status') != 'completed' or not event or event.get('mandatory') or record['date'] > snapshot:
            continue
        period = record['date'][:7] if record['event_id'] == 'EV_036' else 'once'
        conn.execute('INSERT OR IGNORE INTO quest_xp VALUES(?,?,?,?,?)', (employee_id, record['event_id'] + ':' + period, record['event_id'], 100, record['date']))


def balance(conn, employee_id):
    earned = conn.execute('SELECT COALESCE(SUM(points),0) FROM quest_xp WHERE employee_id=?', (employee_id,)).fetchone()[0]
    spent = conn.execute('SELECT COALESCE(SUM(cost),0) FROM quest_redemptions WHERE employee_id=?', (employee_id,)).fetchone()[0]
    return earned, spent


def wallet(conn, employee_id):
    earned, spent = balance(conn, employee_id)
    goal = conn.execute('SELECT reward_id FROM quest_goals WHERE employee_id=?', (employee_id,)).fetchone()
    return {'balance': earned-spent, 'earned': earned, 'spent': spent, 'goal': goal[0] if goal else None, 'catalog': CATALOG,
            'codes': [dict(row) for row in conn.execute('SELECT reward_id,cost,code,date FROM quest_redemptions WHERE employee_id=? ORDER BY rowid DESC', (employee_id,))],
            'awards': [dict(row) for row in conn.execute('SELECT event_id,points,date FROM quest_xp WHERE employee_id=? ORDER BY date DESC,award_key', (employee_id,))]}


def reward(reward_id):
    selected = next((item for item in CATALOG if item['id'] == reward_id), None)
    if not selected:
        raise HTTPException(404, 'reward_not_found')
    return selected


def redeem(conn, employee_id, reward_id, request_id, snapshot):
    selected = reward(reward_id)
    existing = conn.execute('SELECT reward_id FROM quest_redemptions WHERE employee_id=? AND request_id=?', (employee_id, request_id)).fetchone()
    if existing:
        if existing[0] != reward_id:
            raise HTTPException(409, 'redemption_conflict')
        return
    earned, spent = balance(conn, employee_id)
    if earned-spent < selected['cost']:
        raise HTTPException(409, 'insufficient_xp')
    code = 'DEMO-NOT-VALID-' + uuid.uuid4().hex[:16].upper()
    conn.execute('INSERT INTO quest_redemptions VALUES(?,?,?,?,?,?)', (employee_id, request_id, reward_id, selected['cost'], code, snapshot))
