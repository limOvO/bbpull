"""Entry point for the frozen executables.

PyInstaller executes the given script as a *top-level* module, not as a member of
the `bbpull` package. `bbpull/__main__.py` uses relative imports
(`from .cli import main`), which fail there with
"attempted relative import with no known parent package" - the build succeeds and
the executable dies on launch.

This file uses only absolute imports, so it works both frozen and from source.
`python -m bbpull` keeps using `bbpull/__main__.py`, which is correct for the
module case.
"""

import sys

from bbpull.__main__ import run

if __name__ == "__main__":
    sys.exit(run())
