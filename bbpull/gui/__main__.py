"""Allow `python -m bbpull.gui` to open the desktop window directly."""

import sys

from ..cli import build_parser
from ..config import build_config
from .app import run_gui


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv or ["gui"])
    if not getattr(args, "command", None):
        args = parser.parse_args(["gui"])
    cfg = build_config(args)
    return run_gui(cfg, print)


if __name__ == "__main__":
    sys.exit(main())
