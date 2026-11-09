"""Login failure diagnosis.

The first version reported every rejected attempt as "still anonymous", which
is technically true and practically useless: a wrong password, an MFA challenge
and an SSO redirect all looked identical. These tests pin the classification
that turns the site's own response into an actionable message.
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull.errors import LoginError  # noqa: E402
from bbpull.session import (  # noqa: E402
    LearnSession,
    _is_sso_host,
    _login_error_message,
    _login_failure_reason,
    _mfa_challenge,
    _parse_cookie_file,
)

BASE = "https://twc.blackboard.com"

#: Measured on Learn 4001 after a rejected POST (trimmed to the message element).
WRONG_PASSWORD_PAGE = """
<html><body>
<div id="loginErrorMessage" class="receipt bad editmode alert-box alert" role="alert" data-alert>
The username or password you typed is incorrect. Please try again. If you still
can&#x27;t log in, contact your system administrator.
</div>
<form action="/webapps/login/" method="POST" name="login">
  <input type="text" name="user_id">
  <input type="password" name="password">
</form>
</body></html>
"""

#: The initial login page contains the MFA markup in a template. The flag holds
#: the literal placeholder, so the marker alone must not be read as a challenge.
INITIAL_PAGE_WITH_MFA_TEMPLATE = """
<html><body>
<form action="/webapps/login/" method="POST" name="login">
  <input type="password" name="password">
  <input type="hidden" name="showMFAVerification" id="showMFAVerification"
         value="$showMFAVerification" />
  <div id="totp-verification-input"></div>
  <div id="mfa-verification-title"></div>
</form>
</body></html>
"""

MFA_CHALLENGE_PAGE = """
<html><body>
<div id="loginErrorMessage"></div>
<form action="/webapps/login/" method="POST" name="login">
  <input type="hidden" name="showMFAVerification" value="true" />
  <div id="totp-verification-input"><input name="totpCode"></div>
  <button id="totp-submit-button">Verify</button>
</form>
</body></html>
"""


class FakeResponse:
    def __init__(self, text="", url=BASE + "/webapps/login/",
                 status_code=200, content_type="text/html;charset=UTF-8"):
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class LoginErrorMessageTests(unittest.TestCase):
    def test_quotes_the_site_message(self):
        message = _login_error_message(WRONG_PASSWORD_PAGE)
        self.assertIn("username or password", message)
        self.assertIn("incorrect", message)

    def test_absent_message_is_empty(self):
        self.assertEqual(_login_error_message("<html><body>ok</body></html>"), "")

    def test_message_is_tag_stripped(self):
        self.assertNotIn("<", _login_error_message(WRONG_PASSWORD_PAGE))


class MfaDetectionTests(unittest.TestCase):
    def test_initial_page_template_is_not_a_challenge(self):
        """The markup ships in a template; only a rendered flag is evidence."""
        self.assertFalse(_mfa_challenge(INITIAL_PAGE_WITH_MFA_TEMPLATE))

    def test_rendered_challenge_is_detected(self):
        self.assertTrue(_mfa_challenge(MFA_CHALLENGE_PAGE))

    def test_plain_login_page_is_not_mfa(self):
        self.assertFalse(_mfa_challenge(WRONG_PASSWORD_PAGE))


class SsoDetectionTests(unittest.TestCase):
    def test_same_host_is_not_sso(self):
        self.assertFalse(_is_sso_host(BASE + "/webapps/login/", BASE))

    def test_identity_provider_is_sso(self):
        self.assertTrue(_is_sso_host(
            "https://login.microsoftonline.com/tenant/saml2", BASE))
        self.assertTrue(_is_sso_host("https://idp.university.edu/idp/sso", BASE))

    def test_blackboard_operated_hosts_are_allowed(self):
        self.assertFalse(_is_sso_host("https://blackboard.com/x", BASE))
        self.assertFalse(_is_sso_host("https://cdn.blackboardcdn.com/x", BASE))


class FailureReasonTests(unittest.TestCase):
    def test_wrong_password_message_wins(self):
        reason = _login_failure_reason(FakeResponse(WRONG_PASSWORD_PAGE), BASE)
        self.assertIn("incorrect", reason)

    def test_mfa_is_reported_as_mfa(self):
        reason = _login_failure_reason(FakeResponse(MFA_CHALLENGE_PAGE), BASE)
        self.assertIn("MFA", reason)

    def test_sso_redirect_is_reported_with_host(self):
        response = FakeResponse("<html>login</html>",
                                url="https://idp.university.edu/sso")
        reason = _login_failure_reason(response, BASE)
        self.assertIn("idp.university.edu", reason)

    def test_no_response_has_no_reason(self):
        self.assertEqual(_login_failure_reason(None, BASE), "")


class _Cfg:
    base_url = BASE
    username = "student"
    password = "secret"
    timeout = 5
    has_credentials = True

    def __init__(self, cookie_jar):
        self.cookie_file = cookie_jar

    @property
    def cookie_jar_path(self):
        return self.cookie_file


class CookieFileFormatTests(unittest.TestCase):
    """A cookie export is useful only if the app can read the one you have."""

    def test_own_jar_format(self):
        raw = ('{"base_url": "https://twc.blackboard.com", '
               '"cookies": [{"name": "BbRouter", "value": "x", '
               '"domain": "twc.blackboard.com", "path": "/"}]}')
        cookies, base = _parse_cookie_file(raw)
        self.assertEqual(base, "https://twc.blackboard.com")
        self.assertEqual(cookies[0]["name"], "BbRouter")

    def test_browser_extension_json_array(self):
        raw = ('[{"name": "JSESSIONID", "value": "abc", '
               '"domain": "twc.blackboard.com", "path": "/webapps/login", '
               '"secure": true, "expirationDate": 1893456000}]')
        cookies, base = _parse_cookie_file(raw)
        self.assertIsNone(base)
        self.assertEqual(cookies[0]["name"], "JSESSIONID")
        self.assertTrue(cookies[0]["secure"])
        self.assertEqual(cookies[0]["expires"], 1893456000)

    def test_netscape_cookies_txt(self):
        raw = (
            "# Netscape HTTP Cookie File\n"
            "# generated by curl\n"
            ".twc.blackboard.com\tTRUE\t/\tTRUE\t1794641872\tBbRouter\tabc\n"
            "#HttpOnly_twc.blackboard.com\tFALSE\t/webapps/login\tTRUE\t0\t"
            "JSESSIONID\tdef\n"
        )
        cookies, base = _parse_cookie_file(raw)
        self.assertIsNone(base)
        by_name = {cookie["name"]: cookie for cookie in cookies}
        self.assertEqual(by_name["BbRouter"]["domain"], ".twc.blackboard.com")
        self.assertEqual(by_name["JSESSIONID"]["domain"], "twc.blackboard.com")
        self.assertEqual(by_name["JSESSIONID"]["path"], "/webapps/login")

    def test_garbage_is_not_an_exception(self):
        self.assertEqual(_parse_cookie_file("not a cookie file")[0], [])
        self.assertEqual(_parse_cookie_file("")[0], [])

    def test_load_cookies_accepts_netscape_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cookies.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    ".twc.blackboard.com\tTRUE\t/\tTRUE\t0\tBbRouter\tabc\n")
            session = LearnSession(_Cfg(os.path.join(tmp, "jar.json")))
            loaded = session.load_cookies(path)
            self.assertEqual(loaded, 1)
            self.assertTrue(any(c.name == "BbRouter" for c in session.http.cookies))


class LoginFlowTests(unittest.TestCase):
    def _session(self, tmp):
        session = LearnSession(_Cfg(os.path.join(tmp, "cookies.json")))
        session.http.cookies.set("JSESSIONID", "stale", domain="twc.blackboard.com")
        return session

    def test_expired_cookie_is_dropped_before_a_fresh_login(self):
        """A dead session must not ride along with the new login POST.

        The nonce on the login form belongs to the session that fetched the
        page. Sending a stale session identifier next to it is one more reason
        for the server to reject the POST.
        """
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(tmp)
            session.load_cookies = lambda path: 1
            session._verify_authenticated = lambda: False
            session._fetch_login_page = lambda: {"form": None, "hidden": {},
                                                  "html": "", "url": BASE}
            session._strategy_form_post = lambda state: False
            session._strategy_direct_post = lambda state: False
            session._strategy_ultra_login = lambda state: False
            session._login_attempt_response = FakeResponse(WRONG_PASSWORD_PAGE)

            with self.assertRaises(LoginError):
                session.login()

            self.assertEqual(len(session.http.cookies), 0,
                             "stale cookies must be cleared before logging in")

    def test_failure_message_includes_the_server_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(tmp)
            session.load_cookies = lambda path: 0
            session._fetch_login_page = lambda: {"form": None, "hidden": {},
                                                  "html": "", "url": BASE}
            session._strategy_form_post = lambda state: False
            session._strategy_direct_post = lambda state: False
            session._strategy_ultra_login = lambda state: False
            session._login_attempt_response = FakeResponse(WRONG_PASSWORD_PAGE)

            with self.assertRaises(LoginError) as caught:
                session.login()

        message = str(caught.exception)
        self.assertIn("username or password", message)
        self.assertIn("瀏覽器", message, "the advice must tell them what to do")


if __name__ == "__main__":
    unittest.main()
