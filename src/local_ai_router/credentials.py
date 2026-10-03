"""Windows DPAPI credentials: encrypted for the current OS user, never Git."""
import ctypes, os
from pathlib import Path

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
