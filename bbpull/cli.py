"""bbpull entry point.

Commands
--------
  setup          interactive configuration wizard (writes .env)
  menu           interactive main menu (also: run `bbpull` with no arguments)
  login          verify credentials against Blackboard and cache the session
  courses        list every course this account is enrolled in
  pull           pull one course: class content tree + announcements + files
  pull-all       pull every enrolled course (skips ones already finished)
  list-content   print the content outline without downloading
  doctor         show resolved (redacted) configuration and connectivity
  selftest       run the offline unit suite

Exit codes
----------
  0 success   2 config error   3 login error   4 API error   5 partial failures
"""

import argparse
import json
import os
import sys
import time
from collections import OrderedDict
from dataclasses import replace
from functools import partial

from . import __version__
from .announcements import AnnouncementPuller
from .config import (
    COURSE_ID_RE,
    build_config,
    load_config,
    read_env_file,
    scan_template_for_secrets,
)
from .secrets_store import backend_description
from .course import CoursePuller
from .errors import ApiError, BBPullError, ConfigError, LoginError
from .logging_util import configure_console_streams, safe_print, wrap_logger
from .paths import long_path, sanitize
from .session import LearnSession
from .wizard import (
    Cancelled,
    FirstRunServices,
    Prompter,
    clear_env_password,
    course_table_lines,
    filter_courses,
    offer_login_check,
    pick_courses,
    run_first_run,
    run_setup_wizard,
    validate_username,
)
EXIT_OK = 0
EXIT_CONFIG = 2
#: Contradictory flags (e.g. --tk with --qt). Same value as EXIT_CONFIG because
#: argparse already exits 2 on bad arguments, so the documented table holds.
EXIT_USAGE = 2
EXIT_LOGIN = 3
EXIT_API = 4
EXIT_PARTIAL = 5


# --------------------------------------------------------------------- utils
def make_logger(verbose, quiet):
    def log(message):
        if quiet:
            return
        safe_print(sys.stdout, message)

    return log


def interaction_disabled(args=None, environ=None):
    """Honour the two explicit opt-outs for interactive prompts.

    `--no-input` is the standard CLI escape hatch; `BB_NONINTERACTIVE=1` covers
    schedulers and agents that cannot pass flags. Detection alone is not enough,
    because piped stdin can stay open forever.
    """
    environ = os.environ if environ is None else environ
    if args is not None and getattr(args, "no_input", False):
        return True
    raw = str(environ.get("BB_NONINTERACTIVE", "")).strip().lower()
    return raw in ("1", "true", "yes", "y", "on")


def make_prompter(args=None, **kwargs):
    """Build a Prompter, applying this run's interaction policy."""
    if interaction_disabled(args):
        kwargs.setdefault("interactive", False)
    return Prompter(**kwargs)


def prompt_for_credentials(prompter):
    """Credential provider for LearnSession: ask instead of demanding a file."""

    def provider(cfg):
        if not cfg.username:
            cfg.username = prompter.ask("Blackboard 帳號", validator=validate_username)
        prompter.say("請輸入密碼（不會顯示在畫面上）")
        cfg.password = prompter.ask_secret("密碼", has_current=False)
        return cfg.username, cfg.password

    return provider


def make_session(cfg, log, args=None):
    """Create a session that can ask for credentials when they are missing."""
    prompter = make_prompter(args)
    provider = None
    if prompter.interactive:
        provider = prompt_for_credentials(prompter)
    return LearnSession(cfg, log, credential_provider=provider)


def human_bytes(num):
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


# ------------------------------------------------------------------ commands
def cmd_login(args, cfg, log):
    session = make_session(cfg, log, args)
    session.login(force=args.force)
    log("")
    log(f"OK  authenticated against {cfg.base_url}")
    log(f"    user id : {session.user_id}")
    log(f"    strategy: {session.login_strategy}")
    log(f"    cookies : {cfg.cookie_jar_path}")
    return EXIT_OK


def fetch_courses(session, log, include_unavailable=True, with_names=True, cfg=None):
    """Return the enrolment list as normalised rows.

    `/users/me/courses` returns *memberships*. Measured against the live site,
    a membership carries `courseId` / `courseRoleId` at the TOP LEVEL and has no
    nested `course` object, so the name must be fetched separately from
    `/courses/{id}`. Older/other builds do nest a `course` object, so both
    shapes are accepted.
    """
    log("[courses] reading enrolment list")
    memberships = session.get_all("/users/me/courses")
    if not memberships:
        log("[courses] the API returned no courses for this account")
        return []

    rows = []
    for membership in memberships:
        nested = membership.get("course") or {}
        course_id = nested.get("courseId") or nested.get("id") or membership.get("courseId")
        if not course_id:
            continue
        availability = membership.get("availability") or {}
        available = nested.get("isAvailable")
        if available is None:
            available = availability.get("available")
        rows.append(
            OrderedDict(
                [
                    ("courseId", course_id),
                    ("id", nested.get("id") or membership.get("id")),
                    ("name", nested.get("name") or nested.get("displayName") or ""),
                    ("displayName", nested.get("displayName") or ""),
                    ("available", available),
                    ("role", membership.get("courseRoleId") or membership.get("courseRole")),
                    ("ultra", nested.get("ultraStatus") or nested.get("courseView")),
                ]
            )
        )

    if not include_unavailable:
        rows = [r for r in rows if r["available"] in (None, "Yes", True)]

    missing = [r for r in rows if not r["name"]]
    if missing and with_names:
        names = resolve_course_names(
            session, [r["courseId"] for r in missing], log, cfg=cfg
        )
        for row in rows:
            if not row["name"]:
                row["name"] = names.get(row["courseId"]) or row["courseId"]
    for row in rows:
        if not row["name"]:
            row["name"] = row["courseId"]
    return rows


def course_name_cache_path(cfg):
    return os.path.join(cfg.state_dir, "course_names.json")


def resolve_course_names(session, course_ids, log, cache_ttl=86400, cfg=None):
    """Map course id -> display name via `GET /courses/{id}`.

    The membership list does not include names, and no batch form exists
    (verified live: the comma-separated `courseId` query and repeated params both
    returned 0 results, and `/v2/users/me/courses` is a 404). So names cost one
    request per course; results are cached on disk because an account with ~90
    enrolments would otherwise repeat that work on every launch.
    """
    cache_path = course_name_cache_path(cfg) if cfg is not None else None
    cached = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if time.time() - float(payload.get("saved", 0)) < cache_ttl:
                cached = payload.get("names") or {}
        except (OSError, ValueError, TypeError):
            cached = {}

    names = {cid: cached[cid] for cid in course_ids if cid in cached}
    todo = [cid for cid in course_ids if cid not in names]
    if todo:
        log(f"[courses] looking up {len(todo)} course name(s) ...")
    for index, course_id in enumerate(todo, start=1):
        try:
            payload = session.api_get(f"/courses/{course_id}", allow_404=True)
        except ApiError as exc:
            # A 403 here is normal: students cannot read details of courses they
            # can no longer access. Recorded as None so the next run does not
            # repeat the same failing request.
            log(f"[warn] could not read {course_id}: {str(exc).splitlines()[0]}")
            payload = None
        if payload:
            names[course_id] = (
                payload.get("name") or payload.get("displayName") or course_id
            )
        else:
            names[course_id] = None
        if index % 25 == 0:
            log(f"[courses]   ... {index}/{len(todo)}")

    if cache_path and names:
        try:
            merged = dict(cached)
            merged.update(names)
            os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
            with open(long_path(cache_path), "w", encoding="utf-8") as handle:
                json.dump(
                    {"saved": time.time(), "names": merged},
                    handle,
                    indent=2,
                    ensure_ascii=False,
                )
        except OSError:
            pass
    return names


def print_courses(courses, log):
    for line in course_table_lines(courses):
        log(line)
    if courses:
        log("")


def cmd_courses(args, cfg, log):
    session = make_session(cfg, log, args)
    session.login(force=args.force)
    courses = fetch_courses(session, log, include_unavailable=not args.available_only, cfg=cfg)
    print_courses(courses, log)
    if args.json:
        print(json.dumps(courses, indent=2, ensure_ascii=False))
    return EXIT_OK


def resolve_course_id(session, cfg, args, log):
    """Return the course id to pull.

    `--course-id` accepts a Blackboard id (`_12529_1`), a course-list index, or a
    unique name keyword. When nothing is pinned and stdin is a real console, the
    user picks interactively; otherwise this fails with a clear message rather
    than blocking a script.
    """
    pinned = (cfg.course_id or "").strip()
    if pinned and pinned.lower() not in ("auto", "list", "pick", "ask"):
        if COURSE_ID_RE.fullmatch(pinned):
            return pinned
        # Not a Blackboard id: treat it as an index or a name keyword.
        courses = fetch_courses(session, log, include_unavailable=True, cfg=cfg)
        if courses:
            try:
                resolved = match_course(pinned, courses)
            except ConfigError as exc:
                log(f"[warn] {exc}")
            else:
                log(f"[course] resolved {pinned!r} -> {resolved}")
                return resolved
        return pinned

    prompter = make_prompter(args)
    courses = fetch_courses(session, log, include_unavailable=True, cfg=cfg)
    if not courses:
        raise ConfigError("no course id given and the enrolment list is empty; pass --course-id")
    if pinned.lower() == "list":
        print_courses(courses, log)
        raise SystemExit(EXIT_OK)
    if not prompter.interactive:
        print_courses(courses, log)
        raise ConfigError(
            "course id is set to 'auto' but stdin is not interactive. "
            "Run `bbpull pull --course-id <id>` with an id from the list above, "
            "or run `bbpull setup` to pin a default course."
        )
    chosen = pick_courses(prompter, courses, multi=False)
    return chosen[0]["courseId"]


def match_course(answer, courses):
    """Resolve a typed answer against the course list.

    Accepts a 1-based index, an exact course id, or a unique name keyword.
    Retained as the non-interactive resolver (see tests); the interactive path
    uses `wizard.parse_selection` for multi-select.
    """
    text = (answer or "").strip()
    if text.isdigit() and 1 <= int(text) <= len(courses):
        return courses[int(text) - 1]["courseId"]
    lowered = text.lower()
    for course in courses:
        if course["courseId"].lower() == lowered or str(course.get("id", "")).lower() == lowered:
            return course["courseId"]
    hits = filter_courses(courses, lowered)
    if len(hits) == 1:
        return hits[0]["courseId"]
    if not hits:
        raise ConfigError(f"nothing matched {text!r}")
    names = ", ".join(f"{c['courseId']} ({c['name']})" for c in hits[:8])
    raise ConfigError(f"{text!r} matched {len(hits)} courses: {names}")


def course_display_name(session, course_id):
    try:
        payload = session.api_get(f"/courses/{course_id}", allow_404=True)
    except ApiError:
        return course_id
    if not payload:
        return course_id
    return payload.get("name") or payload.get("displayName") or course_id


def out_dir_for(cfg, course_id, course_name):
    """Root output dir, then one folder per course: `_12529_1_課程名稱`."""
    base = cfg.out_dir or "output"
    if not os.path.isabs(base):
        base = os.path.join(os.path.dirname(os.path.abspath(cfg.env_file or ".")), base)
    folder = sanitize(f"{course_id}_{course_name}", fallback=course_id, max_len=90)
    return os.path.join(base, folder)


def cmd_doctor(args, cfg, log):
    log("bbpull doctor")
    log("=" * 60)
    log(cfg.describe())
    warning = cfg.warn_if_env_file_exposed()
    if warning:
        log(warning)
    for line in scan_template_for_secrets(cfg.env_file):
        log(line)
    log("")
    if not cfg.has_credentials:
        log(
            "credentials: MISSING\n"
            "  Run `bbpull` (no arguments) for the interactive setup, or set\n"
            "  BB_USERNAME / BB_PASSWORD, or pass --username/--password."
        )
        return EXIT_CONFIG
    session = make_session(cfg, log, args)
    started = time.time()
    try:
        session.login(force=args.force)
    except LoginError as exc:
        log(f"login: FAILED\n{exc}")
        return EXIT_LOGIN
    log(f"login: OK ({time.time() - started:.1f}s, strategy={session.login_strategy})")
    try:
        me = session.api_get("/users/me")
        # `userName` is null for some institutions; fall back to other ids so the
        # line never reads "None".
        label = (
            me.get("userName")
            or me.get("studentId")
            or me.get("id")
            or "(unnamed)"
        )
        log(f"users/me: {label} (id {me.get('id')})")
    except ApiError as exc:
        log(f"users/me: FAILED {exc}")
        return EXIT_API
    try:
        courses = session.get_all("/users/me/courses")
        log(f"enrolments: {len(courses)}")
    except ApiError as exc:
        log(f"enrolments: FAILED {exc}")
        return EXIT_API
    return EXIT_OK


def effective_options(args, cfg):
    """Merge CLI flags with the wizard-persisted defaults.

    Flags can only turn things OFF (--no-content / --no-files); the .env values
    decide what happens when no flag is given. `--overwrite` can only turn
    overwriting ON. That keeps every saved setting meaningful.
    """
    return {
        "content": (not getattr(args, "no_content", False)) and cfg.include_content,
        "announcements": (not getattr(args, "no_announcements", False))
        and cfg.include_announcements,
        "files": (not getattr(args, "no_files", False)) and cfg.download_files,
        "overwrite": bool(getattr(args, "overwrite", False)) or cfg.overwrite,
    }


def pull_one(session, cfg, course_id, course_name, args, log):
    """Pull content + announcements for a single course. Returns a report dict."""
    target_dir = getattr(args, "out_dir", None) or out_dir_for(cfg, course_id, course_name)
    options = effective_options(args, cfg)
    report = OrderedDict(
        [
            ("courseId", course_id),
            ("courseName", course_name),
            ("outDir", os.path.abspath(target_dir)),
            ("options", dict(options)),
            ("started", time.strftime("%Y-%m-%dT%H:%M:%S")),
            ("content", None),
            ("announcements", None),
            ("problems", []),
            ("ok", True),
        ]
    )
    os.makedirs(long_path(target_dir), exist_ok=True)
    log("")
    log("=" * 78)
    log(f"course : {course_name}  ({course_id})")
    log(f"output : {os.path.abspath(target_dir)}")
    log(
        "scope  : "
        f"content={'on' if options['content'] else 'off'} "
        f"announcements={'on' if options['announcements'] else 'off'} "
        f"files={'on' if options['files'] else 'off'} "
        f"overwrite={'on' if options['overwrite'] else 'off'}"
    )
    log("=" * 78)

    content_index = []
    if options["content"]:
        puller = CoursePuller(
            session,
            course_id,
            os.path.join(target_dir, "content"),
            log,
            download_files=options["files"],
            overwrite=options["overwrite"],
        )
        started = time.time()
        puller.fetch_tree()
        content_index = puller.write()
        report["content"] = {
            "stats": puller.stats,
            "seconds": round(time.time() - started, 1),
            "roots": len(puller.roots),
        }
        report["problems"].extend(puller.problems)
        log(
            f"[content] done: {puller.stats['nodes']} items, "
            f"{puller.stats['files_saved']} file(s) saved "
            f"({human_bytes(puller.stats['bytes'])}), "
            f"{puller.stats['files_failed']} failed"
        )

    ann_rows = []
    if options["announcements"]:
        ann = AnnouncementPuller(
            session,
            course_id,
            os.path.join(target_dir, "announcements"),
            log,
            download_files=options["files"],
            overwrite=options["overwrite"],
        )
        started = time.time()
        ann.fetch()
        ann_rows = ann.write()
        report["announcements"] = {
            "stats": ann.stats,
            "seconds": round(time.time() - started, 1),
        }
        log(f"[announcements] done: {ann.stats['announcements']} item(s)")

    _write_course_index(target_dir, course_id, course_name, content_index, ann_rows, session.base)
    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report["ok"] = not report["problems"]
    with open(long_path(os.path.join(target_dir, "_manifest.json")), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    return report


def _write_course_index(target_dir, course_id, course_name, content_index, ann_rows, base_url):
    lines = [
        f"# {course_name}",
        "",
        f"- course id: `{course_id}`",
        f"- source: {base_url}/ultra/courses/{course_id}/outline",
        f"- pulled: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- tools: bbpull {__version__}",
        "",
        "## Sections",
        "",
        f"- [Announcements](announcements/announcements.md) - {len(ann_rows)} item(s)",
        "- [Class content](content/) - full outline tree below",
        "",
    ]
    if content_index:
        lines.append("## Content tree")
        lines.append("")
        lines.extend(_tree_lines(content_index, depth=0))
        lines.append("")
    with open(long_path(os.path.join(target_dir, "index.md")), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _tree_lines(nodes, depth):
    out = []
    for node in nodes:
        indent = "  " * depth
        label = node.get("title") or node.get("id")
        rel = node.get("dir")
        handler = (node.get("handler") or "").split("/")[-1]
        files = node.get("files") or []
        suffix = ""
        if files:
            ok = [f for f in files if f.get("path")]
            suffix = f" ({len(ok)} file)" if ok else " (file unavailable)"
        link = f"[{label}](content/{rel})" if rel and not node.get("children") else label
        out.append(f"{indent}- {link} `{handler}`{suffix}")
        if node.get("children"):
            out.extend(_tree_lines(node["children"], depth + 1))
    return out


def cmd_pull(args, cfg, log):
    session = make_session(cfg, log, args)
    session.login(force=args.force)
    course_id = resolve_course_id(session, cfg, args, log)
    course_name = course_display_name(session, course_id)
    if getattr(args, "interactive", False) or getattr(args, "ask", False):
        prompter = make_prompter(args)
        prompter.require_interactive(
            "Use flags instead: --course-id, --out, --no-content, "
            "--no-announcements, --no-files, --overwrite."
        )
        args = ask_pull_options(prompter, cfg, args, course_name)
    report = pull_one(session, cfg, course_id, course_name, args, log)
    log("")
    if report["ok"]:
        log("DONE - everything requested was pulled.")
        return EXIT_OK
    log(f"DONE WITH {len(report['problems'])} PROBLEM(S) - see the log above.")
    return EXIT_PARTIAL


def ask_pull_options(prompter, cfg, args, course_name):
    """Ask the per-run pull options and return an updated args namespace."""
    current = effective_options(args, cfg)
    prompter.say("")
    prompter.rule("=")
    prompter.say(f"抓取設定 — {course_name}")
    prompter.rule("=")
    prompter.say("（預設值來自你的設定檔，本次調整不會寫回設定檔）")
    args.no_content = not prompter.ask_bool(
        "抓取 class content（課程內容）？", default=current["content"]
    )
    args.no_announcements = not prompter.ask_bool(
        "抓取 announcements（公告）？", default=current["announcements"]
    )
    args.no_files = not prompter.ask_bool(
        "下載附件與內嵌檔案？", default=current["files"]
    )
    args.overwrite = prompter.ask_bool("重新下載已存在的檔案？", default=current["overwrite"])
    default_out = cfg.out_dir or "output"
    answer = prompter.ask("輸出根目錄（每門課會在其下建立專屬資料夾）", default=default_out)
    # Treat the answer as the ROOT, so each course still gets its own folder and
    # two courses can never overwrite each other.
    cfg.out_dir = os.path.expanduser(answer)
    args.out_dir = None
    prompter.say("")
    if not prompter.confirm("開始抓取？", default=True):
        raise Cancelled()
    return args


def cmd_pull_all(args, cfg, log):
    session = make_session(cfg, log, args)
    session.login(force=args.force)
    courses = fetch_courses(session, log, include_unavailable=not args.available_only, cfg=cfg)
    if args.match:
        courses = filter_courses(courses, args.match)
    if getattr(args, "pick", False):
        prompter = make_prompter(args)
        prompter.require_interactive("Use --match/--limit, or pin a course with --course-id.")
        if not courses:
            log("沒有可選的課程")
            return EXIT_CONFIG
        courses = pick_courses(prompter, courses, multi=True, allow_all=True)
    if args.limit:
        courses = courses[: args.limit]
    if not courses:
        log("no courses matched")
        return EXIT_CONFIG
    if args.dry_run:
        print_courses(courses, log)
        log("dry run: nothing downloaded")
        return EXIT_OK
    reports = []
    failures = 0
    for course in courses:
        args.out_dir = None
        try:
            report = pull_one(session, cfg, course["courseId"], course["name"], args, log)
        except BBPullError as exc:
            failures += 1
            log(f"[error] {course['courseId']}: {exc}")
            reports.append({"courseId": course["courseId"], "ok": False, "error": str(exc)})
            continue
        reports.append(report)
        if not report["ok"]:
            failures += 1
    base = out_dir_for(cfg, "_all", "courses")
    os.makedirs(long_path(base), exist_ok=True)
    with open(long_path(os.path.join(base, "_summary.json")), "w", encoding="utf-8") as fh:
        json.dump({"courses": reports}, fh, indent=2, ensure_ascii=False)
    log("")
    log(f"pulled {len(reports) - failures}/{len(reports)} course(s); summary: {base}")
    return EXIT_OK if failures == 0 else EXIT_PARTIAL


def cmd_list_content(args, cfg, log):
    session = make_session(cfg, log, args)
    session.login(force=args.force)
    course_id = resolve_course_id(session, cfg, args, log)
    puller = CoursePuller(session, course_id, "output", log, download_files=False)
    puller.fetch_tree()
    for line in _tree_lines([n.to_summary() for n in puller.roots], depth=0):
        print(line)
    if args.json:
        print(json.dumps([n.to_summary() for n in puller.roots], indent=2, ensure_ascii=False))
    return EXIT_OK


# ------------------------------------------------------------- interactive UI
def first_run_services(log, args=None):
    """Wire the wizard's login/list callbacks to the real Blackboard client."""
    def login(cfg, username, password):
        # Work on a copy so a failed attempt never poisons the live config.
        trial = replace(cfg, username=username, password=password)
        session = LearnSession(trial, log)
        session.login(force=True)
        return session

    def list_courses(session):
        # The session already carries the config in use, which is what the name
        # cache path is derived from.
        return fetch_courses(
            session, log, include_unavailable=True, cfg=getattr(session, "config", None)
        )

    return FirstRunServices(login=login, list_courses=list_courses)


def ensure_configured(prompter, cfg, log, args=None):
    """Make sure we can log in, asking for whatever is missing.

    Two distinct cases, deliberately handled differently:
      * no account at all   -> the full guided first-run, which can save things
      * account but no password (the user chose not to store it)
                            -> just ask for the password for this run

    The second case matters: re-running the entire wizard every launch because
    someone declined to save a password would be obnoxious.
    """
    if not prompter.interactive:
        raise ConfigError(
            "no credentials configured and stdin is not interactive.\n"
            "Run `bbpull` in a terminal to set up interactively, or provide "
            "--username/--password, or set BB_USERNAME and BB_PASSWORD."
        )
    if cfg.needs_setup:
        run_first_run(prompter, cfg, log, first_run_services(log, args))
        return build_config(args if args is not None else build_parser().parse_args([]))
    if cfg.needs_password:
        prompter.say(f"請輸入 {cfg.username} 的密碼（未儲存，僅本次使用）")
        cfg.password = prompter.ask_secret("密碼", has_current=False) or ""
        if not cfg.password:
            raise Cancelled()
    return cfg


def cmd_setup(args, cfg, log):
    """Interactive configuration wizard."""
    prompter = make_prompter(args)
    prompter.require_interactive(
        "Set values directly instead: create .env from .env.example, or pass "
        "--base-url/--course-id/--username/--password."
    )
    try:
        written = run_setup_wizard(prompter, cfg, log)
    except Cancelled:
        log("")
        log("已取消，未變更任何設定。")
        return EXIT_OK
    log("")
    offer_login_check(prompter, load_config(cfg.env_file), log)
    log("")
    log(f"設定完成（{len(written)} 個項目）。執行 `bbpull` 開啟主選單。")
    return EXIT_OK


MENU_ACTIONS = OrderedDict(
    [
        ("gui", ("開啟桌面應用程式（推薦：像檔案總管一樣瀏覽與勾選下載）", ["gui"])),
        ("pull", ("抓取課程內容 + 公告（可互動選課）", ["pull"])),
        ("pull-all", ("挑選多門課程一次抓取", ["pull-all", "--pick"])),
        ("list-content", ("只看課程大綱，不下載", ["list-content"])),
        ("courses", ("列出我的所有課程", ["courses"])),
        ("doctor", ("檢查設定與連線狀態", ["doctor"])),
        ("secure", ("把密碼改成加密儲存（從 .env 明文搬過來）", ["secure"])),
        ("setup", ("重新設定（站台 / 帳密 / 預設課程 / 輸出）", ["setup"])),
        ("selftest", ("離線自我測試（不連網、不需帳密）", ["selftest"])),
        ("quit", ("離開", [])),
    ]
)


def _menu_header(prompter, cfg):
    prompter.rule("=")
    prompter.say(f"bbpull {__version__} — Blackboard Ultra 課程下載器")
    prompter.rule("=")
    prompter.say(f"  站台    : {cfg.base_url}")
    prompter.say(f"  課程    : {cfg.course_id or '(未設定)'}")
    creds = cfg.username if cfg.username else "(未設定)"
    creds += " / 密碼已設定" if cfg.password else " / 密碼未設定"
    prompter.say(f"  帳號    : {creds}")
    prompter.say(f"  輸出    : {os.path.abspath(cfg.out_dir)}")
    prompter.say(
        "  預設抓取: "
        f"內容={'on' if cfg.include_content else 'off'} "
        f"公告={'on' if cfg.include_announcements else 'off'} "
        f"附件={'on' if cfg.download_files else 'off'}"
    )


def cmd_menu(args, cfg, log, parser=None, forward_argv=None):
    """Interactive main menu. Rebuilds Config every loop so `setup` takes effect."""
    prompter = make_prompter(args)
    prompter.require_interactive(
        "This shell is not interactive. Call a subcommand directly, for example:\n"
        "  bbpull doctor\n"
        "  bbpull pull --course-id _12529_1\n"
        "  bbpull setup   (run it in a real terminal)"
    )
    parser = parser or build_parser()
    forward = list(forward_argv or [])
    handlers = {
        "gui": cmd_gui,
        "setup": cmd_setup,
        "secure": cmd_secure,
        "doctor": cmd_doctor,
        "courses": cmd_courses,
        "pull": cmd_pull,
        "pull-all": cmd_pull_all,
        "list-content": cmd_list_content,
        "selftest": cmd_selftest,
        "venv": cmd_venv,
    }
    options = [(key, label) for key, (label, _) in MENU_ACTIONS.items()]
    while True:
        try:
            cfg = build_config(args)
        except ConfigError as exc:
            log(f"config error: {exc}")
            return EXIT_CONFIG

        # First run, or a password we deliberately did not store: guide instead
        # of failing.
        if cfg.needs_setup or cfg.needs_password:
            try:
                cfg = ensure_configured(prompter, cfg, log, args)
            except Cancelled:
                prompter.say("已取消，未變更任何檔案。")
                return EXIT_OK
            except (LoginError, ApiError, ConfigError) as exc:
                prompter.say(f"設定失敗: {exc}")
                return EXIT_CONFIG

        prompter.say("")
        _menu_header(prompter, cfg)
        try:
            choice = prompter.choose("請選擇操作：", options, default=4)
        except Cancelled:
            # Typing q / 0 / Ctrl-C at the menu means "leave", not "crash".
            prompter.say("再見。")
            return EXIT_OK
        if choice == "quit":
            prompter.say("再見。")
            return EXIT_OK

        _, argv = MENU_ACTIONS[choice]
        action_args = parser.parse_args(forward + list(argv))
        try:
            code = handlers[choice](action_args, cfg, log)
        except Cancelled:
            prompter.say("已取消。")
            code = EXIT_OK
        except (ConfigError, LoginError, ApiError, BBPullError) as exc:
            prompter.say(f"錯誤: {exc}")
            code = EXIT_CONFIG
        except KeyboardInterrupt:
            prompter.say("已中斷。")
            code = EXIT_OK
        prompter.say("")
        prompter.say(f"[{choice}] 結束，exit code = {code}")
        if choice != "setup" and not prompter.confirm("回到主選單？", default=True):
            return code


# ---------------------------------------------------------------------- main
def build_parser():
    parser = argparse.ArgumentParser(
        prog="bbpull",
        description="Pull Blackboard Learn (Ultra) class content and announcements to local disk.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Interactive:\n"
            "  bbpull                          open the main menu\n"
            "  bbpull setup                    guided configuration (writes .env)\n"
            "\n"
            "Scripted:\n"
            "  bbpull doctor\n"
            "  bbpull courses\n"
            "  bbpull pull --course-id _12529_1\n"
            "  bbpull pull --course-id _12529_1 --out D:\\bb\\course12529\n"
            "  bbpull pull-all --match \"資料結構\"\n"
            "  bbpull pull --course-id _12529_1 --no-input   (never prompt)\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"bbpull {__version__}")
    parser.add_argument(
        "--env-file",
        dest="global_env_file",
        help="path to the .env file (mainly for the interactive menu)",
    )
    parser.add_argument(
        "--state-dir",
        dest="global_state_dir",
        help="where cookies and saved credentials live (mainly for the menu)",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", dest="base_url", help="e.g. https://twc.blackboard.com")
    common.add_argument("--course-id", dest="course_id", help="course id, e.g. _12529_1")
    common.add_argument("--username", dest="username", help="Blackboard username")
    common.add_argument("--password", dest="password", help="Blackboard password")
    common.add_argument("--out", dest="out_dir", help="output directory")
    common.add_argument("--cookie-file", dest="cookie_file", help="cookie jar path (JSON)")
    common.add_argument("--env-file", dest="env_file", help="path to .env")
    common.add_argument("--state-dir", dest="state_dir", help="where cookies/logs are cached")
    common.add_argument("--timeout", dest="timeout", type=float, help="HTTP timeout seconds")
    common.add_argument("--no-verify-ssl", dest="verify_ssl", action="store_false", default=None,
                        help="skip TLS verification (only for broken campus proxies)")
    common.add_argument("--force", action="store_true", help="ignore the cached session, log in again")
    common.add_argument("--quiet", action="store_true", help="only print errors")
    common.add_argument("--verbose", action="store_true", help="reserved; printing is already verbose")
    common.add_argument(
        "--no-input", dest="no_input", action="store_true",
        help="never prompt; fail instead (same as BB_NONINTERACTIVE=1)",
    )

    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("menu", parents=[common], help="interactive main menu (same as running bbpull alone)")
    sub.add_parser("setup", parents=[common], help="interactive configuration wizard")
    p_env = sub.add_parser("venv", help="create or inspect the project virtual environment")
    p_env.add_argument("--create", action="store_true",
                       help="create .venv and install dependencies (downloads ~80 MB)")
    p_env.add_argument("--force", action="store_true",
                       help="recreate/reinstall even if the environment looks present")
    p_env.add_argument("--python", default=None,
                       help="interpreter to build the environment from (default: this one)")
    p_env.add_argument("--no-deps", action="store_true",
                       help="create the environment only, skip pip install")
    p_env.add_argument("--no-input", action="store_true", help="never prompt")

    p_gui = sub.add_parser("gui", parents=[common], help="open the desktop application")
    p_gui.add_argument("--tk", action="store_true",
                       help="force the built-in Tk interface instead of Qt")
    p_gui.add_argument("--qt", action="store_true",
                       help="require Qt; fail loudly instead of falling back to Tk")
    p_gui.add_argument("--check", action="store_true",
                       help="report which GUI engine would run, and why, then exit")
    p_gui.add_argument("--inspect", action="store_true",
                       help="record every click (widget, style, magnified crop) to a folder")
    p_gui.add_argument("--inspect-dir", default=None,
                       help="where --inspect writes its records (default: ./_debug)")
    sub.add_parser("secure", parents=[common], help="move the password into encrypted storage")
    sub.add_parser("login", parents=[common], help="verify credentials and cache the session")
    p_courses = sub.add_parser("courses", parents=[common], help="list enrolled courses")
    p_courses.add_argument("--json", action="store_true", help="also print JSON")
    p_courses.add_argument("--available-only", action="store_true", help="hide unavailable courses")
    sub.add_parser("doctor", parents=[common], help="show configuration and connectivity status")
    sub.add_parser("selftest", parents=[common], help="run the offline unit suite (no network)")

    p_pull = sub.add_parser("pull", parents=[common], help="pull one course")
    p_pull.add_argument("--no-content", action="store_true", help="skip class content")
    p_pull.add_argument("--no-announcements", action="store_true", help="skip announcements")
    p_pull.add_argument("--no-files", action="store_true", help="write text/metadata only")
    p_pull.add_argument("--overwrite", action="store_true", help="re-download existing files")
    p_pull.add_argument(
        "-i", "--interactive", "--ask", dest="interactive", action="store_true",
        help="ask for the pull options interactively before starting",
    )

    p_all = sub.add_parser("pull-all", parents=[common], help="pull every enrolled course")
    p_all.add_argument("--match", help="only courses whose name/id contains this text")
    p_all.add_argument("--limit", type=int, help="stop after N courses")
    p_all.add_argument("--pick", action="store_true", help="choose courses interactively")
    p_all.add_argument("--available-only", action="store_true")
    p_all.add_argument("--dry-run", action="store_true", help="list what would be pulled")
    p_all.add_argument("--no-content", action="store_true")
    p_all.add_argument("--no-announcements", action="store_true")
    p_all.add_argument("--no-files", action="store_true")
    p_all.add_argument("--overwrite", action="store_true")

    p_list = sub.add_parser("list-content", parents=[common], help="print the outline only")
    p_list.add_argument("--json", action="store_true")

    return parser


def cmd_secure(args, cfg, log):
    """Move a plaintext .env password into the encrypted credential store."""
    prompter = make_prompter(args)
    prompter.require_interactive(
        "Run `bbpull setup` in a terminal instead, or set the password via the "
        "BB_PASSWORD environment variable."
    )
    values = read_env_file(cfg.env_file)
    env_password = values.get("BB_PASSWORD") or ""
    store = cfg.secret_store()

    log("密碼儲存方式")
    log("=" * 60)
    log(f"  .env 位置        : {os.path.abspath(cfg.env_file)}")
    log(f"  .env 有明文密碼  : {'是' if env_password else '否'}")
    log(f"  加密儲存位置     : {os.path.abspath(store.path)}")
    log(f"  加密儲存已有資料 : {'是' if store.exists() else '否'}")
    log(f"  本機加密方式     : {backend_description()}")
    log("")

    if not cfg.username:
        log("沒有可用的帳號，請先執行 `bbpull setup`。")
        return EXIT_CONFIG

    password = env_password
    if not password:
        if store.exists() and store.load().get("BB_PASSWORD"):
            log("目前密碼已經是加密儲存，不需要搬移。")
            return EXIT_OK
        prompter.say("找不到現成密碼，請輸入一次以便加密儲存。")
        password = prompter.ask_secret("密碼", has_current=False)
        if not password:
            log("未輸入密碼，取消。")
            return EXIT_OK

    if not prompter.confirm(f"要把密碼改存成加密形式，並清空 .env 的明文嗎？", default=True):
        log("已取消，未變更任何設定。")
        return EXIT_OK

    store.save({"BB_USERNAME": cfg.username, "BB_PASSWORD": password})
    if env_password:
        clear_env_password(cfg.env_file)
    log("")
    log(f"完成。密碼已加密儲存於 {os.path.abspath(store.path)}")
    if env_password:
        log(f".env 中的明文密碼已清空（{os.path.abspath(cfg.env_file)}）")
    log("之後執行 bbpull 會自動使用加密儲存的密碼。")
    return EXIT_OK


# Process-wide re-entrancy guard for `selftest`. A module flag rather than an
# environment variable: an env var leaks into the unit suite and changes its
# outcome, which is exactly what happened the first time this guard was written.
_SELFTEST_RUNNING = False


def cmd_gui(args, cfg, log):
    """Open the desktop application.

    Prefers the Qt build: it renders lists through a virtualised model/view, so
    cost is flat in the number of rows (measured 9 ms for 5,000 rows, against
    ~14 s for the Tk build's widget-per-row).

    The engine choice is always reported. A silent downgrade to Tk once caused a
    whole round of debugging against the wrong interface, so `--qt` turns a
    missing PySide6 into an error and `--check` explains the decision.
    """
    from .gui_select import ENGINE_QT, ENGINE_TK, decide, report

    if getattr(args, "check", False):
        log(report(scan=True))
        return EXIT_OK

    force_tk = bool(getattr(args, "tk", False))
    require_qt = bool(getattr(args, "qt", False))
    if force_tk and require_qt:
        log("--tk 與 --qt 不能同時使用。")
        return EXIT_USAGE

    choice = decide(force_tk=force_tk, require_qt=require_qt)
    for warning in choice["warnings"]:
        log(f"注意：{warning}")

    if choice["engine"] is None:
        log(f"無法開啟桌面應用程式：{choice['reason']}")
        if choice["hint"]:
            log(choice["hint"])
        log("（沒有圖形介面的環境仍可使用 `bbpull pull` 等指令。）")
        return EXIT_CONFIG

    if choice["engine"] == ENGINE_QT:
        from .gui_qt.window import run_gui as run_qt

        log(f"正在開啟桌面應用程式（Qt）…  [{choice['info']['exe']}]")
        try:
            return run_qt(cfg, log,
                          inspect=bool(getattr(args, "inspect", False)),
                          inspect_dir=getattr(args, "inspect_dir", None))
        except Exception as exc:  # noqa: BLE001
            # A Qt failure at runtime is reported, then Tk is offered - but it is
            # never swapped in without saying so.
            log(f"Qt 介面無法啟動：{exc}")
            if require_qt:
                return EXIT_CONFIG
            log("改用內建 Tk 介面 …（視窗標題會顯示「Tk」）")

    try:
        from .gui.app import run_gui as run_tk
    except ImportError as exc:
        log(f"無法載入圖形介面：{exc}")
        log(f"請先安裝：{choice['hint']}")
        log("（沒有圖形介面的環境仍可使用 `bbpull pull` 等指令。）")
        return EXIT_CONFIG

    log("正在開啟桌面應用程式（Tk）…")
    notice = None
    if choice["engine"] != ENGINE_TK or choice["warnings"]:
        notice = ("此為降級的 Tk 介面。想用 Qt："
                  f"{choice['hint'].splitlines()[-1].strip()}"
                  if choice["hint"] else "此為降級的 Tk 介面。")
    try:
        return run_tk(cfg, log, notice=notice)
    except Exception as exc:  # noqa: BLE001 - a missing display must not traceback
        log(f"圖形介面無法啟動：{exc}")
        log("如果這台機器沒有桌面環境，請改用 `bbpull pull --course-id <id>`。")
        return EXIT_CONFIG


def cmd_selftest(args, cfg, log):
    """Verify the installation.

    Two modes, because the two shapes cannot do the same thing:

    * **from source** - run the offline unit suite (plus the secret-scan gate);
    * **packaged** - run a runtime health check. A bundled executable has no
      `tests/` directory (it unpacks to a temporary `_MEI…` folder), so
      `unittest discover` fails with "Start directory is not importable".
    """
    import unittest

    global _SELFTEST_RUNNING

    from . import venv_tools

    if venv_tools.is_frozen():
        from .healthcheck import run_checks

        ok, lines = run_checks()
        for line in lines:
            log(line)
        return EXIT_OK if ok else EXIT_PARTIAL

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        from tools.secret_scan import scan as scan_secrets
    except ImportError:
        scan_secrets = None
    if scan_secrets is not None:
        findings, checked = scan_secrets(root)
        if findings:
            log(f"secret scan FAILED：{len(findings)} 個疑似機密（檢查了 {checked} 個檔案）")
            for item in findings[:10]:
                log(f"  {item['file']}:{item['line']}  {item['kind']}  {item['detail']}")
            return EXIT_PARTIAL
        log(f"secret scan OK（{checked} 個檔案）")

    # Guard against recursion: the suite drives cmd_menu, which can reach this
    # command again. Without this the process recurses until it dies.
    if _SELFTEST_RUNNING:
        log("self-test is already running; skipping the nested invocation.")
        return EXIT_OK
    _SELFTEST_RUNNING = True
    try:
        suite = unittest.defaultTestLoader.discover(
            os.path.join(root, "tests"), top_level_dir=root
        )
        log(f"running offline self-test ({suite.countTestCases()} cases)")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        _SELFTEST_RUNNING = False
    if result.wasSuccessful():
        log("SELFTEST OK")
        return EXIT_OK
    return EXIT_PARTIAL


def cmd_venv(args, cfg, log):
    """Create and inspect the project virtual environment.

    A project-local venv is the only way to make the GUI engine deterministic:
    PySide6 installed into one global interpreter is invisible to the others, and
    on a machine with several Pythons that silently produced the Tk fallback.
    """
    from . import venv_tools

    # A bundled executable has everything inside it; there is nothing to install.
    if venv_tools.is_frozen():
        log(venv_tools.report())
        return EXIT_OK

    if not getattr(args, "create", False):
        log(venv_tools.report(log=log))
        if not venv_tools.venv_exists() and not getattr(args, "no_input", False):
            log("")
            if make_prompter(args).confirm("現在就建立嗎？", default=True):
                return cmd_venv(_ForceCreate(args), cfg, log)
        return EXIT_OK

    if venv_tools.venv_exists() and not getattr(args, "force", False):
        check = venv_tools.verify()
        if check.get("ok"):
            log("虛擬環境已存在且完整。")
            log(venv_tools.report())
            return EXIT_OK
        log("虛擬環境已存在但不完整，重新安裝套件 …")

    result = venv_tools.create(
        base_python=getattr(args, "python", None),
        log=log,
        install=not getattr(args, "no_deps", False),
    )
    if not result["ok"]:
        log("建立虛擬環境失敗。")
        if result["error"]:
            for line in str(result["error"]).splitlines():
                log(f"  {line}")
        log("")
        log("可以手動試試：")
        log(f"  {result['command']}")
        return EXIT_CONFIG

    check = venv_tools.verify()
    log("")
    log(venv_tools.report())
    if not check.get("ok"):
        log("")
        log("環境建立了，但相依套件似乎不完整。請再跑一次：")
        log("  python -m bbpull venv --create --force")
        return EXIT_CONFIG

    log("")
    log("完成。之後用下列任一方式啟動，就會固定使用這個環境：")
    log(f"  {venv_tools.venv_python()}")
    log("  bbpull.cmd（會自動選用 .venv）")
    return EXIT_OK


class _ForceCreate:
    """Re-dispatch helper: reuse `cmd_venv` with `--create` implied."""

    def __init__(self, args):
        self._args = args
        self.create = True
        self.force = False
        self.python = getattr(args, "python", None)
        self.no_deps = getattr(args, "no_deps", False)
        self.no_input = True

    def __getattr__(self, name):
        return getattr(self._args, name)


# Commands that cannot do anything useful without a working login.
NEEDS_CREDENTIALS = {"login", "courses", "doctor", "pull", "pull-all", "list-content"}


def prepare_command(args, cfg, log, command):
    """Give any credential-needing command the same guided first-run experience.

    `bbpull pull` on a fresh machine should ask for the login, not tell the user
    to go create a config file. Non-interactive callers still get a clear error.
    """
    if command not in NEEDS_CREDENTIALS:
        return cfg
    if not (cfg.needs_setup or cfg.needs_password):
        return cfg
    prompter = make_prompter(args)
    return ensure_configured(prompter, cfg, log, args)


def main(argv=None):
    parser = build_parser()
    # Must happen before any output: console encodings are not guaranteed to
    # cover the characters Blackboard course titles actually contain.
    configure_console_streams()
    args = parser.parse_args(argv)
    log = make_logger(getattr(args, "verbose", False), getattr(args, "quiet", False))

    # `bbpull` with no subcommand opens the interactive menu.
    if not getattr(args, "command", None):
        command = "menu"
    else:
        command = args.command

    # Forward top-level locations to sub-actions launched from the menu.
    forward = []
    if getattr(args, "global_env_file", None):
        forward += ["--env-file", args.global_env_file]
        args.env_file = args.global_env_file
    if getattr(args, "global_state_dir", None):
        forward += ["--state-dir", args.global_state_dir]
        args.state_dir = args.global_state_dir

    if command == "menu":
        try:
            return cmd_menu(args, None, log, parser=parser, forward_argv=forward)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        except Cancelled:
            print("cancelled", file=sys.stderr)
            return EXIT_OK
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130

    handlers = {
        "gui": cmd_gui,
        "setup": cmd_setup,
        "secure": cmd_secure,
        "login": cmd_login,
        "courses": cmd_courses,
        "doctor": cmd_doctor,
        "pull": cmd_pull,
        "pull-all": cmd_pull_all,
        "list-content": cmd_list_content,
        "selftest": cmd_selftest,
        "venv": cmd_venv,
    }
    try:
        cfg = build_config(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        # A fresh machine should be asked for its login, not told to go edit a file.
        cfg = prepare_command(args, cfg, log, command)
    except Cancelled:
        print("cancelled", file=sys.stderr)
        return EXIT_OK
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        return handlers[command](args, cfg, log)
    except Cancelled:
        print("cancelled", file=sys.stderr)
        return EXIT_OK
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except LoginError as exc:
        print(f"login error: {exc}", file=sys.stderr)
        return EXIT_LOGIN
    except ApiError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return EXIT_API
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except BBPullError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
