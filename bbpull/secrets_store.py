"""Encrypted-at-rest credential storage.

The password should not have to live in a plain-text `.env` the user must edit
by hand. On Windows we use DPAPI (`CryptProtectData`), which ties the ciphertext
to the current Windows user account: another user or another machine cannot
decrypt it, and no master password is needed.

On other platforms we fall back to the `keyring` package when it is installed.
If neither is available we do NOT pretend to be secure: the file is written with
owner-only permissions and the backend is reported as `plain` so the caller can
warn honestly.
"""

import base64
import ctypes
import json
import os
import sys
import tempfile
from ctypes import wintypes

BACKEND_DPAPI = "dpapi"
BACKEND_KEYRING = "keyring"
BACKEND_PLAIN = "plain"

KEYRING_SERVICE = "bbpull"


# --------------------------------------------------------------------- DPAPI
class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_available():
    return os.name == "nt"


def _dpapi_call(func_name, data):
    """Run CryptProtectData/CryptUnprotectData over `data`. Returns bytes."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    func = getattr(crypt32, func_name)
    func.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    func.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = func(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{func_name} failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def dpapi_protect(text):
    return _dpapi_call("CryptProtectData", text.encode("utf-8"))


def dpapi_unprotect(blob):
    return _dpapi_call("CryptUnprotectData", blob).decode("utf-8")


# ------------------------------------------------------------------ backend
def available_backend():
    """Best available storage backend for this machine."""
    if _dpapi_available():
        return BACKEND_DPAPI
    try:
        import keyring  # noqa: F401
    except ImportError:
        return BACKEND_PLAIN
    return BACKEND_KEYRING


def backend_description(backend=None):
    backend = backend or available_backend()
    return {
        BACKEND_DPAPI: "Windows DPAPI 加密（只有你這台電腦的這個 Windows 帳號能解密）",
        BACKEND_KEYRING: "系統鑰匙圈（keyring）",
        BACKEND_PLAIN: "僅檔案權限保護（未加密）",
    }.get(backend, backend)


def default_store_path(state_dir):
    return os.path.join(state_dir, "credentials.dat")


class SecretStore:
    """Stores a small dict of credentials at `path`."""

    def __init__(self, path, backend=None):
        self.path = path
        self.backend = backend or available_backend()

    # -- low level -------------------------------------------------------
    def _encrypt(self, payload):
        text = json.dumps(payload, ensure_ascii=False)
        if self.backend == BACKEND_DPAPI:
            return base64.b64encode(dpapi_protect(text)).decode("ascii"), BACKEND_DPAPI
        if self.backend == BACKEND_KEYRING:
            import keyring

            keyring.set_password(KEYRING_SERVICE, "credentials", text)
            return None, BACKEND_KEYRING
        return base64.b64encode(text.encode("utf-8")).decode("ascii"), BACKEND_PLAIN

    def _decrypt(self, data, backend):
        if backend == BACKEND_DPAPI:
            return json.loads(dpapi_unprotect(base64.b64decode(data)))
        if backend == BACKEND_KEYRING:
            import keyring

            text = keyring.get_password(KEYRING_SERVICE, "credentials")
            return json.loads(text) if text else {}
        return json.loads(base64.b64decode(data).decode("utf-8"))

    # -- public ----------------------------------------------------------
    def save(self, values):
        clean = {
            str(k): str(v)
            for k, v in (values or {}).items()
            if v not in (None, "")
        }
        data, backend = self._encrypt(clean)
        record = {"version": 1, "backend": backend}
        if data is not None:
            record["data"] = data
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cred.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self.backend = backend
        return backend

    def load(self):
        """Return the stored dict, or {} when absent/unreadable.

        A DPAPI blob written by a different Windows user is expected to fail
        here; that is a normal condition (copied profile, restored backup), so it
        degrades to "no saved credentials" instead of crashing the program.
        """
        if not self.path or not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            return {}
        backend = record.get("backend") or BACKEND_PLAIN
        try:
            if backend == BACKEND_KEYRING:
                return self._decrypt(None, backend)
            data = record.get("data")
            if not data:
                return {}
            return self._decrypt(data, backend)
        except Exception:
            return {}

    def clear(self):
        try:
            if os.path.isfile(self.path):
                os.remove(self.path)
                return True
        except OSError:
            pass
        return False

    def exists(self):
        return bool(self.path) and os.path.isfile(self.path)
