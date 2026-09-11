"""Configuration + credential resolution.

Precedence (highest wins):
  1. explicit CLI arguments
  2. real environment variables
  3. .env file in the project root
  4. encrypted credential store (.state/credentials.dat)
  5. built-in defaults

The credential store sits below .env so an explicitly written `.env` always
wins, while a user who never edits files still gets their saved login.
Credentials are never written back to disk by this module.
"""

import os
import re
import stat
import sys
from dataclasses import dataclass, field

from .errors import ConfigError
from .secrets_store import SecretStore, default_store_path

# Blackboard primary keys look like `_12529_1`. Domain knowledge shared by the
# CLI resolver and the setup wizard's validator.
COURSE_ID_RE = re.compile(r"_+[A-Za-z0-9]+_\d+")

DEFAULT_BASE_URL = "https://twc.blackboard.com"
DEFAULT_COURSE_ID = "_12529_1"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir():
    """A persistent per-user directory for credentials and session state.

    A frozen build cannot use the package location: PyInstaller unpacks to a
    temporary `_MEI…` folder that is removed on exit, so `.state/` and `.env`
    written there would vanish every run and the saved login would never
    persist. Measured in the packaged build: the state directory resolved to
    `%TEMP%\\_MEI000007883\\.state`.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return os.path.join(base, "bbpull")
    return os.path.join(os.path.expanduser("~"), ".bbpull")


def default_root():
    """Where `.env` and `.state` live for this kind of installation."""
    if getattr(sys, "frozen", False):
        return user_data_dir()
    return PROJECT_ROOT


DEFAULT_ENV_FILE = os.path.join(default_root(), ".env")
DEFAULT_STATE_DIR = os.path.join(default_root(), ".state")

# Keys whose values must never appear in a committable/template file.
SECRET_KEYS = ("BB_PASSWORD",)
# File names that are expected to be shared or committed.
TEMPLATE_NAMES = (".env.example", ".env.sample", ".env.template", "env.example")


def scan_template_for_secrets(env_file=None):
    """Warn when a committable template file contains real secret values.

    `config.build_config` only ever reads the real `.env`, so a password sitting
    in `.env.example` does nothing functionally - while being fully exposed the
    moment the folder is shared or committed. That silence is exactly the failure
    this check exists to break.
    """
    warnings = []
    env_path = env_file or DEFAULT_ENV_FILE
    directory = os.path.dirname(os.path.abspath(env_path))
    for name in TEMPLATE_NAMES:
        candidate = os.path.join(directory, name)
        if not os.path.isfile(candidate):
            continue
        try:
            values = read_env_file(candidate)
        except OSError as exc:
            warnings.append(f"could not read {candidate}: {exc}")
            continue
        for key in SECRET_KEYS:
            if values.get(key):
                warnings.append(
                    f"SECURITY: {candidate} contains a real value for {key}. "
                    f"That file is meant to be a shareable template and is "
                    f"normally committed to version control. Move the value to "
                    f"{os.path.basename(env_path)} (git-ignored) and blank the "
                    f"template."
                )
    return warnings


def normalize_base_url(url):
    """Accept the many shapes a user may paste and return scheme://host[:port]."""
    if not url:
        raise ConfigError("base URL is empty")
    url = url.strip().strip('"').strip("'")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # Strip anything from the path onward; the API client builds paths itself.
    without_scheme = url.split("://", 1)
    scheme, rest = without_scheme[0], without_scheme[1]
    host = rest.split("/", 1)[0]
    if not host:
        raise ConfigError(f"could not parse host from base URL: {url!r}")
    return f"{scheme}://{host}"


def read_env_file(path):
    """Minimal .env reader: KEY=VALUE, # comments, optional quotes."""
    data = {}
    if not path or not os.path.isfile(path):
        return data
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                data[key] = value
    return data


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    course_id: str = DEFAULT_COURSE_ID
    username: str = ""
    password: str = ""
    out_dir: str = "output"
    state_dir: str = DEFAULT_STATE_DIR
    env_file: str = DEFAULT_ENV_FILE
    cookie_file: str = ""
    timeout: float = 45.0
    retries: int = 3
    verify_ssl: bool = True
    # Defaults for a pull, set by the interactive wizard.
    include_content: bool = True
    include_announcements: bool = True
    download_files: bool = True
    overwrite: bool = False
    _source: dict = field(default_factory=dict, repr=False)

    # -- derived ---------------------------------------------------------
    @property
    def cookie_jar_path(self):
        return self.cookie_file or os.path.join(self.state_dir, "cookies.json")

    @property
    def secret_store_path(self):
        return default_store_path(self.state_dir)

    def secret_store(self):
        return SecretStore(self.secret_store_path)

    @property
    def has_credentials(self):
        return bool(self.username and self.password)

    @property
    def needs_setup(self):
        """True when the user has never told us who they are.

        Note this is about the *account*, not the password: someone who chose not
        to save their password still has a configured account, and must not be
        dragged through the whole setup wizard again.
        """
        return not self.username

    @property
    def needs_password(self):
        """True when we know the account but not the password (asked per run)."""
        return bool(self.username) and not self.password

    def describe(self):
        """Safe, redacted description for logging."""
        lines = [
            f"base_url   : {self.base_url}",
            f"course_id  : {self.course_id}",
            f"out_dir    : {os.path.abspath(self.out_dir)}",
            f"username   : {self.username or '(unset)'}",
            f"password   : {'*' * 8 if self.password else '(unset)'}",
            f"content    : {'on' if self.include_content else 'off'}",
            f"announce   : {'on' if self.include_announcements else 'off'}",
            f"files      : {'on' if self.download_files else 'off'}",
            f"overwrite  : {'on' if self.overwrite else 'off'}",
            f"verify_ssl : {self.verify_ssl}",
        ]
        for key in ("username", "password", "base_url", "course_id"):
            if key in self._source and self._source[key]:
                lines.append(f"  {key} <- {self._source[key]}")
        return "\n".join(lines)

    def warn_if_env_file_exposed(self):
        """Return a warning string when the .env file is readable by others."""
        path = self.env_file
        if not path or not os.path.isfile(path) or os.name == "nt":
            return None
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            return (
                f"WARNING: {path} is readable by other users (mode {oct(mode)}). "
                f"Run: chmod 600 {path}"
            )
        return None


def build_config(args):
    """Merge argparse namespace -> env -> .env -> defaults into a Config."""
    env_file = getattr(args, "env_file", None) or DEFAULT_ENV_FILE
    file_values = read_env_file(env_file)

    def pick(attr, *keys, default=None):
        value = getattr(args, attr, None)
        if value not in (None, "", []):
            return value, f"cli --{attr.replace('_', '-')}"
        for key in keys:
            if os.environ.get(key):
                return os.environ[key], f"env {key}"
        for key in keys:
            if file_values.get(key):
                return file_values[key], os.path.basename(env_file) + f":{key}"
        return default, None

    cfg = Config(env_file=env_file)
    source = {}

    value, src = pick("base_url", "BB_BASE_URL", "BB_URL", default=cfg.base_url)
    cfg.base_url, source["base_url"] = normalize_base_url(value), src or "default"

    value, src = pick("course_id", "BB_COURSE_ID", default=cfg.course_id)
    cfg.course_id, source["course_id"] = str(value).strip(), src or "default"

    value, src = pick("out_dir", "BB_OUT_DIR", "BB_OUTPUT_DIR", default=cfg.out_dir)
    cfg.out_dir, source["out_dir"] = str(value), src or "default"

    value, src = pick("username", "BB_USERNAME", "BB_USER")
    cfg.username, source["username"] = (value or ""), src

    value, src = pick("password", "BB_PASSWORD", "BB_PASS")
    cfg.password, source["password"] = (value or ""), src

    value, src = pick("cookie_file", "BB_COOKIE_FILE")
    cfg.cookie_file, source["cookie_file"] = (value or ""), src

    value, src = pick("state_dir", "BB_STATE_DIR", default=cfg.state_dir)
    cfg.state_dir, source["state_dir"] = str(value), src or "default"

    # Last resort before giving up: the encrypted credential store written by the
    # interactive first-run flow. Keeps a user who never touches a config file
    # logged in, while .env (above) still takes priority when present.
    if not cfg.has_credentials:
        stored = cfg.secret_store().load()
        if stored:
            if not cfg.username and stored.get("BB_USERNAME"):
                cfg.username, source["username"] = stored["BB_USERNAME"], "credential store"
            if not cfg.password and stored.get("BB_PASSWORD"):
                cfg.password, source["password"] = stored["BB_PASSWORD"], "credential store"

    value, _ = pick("timeout", "BB_TIMEOUT")
    if value:
        cfg.timeout = float(value)

    value, _ = pick("retries", "BB_RETRIES")
    if value:
        cfg.retries = max(0, min(10, int(value)))

    verify, _ = pick("verify_ssl", "BB_VERIFY_SSL")
    if verify not in (None, ""):
        cfg.verify_ssl = str(verify).strip().lower() not in ("0", "false", "no", "off")

    # Wizard-persisted pull defaults. These are read back by pull_one, so a
    # saved preference always has an effect.
    for key, attr in (
        ("BB_INCLUDE_CONTENT", "include_content"),
        ("BB_INCLUDE_ANNOUNCEMENTS", "include_announcements"),
        ("BB_DOWNLOAD_FILES", "download_files"),
        ("BB_OVERWRITE", "overwrite"),
    ):
        raw = os.environ.get(key)
        if raw in (None, ""):
            raw = file_values.get(key)
        if raw not in (None, ""):
            setattr(cfg, attr, _as_bool(raw))

    cfg._source = source
    return cfg


def _as_bool(value, default=False):
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on", "是"):
        return True
    if text in ("0", "false", "no", "n", "off", "否"):
        return False
    return default


class _Defaults:
    """Attribute bag so `build_config` can be called outside argparse.

    Every missing attribute reads as None, which is exactly what build_config's
    `getattr(args, name, None)` fallbacks expect.
    """

    def __init__(self, **overrides):
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return None


def load_config(env_file=None, **overrides):
    """Build a Config outside argparse (used after the interactive wizard)."""
    return build_config(_Defaults(env_file=env_file, **overrides))
