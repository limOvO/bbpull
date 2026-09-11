"""Allow `python -m bbpull`.

Also the place where the project virtual environment takes over. Doing it here
(rather than only in `bbpull.cmd`) means the environment is consistent no matter
how the app is started - `python -m bbpull` from anaconda base, from Python 3.13,
or from a shortcut all end up in the same interpreter with the same dependencies.
Without this, which GUI engine you got depended on PATH order.
"""

import sys

from .cli import main
from .venv_tools import REEXEC_GUARD, maybe_reexec


def run():
    """Entry point: hand off to the project venv if one exists, then run."""
    # The guard makes the hand-off happen at most once, so a venv that somehow
    # reports the wrong prefix cannot cause an endless loop of processes.
    if not __import__("os").environ.get(REEXEC_GUARD):
        code = maybe_reexec(argv=sys.argv[1:], log=lambda message: print(message))
        if code is not None:
            return code
    return main()


if __name__ == "__main__":
    sys.exit(run())
