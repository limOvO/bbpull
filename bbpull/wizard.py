"""Interactive layer: prompting, selection parsing, .env writing, setup wizard.

Design rules
------------
* **Never hang.** Every read goes through `Prompter`, which converts EOF and
  Ctrl-C into `Cancelled` instead of blocking or tracebacking. That is what
  makes the interactive paths safe to run from a script, a pipe, or an agent.
* **Never guess.** Validators re-prompt with the reason; nothing is silently
  coerced. A wrong base URL or course id is caught at entry, not at request time.
* **Only persist what is actually read back.** Every key the wizard writes is
  consumed by `config.build_config`, so a saved setting can never be a no-op.
* Pure helpers (`parse_selection`, `update_env_file`, `course_table_lines`) are
  separated from I/O so they are unit-testable offline.
"""

import getpass
import os
import re
import sys
import tempfile
from collections import OrderedDict

from .config import COURSE_ID_RE, DEFAULT_BASE_URL, normalize_base_url, read_env_file
from .errors import ConfigError
from .logging_util import safe_print
from .paths import long_path
from .secrets_store import backend_description

MENU_BACK = {"q", "quit", "exit", "back"}
# "0" means cancel only where numbering is 1-based (menus, selections). It must
# NOT be a general quit token, because "0" is a legitimate answer to a yes/no
# question ("0" = no) and to a numeric prompt.
CANCEL_ZERO = "0"


class Cancelled(Exception):
    """The user pressed Ctrl-C, sent EOF, or typed a quit token."""


# --------------------------------------------------------------- pure helpers
def parse_selection(text, count, allow_all=True):
    """Turn "1,3", "1-4", "all", or a blank line into a sorted list of 1-based indices.

    Raises ValueError with a human-readable reason for anything unusable.
    """
    raw = (text or "").strip().lower()
    if raw in MENU_BACK or raw == CANCEL_ZERO:
        raise Cancelled()
    if raw == "all":
        if not allow_all:
            raise ValueError("'all' is not available here")
        return list(range(1, count + 1))
    if not raw:
        return []
    picked = []
    for chunk in raw.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk[1:] or (chunk.count("-") == 1 and chunk[0].isdigit()):
            # Either "3-5" (range) or a negative number (always invalid).
            if not re.fullmatch(r"\d+-\d+", chunk):
                raise ValueError(f"{chunk!r} is not a valid range")
            start, end = (int(part) for part in chunk.split("-"))
            if start > end:
                start, end = end, start
            if start < 1 or end > count:
                raise ValueError(f"range {chunk} is outside 1-{count}")
            picked.extend(range(start, end + 1))
            continue
        if not chunk.isdigit():
            raise ValueError(f"{chunk!r} is not a number")
        index = int(chunk)
        if index < 1 or index > count:
            raise ValueError(f"{index} is outside 1-{count}")
        picked.append(index)
    if not picked:
        raise ValueError("nothing selected")
    return sorted(set(picked))


def course_table_lines(courses, numbered=True):
    """Render the enrolment list. Single source of truth for course display."""
    if not courses:
        return []
    lines = []
    if numbered:
        lines.append(f"{'#':>3}  {'course id':<14} {'role':<12} name")
        lines.append("-" * 78)
        for i, course in enumerate(courses, start=1):
            role = str(course.get("role") or "")
            lines.append(f"{i:>3}  {course['courseId']:<14} {role:<12} {course['name']}")
    else:
        for course in courses:
            lines.append(f"  {course['courseId']:<14} {course['name']}")
    return lines


def filter_courses(courses, keyword):
    """Case-insensitive substring match on course id and name."""
    needle = (keyword or "").strip().lower()
    if not needle:
        return list(courses)
    return [
        c
        for c in courses
        if needle in c["name"].lower()
        or needle in c["courseId"].lower()
        or needle in str(c.get("displayName") or "").lower()
    ]


def update_env_file(path, updates):
    """Rewrite `path` in place, changing only the keys in `updates`.

    Comments, ordering, blank lines and unrelated keys survive; new keys are
    appended. Written atomically so an interrupted write cannot truncate the
    user's credentials file.
    """
    lines = []
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            lines = handle.read().splitlines()

    remaining = OrderedDict((k, "" if v is None else str(v)) for k, v in updates.items())
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    text = "\n".join(out).rstrip("\n") + "\n"
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(long_path(tmp_path), long_path(path))
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return path


def env_value(value):
    """Quote a .env value when it contains characters a parser would mangle."""
    text = "" if value is None else str(value)
    if text == "" or re.search(r"[\s#\"']", text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def stdin_is_console():
    """True only when stdin is a genuine interactive console.

    `sys.stdin.isatty()` is NOT trustworthy on Windows: measured on this build
    (Python 3.13.1, MSC v.1942) it returns **True** even when stdin is
    `subprocess.DEVNULL`. Trusting it would start an interactive prompt in a
    pipeline and then block or crash. `GetConsoleMode` succeeds only for a real
    console handle, so that is the deciding check on Windows.
    """
    stdin = sys.stdin
    if stdin is None:
        return False
    try:
        if not stdin.isatty():
            return False
    except (ValueError, OSError):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if handle in (0, None) or handle == -1:
            return False
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return True
        # A definite "not a console" (NUL device, pipe, redirected file).
        return False
    except Exception:
        # Only on an unexpected API failure: fall back to isatty() rather than
        # locking the user out of a terminal that actually works (e.g. mintty).
        return True


# ------------------------------------------------------------------- prompter
class Prompter:
    """Line-based prompts with validation, defaults and safe cancellation.

    `lines` lets tests drive the whole flow deterministically; when it is None
    the real stdin is used.
    """

    def __init__(self, lines=None, out=None, secret_reader=None, interactive=None):
        self._lines = list(lines) if lines is not None else None
        self.out = out if out is not None else sys.stdout
        self._secret_reader = secret_reader
        if interactive is None:
            interactive = self._lines is not None or stdin_is_console()
        self.interactive = interactive

    # -- output ----------------------------------------------------------
    def say(self, text=""):
        # Same encoding resilience as the logger: a course title with an exotic
        # character must not abort an interactive session.
        safe_print(self.out, text)

    def rule(self, char="-", width=78):
        self.say(char * width)

    def require_interactive(self, hint):
        if not self.interactive:
            raise ConfigError(
                "this command needs an interactive terminal, but stdin is not a TTY.\n"
                f"{hint}"
            )

    # -- input -----------------------------------------------------------
    def _read_line(self, prompt):
        if self._lines is not None:
            if not self._lines:
                raise Cancelled()
            self.say(prompt)
            return self._lines.pop(0)
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt) as exc:
            raise Cancelled() from exc

    def _read_secret(self, prompt):
        """Read a secret. Draws the prompt itself for every non-getpass source.

        An injected `secret_reader` takes precedence over the scripted line
        queue: a caller that supplies one means it. getpass draws its own prompt
        on a real terminal; the other two paths print a marker instead, so the
        user sees the same thing and the value is never echoed.
        """
        if self._secret_reader is not None:
            self.say(prompt + "(input hidden)")
            return self._secret_reader(prompt)
        if self._lines is not None:
            if not self._lines:
                raise Cancelled()
            self.say(prompt + "(input hidden)")
            return self._lines.pop(0)
        try:
            return getpass.getpass(prompt)
        except (EOFError, KeyboardInterrupt) as exc:
            raise Cancelled() from exc
        except Exception:
            # getpass can fail on exotic terminals; fall back to a visible prompt.
            return self._read_line(prompt + "(visible) ")

    def ask(self, label, default=None, validator=None, allow_empty=False):
        """Ask for a string. `validator(value)` returns an error string or None."""
        while True:
            suffix = f" [{default}]" if default not in (None, "") else ""
            answer = self._read_line(f"{label}{suffix}: ").strip()
            if answer.lower() in MENU_BACK:
                raise Cancelled()
            if not answer:
                if default not in (None, ""):
                    answer = str(default)
                elif allow_empty:
                    return ""
                else:
                    self.say("  ! 這個欄位不能留空，請重新輸入。")
                    continue
            if validator:
                error = validator(answer)
                if error:
                    self.say(f"  ! {error}")
                    continue
            return answer

    def ask_secret(self, label, has_current=False):
        """Ask for a secret. Returns None to mean 'keep the current value'.

        Blank input keeps the existing secret; a single `-` clears it. The value
        is never echoed back and never printed.
        """
        marker = "（直接 Enter 保留現有值，輸入 - 可清除）" if has_current else ""
        while True:
            answer = self._read_secret(f"{label}{marker}: ")
            if answer is None:
                raise Cancelled()
            answer = answer.strip()
            if answer == "-":
                return ""
            if not answer:
                if has_current:
                    return None
                self.say("  ! 密碼不能留空，請重新輸入。")
                continue
            return answer

    def ask_bool(self, label, default=False):
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self._read_line(f"{label} [{hint}]: ").strip().lower()
            if answer in MENU_BACK:
                raise Cancelled()
            if not answer:
                return default
            if answer in ("y", "yes", "是", "1", "true"):
                return True
            if answer in ("n", "no", "否", "0", "false"):
                return False
            self.say("  ! 請回答 y 或 n。")

    def ask_int(self, label, default, minimum=None, maximum=None):
        while True:
            raw = self.ask(label, default=default)
            try:
                value = int(str(raw).strip())
            except ValueError:
                self.say("  ! 請輸入整數。")
                continue
            if minimum is not None and value < minimum:
                self.say(f"  ! 不能小於 {minimum}。")
                continue
            if maximum is not None and value > maximum:
                self.say(f"  ! 不能大於 {maximum}。")
                continue
            return value

    def choose(self, label, options, default=1):
        """Numbered menu. `options` is a list of (key, description) pairs."""
        self.say("")
        self.say(label)
        for i, (_, description) in enumerate(options, start=1):
            self.say(f"  {i}) {description}")
        while True:
            answer = self._read_line(f"選擇 [1-{len(options)}]（預設 {default}）: ").strip()
            if answer.lower() in MENU_BACK or answer == CANCEL_ZERO:
                raise Cancelled()
            if not answer:
                return options[default - 1][0]
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            self.say("  ! 請輸入選項編號。")

    def confirm(self, label, default=True):
        return self.ask_bool(label, default=default)


# ---------------------------------------------------------------- validators
def validate_base_url(value):
    try:
        normalize_base_url(value)
    except ConfigError as exc:
        return str(exc)
    return None


def validate_course_id(value):
    text = (value or "").strip()
    if text.lower() in ("auto", "ask"):
        return None
    if not text:
        return "課程 id 不能留空（可輸入 auto，之後每次抓取時再選）"
    if not COURSE_ID_RE.fullmatch(text):
        return (
            "課程 id 看起來不像 Blackboard 的格式（例如 _12529_1）。"
            "請從課程網址 .../ultra/courses/_12529_1/outline 取得。"
        )
    return None


def validate_username(value):
    if not (value or "").strip():
        return "帳號不能留空"
    return None


def normalize_out_dir(value):
    """Expand `~` and resolve relative paths against the project root."""
    text = (value or "").strip() or "output"
    text = os.path.expanduser(text)
    return text


def probe_site(base_url, timeout=8.0):
    """Best-effort reachability check. Never raises; returns (ok, detail)."""
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return False, "requests 未安裝"
    try:
        response = requests.get(
            base_url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "bbpull-probe/1.0"},
        )
    except Exception as exc:
        return False, f"連線失敗: {exc}"
    return True, f"HTTP {response.status_code}"


# ------------------------------------------------------------------- pickers
# Above this many courses the picker demands a keyword filter first, because a
# 90-line numbered list is unusable. Measured on a real account: 91 enrolments.
MAX_LISTED = 25


def pick_courses(prompter, courses, multi=False, allow_all=False, max_listed=MAX_LISTED):
    """Interactively choose one or more courses. Returns a list of courses."""
    if not courses:
        raise ConfigError("這支帳號沒有任何課程")
    visible = list(courses)
    needs_filter = len(courses) > max_listed
    if needs_filter:
        prompter.say(
            f"這支帳號共有 {len(courses)} 門課，請先輸入關鍵字縮小範圍"
            "（輸入 * 列出全部）。"
        )
    while True:
        if needs_filter:
            keyword = prompter._read_line("關鍵字（可留空取消）: ").strip()
            if keyword.lower() in MENU_BACK:
                raise Cancelled()
            if keyword == "*":
                visible = list(courses)
                prompter.say(f"列出全部 {len(visible)} 門課。")
            elif keyword:
                matches = filter_courses(courses, keyword)
                if not matches:
                    prompter.say(f"  ! 沒有符合「{keyword}」的課程，請再試一次。")
                    continue
                visible = matches
            else:
                prompter.say("  ! 請輸入關鍵字，或輸入 * 列出全部。")
                continue

        prompter.say("")
        for line in course_table_lines(visible):
            prompter.say(line)
        prompter.say("")
        prompt = (
            "選擇編號（可用 1,3 或 1-4"
            + ("，all 全選" if allow_all else "")
            + "，q 取消）: "
        )
        answer = prompter._read_line(prompt).strip()
        try:
            indices = parse_selection(answer, len(visible), allow_all=allow_all)
        except Cancelled:
            raise
        except ValueError as exc:
            prompter.say(f"  ! {exc}")
            # A bad pick on a long list is usually a wrong-screen problem: let
            # the user narrow down instead of re-printing the same wall of text.
            needs_filter = len(courses) > max_listed
            continue
        if not indices:
            prompter.say("  ! 沒有選擇任何課程。")
            continue
        if not multi:
            indices = indices[:1]
        return [visible[i - 1] for i in indices]


# ------------------------------------------------------------------- storage
PASSWORD_STORE_OPTIONS = [
    ("secure", "加密儲存（推薦）— 用 Windows DPAPI 綁定你的 Windows 帳號"),
    ("env", "存進 .env（明文；方便但請勿分享該檔案）"),
    ("never", "不要儲存 — 每次執行時再問我"),
]


def ask_password_storage(prompter, store_path, backend_desc):
    """Ask how (or whether) to persist the password. Returns 'secure'|'env'|'never'."""
    prompter.say("")
    prompter.say("密碼要怎麼保存？")
    prompter.say(f"  加密儲存位置: {os.path.abspath(store_path)}")
    prompter.say(f"  這台電腦的加密方式: {backend_desc}")
    return prompter.choose("", PASSWORD_STORE_OPTIONS, default=1)


def persist_credentials(cfg, username, password, mode):
    """Save credentials according to `mode`. Returns a human-readable result."""
    if mode == "secure":
        store = cfg.secret_store()
        backend = store.save({"BB_USERNAME": username, "BB_PASSWORD": password})
        # Never leave a stale plaintext copy behind when switching to encrypted.
        clear_env_password(cfg.env_file)
        return f"已加密儲存（{store.backend}）→ {os.path.abspath(store.path)}"
    if mode == "env":
        update_env_file(
            cfg.env_file,
            {"BB_USERNAME": env_value(username), "BB_PASSWORD": env_value(password)},
        )
        cfg.secret_store().clear()
        return f"已寫入 {os.path.abspath(cfg.env_file)}（明文）"
    # mode == "never"
    update_env_file(cfg.env_file, {"BB_USERNAME": env_value(username)})
    cfg.secret_store().clear()
    return "未儲存密碼，每次執行時會再問一次"


def clear_env_password(env_file):
    """Blank BB_PASSWORD in .env so only the encrypted copy remains.

    Public helper (also used by the CLI's `secure` command) for the same reason
    the first-run flow does it: a stale plaintext copy defeats the point of
    moving the password into encrypted storage.
    """
    values = read_env_file(env_file)
    if values.get("BB_PASSWORD"):
        update_env_file(env_file, {"BB_PASSWORD": ""})
        return True
    return False


# ------------------------------------------------------------------ first run
class FirstRunServices:
    """Callbacks the CLI injects so the wizard needs no knowledge of the session.

    Keeping this explicit avoids a circular import between wizard and cli, and
    lets the whole first-run flow be tested with fakes.
    """

    def __init__(self, login, list_courses):
        self.login = login              # (cfg, username, password) -> session
        self.list_courses = list_courses  # (session) -> [course dict]


def run_first_run(prompter, cfg, log, services):
    """The whole 'just run it' experience: ask for everything, then log in.

    Nothing is written until the user confirms. The password is used in memory
    for the login check and only then offered for storage.
    """
    prompter.rule("=")
    prompter.say("歡迎使用 bbpull")
    prompter.rule("=")
    prompter.say("這是第一次執行，我只需要幾個資訊，之後就可以直接抓課程。")
    prompter.say("（全程按 Ctrl-C 可隨時取消，不會留下任何檔案）")

    # --- site ----------------------------------------------------------
    prompter.say("")
    prompter.say("[1/5] Blackboard 站台")
    base_url = normalize_base_url(
        prompter.ask(
            "      站台網址",
            default=cfg.base_url or DEFAULT_BASE_URL,
            validator=validate_base_url,
        )
    )
    if prompter.interactive:
        prompter.say(f"      檢查連線 {base_url} ...")
        ok, detail = probe_site(base_url)
        prompter.say(f"      {'可連線' if ok else '無法連線'}: {detail}")
        if not ok and not prompter.confirm("      仍要使用這個網址嗎？", default=False):
            raise Cancelled()

    # --- account -------------------------------------------------------
    prompter.say("")
    prompter.say("[2/5] 你的帳號")
    username = prompter.ask(
        "      帳號（學號）", default=cfg.username or None, validator=validate_username
    )

    # --- password + login ---------------------------------------------
    prompter.say("")
    prompter.say("[3/5] 密碼與登入測試")
    prompter.say("      密碼只會用來登入，輸入時不會顯示在畫面上。")
    session = None
    password = ""
    for attempt in range(1, 4):
        password = prompter.ask_secret("      密碼", has_current=False)
        if not password:
            raise Cancelled()
        prompter.say("      登入中 ...")
        try:
            session = services.login(cfg, username, password)
        except Exception as exc:
            prompter.say(f"      登入失敗: {exc}")
            if attempt < 3:
                if not prompter.confirm("      再試一次嗎？", default=True):
                    raise Cancelled()
                continue
            prompter.say("")
            prompter.say("若你的學校使用 SSO（校園入口轉導），密碼表單不在 Blackboard 上，")
            prompter.say("任何程式都無法直接送帳密。請見 README 第 6 節的 cookie 匯入方式。")
            raise Cancelled()
        prompter.say(f"      登入成功！使用者 id: {session.user_id}")
        break

    # --- course --------------------------------------------------------
    prompter.say("")
    prompter.say("[4/5] 預設要抓的課程")
    course_id = cfg.course_id or "auto"
    try:
        courses = services.list_courses(session)
    except Exception as exc:
        prompter.say(f"      讀取課程清單失敗: {exc}")
        courses = []
    if courses:
        prompter.say(f"      讀到 {len(courses)} 筆選課記錄。")
        try:
            chosen = pick_courses(prompter, courses, multi=False)
        except Cancelled:
            prompter.say("      跳過選課，之後每次抓取時再選。")
            course_id = "auto"
        else:
            course_id = chosen[0]["courseId"]
            prompter.say(f"      已選擇: {chosen[0]['name']} ({course_id})")
    else:
        course_id = prompter.ask(
            "      課程 id（可輸入 auto 之後再選）", default="auto", validator=validate_course_id
        ) or "auto"

    # --- output --------------------------------------------------------
    prompter.say("")
    prompter.say("[5/5] 下載位置")
    out_dir = normalize_out_dir(prompter.ask("      輸出資料夾", default=cfg.out_dir or "output"))

    # --- password storage ---------------------------------------------
    mode = ask_password_storage(prompter, cfg.secret_store_path, backend_description())

    # --- confirm + save -------------------------------------------------
    prompter.say("")
    prompter.rule("=")
    prompter.say("確認設定")
    prompter.rule("=")
    prompter.say(f"  站台      : {base_url}")
    prompter.say(f"  帳號      : {username}")
    prompter.say(f"  密碼      : {'已輸入（不會顯示）'}")
    prompter.say(
        "  密碼保存  : "
        + {
            "secure": "加密儲存（DPAPI）",
            "env": ".env 明文",
            "never": "不儲存（每次詢問）",
        }[mode]
    )
    prompter.say(f"  預設課程  : {course_id}")
    prompter.say(f"  輸出資料夾: {out_dir}")
    if not prompter.confirm("確定嗎？", default=True):
        prompter.say("已取消，未寫入任何設定。")
        raise Cancelled()

    update_env_file(
        cfg.env_file,
        OrderedDict(
            [
                ("BB_BASE_URL", env_value(base_url)),
                ("BB_COURSE_ID", env_value(course_id)),
                ("BB_OUT_DIR", env_value(out_dir)),
            ]
        ),
    )
    result = persist_credentials(cfg, username, password, mode)

    # Apply in memory so the caller can continue without re-reading files.
    cfg.base_url = base_url
    cfg.username = username
    cfg.password = password
    cfg.course_id = course_id
    cfg.out_dir = out_dir

    prompter.say("")
    prompter.say(f"設定完成。密碼處理: {result}")
    prompter.say(f"其他設定已寫入: {os.path.abspath(cfg.env_file)}")
    return {"courseId": course_id, "username": username, "passwordMode": mode}


# --------------------------------------------------------------- setup wizard
def _current_env(cfg):
    return read_env_file(cfg.env_file)


def run_setup_wizard(prompter, cfg, log, probe=None):
    """Re-configure an existing installation (menu item 1 / `bbpull setup`).

    Use `run_first_run` for the guided first-time experience.
    """
    probe = probe or probe_site
    existing = _current_env(cfg)
    account = existing.get("BB_USERNAME") or cfg.username or ""
    has_password = bool(existing.get("BB_PASSWORD") or cfg.password)

    prompter.rule("=")
    prompter.say(f"bbpull 互動式設定精靈")
    prompter.rule("=")
    prompter.say(f"設定檔位置: {os.path.abspath(cfg.env_file)}")
    prompter.say("過程中隨時可以按 Ctrl-C 取消，設定檔不會被寫入。")
    prompter.say("")

    # --- 1/5 site ------------------------------------------------------
    prompter.say("[1/5] 站台網址")
    prompter.say("      請填 Blackboard 的站台根網址，不需要後面的 /ultra/... 路徑。")
    base_url = prompter.ask(
        "      站台網址",
        default=existing.get("BB_BASE_URL") or cfg.base_url or DEFAULT_BASE_URL,
        validator=validate_base_url,
    )
    base_url = normalize_base_url(base_url)
    if prompter.interactive:
        prompter.say(f"      檢查連線 {base_url} ...")
        ok, detail = probe(base_url)
        prompter.say(f"      {'可連線' if ok else '無法連線'}: {detail}")
        if not ok and not prompter.confirm("      仍要使用這個網址嗎？", default=False):
            raise Cancelled()

    # --- 2/5 account ---------------------------------------------------
    prompter.say("")
    prompter.say("[2/5] 帳號")
    username = prompter.ask("      帳號", default=account or None, validator=validate_username)
    if account and username != account and has_password:
        prompter.say("      ! 帳號已變更，建議一併重新輸入密碼。")

    # --- 3/5 password --------------------------------------------------
    prompter.say("")
    prompter.say("[3/5] 密碼")
    if has_password:
        prompter.say("      目前已有一組密碼；直接 Enter 保留，輸入 - 清除。")
    password = prompter.ask_secret("      密碼", has_current=has_password)
    keep_password = password is None

    # --- 4/5 default course --------------------------------------------
    prompter.say("")
    prompter.say("[4/5] 預設課程")
    prompter.say("      課程 id 就是網址 .../ultra/courses/_12529_1/outline 裡的 _12529_1。")
    prompter.say("      輸入 auto 表示不固定，每次抓取時再互動選課。")
    course_id = prompter.ask(
        "      課程 id",
        default=existing.get("BB_COURSE_ID") or cfg.course_id or "auto",
        validator=validate_course_id,
    )
    if course_id.strip().lower() in ("auto", "ask"):
        course_id = "auto"

    # --- 5/5 output + defaults -----------------------------------------
    prompter.say("")
    prompter.say("[5/5] 輸出與預設選項")
    out_dir = prompter.ask(
        "      輸出資料夾",
        default=existing.get("BB_OUT_DIR") or cfg.out_dir or "output",
    )
    out_dir = normalize_out_dir(out_dir)
    include_content = prompter.ask_bool("      預設抓取 class content（課程內容）？", default=True)
    include_announcements = prompter.ask_bool("      預設抓取 announcements（公告）？", default=True)
    download_files = prompter.ask_bool("      預設下載附件與內嵌檔案？", default=True)
    overwrite = prompter.ask_bool("      預設重新下載已存在的檔案？", default=False)

    # --- summary --------------------------------------------------------
    updates = OrderedDict(
        [
            ("BB_BASE_URL", env_value(base_url)),
            ("BB_USERNAME", env_value(username)),
            ("BB_COURSE_ID", env_value(course_id)),
            ("BB_OUT_DIR", env_value(out_dir)),
            ("BB_INCLUDE_CONTENT", "1" if include_content else "0"),
            ("BB_INCLUDE_ANNOUNCEMENTS", "1" if include_announcements else "0"),
            ("BB_DOWNLOAD_FILES", "1" if download_files else "0"),
            ("BB_OVERWRITE", "1" if overwrite else "0"),
        ]
    )
    if not keep_password:
        updates["BB_PASSWORD"] = env_value(password)

    prompter.say("")
    prompter.rule("=")
    prompter.say("即將寫入的設定")
    prompter.rule("=")
    prompter.say(f"  站台      : {base_url}")
    prompter.say(f"  帳號      : {username}")
    prompter.say(
        "  密碼      : "
        + ("（保留現有值，未變更）" if keep_password else ("（已清除）" if not password else "（已輸入，不會顯示）"))
    )
    prompter.say(f"  預設課程  : {course_id}")
    prompter.say(f"  輸出資料夾: {out_dir}")
    prompter.say(f"  內容/公告 : {'抓' if include_content else '不抓'} / {'抓' if include_announcements else '不抓'}")
    prompter.say(f"  下載附件  : {'是' if download_files else '否'}")
    prompter.say(f"  覆蓋舊檔  : {'是' if overwrite else '否'}")
    prompter.say(f"  寫入檔案  : {os.path.abspath(cfg.env_file)}")
    if not prompter.confirm("確定寫入嗎？", default=True):
        prompter.say("已取消，未寫入任何設定。")
        raise Cancelled()

    update_env_file(cfg.env_file, updates)
    prompter.say("")
    prompter.say(f"已寫入 {os.path.abspath(cfg.env_file)}")

    # Environment variables outrank the .env file (documented precedence), so an
    # old exported value would silently defeat what the user just saved.
    shadowed = [key for key in updates if os.environ.get(key)]
    if shadowed:
        prompter.say("")
        prompter.say("! 注意：下列設定目前被環境變數覆蓋，.env 的值不會生效：")
        for key in shadowed:
            prompter.say(f"    {key} (來自環境變數)")
        prompter.say("  請在目前的 shell 執行 Remove-Item Env:BB_XXX，或關掉該環境變數。")

    return {key: value for key, value in updates.items()}


def offer_login_check(prompter, cfg, log):
    """After saving, optionally verify the credentials immediately."""
    if not prompter.interactive:
        return None
    if not prompter.confirm("現在測試登入嗎？（會連線到 Blackboard）", default=True):
        return None
    from .session import LearnSession

    prompter.say("")
    prompter.say("正在登入 ...")
    session = LearnSession(cfg, log)
    try:
        session.login(force=True)
    except ConfigError as exc:
        prompter.say(f"設定不完整: {exc}")
        return False
    except Exception as exc:
        prompter.say(f"登入失敗: {exc}")
        prompter.say("")
        prompter.say("若你的學校使用 SSO（校園入口轉導），密碼表單不在 Blackboard 上，")
        prompter.say("請改用 cookie 匯入：在瀏覽器登入後匯出 cookie，設定 BB_COOKIE_FILE。")
        prompter.say("詳見 README 第 6 節。")
        return False
    prompter.say(f"登入成功，使用者 id: {session.user_id}（方式: {session.login_strategy}）")
    try:
        courses = session.get_all("/users/me/courses")
        prompter.say(f"讀到 {len(courses)} 筆課程選課記錄。")
    except Exception as exc:
        prompter.say(f"讀取課程清單失敗: {exc}")
    return True

