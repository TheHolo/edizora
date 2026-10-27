"""Private machine secrets and authenticated encrypted handoff/recovery envelopes."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.host.control import durable_json
from app.runtime import InstanceLock


ALLOWED_SECRETS = frozenset({"BOT_TOKEN", "PIXIV_COOKIES", "NEWS_WORKER_TOKEN", "HOME_WORKER_LMSTUDIO_API_KEY",
                            "HOME_WORKER_TOKEN", "HTTP_PROXY", "SOCKS_PROXY", "DESKTOP_AI_CONNECTIONS"})
TRANSFERABLE_SECRETS = frozenset({"BOT_TOKEN", "PIXIV_COOKIES", "NEWS_WORKER_TOKEN"})


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def unb64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _dpapi(value: bytes, *, decrypt: bool = False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    incoming_buffer = ctypes.create_string_buffer(value)
    incoming = Blob(len(value), ctypes.cast(incoming_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_bool
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise RuntimeError("Защищённое хранилище Windows недоступно этому пользователю")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel32.LocalFree(outgoing.data)


class SecretStore:
    def __init__(self, control_dir: str | Path):
        self.root = Path(control_dir).resolve() / "private"
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        self.key_path = self.root / "machine.key"
        self.path = self.root / "secrets.json"
        with self._lock():
            if not self.key_path.exists():
                raw = secrets.token_bytes(32)
                self.key_path.write_bytes(_dpapi(raw) if os.name == "nt" else raw)
                if os.name != "nt":
                    self.key_path.chmod(0o600)
            encoded = self.key_path.read_bytes()
            self.key = _dpapi(encoded, decrypt=True) if os.name == "nt" else encoded
            if len(self.key) != 32:
                raise RuntimeError("Неверный ключ защищённого хранилища")

    @contextmanager
    def _lock(self):
        lock = InstanceLock(self.root / "secrets.lock")
        deadline = time.monotonic() + 10
        while True:
            try:
                lock.__enter__()
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Защищённое хранилище занято") from None
                time.sleep(.02)
        try:
            yield
        finally:
            lock.__exit__()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            raw = AESGCM(self.key).decrypt(unb64(envelope["nonce"]), unb64(envelope["ciphertext"]), b"publisher-machine-secrets-v1")
            return json.loads(raw)
        except Exception:
            raise RuntimeError("Не удалось открыть защищённое хранилище; проверьте ключ установки") from None

    def _save(self, value: dict) -> None:
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self.key).encrypt(nonce, json.dumps(value, ensure_ascii=False).encode(), b"publisher-machine-secrets-v1")
        durable_json(self.path, {"format": "publisher-machine-secrets-v1", "nonce": b64(nonce), "ciphertext": b64(encrypted)})
        if os.name != "nt":
            self.path.chmod(0o600)

    def read(self, name: str, default=None):
        if name not in ALLOWED_SECRETS:
            raise ValueError("Неизвестное имя секрета")
        with self._lock():
            return self._load().get(name, default)

    def write(self, name: str, value: str | None) -> None:
        if name not in ALLOWED_SECRETS or (value is not None and (not isinstance(value, str) or len(value) > 65536)):
            raise ValueError("Недопустимый секрет")
        with self._lock():
            contents = self._load()
            if value:
                contents[name] = value
            else:
                contents.pop(name, None)
            self._save(contents)

    def status(self) -> dict[str, bool]:
        with self._lock():
            contents = self._load()
            return {key: bool(contents.get(key)) for key in sorted(ALLOWED_SECRETS)}

    def bootstrap_environment(self,values: dict) -> None:
        """One trusted import; clearing a managed secret must survive a restart."""
        with self._lock():
            contents=self._load()
            if contents.get('_environment_imported'):return
            for name in ALLOWED_SECRETS:
                if name not in contents and isinstance(values.get(name),str) and values[name]:
                    contents[name]=values[name]
            contents['_environment_imported']=True
            self._save(contents)

    def manages_environment(self) -> bool:
        with self._lock():return bool(self._load().get('_environment_imported'))

    def _private_key(self):
        with self._lock():
            contents = self._load()
            if "_node_private_key" not in contents:
                key = X25519PrivateKey.generate()
                contents["_node_private_key"] = b64(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
                self._save(contents)
            return X25519PrivateKey.from_private_bytes(unb64(contents["_node_private_key"]))

    def public_key(self) -> str:
        return b64(self._private_key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def _portable(self) -> dict:
        with self._lock():
            contents = self._load()
            values={key: contents[key] for key in sorted(TRANSFERABLE_SECRETS) if key in contents}
        from app.api.auth import export_credential_records
        records=export_credential_records(self.root.parent)
        if records:values['_admin_credentials']=records
        return values

    def has_portable_secrets(self) -> bool:
        return bool(self._portable())

    def set_backup_passphrase(self, value: str | None) -> None:
        if value is not None and (not isinstance(value, str) or len(value) < 12):
            raise ValueError("Пароль восстановления должен содержать не менее 12 символов")
        with self._lock():
            contents = self._load()
            if value is None:
                contents.pop("_backup_recovery_passphrase", None)
            else:
                contents["_backup_recovery_passphrase"] = value
            self._save(contents)

    def backup_passphrase(self) -> str | None:
        with self._lock():
            return self._load().get("_backup_recovery_passphrase")

    @staticmethod
    def _seal(key: bytes, values: dict, context: dict, **metadata):
        nonce = secrets.token_bytes(12)
        aad = json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        ciphertext = AESGCM(key).encrypt(nonce, json.dumps(values, ensure_ascii=False).encode(), aad)
        return {"format": "publisher-secret-envelope-v1", "context": context, "nonce": b64(nonce),
                "ciphertext": b64(ciphertext), **metadata}

    def export_for_target(self, target_public_key: str, *, context: dict) -> dict:
        ephemeral = X25519PrivateKey.generate()
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(unb64(target_public_key)))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"publisher-target-secret-v1").derive(shared)
        return self._seal(key, self._portable(), context, method="x25519-aesgcm",
                          ephemeral_key=b64(ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)))

    def import_for_target(self, envelope: dict, *, context: dict) -> None:
        if envelope.get("method") != "x25519-aesgcm" or envelope.get("context") != context:
            raise ValueError("Пакет секретов предназначен другой операции или узлу")
        shared = self._private_key().exchange(X25519PublicKey.from_public_bytes(unb64(envelope["ephemeral_key"])))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"publisher-target-secret-v1").derive(shared)
        self._import(envelope, key, context)

    def export_backup(self, recovery_passphrase: str, *, context: dict) -> dict:
        if not isinstance(recovery_passphrase, str) or len(recovery_passphrase) < 12:
            raise ValueError("Пароль восстановления должен содержать не менее 12 символов")
        salt = secrets.token_bytes(16)
        key = hashlib.scrypt(recovery_passphrase.encode(), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=64*1024*1024)
        return self._seal(key, self._portable(), context, method="scrypt-aesgcm", salt=b64(salt))

    def import_backup(self, envelope: dict, recovery_passphrase: str, *, context: dict) -> None:
        if envelope.get("method") != "scrypt-aesgcm" or envelope.get("context") != context:
            raise ValueError("Неверный формат или назначение пакета восстановления")
        key = hashlib.scrypt(recovery_passphrase.encode(), salt=unb64(envelope["salt"]), n=32768, r=8, p=1, dklen=32, maxmem=64*1024*1024)
        self._import(envelope, key, context)

    def validate_backup(self, envelope: dict, recovery_passphrase: str, *, context: dict) -> None:
        if envelope.get("method") != "scrypt-aesgcm" or envelope.get("context") != context:
            raise ValueError("Неверный формат пакета восстановления")
        key = hashlib.scrypt(recovery_passphrase.encode(), salt=unb64(envelope["salt"]), n=32768, r=8, p=1, dklen=32, maxmem=64*1024*1024)
        self._decrypt(envelope, key, context)

    def _import(self, envelope, key, context):
        values = self._decrypt(envelope, key, context)
        credentials=values.pop('_admin_credentials',[])
        with self._lock():
            current = self._load()
            for name in TRANSFERABLE_SECRETS:
                current.pop(name, None)
            current.update(values)
            self._save(current)
        from app.api.auth import AuthStore
        AuthStore(self.root.parent).import_trusted_credentials(credentials)

    def _decrypt(self, envelope, key, context):
        try:
            aad = json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
            values = json.loads(AESGCM(key).decrypt(unb64(envelope["nonce"]), unb64(envelope["ciphertext"]), aad))
        except Exception:
            raise ValueError("Не удалось проверить пакет секретов: неверный ключ или повреждённые данные") from None
        if not isinstance(values, dict) or set(values) - (TRANSFERABLE_SECRETS|{'_admin_credentials'}):
            raise ValueError("Пакет содержит недопустимые настройки")
        from app.api.auth import validate_credential_records
        validate_credential_records(values.get('_admin_credentials',[]))
        return values
