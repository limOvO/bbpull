"""Console-safe output helpers shared by the CLI, the wizard and the pullers.

Live regression that motivated this module: a Blackboard course title contained
U+2011 (non-breaking hyphen). Printing it on a GBK console raised
UnicodeEncodeError and aborted an entire course pull. Console output is not
allowed to be a single point of failure for a long download, so every log path
goes through here.

Files are never affected: they are always written with an explicit UTF-8
encoding, so the real characters survive on disk. Only the console degrades.
"""

import sys


def safe_print(stream, text):
    """Print `text`, degrading rather than raising on an encoding error."""
    try:
        print(text, file=stream, flush=True)
    except UnicodeEncodeError as exc:
        try:
            print(encode_for(stream, text, getattr(exc, "encoding", None)),
                  file=stream, flush=True)
        except (ValueError, OSError, UnicodeEncodeError):
            pass
    except (ValueError, OSError):
        # Closed or otherwise unusable stream: never let logging abort the work.
        pass


def configure_console_streams(streams=None):
    """Make console streams tolerant of characters the codepage cannot encode.

    Keeps each stream's own encoding (so Chinese still renders correctly on a
    GBK console) and only relaxes error handling.
    """
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a TextIOWrapper (e.g. a test double) - nothing to configure.
            pass


def encode_for(stream, text, encoding=None):
    """Encode `text` for `stream`, replacing characters it cannot represent.

    `encoding` wins when given: on a retry the authoritative codec is the one
    named by the `UnicodeEncodeError` that just failed, not whatever
    `sys.stdout` happens to be (they differ when a sink writes to its own
    stream, e.g. a GBK console while stdout is UTF-8).
    """
    codec = encoding or getattr(stream, "encoding", None) or "ascii"
    try:
        return str(text).encode(codec, "replace").decode(codec, "replace")
    except (LookupError, UnicodeError):
        return str(text).encode("ascii", "replace").decode("ascii")


def wrap_logger(logger):
    """Return a logger that cannot raise on output.

    Delegates to the original sink (so a caller collecting messages still gets
    them) and only adds encoding resilience. Applied to whatever logger a puller
    is given, so passing the bare `print` cannot crash a download on an exotic
    character.
    """
    if logger is None:
        logger = print

    def log(message):
        try:
            logger(message)
        except UnicodeEncodeError as exc:
            # Retry with the offending characters replaced, using the codec the
            # failure actually reported.
            try:
                logger(encode_for(sys.stdout, message, getattr(exc, "encoding", None)))
            except Exception:  # noqa: BLE001
                pass
        except (ValueError, OSError):
            # Closed or otherwise unusable sink: never abort work for a log line.
            pass

    log._bbpull_safe = True
    return log
