"""OS-protected credentials and references; no plaintext fallback."""

import ctypes
import os


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_byte))]


def _crypt(data: bytes, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Use environment variables on non-Windows hosts")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    call = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    ok = call(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise RuntimeError("Windows credential protection failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def secret_path(settings, name):
    import re

    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", name):
        raise ValueError("Invalid credential name")
    folder = settings.state / "credentials"
    folder.mkdir(exist_ok=True)
    return folder / (name + ".dpapi")


def save_secret(settings, name, value):
    if not 8 <= len(value) <= 8192:
        raise ValueError("Invalid credential length")
    secret_path(settings, name).write_bytes(_crypt(value.encode()))


def read_secret(settings, name):
    if not name:
        return ""
    if os.getenv(name):
        return os.environ[name]
    path = secret_path(settings, name)
    return _crypt(path.read_bytes(), decrypt=True).decode() if path.exists() else ""


class CredentialStore:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def validate_reference(reference):
        import re

        if not re.fullmatch(r"(?:provider/)?[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+){0,3}", reference):
            raise ValueError("Invalid credential reference")

    def _path(self, reference):
        import hashlib

        self.validate_reference(reference)
        from .safety import safe_path

        path = safe_path(
            self.settings.home,
            ".router/credentials/" + hashlib.sha256(reference.encode()).hexdigest() + ".dpapi",
            internal=True,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _keyring():
        try:
            import keyring

            backend = keyring.get_keyring()
            if backend.priority < 1 or "keyrings.alt" in type(backend).__module__:
                raise RuntimeError("No supported secure OS keyring")
            return keyring
        except ImportError:
            raise RuntimeError(
                "Install accension[vault] for Keychain/Secret Service, or use an environment reference"
            ) from None

    def save(self, reference, value):
        self.validate_reference(reference)
        if not isinstance(value, str) or not 8 <= len(value) <= 8192:
            raise ValueError("Invalid credential length")
        if os.name == "nt":
            target = self._path(reference)
            encrypted = _crypt(value.encode())
            temp = target.with_suffix(".tmp")
            with temp.open("wb") as stream:
                stream.write(encrypted)
            temp.replace(target)
            target.chmod(0o600)
        else:
            try:
                self._keyring().set_password("accension", reference, value)
            except Exception:
                raise RuntimeError(
                    "Secure OS vault unavailable; unlock Keychain/Secret Service or use an environment reference"
                ) from None

    def read(self, reference):
        if not reference:
            return ""
        if reference.startswith("env:"):
            import re

            name = reference[4:]
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                raise ValueError("Invalid environment credential reference")
            return os.environ.get(name, "")
        self.validate_reference(reference)
        if os.name == "nt":
            path = self._path(reference)
            return _crypt(path.read_bytes(), decrypt=True).decode() if path.exists() else ""
        try:
            return self._keyring().get_password("accension", reference) or ""
        except Exception:
            raise RuntimeError("Secure OS vault unavailable") from None

    def delete(self, reference):
        self.validate_reference(reference)
        if os.name == "nt":
            self._path(reference).unlink(missing_ok=True)
        else:
            self._keyring().delete_password("accension", reference)
