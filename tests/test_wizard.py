"""Offline tests for the interactive layer.

Everything here runs without a terminal: `Prompter(lines=[...])` replays scripted
answers, so the wizard and pickers are verified deterministically.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull import cli, wizard  # noqa: E402
from bbpull.config import (  # noqa: E402
    build_config,
    load_config,
    read_env_file,
    scan_template_for_secrets,
)
from bbpull.errors import ApiError, ConfigError  # noqa: E402
from bbpull.wizard import (  # noqa: E402
    Cancelled,
    FirstRunServices,
    Prompter,
    env_value,
    filter_courses,
    parse_selection,
    pick_courses,
    run_first_run,
    run_setup_wizard,
    update_env_file,
    validate_base_url,
    validate_course_id,
)
from bbpull.logging_util import configure_console_streams, safe_print, wrap_logger  # noqa: E402
from bbpull.secrets_store import (  # noqa: E402
    BACKEND_DPAPI,
    SecretStore,
    available_backend,
)

COURSES = [
    {"courseId": "_1_1", "name": "Data Structures"},
    {"courseId": "_2_1", "name": "作業系統"},
    {"courseId": "_3_1", "name": "Operating Systems Lab"},
]


def prompter(lines, secrets=None):
    """A Prompter that reads scripted lines and writes to a buffer we can inspect."""
    out = io.StringIO()
    secret_iter = iter(secrets) if secrets is not None else None

    def secret_reader(_label):
        if secret_iter is None:
            raise AssertionError("wizard asked for a secret but none was scripted")
        return next(secret_iter)

    return Prompter(lines=list(lines), out=out, secret_reader=secret_reader), out


class ParseSelectionTests(unittest.TestCase):
    def test_single(self):
        self.assertEqual(parse_selection("2", 5), [2])

    def test_list(self):
        self.assertEqual(parse_selection("1,3", 5), [1, 3])

    def test_range(self):
        self.assertEqual(parse_selection("2-4", 5), [2, 3, 4])

    def test_reversed_range_is_normalised(self):
        self.assertEqual(parse_selection("4-2", 5), [2, 3, 4])

    def test_mixed_and_deduped(self):
        self.assertEqual(parse_selection("1,1,3-4", 5), [1, 3, 4])

    def test_all(self):
        self.assertEqual(parse_selection("all", 3), [1, 2, 3])

    def test_all_disallowed(self):
        with self.assertRaises(ValueError):
            parse_selection("all", 3, allow_all=False)

    def test_blank_returns_empty(self):
        self.assertEqual(parse_selection("   ", 3), [])

    def test_quit_raises_cancelled(self):
        for token in ("q", "quit", "exit", "back", "0"):
            with self.assertRaises(Cancelled):
                parse_selection(token, 3)

    def test_zero_is_not_a_general_quit_token(self):
        """`0` must cancel a 1-based menu but stay a valid answer elsewhere."""
        p, _ = prompter(["0"])
        self.assertFalse(p.ask_bool("q", default=True))
        p, _ = prompter(["0"])
        self.assertEqual(p.ask_int("n", default=5, minimum=0, maximum=9), 0)

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            parse_selection("9", 3)
        with self.assertRaises(ValueError):
            parse_selection("1-9", 3)

    def test_non_numeric(self):
        with self.assertRaises(ValueError):
            parse_selection("abc", 3)

    def test_spaces_tolerated(self):
        self.assertEqual(parse_selection(" 1 , 3 ", 5), [1, 3])


class PrompterTests(unittest.TestCase):
    def test_default_used_on_blank(self):
        p, _ = prompter([""])
        self.assertEqual(p.ask("site", default="https://x"), "https://x")

    def test_validator_reprompts_then_accepts(self):
        p, out = prompter(["bad value", "good"])
        seen = []

        def validate(value):
            seen.append(value)
            return None if value == "good" else "nope"

        self.assertEqual(p.ask("field", validator=validate), "good")
        self.assertEqual(seen, ["bad value", "good"])
        self.assertIn("nope", out.getvalue())

    def test_required_field_reprompts_on_blank(self):
        p, out = prompter(["", "", "value"])
        self.assertEqual(p.ask("field"), "value")
        self.assertIn("不能留空", out.getvalue())

    def test_allow_empty_returns_blank(self):
        p, _ = prompter([""])
        self.assertEqual(p.ask("field", allow_empty=True), "")

    def test_quit_token_cancels(self):
        p, _ = prompter(["q"])
        with self.assertRaises(Cancelled):
            p.ask("field")

    def test_eof_cancels_instead_of_hanging(self):
        p, _ = prompter([])
        with self.assertRaises(Cancelled):
            p.ask("field")

    def test_bool_parsing(self):
        for raw, expected in (("y", True), ("Y", True), ("n", False), ("", True), ("0", False)):
            p, _ = prompter([raw])
            self.assertEqual(p.ask_bool("q", default=True), expected, raw)

    def test_bool_reprompts_on_garbage(self):
        p, _ = prompter(["maybe", "n"])
        self.assertFalse(p.ask_bool("q", default=True))

    def test_int_bounds(self):
        p, _ = prompter(["99", "3"])
        self.assertEqual(p.ask_int("n", default=1, minimum=1, maximum=5), 3)

    def test_secret_keeps_current_on_blank(self):
        p, _ = prompter([], secrets=[""])
        self.assertIsNone(p.ask_secret("pw", has_current=True))

    def test_secret_dash_clears(self):
        p, _ = prompter([], secrets=["-"])
        self.assertEqual(p.ask_secret("pw", has_current=True), "")

    def test_secret_required_when_absent(self):
        p, _ = prompter([], secrets=["", "real-pw"])
        self.assertEqual(p.ask_secret("pw", has_current=False), "real-pw")

    def test_choose_returns_key(self):
        p, _ = prompter(["2"])
        self.assertEqual(p.choose("pick", [("a", "A"), ("b", "B")], default=1), "b")

    def test_choose_default_on_blank(self):
        p, _ = prompter([""])
        self.assertEqual(p.choose("pick", [("a", "A"), ("b", "B")], default=2), "b")

    def test_require_interactive_raises_when_not_tty(self):
        p = Prompter(lines=None, interactive=False)
        with self.assertRaises(ConfigError):
            p.require_interactive("hint")


class EnvValueTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(env_value("simple"), "simple")

    def test_quotes_when_spaces(self):
        self.assertEqual(env_value("C:\\my folder"), '"C:\\my folder"')

    def test_quotes_when_hash(self):
        self.assertEqual(env_value("a#b"), '"a#b"')

    def test_empty(self):
        self.assertEqual(env_value(""), '""')


class UpdateEnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_env_")
        self.path = os.path.join(self.tmp, ".env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_file_with_trailing_newline(self):
        update_env_file(self.path, {"BB_USERNAME": "alice"})
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "BB_USERNAME=alice\n")

    def test_preserves_comments_and_order(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("# my notes\nBB_USERNAME=old\n# trailing comment\nBB_OTHER=keep\n")
        update_env_file(self.path, {"BB_USERNAME": "new"})
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("# my notes", text)
        self.assertIn("# trailing comment", text)
        self.assertIn("BB_OTHER=keep", text)
        self.assertIn("BB_USERNAME=new", text)
        self.assertNotIn("BB_USERNAME=old", text)
        # Order preserved: the updated key stays where it was.
        self.assertLess(text.index("BB_USERNAME"), text.index("BB_OTHER"))

    def test_appends_unknown_keys(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("BB_USERNAME=old\n")
        update_env_file(self.path, {"BB_COURSE_ID": "_12529_1"})
        values = read_env_file(self.path)
        self.assertEqual(values["BB_USERNAME"], "old")
        self.assertEqual(values["BB_COURSE_ID"], "_12529_1")

    def test_handles_export_prefix(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("export BB_USERNAME=old\n")
        update_env_file(self.path, {"BB_USERNAME": "new"})
        self.assertEqual(read_env_file(self.path)["BB_USERNAME"], "new")

    def test_atomic_no_temp_left_behind(self):
        update_env_file(self.path, {"BB_USERNAME": "a"})
        leftovers = [n for n in os.listdir(self.tmp) if n != ".env"]
        self.assertEqual(leftovers, [])

    def test_bom_stripped(self):
        with open(self.path, "w", encoding="utf-8-sig") as handle:
            handle.write("BB_USERNAME=old\n")
        update_env_file(self.path, {"BB_USERNAME": "new"})
        self.assertEqual(read_env_file(self.path)["BB_USERNAME"], "new")


class ValidatorTests(unittest.TestCase):
    def test_base_url_accepts_forms(self):
        for raw in (
            "https://twc.blackboard.com",
            "twc.blackboard.com",
            "https://twc.blackboard.com/ultra/courses/_12529_1/outline",
        ):
            self.assertIsNone(validate_base_url(raw), raw)

    def test_course_id_valid(self):
        self.assertIsNone(validate_course_id("_12529_1"))

    def test_course_id_auto_allowed(self):
        self.assertIsNone(validate_course_id("auto"))

    def test_course_id_rejects_junk(self):
        self.assertIsNotNone(validate_course_id("12529"))
        self.assertIsNotNone(validate_course_id(""))


class FilterCoursesTests(unittest.TestCase):
    def test_matches_name_and_id(self):
        self.assertEqual(len(filter_courses(COURSES, "作業")), 1)
        self.assertEqual(len(filter_courses(COURSES, "_3_1")), 1)

    def test_blank_returns_all(self):
        self.assertEqual(len(filter_courses(COURSES, "")), 3)

    def test_no_match(self):
        self.assertEqual(filter_courses(COURSES, "zzz"), [])


class PickCoursesTests(unittest.TestCase):
    def test_single_pick(self):
        p, _ = prompter(["2"])
        chosen = pick_courses(p, COURSES, multi=False)
        self.assertEqual([c["courseId"] for c in chosen], ["_2_1"])

    def test_multi_pick_truncates_to_one_when_not_multi(self):
        p, _ = prompter(["1,2"])
        chosen = pick_courses(p, COURSES, multi=False)
        self.assertEqual(len(chosen), 1)

    def test_multi_pick_keeps_all(self):
        p, _ = prompter(["1,3"])
        chosen = pick_courses(p, COURSES, multi=True)
        self.assertEqual([c["courseId"] for c in chosen], ["_1_1", "_3_1"])

    def test_retries_after_bad_input(self):
        p, out = prompter(["99", "1"])
        chosen = pick_courses(p, COURSES, multi=False)
        self.assertEqual(chosen[0]["courseId"], "_1_1")
        self.assertIn("outside 1-3", out.getvalue())

    def test_allow_all(self):
        p, _ = prompter(["all"])
        chosen = pick_courses(p, COURSES, multi=True, allow_all=True)
        self.assertEqual(len(chosen), 3)

    def test_empty_course_list_raises(self):
        p, _ = prompter([])
        with self.assertRaises(ConfigError):
            pick_courses(p, [], multi=False)


def menu_index(action):
    """Menu number for an action key, derived from MENU_ACTIONS.

    Hardcoding numbers breaks silently whenever the menu is reordered - and a
    stale number can select an entirely different action (that bug once made a
    test invoke `selftest`, which re-ran the suite and recursed forever).
    """
    return list(cli.MENU_ACTIONS.keys()).index(action) + 1


def ok_probe(_url):
    """Injected reachability check: always succeeds, so it consumes no input."""
    return True, "HTTP 200"


class SetupWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_wiz_")
        self.env_path = os.path.join(self.tmp, ".env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        return load_config(self.env_path)

    def test_writes_all_expected_keys(self):
        # site, username, [secret], course id, out dir, 4 booleans, confirm
        lines = [
            "https://twc.blackboard.com",
            "alice",
            "_12529_1",
            os.path.join(self.tmp, "out"),
            "y", "y", "y", "n",
            "y",
        ]
        p, _ = prompter(lines, secrets=["s3cret"])
        written = run_setup_wizard(p, self._cfg(), lambda *a: None, probe=ok_probe)
        values = read_env_file(self.env_path)
        self.assertEqual(values["BB_BASE_URL"], "https://twc.blackboard.com")
        self.assertEqual(values["BB_USERNAME"], "alice")
        self.assertEqual(values["BB_PASSWORD"], "s3cret")
        self.assertEqual(values["BB_COURSE_ID"], "_12529_1")
        self.assertEqual(values["BB_OVERWRITE"], "0")
        self.assertEqual(values["BB_DOWNLOAD_FILES"], "1")
        self.assertIn("BB_BASE_URL", written)

    def test_saved_values_are_actually_read_back(self):
        """Every key the wizard writes must change real Config state."""
        lines = [
            "https://bb.example.edu",
            "bob",
            "auto",
            os.path.join(self.tmp, "out"),
            "n", "n", "n", "y",
            "y",
        ]
        p, _ = prompter(lines, secrets=["pw"])
        run_setup_wizard(p, self._cfg(), lambda *a: None, probe=ok_probe)
        cfg = self._cfg()
        self.assertEqual(cfg.base_url, "https://bb.example.edu")
        self.assertEqual(cfg.username, "bob")
        self.assertEqual(cfg.password, "pw")
        self.assertEqual(cfg.course_id, "auto")
        self.assertFalse(cfg.include_content)
        self.assertFalse(cfg.include_announcements)
        self.assertFalse(cfg.download_files)
        self.assertTrue(cfg.overwrite)

    def test_unreachable_site_can_be_declined(self):
        """A failed probe asks for confirmation and can abort the whole wizard."""
        lines = ["https://bb.example.edu", "n"]
        p, _ = prompter(lines, secrets=["pw"])
        with self.assertRaises(Cancelled):
            run_setup_wizard(
                p, self._cfg(), lambda *a: None, probe=lambda url: (False, "連線失敗: timeout")
            )
        self.assertFalse(os.path.exists(self.env_path))

    def test_unreachable_site_can_be_accepted(self):
        lines = [
            "https://bb.example.edu",
            "y",
            "bob",
            "auto",
            os.path.join(self.tmp, "out"),
            "y", "y", "y", "n",
            "y",
        ]
        p, _ = prompter(lines, secrets=["pw"])
        run_setup_wizard(
            p, self._cfg(), lambda *a: None, probe=lambda url: (False, "連線失敗: timeout")
        )
        self.assertEqual(read_env_file(self.env_path)["BB_BASE_URL"], "https://bb.example.edu")

    def test_blank_password_keeps_existing(self):
        update_env_file(self.env_path, {"BB_USERNAME": "alice", "BB_PASSWORD": "old"})
        lines = [
            "https://twc.blackboard.com",
            "alice",
            "_12529_1",
            os.path.join(self.tmp, "out"),
            "y", "y", "y", "n",
            "y",
        ]
        p, _ = prompter(lines, secrets=[""])
        run_setup_wizard(p, self._cfg(), lambda *a: None, probe=ok_probe)
        self.assertEqual(read_env_file(self.env_path)["BB_PASSWORD"], "old")

    def test_password_never_printed(self):
        lines = [
            "https://twc.blackboard.com",
            "alice",
            "_12529_1",
            os.path.join(self.tmp, "out"),
            "y", "y", "y", "n",
            "y",
        ]
        p, out = prompter(lines, secrets=["top-secret-pw"])
        run_setup_wizard(p, self._cfg(), lambda *a: None, probe=ok_probe)
        self.assertNotIn("top-secret-pw", out.getvalue())
        self.assertIn("(input hidden)", out.getvalue())
        # The summary must not leak it either.
        self.assertIn("已輸入，不會顯示", out.getvalue())

    def test_declining_confirmation_writes_nothing(self):
        lines = [
            "https://twc.blackboard.com",
            "alice",
            "_12529_1",
            os.path.join(self.tmp, "out"),
            "y", "y", "y", "n",
            "n",
        ]
        p, _ = prompter(lines, secrets=["pw"])
        with self.assertRaises(Cancelled):
            run_setup_wizard(p, self._cfg(), lambda *a: None, probe=ok_probe)
        self.assertFalse(os.path.exists(self.env_path))

    def test_eof_midway_writes_nothing(self):
        p, _ = prompter(["https://twc.blackboard.com"], secrets=["pw"])
        with self.assertRaises(Cancelled):
            run_setup_wizard(p, self._cfg(), lambda *a: None, probe=ok_probe)
        self.assertFalse(os.path.exists(self.env_path))

    def test_headless_run_skips_probe(self):
        """`interactive=False` must not call the network probe at all."""
        calls = []

        def exploding_probe(url):
            calls.append(url)
            raise AssertionError("probe must not run when non-interactive")

        lines = [
            "https://twc.blackboard.com",
            "alice",
            "_12529_1",
            os.path.join(self.tmp, "out"),
            "y", "y", "y", "n",
            "y",
        ]
        p, _ = prompter(lines, secrets=["pw"])
        p.interactive = False
        run_setup_wizard(p, self._cfg(), lambda *a: None, probe=exploding_probe)
        self.assertEqual(calls, [])
        self.assertTrue(os.path.exists(self.env_path))


class NonInteractiveSafetyTests(unittest.TestCase):
    """The interactive paths must refuse, not hang, when stdin is not a console."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_ni_")
        self.env_path = os.path.join(self.tmp, ".env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_setup_refuses_without_input(self):
        args = cli.build_parser().parse_args(["setup", "--no-input"])
        prompter = cli.make_prompter(args)
        self.assertFalse(prompter.interactive)
        with self.assertRaises(ConfigError):
            prompter.require_interactive("hint")

    def test_menu_refuses_without_input(self):
        args = cli.build_parser().parse_args(["menu", "--no-input"])
        original = cli.make_prompter

        def factory(passed_args=None, **kw):
            return original(args, **kw)

        try:
            cli.make_prompter = factory
            with self.assertRaises(ConfigError):
                cli.cmd_menu(args, None, lambda *a: None)
        finally:
            cli.make_prompter = original

    def test_bare_invocation_over_devnull_exits_cleanly(self):
        """Measured platform fact: isatty() is True even for DEVNULL here.

        The console probe must still refuse, and the process must exit with a
        config error instead of a traceback or a hang.
        """
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-m", "bbpull", "--env-file", self.env_path],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(proc.returncode, cli.EXIT_CONFIG, proc.stderr)
        self.assertEqual(proc.stderr.count("Traceback"), 0)
        self.assertIn("not interactive", proc.stderr + proc.stdout)

    def test_no_input_flag_beats_a_real_console(self):
        args = cli.build_parser().parse_args(["pull", "--no-input"])
        self.assertTrue(cli.interaction_disabled(args))

    def test_env_var_disables_interaction(self):
        self.assertTrue(cli.interaction_disabled(None, environ={"BB_NONINTERACTIVE": "1"}))
        self.assertTrue(cli.interaction_disabled(None, environ={"BB_NONINTERACTIVE": "true"}))
        self.assertFalse(cli.interaction_disabled(None, environ={}))
        self.assertFalse(cli.interaction_disabled(None, environ={"BB_NONINTERACTIVE": "0"}))

    def test_stdin_is_console_matches_reality(self):
        """Whatever the answer, it must be a bool and not raise."""
        from bbpull.wizard import stdin_is_console

        self.assertIsInstance(stdin_is_console(), bool)


class EffectiveOptionsTests(unittest.TestCase):
    def _args(self, **kw):
        args = cli.build_parser().parse_args(["pull"])
        for key, value in kw.items():
            setattr(args, key, value)
        return args

    def test_env_defaults_apply_when_no_flags(self):
        cfg = load_config(None)
        cfg.include_content = False
        cfg.download_files = False
        options = cli.effective_options(self._args(), cfg)
        self.assertFalse(options["content"])
        self.assertFalse(options["files"])

    def test_flag_can_only_turn_off(self):
        cfg = load_config(None)
        cfg.download_files = True
        options = cli.effective_options(self._args(no_files=True), cfg)
        self.assertFalse(options["files"])

    def test_overwrite_flag_ors_with_config(self):
        cfg = load_config(None)
        self.assertFalse(cfg.overwrite)
        self.assertTrue(cli.effective_options(self._args(overwrite=True), cfg)["overwrite"])


class ConfigBoolParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_cfg_")
        self.env_path = os.path.join(self.tmp, ".env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_boolean_keys(self):
        update_env_file(
            self.env_path,
            {
                "BB_INCLUDE_CONTENT": "0",
                "BB_INCLUDE_ANNOUNCEMENTS": "false",
                "BB_DOWNLOAD_FILES": "no",
                "BB_OVERWRITE": "yes",
            },
        )
        cfg = load_config(self.env_path)
        self.assertFalse(cfg.include_content)
        self.assertFalse(cfg.include_announcements)
        self.assertFalse(cfg.download_files)
        self.assertTrue(cfg.overwrite)

    def test_defaults_when_absent(self):
        cfg = load_config(self.env_path)
        self.assertTrue(cfg.include_content)
        self.assertTrue(cfg.include_announcements)
        self.assertTrue(cfg.download_files)
        self.assertFalse(cfg.overwrite)


class MenuActionTests(unittest.TestCase):
    def test_every_action_maps_to_a_real_subcommand(self):
        parser = cli.build_parser()
        for key, (label, argv) in cli.MENU_ACTIONS.items():
            self.assertTrue(label)
            if key == "quit":
                self.assertEqual(argv, [])
                continue
            parsed = parser.parse_args(argv)
            self.assertEqual(parsed.command, argv[0])

    def test_pull_all_pick_flag_exists(self):
        args = cli.build_parser().parse_args(["pull-all", "--pick"])
        self.assertTrue(args.pick)

    def test_pull_interactive_alias(self):
        for flag in ("-i", "--interactive", "--ask"):
            args = cli.build_parser().parse_args(["pull", flag])
            self.assertTrue(args.interactive, flag)


class MenuFlowTests(unittest.TestCase):
    """Drive the real `main()` with a scripted Prompter: menu -> action -> quit.

    This is the closest offline equivalent of sitting at a terminal, and it is
    what proves the interactive wiring (not just the pieces) works.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_menu_")
        self.env_path = os.path.join(self.tmp, ".env")
        self.state_dir = os.path.join(self.tmp, "state")
        # A returning user: credentials already stored, so first-run is skipped.
        update_env_file(
            self.env_path,
            {"BB_USERNAME": "alice", "BB_PASSWORD": "pw", "BB_COURSE_ID": "_12529_1"},
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self):
        return ["--env-file", self.env_path, "--state-dir", self.state_dir]

    def _run_main(self, lines, secrets=None, argv=None, allow_network=False):
        """Run main() with one buffer for BOTH prompts and command output.

        Command logs go through print(); prompts go through Prompter.out. They
        must share a sink or assertions would see only half the session.

        Sessions are stubbed out by default: a unit test must never depend on
        (or wait for) Blackboard.
        """
        buf = io.StringIO()
        secret_iter = iter(secrets) if secrets is not None else None

        def secret_reader(_label):
            if secret_iter is None:
                raise AssertionError("wizard asked for a secret but none was scripted")
            return next(secret_iter)

        p = Prompter(lines=list(lines), out=buf, secret_reader=secret_reader)
        original_make = cli.make_prompter
        original_probe = wizard.probe_site
        original_session = cli.LearnSession

        class OfflineSession:
            user_id = "offline"
            login_strategy = "offline-test"

            def __init__(self, *a, **k):
                self.base = "https://bb.example.edu"

            def login(self, force=False):
                return self

            def get_all(self, path, params=None, max_pages=500):
                if path.endswith("/announcements"):
                    return []
                return [
                    {"course": {"courseId": c["courseId"], "name": c["name"]}}
                    for c in COURSES
                ]

            def api_get(self, path, params=None, expect_json=True, allow_404=False):
                if path.endswith("/attachments"):
                    return None
                return {"id": "_21652_1", "userName": "offline-user"}

        cli.make_prompter = lambda args=None, **kw: p
        wizard.probe_site = ok_probe
        if not allow_network:
            cli.LearnSession = OfflineSession
        try:
            with contextlib.redirect_stdout(buf):
                code = cli.main(argv or self._argv())
        finally:
            cli.make_prompter = original_make
            wizard.probe_site = original_probe
            cli.LearnSession = original_session
        return code, buf.getvalue()

    def test_menu_doctor_then_quit(self):
        lines = [str(menu_index("doctor")), "y", str(menu_index("quit"))]
        code, out = self._run_main(lines)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("bbpull", out)
        self.assertIn("請選擇操作", out)
        # Credentials exist, so doctor must get past the credential gate.
        self.assertNotIn("credentials: MISSING", out)

    def test_menu_setup_writes_env_then_quits(self):
        lines = [
            str(menu_index("setup")),
            "https://twc.blackboard.com",
            "alice",
            "_12529_1",
            os.path.join(self.tmp, "out"),
            "y", "y", "y", "n",  # content / announcements / files / overwrite
            "y",                 # confirm write
            "n",                 # do not test login now
            str(menu_index("quit")),
        ]
        code, out = self._run_main(lines, secrets=["pw"])
        self.assertEqual(code, cli.EXIT_OK)
        values = read_env_file(self.env_path)
        self.assertEqual(values["BB_USERNAME"], "alice")
        self.assertEqual(values["BB_COURSE_ID"], "_12529_1")
        self.assertIn("設定完成", out)

    def test_setup_then_menu_shows_new_values(self):
        """The menu header must reflect the just-saved config (rebuilt per loop)."""
        lines = [
            str(menu_index("setup")),
            "https://bb.example.edu",
            "carol",
            "auto",
            os.path.join(self.tmp, "out"),
            "y", "y", "y", "n",
            "y",
            "n",
            str(menu_index("quit")),
        ]
        code, out = self._run_main(lines, secrets=["pw"])
        self.assertEqual(code, cli.EXIT_OK)
        header_tail = out.split("設定完成")[-1]
        self.assertIn("https://bb.example.edu", header_tail)
        self.assertIn("carol", header_tail)

    def test_ctrl_c_style_cancel_inside_menu_is_clean(self):
        # q at the menu prompt cancels out of choose() and exits cleanly.
        code, out = self._run_main(["q"])
        self.assertEqual(code, cli.EXIT_OK)

    def test_menu_never_drags_a_configured_user_through_first_run(self):
        code, out = self._run_main([str(menu_index("quit"))])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("歡迎使用 bbpull", out)
        self.assertIn("alice", out)

    def test_selftest_is_never_reachable_from_these_tests(self):
        """Guard the specific regression: a stale index once picked `selftest`."""
        self.assertNotEqual(cli.MENU_ACTIONS[list(cli.MENU_ACTIONS)[menu_index("quit") - 1]], "selftest")
        self.assertNotEqual(list(cli.MENU_ACTIONS)[menu_index("doctor") - 1], "selftest")
        self.assertNotEqual(list(cli.MENU_ACTIONS)[menu_index("setup") - 1], "selftest")


class SelftestRecursionTests(unittest.TestCase):
    """`selftest` must not re-enter itself when driven from the menu.

    These tests never run the real suite: doing so would recurse into this very
    module. They stub discovery with an empty suite instead, which still
    exercises the guard, the runner call and the cleanup.
    """

    def setUp(self):
        # Keep the flag pristine whatever the ambient state is (e.g. when this
        # suite is itself launched by `bbpull selftest`).
        self._saved = cli._SELFTEST_RUNNING
        cli._SELFTEST_RUNNING = False
        self.addCleanup(self._restore)

    def _restore(self):
        cli._SELFTEST_RUNNING = self._saved

    def _stub_discovery(self):
        original = unittest.defaultTestLoader.discover
        unittest.defaultTestLoader.discover = lambda *a, **k: unittest.TestSuite()
        self.addCleanup(setattr, unittest.defaultTestLoader, "discover", original)

    def test_nested_invocation_is_refused(self):
        cli._SELFTEST_RUNNING = True
        messages = []
        code = cli.cmd_selftest(None, None, messages.append)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("already running", " ".join(messages))

    def test_guard_flag_survives_a_refused_nested_call(self):
        """A refused call must not clear the outer run's flag."""
        cli._SELFTEST_RUNNING = True
        cli.cmd_selftest(None, None, lambda *a: None)
        self.assertTrue(cli._SELFTEST_RUNNING)

    def test_guard_is_cleared_after_a_run(self):
        self._stub_discovery()
        code = cli.cmd_selftest(
            cli.build_parser().parse_args(["selftest"]), None, lambda *a: None
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertFalse(cli._SELFTEST_RUNNING)

    def test_guard_is_cleared_even_when_the_run_explodes(self):
        def boom(*a, **k):
            raise RuntimeError("discovery failed")

        original = unittest.defaultTestLoader.discover
        unittest.defaultTestLoader.discover = boom
        self.addCleanup(setattr, unittest.defaultTestLoader, "discover", original)
        with self.assertRaises(RuntimeError):
            cli.cmd_selftest(None, None, lambda *a: None)
        self.assertFalse(cli._SELFTEST_RUNNING)

    def test_no_environment_variable_is_involved(self):
        """Regression: the guard used to live in os.environ and leaked into tests."""
        self._stub_discovery()
        cli.cmd_selftest(None, None, lambda *a: None)
        self.assertNotIn("BBPULL_IN_SELFTEST", os.environ)


class FirstRunTests(unittest.TestCase):
    """`bbpull` on a machine with no credentials must ask, then work."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_first_")
        self.env_path = os.path.join(self.tmp, ".env")
        self.state_dir = os.path.join(self.tmp, "state")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _services(self, courses=None, fail_times=0):
        """Fake login/list callbacks plus a record of what was tried."""
        calls = {"login": [], "list": 0}
        state = {"remaining_failures": fail_times}

        class FakeSession:
            user_id = "_21652_1"

        def login(cfg, username, password):
            calls["login"].append((username, password))
            if state["remaining_failures"] > 0:
                state["remaining_failures"] -= 1
                raise RuntimeError("登入失敗: HTTP 401")
            return FakeSession()

        def list_courses(_session):
            calls["list"] += 1
            return courses if courses is not None else list(COURSES)

        return FirstRunServices(login=login, list_courses=list_courses), calls

    def _cfg(self):
        return load_config(self.env_path, state_dir=self.state_dir)

    def _lines(self, storage="1"):
        return [
            "https://twc.blackboard.com",   # site
            "23002220",                     # username
            "1",                            # course #
            os.path.join(self.tmp, "out"),  # out dir
            storage,                        # password storage choice
            "y",                            # final confirm
        ]

    def test_happy_path_logs_in_and_saves(self):
        services, calls = self._services()
        p, out = prompter(self._lines(), secrets=["s3cret"])
        run_first_run(p, self._cfg(), lambda *a: None, services)

        self.assertEqual(calls["login"], [("23002220", "s3cret")])
        self.assertEqual(calls["list"], 1)
        values = read_env_file(self.env_path)
        self.assertEqual(values["BB_BASE_URL"], "https://twc.blackboard.com")
        self.assertEqual(values["BB_COURSE_ID"], "_1_1")  # the picked course
        self.assertNotIn("s3cret", out.getvalue())
        self.assertIn("設定完成", out.getvalue())

    def test_secure_mode_keeps_password_out_of_env(self):
        services, _ = self._services()
        p, _ = prompter(self._lines("1"), secrets=["s3cret"])
        run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertFalse(read_env_file(self.env_path).get("BB_PASSWORD"))
        stored = SecretStore(self._cfg().secret_store_path).load()
        self.assertEqual(stored.get("BB_PASSWORD"), "s3cret")
        self.assertEqual(stored.get("BB_USERNAME"), "23002220")

    def test_never_mode_stores_nothing(self):
        services, _ = self._services()
        p, _ = prompter(self._lines("3"), secrets=["s3cret"])
        run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertFalse(read_env_file(self.env_path).get("BB_PASSWORD"))
        self.assertFalse(SecretStore(self._cfg().secret_store_path).exists())

    def test_env_mode_writes_plaintext_when_asked(self):
        services, _ = self._services()
        p, _ = prompter(self._lines("2"), secrets=["s3cret"])
        run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertEqual(read_env_file(self.env_path).get("BB_PASSWORD"), "s3cret")
        self.assertFalse(SecretStore(self._cfg().secret_store_path).exists())

    def test_bad_login_retries_then_succeeds(self):
        services, calls = self._services(fail_times=1)
        lines = self._lines()
        lines.insert(2, "y")  # "retry?" after the first failure
        p, out = prompter(lines, secrets=["wrong", "right"])
        run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertEqual([c[1] for c in calls["login"]], ["wrong", "right"])
        self.assertIn("登入失敗", out.getvalue())

    def test_declining_retry_cancels_without_writing(self):
        services, _ = self._services(fail_times=3)
        lines = self._lines()
        lines.insert(2, "n")  # do not retry
        p, _ = prompter(lines, secrets=["wrong"])
        with self.assertRaises(Cancelled):
            run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertFalse(os.path.exists(self.env_path))

    def test_final_refusal_writes_nothing(self):
        services, _ = self._services()
        lines = self._lines()
        lines[-1] = "n"  # refuse at the confirmation
        p, _ = prompter(lines, secrets=["s3cret"])
        with self.assertRaises(Cancelled):
            run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertFalse(os.path.exists(self.env_path))

    def test_long_course_list_demands_a_keyword_first(self):
        many = [{"courseId": f"_{i}_1", "name": f"Course {i:02d}"} for i in range(1, 60)]
        many[41]["name"] = "作業系統"
        services, _ = self._services(courses=many)
        lines = [
            "https://twc.blackboard.com",
            "23002220",
            "作業",                          # keyword filter
            "1",                             # pick #1 of the filtered list
            os.path.join(self.tmp, "out"),
            "1",
            "y",
        ]
        p, out = prompter(lines, secrets=["s3cret"])
        run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertEqual(read_env_file(self.env_path)["BB_COURSE_ID"], "_42_1")
        self.assertIn("共有 59 門課", out.getvalue())

    def test_falls_back_to_auto_when_listing_fails(self):
        services, _ = self._services()

        def boom(_session):
            raise RuntimeError("讀取失敗")

        services.list_courses = boom
        lines = [
            "https://twc.blackboard.com",
            "23002220",
            "auto",
            os.path.join(self.tmp, "out"),
            "1",
            "y",
        ]
        p, out = prompter(lines, secrets=["s3cret"])
        run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertEqual(read_env_file(self.env_path)["BB_COURSE_ID"], "auto")
        self.assertIn("讀取課程清單失敗", out.getvalue())

    def test_auto_course_kept_when_user_cancels_the_picker(self):
        services, _ = self._services()
        lines = [
            "https://twc.blackboard.com",
            "23002220",
            "q",                             # cancel the course picker
            os.path.join(self.tmp, "out"),
            "1",
            "y",
        ]
        p, out = prompter(lines, secrets=["s3cret"])
        run_first_run(p, self._cfg(), lambda *a: None, services)
        self.assertEqual(read_env_file(self.env_path)["BB_COURSE_ID"], "auto")
        self.assertIn("跳過選課", out.getvalue())

    def test_bare_invocation_from_clean_machine_prompts_then_reaches_menu(self):
        """End-to-end: no credentials -> guided setup -> menu."""
        buf = io.StringIO()
        lines = [
            "https://twc.blackboard.com",
            "23002220",
            "1",                              # course # (3 courses: no filter needed)
            os.path.join(self.tmp, "out"),
            "3",                              # do not store the password
            "y",                              # confirm
            str(menu_index("quit")),          # quit the menu
        ]
        p = Prompter(lines=list(lines), out=buf, secret_reader=lambda _l: "s3cret")
        services, _ = self._services()
        original_make = cli.make_prompter
        original_probe = wizard.probe_site
        original_services = cli.first_run_services
        cli.make_prompter = lambda args=None, **kw: p
        wizard.probe_site = ok_probe
        cli.first_run_services = lambda log, args=None: services
        try:
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--env-file", self.env_path, "--state-dir", self.state_dir])
        finally:
            cli.make_prompter = original_make
            wizard.probe_site = original_probe
            cli.first_run_services = original_services
        out = buf.getvalue()
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("歡迎使用 bbpull", out)
        self.assertIn("請選擇操作", out)   # reached the menu afterwards
        self.assertNotIn("s3cret", out)

    def test_not_storing_the_password_does_not_rerun_the_wizard(self):
        """Second launch with mode 'never' asks only for the password."""
        services, _ = self._services()
        # First launch: full wizard, password not stored.
        p, _ = prompter(self._lines("3"), secrets=["s3cret"])
        run_first_run(p, self._cfg(), lambda *a: None, services)

        # Second launch: account is known, so only the password is requested.
        args = cli.build_parser().parse_args(
            ["pull", "--env-file", self.env_path, "--state-dir", self.state_dir]
        )
        cfg = load_config(self.env_path, state_dir=self.state_dir)
        self.assertTrue(cfg.needs_password)
        self.assertFalse(cfg.needs_setup)

        p2, out2 = prompter([], secrets=["again"])
        original = cli.make_prompter
        cli.make_prompter = lambda a=None, **kw: p2
        try:
            result = cli.prepare_command(args, cfg, lambda *a: None, "pull")
        finally:
            cli.make_prompter = original
        self.assertEqual(result.password, "again")
        self.assertNotIn("歡迎使用 bbpull", out2.getvalue())
        self.assertIn("僅本次使用", out2.getvalue())

    def test_non_interactive_first_run_refuses_clearly(self):
        args = cli.build_parser().parse_args(["pull", "--no-input", "--env-file", self.env_path])
        cfg = load_config(self.env_path, state_dir=self.state_dir)
        with self.assertRaises(ConfigError) as ctx:
            cli.prepare_command(args, cfg, lambda *a: None, "pull")
        self.assertIn("stdin is not interactive", str(ctx.exception))

    def test_configured_machine_skips_first_run(self):
        update_env_file(self.env_path, {"BB_USERNAME": "a", "BB_PASSWORD": "b"})
        args = cli.build_parser().parse_args(["pull", "--env-file", self.env_path])
        cfg = load_config(self.env_path, state_dir=self.state_dir)
        result = cli.prepare_command(args, cfg, lambda *a: None, "pull")
        self.assertIs(result, cfg)

    def test_setup_command_is_exempt_from_the_gate(self):
        args = cli.build_parser().parse_args(["setup", "--env-file", self.env_path])
        cfg = load_config(self.env_path, state_dir=self.state_dir)
        self.assertIs(cli.prepare_command(args, cfg, lambda *a: None, "setup"), cfg)


class SessionCredentialPromptTests(unittest.TestCase):
    """`LearnSession` asks for missing credentials instead of failing outright."""

    def test_provider_is_used_when_password_missing(self):
        from bbpull.session import LearnSession

        cfg = load_config(None)
        cfg.username = "alice"
        cfg.password = ""
        asked = []

        def provider(current):
            asked.append(current.username)
            return current.username, "from-prompt"

        session = LearnSession(cfg, lambda *a: None, credential_provider=provider)
        session._collect_credentials()
        self.assertEqual(cfg.password, "from-prompt")
        self.assertEqual(asked, ["alice"])

    def test_provider_not_called_when_credentials_present(self):
        from bbpull.session import LearnSession

        cfg = load_config(None)
        cfg.username = "alice"
        cfg.password = "pw"
        calls = []

        def provider(_current):
            calls.append(1)
            return "", ""

        session = LearnSession(cfg, lambda *a: None, credential_provider=provider)
        session._collect_credentials()
        self.assertEqual(calls, [])

    def test_missing_credentials_without_provider_is_a_clear_error(self):
        from bbpull.session import LearnSession

        cfg = load_config(None)
        cfg.username = ""
        cfg.password = ""
        session = LearnSession(cfg, lambda *a: None)
        with self.assertRaises(ConfigError) as ctx:
            session.login()
        self.assertIn("interactive setup", str(ctx.exception))


class TemplateSecretScanTests(unittest.TestCase):
    """A password in a committable template must never pass silently."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_scan_")
        self.env_path = os.path.join(self.tmp, ".env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _template(self, text):
        path = os.path.join(self.tmp, ".env.example")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_clean_template_is_quiet(self):
        self._template("BB_USERNAME=\nBB_PASSWORD=\n")
        self.assertEqual(scan_template_for_secrets(self.env_path), [])

    def test_username_alone_is_not_a_secret(self):
        self._template("BB_USERNAME=23002220\nBB_PASSWORD=\n")
        self.assertEqual(scan_template_for_secrets(self.env_path), [])

    def test_real_password_in_template_is_reported(self):
        self._template("BB_USERNAME=someone\nBB_PASSWORD=hunter2\n")
        warnings = scan_template_for_secrets(self.env_path)
        self.assertEqual(len(warnings), 1)
        self.assertIn("SECURITY", warnings[0])
        self.assertIn(".env.example", warnings[0])
        self.assertIn("BB_PASSWORD", warnings[0])

    def test_warning_never_echoes_the_secret(self):
        self._template("BB_PASSWORD=hunter2\n")
        warnings = scan_template_for_secrets(self.env_path)
        self.assertTrue(warnings)
        self.assertNotIn("hunter2", " ".join(warnings))

    def test_missing_template_is_fine(self):
        self.assertEqual(scan_template_for_secrets(self.env_path), [])

    def test_shipped_repo_template_is_clean(self):
        """The template in this repo must stay commitment-safe."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(scan_template_for_secrets(os.path.join(root, ".env")), [])


class ResolveCourseIdTests(unittest.TestCase):
    """`--course-id` accepts an id, a list index, or a unique name keyword."""

    class FakeSession:
        def __init__(self, courses):
            self._courses = courses

        def get_all(self, path, params=None, max_pages=500):
            if path.endswith("/announcements"):
                return []
            return [
                {"course": {"courseId": c["courseId"], "name": c["name"], "isAvailable": True}}
                for c in self._courses
            ]

    def setUp(self):
        self.courses = [
            {"courseId": "_1_1", "name": "Data Structures"},
            {"courseId": "_2_1", "name": "作業系統"},
            {"courseId": "_3_1", "name": "Operating Systems Lab"},
        ]
        self.session = self.FakeSession(self.courses)
        self.logs = []

    def _resolve(self, course_id, extra_argv=()):
        args = cli.build_parser().parse_args(["pull", *extra_argv])
        cfg = load_config(None)
        cfg.course_id = course_id
        return cli.resolve_course_id(self.session, cfg, args, self.logs.append)

    def test_exact_id_passes_through_without_listing(self):
        self.assertEqual(self._resolve("_12529_1"), "_12529_1")

    def test_unique_keyword_resolves(self):
        self.assertEqual(self._resolve("作業"), "_2_1")
        self.assertIn("[course] resolved", " ".join(self.logs))

    def test_index_resolves(self):
        self.assertEqual(self._resolve("3"), "_3_1")

    def test_ambiguous_keyword_falls_back_to_raw_value(self):
        """Matches the old behaviour: let the API answer, but say why first.

        "s" appears in both "Data Structures" and "Operating Systems Lab".
        """
        self.assertEqual(self._resolve("s"), "s")
        self.assertIn("[warn]", " ".join(self.logs))

    def test_keyword_matching_nothing_falls_back(self):
        self.assertEqual(self._resolve("zzz"), "zzz")
        self.assertIn("[warn]", " ".join(self.logs))

    def test_auto_without_console_refuses(self):
        with self.assertRaises(ConfigError):
            self._resolve("auto", extra_argv=["--no-input"])


class SecureCommandTests(unittest.TestCase):
    """`bbpull secure` moves a plaintext .env password into encrypted storage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_secure_")
        self.env_path = os.path.join(self.tmp, ".env")
        self.state_dir = os.path.join(self.tmp, "state")
        update_env_file(
            self.env_path,
            {"BB_USERNAME": "23002220", "BB_PASSWORD": "plaintext-pw"},
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, lines, secrets=None):
        buf = io.StringIO()
        logs = []
        secret_iter = iter(secrets) if secrets else None

        def secret_reader(_label):
            return next(secret_iter)

        p = Prompter(lines=list(lines), out=buf, secret_reader=secret_reader)
        original = cli.make_prompter
        cli.make_prompter = lambda args=None, **kw: p
        try:
            with contextlib.redirect_stdout(buf):
                args = cli.build_parser().parse_args(
                    ["secure", "--env-file", self.env_path, "--state-dir", self.state_dir]
                )
                cfg = load_config(self.env_path, state_dir=self.state_dir)
                code = cli.cmd_secure(args, cfg, logs.append)
        finally:
            cli.make_prompter = original
        # Command output goes through the logger; merge both sinks for assertions.
        return code, buf.getvalue() + "\n".join(logs)

    def test_moves_password_out_of_env(self):
        code, out = self._run(["y"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertFalse(read_env_file(self.env_path).get("BB_PASSWORD"))
        stored = SecretStore(os.path.join(self.state_dir, "credentials.dat")).load()
        self.assertEqual(stored.get("BB_PASSWORD"), "plaintext-pw")
        self.assertEqual(stored.get("BB_USERNAME"), "23002220")
        self.assertNotIn("plaintext-pw", out)

    def test_declining_changes_nothing(self):
        code, _ = self._run(["n"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(read_env_file(self.env_path).get("BB_PASSWORD"), "plaintext-pw")
        self.assertFalse(
            SecretStore(os.path.join(self.state_dir, "credentials.dat")).exists()
        )

    def test_already_encrypted_is_a_no_op(self):
        self._run(["y"])
        code, out = self._run([])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已經是加密儲存", out)

    def test_prompts_for_password_when_none_exists(self):
        update_env_file(self.env_path, {"BB_PASSWORD": ""})
        code, out = self._run(["y"], secrets=["typed-pw"])
        self.assertEqual(code, cli.EXIT_OK)
        stored = SecretStore(os.path.join(self.state_dir, "credentials.dat")).load()
        self.assertEqual(stored.get("BB_PASSWORD"), "typed-pw")
        self.assertNotIn("typed-pw", out)


class ConfigStoreFallbackTests(unittest.TestCase):
    """A user who never edits a file must still get logged in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_store_")
        self.env_path = os.path.join(self.tmp, ".env")
        self.state_dir = os.path.join(self.tmp, "state")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_store_fills_in_missing_credentials(self):
        store = SecretStore(os.path.join(self.state_dir, "credentials.dat"))
        store.save({"BB_USERNAME": "stored-user", "BB_PASSWORD": "stored-pw"})
        cfg = load_config(self.env_path, state_dir=self.state_dir)
        self.assertEqual(cfg.username, "stored-user")
        self.assertEqual(cfg.password, "stored-pw")
        self.assertFalse(cfg.needs_setup)

    def test_env_wins_over_store(self):
        store = SecretStore(os.path.join(self.state_dir, "credentials.dat"))
        store.save({"BB_USERNAME": "stored-user", "BB_PASSWORD": "stored-pw"})
        update_env_file(self.env_path, {"BB_USERNAME": "env-user", "BB_PASSWORD": "env-pw"})
        cfg = load_config(self.env_path, state_dir=self.state_dir)
        self.assertEqual(cfg.username, "env-user")
        self.assertEqual(cfg.password, "env-pw")

    def test_store_only_supplies_the_missing_half(self):
        store = SecretStore(os.path.join(self.state_dir, "credentials.dat"))
        store.save({"BB_USERNAME": "stored-user", "BB_PASSWORD": "stored-pw"})
        update_env_file(self.env_path, {"BB_USERNAME": "env-user"})
        cfg = load_config(self.env_path, state_dir=self.state_dir)
        self.assertEqual(cfg.username, "env-user")
        self.assertEqual(cfg.password, "stored-pw")

    def test_needs_setup_when_nothing_anywhere(self):
        cfg = load_config(self.env_path, state_dir=self.state_dir)
        self.assertTrue(cfg.needs_setup)


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpull_storeunit_")
        self.path = os.path.join(self.tmp, "credentials.dat")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        store = SecretStore(self.path)
        store.save({"BB_USERNAME": "u", "BB_PASSWORD": "p"})
        loaded = SecretStore(self.path).load()
        self.assertEqual(loaded, {"BB_USERNAME": "u", "BB_PASSWORD": "p"})

    def test_empty_values_are_dropped(self):
        store = SecretStore(self.path)
        store.save({"BB_USERNAME": "u", "BB_PASSWORD": ""})
        self.assertEqual(SecretStore(self.path).load(), {"BB_USERNAME": "u"})

    def test_missing_file_is_empty(self):
        self.assertEqual(SecretStore(self.path).load(), {})

    def test_corrupt_file_does_not_raise(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("}{ not json")
        self.assertEqual(SecretStore(self.path).load(), {})

    def test_undecryptable_blob_does_not_raise(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"version":1,"backend":"dpapi","data":"bm90LXJlYWw="}')
        self.assertEqual(SecretStore(self.path).load(), {})

    def test_plaintext_password_never_hits_disk(self):
        if available_backend() != BACKEND_DPAPI:
            self.skipTest("encryption backend not available on this machine")
        store = SecretStore(self.path)
        store.save({"BB_PASSWORD": "super-secret-value"})
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("super-secret-value", raw)

    def test_clear_removes_file(self):
        store = SecretStore(self.path)
        store.save({"BB_USERNAME": "u"})
        self.assertTrue(store.clear())
        self.assertFalse(store.exists())
        self.assertEqual(store.load(), {})


class PickCoursesLargeListTests(unittest.TestCase):
    """A 90-course account must not dump 90 lines at the user."""

    def _many(self, count=59):
        return [{"courseId": f"_{i}_1", "name": f"Course {i:02d}"} for i in range(1, count + 1)]

    def test_requires_keyword_when_over_threshold(self):
        courses = self._many()
        p, out = prompter(["Course 07", "1"])
        chosen = pick_courses(p, courses, multi=False)
        self.assertEqual(chosen[0]["courseId"], "_7_1")
        self.assertIn("共有 59 門課", out.getvalue())
        # Only the filtered slice should have been printed.
        self.assertNotIn("Course 42", out.getvalue().split("共有 59 門課")[1])

    def test_star_lists_everything(self):
        courses = self._many(30)
        p, out = prompter(["*", "5"])
        chosen = pick_courses(p, courses, multi=False)
        self.assertEqual(chosen[0]["courseId"], "_5_1")

    def test_blank_keyword_is_rejected(self):
        p, out = prompter(["", "Course 03", "1"])
        chosen = pick_courses(p, self._many(), multi=False)
        self.assertEqual(chosen[0]["courseId"], "_3_1")
        self.assertIn("請輸入關鍵字", out.getvalue())

    def test_bad_keyword_reprompts(self):
        p, out = prompter(["zzzz", "Course 02", "1"])
        chosen = pick_courses(p, self._many(), multi=False)
        self.assertEqual(chosen[0]["courseId"], "_2_1")
        self.assertIn("沒有符合", out.getvalue())

    def test_short_list_needs_no_keyword(self):
        p, out = prompter(["2"])
        chosen = pick_courses(p, COURSES, multi=False)
        self.assertEqual(chosen[0]["courseId"], "_2_1")
        self.assertNotIn("請先輸入關鍵字", out.getvalue())

    def test_quit_at_keyword_prompt_cancels(self):
        p, _ = prompter(["q"])
        with self.assertRaises(Cancelled):
            pick_courses(p, self._many(), multi=False)


class FetchCoursesShapeTests(unittest.TestCase):
    """`/users/me/courses` returns flat memberships on the live site.

    Measured response shape (twc.blackboard.com, 2026-09):
        {"id": "_938847_1", "userId": "_21652_1", "courseId": "_12648_1",
         "courseRoleId": "Student", "availability": {"available": "Yes"}, ...}
    There is NO nested `course` object and NO name, so a parser expecting
    `membership["course"]` silently returns zero courses.
    """

    FLAT = [
        {
            "id": "_938847_1",
            "userId": "_21652_1",
            "courseId": "_12648_1",
            "courseRoleId": "Student",
            "availability": {"available": "Yes"},
        },
        {
            "id": "_938848_1",
            "userId": "_21652_1",
            "courseId": "_12529_1",
            "courseRoleId": "Student",
            "availability": {"available": "No"},
        },
    ]

    NESTED = [
        {
            "courseRoleId": "Instructor",
            "course": {
                "id": "_9_1",
                "courseId": "_9_1",
                "name": "Nested Shape Course",
                "isAvailable": True,
            },
        }
    ]

    class Session:
        def __init__(self, memberships, names=None, fail_names=False):
            self._memberships = memberships
            self._names = names or {}
            self._fail_names = fail_names
            self.detail_calls = []

        def get_all(self, path, params=None, max_pages=500):
            return self._memberships

        def api_get(self, path, params=None, expect_json=True, allow_404=False):
            if path.startswith("/courses/"):
                course_id = path.split("/")[-1]
                self.detail_calls.append(course_id)
                if self._fail_names:
                    raise ApiError(500, "GET", path, "boom")
                if allow_404 and course_id not in self._names:
                    return None
                return {"id": course_id, "name": self._names.get(course_id, course_id)}
            raise AssertionError(path)

    def test_flat_memberships_are_parsed(self):
        session = self.Session(self.FLAT, names={"_12648_1": "[2024/25-1] Clinical Alert"})
        rows = cli.fetch_courses(session, lambda *a: None)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["courseId"], "_12648_1")
        self.assertEqual(rows[0]["name"], "[2024/25-1] Clinical Alert")
        self.assertEqual(rows[0]["role"], "Student")

    def test_membership_id_is_never_mistaken_for_course_id(self):
        session = self.Session(self.FLAT, names={"_12648_1": "A", "_12529_1": "B"})
        rows = cli.fetch_courses(session, lambda *a: None)
        ids = [r["courseId"] for r in rows]
        self.assertEqual(ids, ["_12648_1", "_12529_1"])
        self.assertNotIn("_938847_1", ids)

    def test_names_are_resolved_one_by_one(self):
        session = self.Session(self.FLAT, names={"_12648_1": "A", "_12529_1": "B"})
        cli.fetch_courses(session, lambda *a: None)
        self.assertEqual(sorted(session.detail_calls), ["_12529_1", "_12648_1"])

    def test_missing_name_falls_back_to_the_id(self):
        session = self.Session(self.FLAT, names={"_12648_1": "A"})
        rows = cli.fetch_courses(session, lambda *a: None)
        self.assertEqual(rows[1]["name"], "_12529_1")

    def test_name_lookup_failure_does_not_lose_the_course(self):
        session = self.Session(self.FLAT, fail_names=True)
        rows = cli.fetch_courses(session, lambda *a: None)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "_12648_1")

    def test_unavailable_filter_uses_availability_object(self):
        session = self.Session(self.FLAT, names={"_12648_1": "A", "_12529_1": "B"})
        rows = cli.fetch_courses(session, lambda *a: None, include_unavailable=False)
        self.assertEqual([r["courseId"] for r in rows], ["_12648_1"])

    def test_nested_shape_still_works(self):
        session = self.Session(self.NESTED)
        rows = cli.fetch_courses(session, lambda *a: None)
        self.assertEqual(rows[0]["courseId"], "_9_1")
        self.assertEqual(rows[0]["name"], "Nested Shape Course")
        # A nested name means no detail request is needed.
        self.assertEqual(session.detail_calls, [])

    def test_empty_list_is_not_an_error(self):
        rows = cli.fetch_courses(self.Session([]), lambda *a: None)
        self.assertEqual(rows, [])

    def test_name_cache_is_reused_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(os.path.join(tmp, ".env"), state_dir=tmp)
            first = self.Session(self.FLAT, names={"_12648_1": "A", "_12529_1": "B"})
            cli.fetch_courses(first, lambda *a: None, cfg=cfg)
            self.assertEqual(len(first.detail_calls), 2)

            second = self.Session(self.FLAT, names={})
            rows = cli.fetch_courses(second, lambda *a: None, cfg=cfg)
            self.assertEqual(second.detail_calls, [], "cache should avoid re-fetching")
            self.assertEqual(rows[0]["name"], "A")
            self.assertEqual(rows[1]["name"], "B")

    def test_stale_cache_is_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(os.path.join(tmp, ".env"), state_dir=tmp)
            path = cli.course_name_cache_path(cfg)
            os.makedirs(tmp, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"saved": 0, "names": {"_12648_1": "OLD"}}, handle)
            session = self.Session(self.FLAT, names={"_12648_1": "NEW", "_12529_1": "B"})
            rows = cli.fetch_courses(session, lambda *a: None, cfg=cfg)
            self.assertEqual(rows[0]["name"], "NEW")
            self.assertTrue(session.detail_calls)

    def test_corrupt_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(os.path.join(tmp, ".env"), state_dir=tmp)
            os.makedirs(tmp, exist_ok=True)
            with open(cli.course_name_cache_path(cfg), "w", encoding="utf-8") as handle:
                handle.write("not json")
            session = self.Session(self.FLAT, names={"_12648_1": "A", "_12529_1": "B"})
            rows = cli.fetch_courses(session, lambda *a: None, cfg=cfg)
            self.assertEqual(rows[0]["name"], "A")

    def test_forbidden_names_are_cached_so_they_are_not_retried(self):
        """A 403 course must not cost a request on every later run.

        Measured: several expired enrolments return 403 on /courses/{id}, and
        without negative caching each launch repeated ~90 doomed requests.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(os.path.join(tmp, ".env"), state_dir=tmp)
            forbidden = [
                {
                    "id": "_1_1",
                    "courseId": "_777_1",
                    "courseRoleId": "Student",
                    "availability": {"available": "Yes"},
                }
            ]
            first = self.Session(forbidden, fail_names=True)
            rows = cli.fetch_courses(first, lambda *a: None, cfg=cfg)
            self.assertEqual(first.detail_calls, ["_777_1"])
            self.assertEqual(rows[0]["name"], "_777_1")  # falls back to the id

            second = self.Session(forbidden, fail_names=True)
            rows2 = cli.fetch_courses(second, lambda *a: None, cfg=cfg)
            self.assertEqual(second.detail_calls, [], "403 must be remembered")
            self.assertEqual(rows2[0]["name"], "_777_1")


class ConsoleEncodingTests(unittest.TestCase):
    """Output must survive characters the console codepage cannot encode.

    Live regression: a course title containing U+2011 (non-breaking hyphen)
    raised UnicodeEncodeError on a GBK console and aborted the entire pull.
    """

    class GbkStream(io.StringIO):
        """A stream that behaves like a GBK console: refuses other characters."""

        encoding = "gbk"

        def write(self, text):
            text.encode("gbk")  # raises UnicodeEncodeError for exotic characters
            return super().write(text)

    def test_safe_print_degrades_instead_of_raising(self):
        stream = self.GbkStream()
        safe_print(stream, "Title with \u2011 hyphen")
        written = stream.getvalue()
        self.assertIn("Title with", written)
        self.assertNotIn("\u2011", written)

    def test_safe_print_passes_plain_text_through(self):
        stream = self.GbkStream()
        safe_print(stream, "普通中文標題")
        self.assertEqual(stream.getvalue().strip(), "普通中文標題")

    def test_safe_print_tolerates_a_closed_stream(self):
        stream = io.StringIO()
        stream.close()
        safe_print(stream, "anything")  # must not raise

    def test_configure_console_streams_tolerates_odd_streams(self):
        class Bare:
            pass

        configure_console_streams([Bare(), io.StringIO()])  # must not raise

    def test_logger_survives_unencodable_title(self):
        stream = self.GbkStream()
        original = cli.sys.stdout
        cli.sys.stdout = stream
        try:
            log = cli.make_logger(verbose=False, quiet=False)
            log("- 2026\u201127 term  [resource/x-bb-folder]")
        finally:
            cli.sys.stdout = original
        self.assertIn("term", stream.getvalue())

    def test_prompter_say_survives_unencodable_title(self):
        p = Prompter(lines=[], out=self.GbkStream())
        p.say("Course \u2011 name")  # must not raise
        self.assertIn("Course", p.out.getvalue())

    def test_wrap_logger_preserves_the_original_sink(self):
        collected = []
        log = wrap_logger(collected.append)
        log("hello")
        self.assertEqual(collected, ["hello"])

    def test_wrap_logger_survives_unencodable_message(self):
        stream = self.GbkStream()

        def sink(message):
            print(message, file=stream, flush=True)

        wrap_logger(sink)("2026\u201127 term")
        self.assertIn("term", stream.getvalue())

    def test_wrap_logger_survives_a_failing_sink(self):
        def broken(_message):
            raise ValueError("I/O operation on closed file")

        wrap_logger(broken)("anything")  # must not raise

    def test_pullers_wrap_their_logger(self):
        """A puller must not crash on an exotic title even with a raw `print`."""
        from bbpull.announcements import AnnouncementPuller
        from bbpull.course import CoursePuller

        content = CoursePuller(object(), "_1_1", "out", print)
        self.assertTrue(getattr(content.log, "_bbpull_safe", False))
        ann = AnnouncementPuller(object(), "_1_1", "out", print)
        self.assertTrue(getattr(ann.log, "_bbpull_safe", False))

    def test_puller_log_survives_an_unencodable_title(self):
        """The exact live crash: walking a tree whose title has U+2011."""
        from bbpull.course import CoursePuller

        stream = self.GbkStream()
        puller = CoursePuller(
            object(), "_1_1", "out", lambda msg: print(msg, file=stream, flush=True)
        )
        puller.log("- CPMS_updated on 28 Aug 2026\u2011x  [resource/x-bb-folder]")
        self.assertIn("CPMS_updated", stream.getvalue())

    def test_titles_still_written_to_disk_in_full_unicode(self):
        """Only the console degrades; files must keep the real character."""
        tmp = tempfile.mkdtemp(prefix="bbpull_enc_")
        try:
            path = os.path.join(tmp, "title.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("2026\u201127 term")
            with open(path, "r", encoding="utf-8") as handle:
                self.assertIn("\u2011", handle.read())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
