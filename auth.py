"""Local accounts and opaque server-side sessions; no role claims from clients."""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

router = APIRouter(prefix='/api/auth')
COOKIE = 'cq_session'
SESSION_SECONDS = 8 * 60 * 60
_connect = None
_attempts = defaultdict(deque)
_lock = Lock()


def init_storage(connection_factory):
    global _connect
    _connect = connection_factory
    with _connect() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS accounts (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN (\'employee\',\'hr\')), employee_id TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL)')
        conn.execute('DELETE FROM sessions WHERE expires <= ?', (time.time(),))


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return salt.hex() + ':' + digest.hex()


def password_matches(password, encoded):
    salt, _ = encoded.split(':')
    return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), encoded)


def create_account(username, password, role, employee_id=None):
    username = username.strip().lower()
    if not username or len(username) > 100 or len(password) < 12 or len(password) > 256:
        raise ValueError('Логин обязателен; пароль должен содержать от 12 до 256 символов.')
    if role not in ('employee', 'hr') or (role == 'employee' and not employee_id):
        raise ValueError('Для сотрудника обязателен employee_id.')
    with _connect() as conn:
        conn.execute('INSERT INTO accounts(username,password_hash,role,employee_id) VALUES(?,?,?,?)',
                     (username, password_hash(password), role, employee_id if role == 'employee' else None))


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    employee_id: str | None
    csrf: str = ''


def check_origin(request):
    origin = request.headers.get('origin')
    expected = str(request.base_url).rstrip('/')
    if origin is not None and origin != expected:
        raise HTTPException(403, 'Недопустимый источник запроса')


def require_session(request: Request) -> Principal:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 256:
        raise HTTPException(401, 'Войдите в аккаунт')
    with _connect() as conn:
        row = conn.execute('SELECT a.username,a.role,a.employee_id,s.csrf FROM sessions s JOIN accounts a ON a.username=s.username WHERE s.token_hash=? AND s.expires>?',
                           (token_hash(token), time.time())).fetchone()
    if not row:
        raise HTTPException(401, 'Сессия завершена. Войдите снова.')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        check_origin(request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), row['csrf']):
            raise HTTPException(403, 'Некорректный токен запроса')
    return Principal(**dict(row))


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


def session_info(principal):
    return {'username': principal.username, 'role': principal.role, 'employee_id': principal.employee_id, 'csrf_token': principal.csrf}


@router.post('/login')
def login(body: LoginRequest, request: Request, response: Response):
    check_origin(request)
    if request.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise HTTPException(415, 'Требуется application/json')
    username = body.username.strip().lower()
    # Bound both account and source attempts. Throttle also applies to unknown logins.
    buckets = ('user:' + username, 'ip:' + (request.client.host if request.client else 'unknown'))
    now = time.time()
    with _lock:
        for key in list(_attempts):
            while _attempts[key] and _attempts[key][0] < now - 300:
                _attempts[key].popleft()
            if not _attempts[key]:
                del _attempts[key]
        if any(len(_attempts[key]) >= (10 if key.startswith('user:') else 40) for key in buckets):
            raise HTTPException(429, 'Слишком много попыток. Повторите через 5 минут.')
        for key in buckets:
            _attempts[key].append(now)
    with _connect() as conn:
        account = conn.execute('SELECT * FROM accounts WHERE username=?', (username,)).fetchone()
    # Perform the same password derivation for an unknown account.
    encoded = account['password_hash'] if account else ('00' * 16 + ':' + '00' * 32)
    matches = password_matches(body.password, encoded)
    if not account or not matches:
        raise HTTPException(401, 'Неверный логин или пароль')
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with _connect() as conn:
        old_token = request.cookies.get(COOKIE)
        if old_token:
            conn.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(old_token),))
        conn.execute('DELETE FROM sessions WHERE expires<=?', (now,))
        conn.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(token), username, csrf, now + SESSION_SECONDS))
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == 'https', samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return session_info(Principal(username, account['role'], account['employee_id'], csrf))


@router.get('/session')
def current_session(principal: Principal = Depends(require_session)):
    return session_info(principal)


@router.post('/logout')
def logout(request: Request, response: Response, principal: Principal = Depends(require_session)):
    with _connect() as conn:
        conn.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(request.cookies[COOKIE]),))
    response.delete_cookie(COOKIE, path='/', httponly=True, samesite='strict', secure=request.url.scheme == 'https')
    return {'ok': True}
