"""Blackboard Learn session: login, cookie persistence, REST plumbing.

Design notes
------------
* `LearnSession` owns one `requests.Session` and is NOT thread-safe for
  concurrent requests. `api_get` serialises calls with a lock, and downloads run
  one course at a time, sequentially, to stay gentle on campus servers.
* Login negotiates three strategies in order and reports which one worked, so a
  school-specific login page does not silently produce an anonymous session.
"""

import html
import json
import os
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlparse

import requests

from .errors import ApiError, ConfigError, LoginError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 bbpull/1.0"
)

LOGIN_PATHS = ("/webapps/login/", "/webapps/login", "/")
LEARN_API_ROOT = "/learn/api/public/v1"
ALLOWED_REDIRECT_HOSTS = ("blackboard.com", "bbpd.io", "anthology.com", "blackboardcdn.com")


class _FormScanner(HTMLParser):
    """Extract <form> action and all hidden input name/value pairs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._current = {
                "action": a.get("action", ""),
                "method": (a.get("method") or "get").lower(),
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "hidden": [],
                "inputs": [],
            }
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None:
            field = {
                "name": a.get("name", ""),
                "type": (a.get("type") or "text").lower(),
                "value": a.get("value", ""),
            }
            if not field["name"]:
                return
            self._current["inputs"].append(field)
            if field["type"] == "hidden":
                self._current["hidden"].append(field)

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


class _InputScanner(HTMLParser):
    """Fallback: collect every input on the page, form or not."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        a = dict(attrs)
        name = a.get("name", "")
        if not name:
            return
        self.inputs.append(
            {
                "name": name,
                "type": (a.get("type") or "text").lower(),
                "value": a.get("value", ""),
            }
        )


def _hidden_payload(scanner):
    payload = {}
    for form in scanner.forms:
        for field in form["hidden"]:
            payload.setdefault(field["name"], field["value"])
    return payload


def _pick_login_form(scanner):
    """Choose the form most likely to be the credential form."""
    best = None
    best_score = -1
    for form in scanner.forms:
        types = {f["type"] for f in form["inputs"]}
        names = {f["name"].lower() for f in form["inputs"]}
        score = 0
        if "password" in types:
            score += 10
        if any("user" in n or "login" in n for n in names):
            score += 4
        if form["method"] == "post":
            score += 2
        if any(token in (form["id"] + form["name"] + form["action"]).lower()
               for token in ("login", "entry")):
            score += 2
        if score > best_score:
            best_score, best = score, form
    return best if best_score > 0 else None


def _looks_authenticated(html_text):
    """Heuristic: are we on an authenticated Learn page?"""
    lowered = (html_text or "").lower()
    if "login" in lowered and "user_id" in lowered and "password" in lowered:
        # Still a login form - probably not authenticated.
        if "logout" not in lowered:
            return False
    markers = (
        "ultra/courses",
        "mycourses",
        "base_nav",
        "course-outline",
        '"userid"',
        "global-nav",
        "logout",
    )
    return any(marker in lowered for marker in markers)


#: The login page renders its failure text into this element. Quoting the
#: message back to the user is the difference between "still anonymous" and
#: "the site says your password is wrong".
_LOGIN_ERROR_RE = re.compile(
    r'id="loginErrorMessage"[^>]*>(.*?)(?:</(?:div|span|p|li|section)>|</form>)',
    re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _visible_text(fragment):
    """Tag-stripped, entity-decoded, single-spaced text from an HTML fragment."""
    text = html.unescape(_TAG_RE.sub(" ", fragment or ""))
    return _WS_RE.sub(" ", text).strip()


def _login_error_message(html_text):
    """The site's own login error text, or "" when it did not render one."""
    match = _LOGIN_ERROR_RE.search(html_text or "")
    if not match:
        return ""
    return _visible_text(match.group(1))[:400]


def _mfa_challenge(html_text):
    """True when the response is the second-factor form rather than the login form.

    Measured on Learn 4001: the initial login page already *contains* the MFA
    markup inside a template (`showMFAVerification` literally holds
    `$showMFAVerification`), so the marker alone proves nothing. A real challenge
    replaces the password field and sets the flag to `true`.
    """
    text = html_text or ""
    lowered = text.lower()
    if re.search(r'name="showMFAVerification"[^>]*value="true"', text, re.I):
        return True
    has_password = bool(re.search(r'type=["\']password["\']', text, re.I))
    has_mfa = ("totp-verification-input" in lowered
               or "mfa-verification-title" in lowered
               or "mfa-registration-title" in lowered)
    return has_mfa and not has_password


def _is_sso_host(final_url, base_url):
    """True when a redirect landed on a non-Blackboard identity provider."""
    host = (urlparse(final_url or "").hostname or "").lower()
    origin = (urlparse(base_url or "").hostname or "").lower()
    if not host or not origin or host == origin:
        return False
    if any(host == allowed or host.endswith("." + allowed)
           for allowed in ALLOWED_REDIRECT_HOSTS):
        return False
    return True


def _login_failure_reason(response, base_url):
    """Explain a rejected login attempt, in the site's or our own words."""
    if response is None:
        return ""
    text = getattr(response, "text", "") or ""
    final_url = getattr(response, "url", "") or ""
    if _is_sso_host(final_url, base_url):
        return f"站台把登入導向 {urlparse(final_url).hostname}（SSO），本機帳密表單不適用"
    if _mfa_challenge(text):
        return "站台要求第二因素驗證（MFA／TOTP）；bbpull 不會代填驗證碼"
    message = _login_error_message(text)
    if message:
        return message
    lowered = text.lower()
    if "samlrequest" in lowered or "shibboleth" in lowered:
        return "登入頁是 SAML／Shibboleth SSO，本機帳密表單不適用"
    return ""


def _normalize_cookie(item):
    """One cookie dict in the shape `import_cookies` understands."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not name:
        return None
    domain = (item.get("domain") or "").strip()
    if domain.startswith("#HttpOnly_"):
        domain = domain[len("#HttpOnly_"):]
    return {
        "name": str(name),
        "value": "" if item.get("value") is None else str(item.get("value")),
        "domain": domain,
        "path": item.get("path") or "/",
        "secure": bool(item.get("secure")),
        "expires": item.get("expires", item.get("expirationDate")),
    }


def _parse_cookie_file(raw):
    """Parse a cookie export. Returns (cookies, stored_base_url).

    Three formats show up in practice, and telling the user "your cookie file is
    malformed" when it is merely a different export is not helpful:

    * our own jar:            {"base_url": ..., "cookies": [...]}
    * browser-extension JSON: [{"name": ..., "value": ..., "domain": ...}, ...]
    * Netscape cookies.txt:   domain\\tflag\\tpath\\tsecure\\texpiry\\tname\\tvalue
      (what `curl -c` and most "export cookies" extensions produce)
    """
    text = (raw or "").strip()
    if not text:
        return [], None
    if text[0] in "[{":
        try:
            payload = json.loads(text)
        except ValueError:
            return [], None
        if isinstance(payload, dict):
            items = payload.get("cookies")
            base = payload.get("base_url")
        else:
            items, base = payload, None
        cookies = [cookie for cookie in
                   (_normalize_cookie(item) for item in (items or []))
                   if cookie]
        return cookies, base

    cookies = []
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, _expires, name, value = parts[:7]
        cookie = _normalize_cookie({
            "name": name, "value": value, "domain": domain, "path": path,
            "secure": secure.strip().lower() == "true",
        })
        if cookie:
            cookies.append(cookie)
    return cookies, None


class LearnSession:
    def __init__(self, config, logger=None, credential_provider=None):
        self.config = config
        self.log = logger or (lambda *a, **k: None)
        # Optional `provider(cfg) -> (username, password)`. The CLI passes one
        # that prompts, so a missing password is asked for instead of requiring
        # the user to create a config file first.
        self.credential_provider = credential_provider
        self.http = requests.Session()
        self.http.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
            }
        )
        self.user_id = None
        self.login_strategy = None
        #: Last meaningful HTTP response from a login attempt. Used to quote the
        #: site's own rejection text (wrong password, MFA, SSO) instead of
        #: reporting every failure as "still anonymous".
        self._login_attempt_response = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- paths
    @property
    def base(self):
        return self.config.base_url.rstrip("/")

    def url(self, path):
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    def safe_url(self, path):
        """Resolve `path` and refuse to leave the trusted Blackboard domain."""
        target = urljoin(self.base + "/", path)
        parsed = urlparse(target)
        origin = urlparse(self.base)
        if parsed.hostname == origin.hostname:
            return target
        host = parsed.hostname or ""
        # Allow Blackboard-operated hosts (SSO completion, CDN, inline frames).
        if any(host == h or host.endswith("." + h) for h in ALLOWED_REDIRECT_HOSTS):
            return target
        raise ConfigError(
            f"refusing to follow off-site URL {target!r} "
            f"(allowed hosts: {origin.hostname}, {', '.join(ALLOWED_REDIRECT_HOSTS)})"
        )

    # -------------------------------------------------------------- cookies
    def export_cookies(self):
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": bool(c.secure),
                "expires": c.expires,
            }
            for c in self.http.cookies
        ]

    def import_cookies(self, cookies):
        count = 0
        for item in cookies or []:
            try:
                self.http.cookies.set(
                    item.get("name"),
                    item.get("value"),
                    domain=item.get("domain"),
                    path=item.get("path") or "/",
                )
                count += 1
            except (KeyError, TypeError):
                continue
        return count

    def save_cookies(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {"base_url": self.base, "cookies": self.export_cookies()}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def load_cookies(self, path):
        if not path or not os.path.isfile(path):
            return 0
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
        cookies, stored_base = _parse_cookie_file(raw)
        if not cookies:
            self.log(f"[warn] no cookies could be read from {path}")
            return 0
        if stored_base and stored_base.rstrip("/") != self.base:
            self.log(
                f"[warn] cookie file was saved for {stored_base}, "
                f"current base is {self.base}"
            )
        return self.import_cookies(cookies)

    # ---------------------------------------------------------------- login
    def _collect_credentials(self):
        """Fill in missing credentials from the provider (an interactive prompt)."""
        if self.config.has_credentials or self.credential_provider is None:
            return
        supplied = self.credential_provider(self.config)
        if not supplied:
            return
        username, password = (list(supplied) + ["", ""])[:2]
        if username:
            self.config.username = username
        if password:
            self.config.password = password

    def login(self, force=False):
        cfg = self.config
        self._collect_credentials()
        if not cfg.has_credentials:
            raise ConfigError(
                "no credentials available.\n"
                "Run `bbpull` (no arguments) and use the interactive setup, which "
                "asks for your username and password and can store them securely.\n"
                "Alternatively set BB_USERNAME / BB_PASSWORD in the environment, "
                "or pass --username/--password."
            )
        jar = cfg.cookie_jar_path
        if not force:
            loaded = self.load_cookies(jar)
            if loaded:
                self.log(f"[auth] loaded {loaded} cookie(s) from {jar}")
                if self._verify_authenticated():
                    self.user_id = self._discover_user_id() or "unknown"
                    self.login_strategy = "cached-cookie"
                    self.log(f"[auth] existing session is valid (user {self.user_id})")
                    return self
                # The cached session is dead. Do not carry its cookies into a
                # fresh login: the form's nonce is issued for the session that
                # fetched the page, and a stale identifier alongside it is one
                # more way for the POST to be rejected.
                self.http.cookies.clear()
                self.log("[auth] cached session expired; starting a fresh login")

        state = self._fetch_login_page()
        errors = []
        for name, strategy in (
            ("form-post", self._strategy_form_post),
            ("direct-post", self._strategy_direct_post),
            ("ultra-login", self._strategy_ultra_login),
        ):
            try:
                result = strategy(state)
            except LoginError as exc:
                errors.append(f"{name}: {exc}")
                self.log(f"[auth] {name} failed: {exc}")
                continue
            except requests.RequestException as exc:
                # A network failure must be a reported login failure, not an
                # uncaught traceback out of the GUI's worker thread.
                errors.append(f"{name}: network error: {exc}")
                self.log(f"[auth] {name} failed: {exc}")
                continue
            if result:
                self.login_strategy = name
                self.user_id = self._discover_user_id() or "unknown"
                os.makedirs(os.path.dirname(os.path.abspath(jar)), exist_ok=True)
                self.save_cookies(jar)
                self.log(f"[auth] logged in via {name} as user {self.user_id}")
                self.log(f"[auth] cookies cached at {jar}")
                return self
            reason = _login_failure_reason(self._login_attempt_response, self.base)
            errors.append(f"{name}: rejected" + (f": {reason}" if reason else ""))
            self.log(f"[auth] {name} rejected"
                     + (f" - {reason}" if reason else " - still anonymous"))

        detail = _login_failure_reason(self._login_attempt_response, self.base)
        if detail and not _mfa_challenge(getattr(self._login_attempt_response, "text", "")
                                         or "") and not _is_sso_host(
                getattr(self._login_attempt_response, "url", ""), self.base):
            advice = (
                "\n站台回覆：{message}"
                "\n請先用瀏覽器以同一組帳號密碼登入確認；密碼錯誤、被鎖定或"
                "需要改密碼時，bbpull 無法繞過。"
            ).format(message=detail)
        else:
            advice = (
                "\n如果瀏覽器登入時會轉到 SSO（Azure AD／Shibboleth／校園入口）"
                "或要求驗證碼（MFA），那些步驟無法自動化：請用瀏覽器登入後"
                "匯出 cookie，並把 BB_COOKIE_FILE 指向該 JSON 檔（詳見 README"
                "「SSO 備援」）。"
            )
        raise LoginError(
            "all login strategies failed.\n  - "
            + "\n  - ".join(errors)
            + advice
        )

    # -- login internals --------------------------------------------------
    def _fetch_login_page(self):
        """Anonymous GET so we get the session cookie + hidden form tokens."""
        state = {"html": "", "url": self.base, "hidden": {}, "form": None}
        last_error = None
        for path in LOGIN_PATHS:
            try:
                response = self.http.get(self.url(path), timeout=self.config.timeout)
            except requests.RequestException as exc:
                last_error = exc
                continue
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code} for {path}"
                continue
            state["html"] = response.text
            state["url"] = response.url
            if response.status_code >= 400:
                continue
            scanner = _FormScanner()
            scanner.feed(response.text)
            state["hidden"] = _hidden_payload(scanner)
            state["form"] = _pick_login_form(scanner)
            if state["form"] or state["hidden"]:
                return state
        if last_error:
            self.log(f"[auth] login page probe issue: {last_error}")
        return state

    def _authenticated_probe(self):
        """Return (ok, user_id) using the documented session endpoint."""
        try:
            response = self.http.get(
                self.url(f"{LEARN_API_ROOT}/users/me"),
                headers={"Accept": "application/json"},
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            self.log(f"[auth] probe error: {exc}")
            return False, None
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                return False, None
            return True, data.get("id") or data.get("userName")
        return False, None

    def _verify_authenticated(self):
        ok, _ = self._authenticated_probe()
        return ok

    def _discover_user_id(self):
        ok, user_id = self._authenticated_probe()
        if ok:
            return self._short_user_id(user_id)
        # Fall back to scraping the global navigation.
        try:
            response = self.http.get(self.url("/ultra/courses"), timeout=self.config.timeout)
        except requests.RequestException:
            return None
        if not _looks_authenticated(response.text):
            return None
        match = re.search(r'"(?:userId|user_id|pk1)"\s*:\s*"([^"]+)"', response.text)
        return self._short_user_id(match.group(1)) if match else "unknown"

    @staticmethod
    def _short_user_id(value):
        """`_12345_1` -> left as-is; long student PKs kept verbatim."""
        return value or "unknown"

    def _login_payload(self, state):
        payload = dict(state.get("hidden") or {})
        payload["user_id"] = self.config.username
        payload["username"] = self.config.username
        payload["password"] = self.config.password
        payload.setdefault("login", "Sign In")
        payload.setdefault("action", "login")
        payload.setdefault("new_loc", "")
        payload.setdefault("auth_type", "")
        # Blackboard 9.x style CSRF/session tokens.
        if "blackboard.platform.security.NonceUtil.nonce" not in payload:
            payload["blackboard.platform.security.NonceUtil.nonce"] = ""
        return payload

    def _strategy_form_post(self, state):
        form = state.get("form")
        if not form:
            raise LoginError("no credential form found on the page")
        action = form.get("action") or "/webapps/login/"
        payload = self._login_payload(state)
        # Only send names the form/server plausibly accepts: keep hidden fields
        # verbatim, then the credential fields we control.
        allowed = {f["name"] for f in form["inputs"]}
        filtered = {
            k: v
            for k, v in payload.items()
            if k in allowed
            or k in {
                "user_id",
                "username",
                "password",
                "login",
                "action",
                "new_loc",
                "auth_type",
            }
        }
        target = urljoin(state.get("url") or self.base + "/", action)
        self.log(f"[auth] POST {target} (form-post)")
        response = self.http.post(
            target,
            data=filtered,
            timeout=self.config.timeout,
            allow_redirects=True,
        )
        self._remember_login_response(response)
        return self._verify_authenticated()

    def _strategy_direct_post(self, state):
        payload = self._login_payload(state)
        target = self.url("/webapps/login/")
        self.log(f"[auth] POST {target} (direct-post)")
        response = self.http.post(
            target,
            data=payload,
            timeout=self.config.timeout,
            allow_redirects=True,
        )
        self._remember_login_response(response)
        return self._verify_authenticated()

    def _remember_login_response(self, response):
        """Keep the first informative HTML response from a login attempt.

        The form-based POSTs return the page that says *why* the login failed
        ("The username or password you typed is incorrect", an MFA challenge, an
        SSO landing page). The later JSON probes return opaque bodies, so they
        must not overwrite it.
        """
        if self._login_attempt_response is not None:
            return
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" in content_type and response.status_code < 500:
            self._login_attempt_response = response

    def _strategy_ultra_login(self, state):
        """Some Ultra tenants expose /api/ultra/... or an LTI-free JSON login."""
        candidates = (
            "/ultra/login",
            "/api/ultra/v1/login",
            "/learn/api/public/v1/login",
        )
        json_body = {
            "userName": self.config.username,
            "password": self.config.password,
            "rememberMe": False,
        }
        for path in candidates:
            target = self.url(path)
            self.log(f"[auth] POST {target} (ultra-login)")
            try:
                response = self.http.post(
                    target,
                    json=json_body,
                    timeout=self.config.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                self.log(f"[auth]   {path} -> {exc}")
                continue
            if response.status_code in (401, 403, 404):
                continue
            if self._verify_authenticated():
                return True
        return False

    # ------------------------------------------------------------ REST core
    def api_get(self, path, params=None, expect_json=True, allow_404=False):
        """GET a Learn REST path with retries, 401 refresh and pagination-free JSON."""
        if not path.startswith("/learn/"):
            path = LEARN_API_ROOT + path if path.startswith("/") else path
        target = self.url(path)
        attempt = 0
        delay = 1.0
        while True:
            attempt += 1
            try:
                with self._lock:
                    response = self.http.get(
                        target,
                        params=params,
                        timeout=self.config.timeout,
                        allow_redirects=True,
                        headers={"Accept": "application/json"},
                    )
            except requests.RequestException as exc:
                if attempt > self.config.retries:
                    raise ApiError(0, "GET", target, str(exc)) from exc
                self.log(f"[retry {attempt}] {target}: {exc}")
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 200:
                if not expect_json:
                    return response
                try:
                    return response.json()
                except ValueError as exc:
                    raise ApiError(200, "GET", target, "response was not JSON") from exc
            if response.status_code == 404 and allow_404:
                return None
            if response.status_code == 401:
                raise ApiError(401, "GET", target, "session expired - re-run `bbpull login`")
            if response.status_code == 403:
                raise ApiError(
                    403,
                    "GET",
                    target,
                    "forbidden - this account cannot read this resource "
                    "(student accounts cannot read some instructor-only endpoints)",
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt > self.config.retries:
                    raise ApiError(response.status_code, "GET", target, response.text)
                wait = self._retry_after(response, delay)
                self.log(f"[retry {attempt}] HTTP {response.status_code} -> sleep {wait:.1f}s")
                time.sleep(wait)
                delay *= 2
                continue
            raise ApiError(response.status_code, "GET", target, response.text)

    @staticmethod
    def _retry_after(response, fallback):
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return max(0.5, min(60.0, float(raw)))
            except ValueError:
                pass
        return fallback

    def get_all(self, path, params=None, max_pages=500):
        """Follow `paging.nextPage` until exhausted; return the merged results."""
        collected = []
        query = dict(params or {})
        query.setdefault("limit", 200)
        seen_cursors = set()
        for _ in range(max_pages):
            payload = self.api_get(path, params=query)
            results = payload.get("results", []) if isinstance(payload, dict) else []
            collected.extend(results)
            paging = (payload or {}).get("paging") or {}
            next_page = paging.get("nextPage")
            if not next_page:
                return collected
            parsed = urlparse(next_page)
            cursor = parsed.query
            if cursor in seen_cursors:
                self.log(f"[warn] pagination loop detected on {path}, stopping")
                return collected
            seen_cursors.add(cursor)
            # nextPage looks like "/learn/api/...?offset=200"; re-query with merged params.
            query = {k: v for k, v in _parse_qs(cursor).items()}
            query.setdefault("limit", 200)
        self.log(f"[warn] pagination cap ({max_pages} pages) reached on {path}")
        return collected

    # ----------------------------------------------------------- downloads
    def download(self, url, dest_path, overwrite=False, expected_size=None,
                 http=None):
        """Stream a file to disk. Returns (status, bytes, final_path).

        status is one of: 'saved', 'skipped', 'failed'.
        Writes to a `.part` file first so an interrupted run never leaves a
        truncated file that a later resume would treat as complete.

        `http` is for callers that must not share the main session (preview
        downloads run on a GUI worker thread while a pull may be running).
        """
        from .paths import long_path

        client = http or self.http
        if os.path.exists(dest_path) and not overwrite:
            size = os.path.getsize(dest_path)
            if size > 0 and (expected_size is None or size == expected_size):
                return "skipped", size, dest_path

        target = self.safe_url(url)
        tmp_path = dest_path + ".part"
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        attempt = 0
        delay = 1.0
        while True:
            attempt += 1
            try:
                with client.get(
                    target,
                    stream=True,
                    timeout=self.config.timeout,
                    allow_redirects=True,
                ) as response:
                    if response.status_code in (401, 403):
                        return "failed", 0, f"HTTP {response.status_code} (no access)"
                    if response.status_code == 404:
                        return "failed", 0, "HTTP 404 (not found)"
                    if response.status_code >= 400:
                        if attempt > self.config.retries:
                            return "failed", 0, f"HTTP {response.status_code}"
                        time.sleep(delay)
                        delay *= 2
                        continue
                    written = 0
                    with open(long_path(tmp_path), "wb") as handle:
                        for chunk in response.iter_content(chunk_size=131072):
                            if chunk:
                                handle.write(chunk)
                                written += len(chunk)
                if written == 0:
                    _quiet_remove(tmp_path)
                    return "failed", 0, "empty response body"
                os.replace(long_path(tmp_path), long_path(dest_path))
                return "saved", written, dest_path
            except requests.RequestException as exc:
                if attempt > self.config.retries:
                    _quiet_remove(tmp_path)
                    return "failed", 0, str(exc)
                self.log(f"[retry {attempt}] download {target}: {exc}")
                time.sleep(delay)
                delay *= 2
            except OSError as exc:
                _quiet_remove(tmp_path)
                return "failed", 0, f"write error: {exc}"

    def download_preview(self, url, dest_path):
        """Download one attachment for an interactive preview.

        The main `requests.Session` is explicitly not safe for concurrent use
        and the preview runs on a GUI worker thread, possibly while a pull is
        in progress. A scratch session carrying the same cookies keeps the two
        independent (and inherits every retry/`.part` guarantee of `download`).
        """
        scratch = requests.Session()
        scratch.headers.update(self.http.headers)
        scratch.cookies.update(self.http.cookies)
        return self.download(url, dest_path, overwrite=True, http=scratch)


def _quiet_remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _parse_qs(query):
    out = {}
    for pair in (query or "").split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        out[key] = value
    return out
