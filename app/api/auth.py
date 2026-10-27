from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from cryptography.fernet import Fernet


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: int
    telegram_id: int
    token_hash: str
    csrf: str
    method: str
    fresh_at: float


class AuthStore:
    """Host-only credentials and sessions. Never writes the content database.

    Only digests of long-lived opaque credentials persist. The short Telegram
    retry response is encrypted and deleted when its exchange window expires.
    """

    def __init__(self, directory: Path, *, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "auth.sqlite"
        self.clock = clock
        key_path = self.directory / "auth-exchange.key"
        try:
            with key_path.open("xb") as stream:
                stream.write(Fernet.generate_key())
            key_path.chmod(0o600)
        except FileExistsError:
            pass
        self.cipher = Fernet(key_path.read_bytes())
        self.hasher = PasswordHasher()
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS credentials (
                user_id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL,
                password_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
              CREATE TABLE IF NOT EXISTS local_codes (
                digest TEXT PRIMARY KEY, user_id INTEGER, telegram_id INTEGER,
                expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS sessions (
                digest TEXT PRIMARY KEY, user_id INTEGER, telegram_id INTEGER,
                csrf TEXT NOT NULL, method TEXT NOT NULL, created REAL,
                last_seen REAL, fresh_at REAL, expires REAL, idle_seconds REAL);
              CREATE TABLE IF NOT EXISTS exchanges (
                digest TEXT PRIMARY KEY, nonce TEXT NOT NULL, response BLOB NOT NULL,
                session_digest TEXT NOT NULL, expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS login_limits (
                key TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS operation_grants (
                session_digest TEXT NOT NULL, operation_id TEXT NOT NULL,
                actions TEXT NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(session_digest,operation_id));
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def provision(self, user_id: int, telegram_id: int, password: str):
        if len(password) < 12 or len(password) > 1024:
            raise AuthError("password_policy", "Пароль должен содержать от 12 до 1024 символов", 422)
        encoded = self.hasher.hash(password)
        with self.connect() as db:
            db.execute("""INSERT INTO credentials(user_id,telegram_id,password_hash)
              VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
              password_hash=excluded.password_hash, revision=credentials.revision+1""",
                       (user_id, telegram_id, encoded))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM exchanges")

    def local_code(self, user_id: int, telegram_id: int) -> str:
        code = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM local_codes WHERE expires<? OR used=1", (self.clock(),))
            db.execute("INSERT INTO local_codes VALUES(?,?,?,?,0)",
                       (digest(code), user_id, telegram_id, self.clock() + 60))
        return code

    def _session(self, db, user_id: int, telegram_id: int, method: str) -> dict:
        token, csrf, now = secrets.token_urlsafe(48), secrets.token_urlsafe(24), self.clock()
        mini = method == "telegram"
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (digest(token), user_id, telegram_id, csrf, method, now, now, now,
                    now + (43200 if mini else 604800), 3600 if mini else 43200))
        return {"token": token, "csrf_token": csrf,
                "user": {"id": user_id, "telegram_id": telegram_id, "name": str(telegram_id)},
                "method": method, "timezone": "Asia/Vladivostok"}

    def exchange_local(self, code: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM local_codes WHERE digest=?", (digest(code),)).fetchone()
            if not row or row["used"] or row["expires"] < self.clock():
                raise AuthError("invalid_local_code", "Код входа истёк или уже использован. Откройте панель через launcher.")
            db.execute("UPDATE local_codes SET used=1 WHERE digest=?", (digest(code),))
            return self._session(db, row["user_id"], row["telegram_id"], "local")

    def _rate_limit(self, key: str):
        now = self.clock()
        with self.connect() as db:
            db.execute("DELETE FROM login_limits WHERE until<?", (now,))
            row = db.execute("SELECT * FROM login_limits WHERE key=?", (key,)).fetchone()
            if row and row["count"] >= 8:
                raise AuthError("rate_limited", "Слишком много попыток. Повторите через 15 минут.", 429)
            db.execute("INSERT INTO login_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1",
                       (key, now + 900))

    def password_login(self, telegram_id: int, password: str, remote: str) -> dict:
        self._rate_limit("ip:" + remote)
        self._rate_limit("user:" + str(telegram_id))
        with self.connect() as db:
            row = db.execute("SELECT * FROM credentials WHERE telegram_id=?", (telegram_id,)).fetchone()
        # A fixed, invalid password is still hashed for unknown users to avoid a cheap enumeration path.
        encoded = row["password_hash"] if row else self.hasher.hash(secrets.token_urlsafe(20))
        try:
            correct = self.hasher.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            correct = False
        if not row or not correct:
            raise AuthError("invalid_credentials", "Неверный Telegram ID или пароль")
        with self.connect() as db:
            db.execute("DELETE FROM login_limits WHERE key=?", ("user:" + str(telegram_id),))
            return self._session(db, row["user_id"], telegram_id, "password")

    def telegram_login(self, init_data: str, nonce: str, bot_token: str, allowed_ids: set[int], user_id: int) -> dict:
        if len(init_data) > 16384 or len(nonce) < 16 or len(nonce) > 128:
            raise AuthError("invalid_init_data", "Некорректные данные Telegram")
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise AuthError("invalid_init_data", "Повторяющиеся поля Telegram")
        supplied = fields.pop("hash", "")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(expected, supplied):
            raise AuthError("invalid_init_data", "Не удалось подтвердить подпись Telegram")
        try:
            age = self.clock() - int(fields["auth_date"])
            telegram_id = int(json.loads(fields["user"])["id"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            raise AuthError("invalid_init_data", "Нет подтверждённого пользователя Telegram") from None
        if age > 300 or age < -30 or telegram_id not in allowed_ids:
            raise AuthError("invalid_init_data", "Вход недоступен. Переоткройте приложение в Telegram.")
        key = digest(init_data)
        with self.connect() as db:
            row = db.execute("SELECT * FROM exchanges WHERE digest=?", (key,)).fetchone()
            if row:
                if not hmac.compare_digest(row["nonce"], nonce) or row["expires"] < self.clock():
                    raise AuthError("telegram_replay", "Повторный вход отклонён. Переоткройте Mini App.")
                if not db.execute("SELECT 1 FROM sessions WHERE digest=?", (row["session_digest"],)).fetchone():
                    raise AuthError("session_revoked", "Сессия завершена. Переоткройте Mini App.")
                return json.loads(self.cipher.decrypt(row["response"]))
            result = self._session(db, user_id, telegram_id, "telegram")
            db.execute("INSERT INTO exchanges VALUES(?,?,?,?,?)", (key, nonce,
                       self.cipher.encrypt(json.dumps(result).encode()), digest(result["token"]), self.clock() + 60))
            # Retain replay digests through the validity window, erase response material after 60s.
            db.execute("UPDATE exchanges SET response=X'' WHERE expires<?", (self.clock(),))
            db.execute("DELETE FROM exchanges WHERE expires<?", (self.clock() - 360,))
            return result

    def authenticate(self, token: str) -> Principal:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE digest=?", (digest(token),)).fetchone()
            now = self.clock()
            if not row or row["expires"] < now or row["last_seen"] + row["idle_seconds"] < now:
                raise AuthError("session_expired", "Войдите в панель")
            db.execute("UPDATE sessions SET last_seen=? WHERE digest=?", (now, row["digest"]))
            return Principal(row["user_id"], row["telegram_id"], row["digest"], row["csrf"], row["method"], row["fresh_at"])

    def logout(self, principal: Principal):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (principal.token_hash,))
            db.execute("DELETE FROM exchanges WHERE session_digest=?", (principal.token_hash,))

    def reauth(self, principal: Principal, password: str):
        with self.connect() as db:
            row = db.execute("SELECT password_hash FROM credentials WHERE user_id=?", (principal.user_id,)).fetchone()
            try:
                valid = row is not None and self.hasher.verify(row[0], password)
            except VerificationError:
                valid = False
            if not valid:
                raise AuthError("invalid_credentials", "Неверный пароль")
            db.execute("UPDATE sessions SET fresh_at=? WHERE digest=?", (self.clock(), principal.token_hash))

    def require_fresh(self, principal: Principal):
        if self.clock() - principal.fresh_at > 600:
            raise AuthError("reauth_required", "Подтвердите личность для изменения размещения или секретов", 403)

    def grant_operation(self,principal: Principal,operation_id: str,actions: list[str]):
        """Bind continuation of a freshly approved operation to this exact session."""
        self.require_fresh(principal)
        with self.connect() as db:
            session=db.execute('SELECT expires FROM sessions WHERE digest=?',(principal.token_hash,)).fetchone()
            if not session:raise AuthError('session_revoked','Войдите в панель')
            db.execute('INSERT OR REPLACE INTO operation_grants VALUES(?,?,?,?)',
                       (principal.token_hash,operation_id,json.dumps(actions),session['expires']))

    def require_operation_action(self,principal: Principal,operation_id: str,action: str):
        if self.clock()-principal.fresh_at<=600:
            return self.require_fresh(principal)
        with self.connect() as db:
            grant=db.execute('SELECT * FROM operation_grants WHERE session_digest=? AND operation_id=?',
                             (principal.token_hash,operation_id)).fetchone()
            session=db.execute('SELECT expires,last_seen,idle_seconds FROM sessions WHERE digest=?',
                               (principal.token_hash,)).fetchone()
            now=self.clock()
            if (grant and session and grant['expires']>=now and session['expires']>=now
                    and session['last_seen']+session['idle_seconds']>=now and action in json.loads(grant['actions'])):
                return
        self.require_fresh(principal)

    def import_trusted_credentials(self,records):
        """Called only after authenticated envelope verification, never by public HTTP."""
        validate_credential_records(records)
        with self.connect() as db:
            db.execute('DELETE FROM credentials')
            for record in records:
                db.execute('INSERT INTO credentials(user_id,telegram_id,password_hash,revision) VALUES(?,?,?,?)',
                    (record['user_id'],record['telegram_id'],record['password_hash'],record['revision']))
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM exchanges')
            db.execute('DELETE FROM local_codes')


def validate_credential_records(records):
    from argon2 import extract_parameters,Type
    if not isinstance(records,list) or len(records)>100:
        raise ValueError('Invalid administrator credential envelope')
    ids=set();telegram_ids=set()
    for record in records:
        if not isinstance(record,dict) or set(record)!={'user_id','telegram_id','password_hash','revision'}:
            raise ValueError('Invalid administrator credential record')
        if any(type(record[key]) is not int or record[key]<1 for key in ('user_id','telegram_id','revision')):
            raise ValueError('Invalid administrator identity')
        if record['user_id'] in ids or record['telegram_id'] in telegram_ids:
            raise ValueError('Duplicate administrator identity')
        ids.add(record['user_id']);telegram_ids.add(record['telegram_id'])
        encoded=record['password_hash']
        if not isinstance(encoded,str) or len(encoded)>1024 or extract_parameters(encoded).type!=Type.ID:
            raise ValueError('Administrator credential must use Argon2id')


def export_credential_records(directory):
    path=Path(directory)/'auth.sqlite'
    if not path.is_file():return []
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        records=[dict(row) for row in db.execute('SELECT user_id,telegram_id,password_hash,revision FROM credentials ORDER BY user_id')]
    validate_credential_records(records)
    return records
