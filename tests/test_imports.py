"""Every import in the codebase must actually resolve.

Motivated by a real release bug: `bbpull/gui_qt/window.py` did
``from ..gui.wizard import ...`` inside the connect dialog's submit handler.
There is no `bbpull.gui.wizard` (the module is `bbpull.wizard`), so pressing
"登入" in the shipped executable produced

    No module named 'bbpull.gui.wizard'

No test caught it because it is a *deferred* import: nothing imports it at module
load, no unit test exercises it, and the GUI tests never submit the dialog. It
only failed in front of a user.

Rather than fix that one line and wait for the next occurrence, this walks the
AST of every module and resolves every import statement - including the deferred
ones inside functions - so the whole class of mistake fails in CI instead.
"""

import ast
import importlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("bbpull", "tools", "installer", "tests")


def source_files():
    for package in PACKAGES:
        base = ROOT / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def package_of(path, root=None):
    """Dotted package containing `path`, or "" for a top-level script.

    Tolerates paths outside the project root: the guard-detection tests build
    sample files in a temporary directory.
    """
    root = root or ROOT
    path = Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]
    return ".".join(parts)


def module_name_of(path, root=None):
    """Fully dotted name of the module in `path` itself."""
    root = root or ROOT
    package = package_of(path, root)
    stem = Path(path).stem
    if stem == "__init__":
        return package
    return f"{package}.{stem}" if package else stem


def resolve(package, level, module):
    """Absolute module name for a `from ... import` statement.

    `level` 1 means the current package, 2 the parent, and so on.
    """
    if level == 0:
        return module
    parts = package.split(".") if package else []
    keep = len(parts) - (level - 1)
    base = parts[:max(0, keep)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def parent_map(tree):
    """child node -> parent node, for walking back up the tree."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


#: Exception names that mark a guarded, optional import.
OPTIONAL_HANDLERS = {"ImportError", "ModuleNotFoundError", "Exception",
                     "BaseException"}


def is_guarded(node, parents):
    """True when the import sits in a `try` whose handler catches ImportError.

    Optional dependencies are imported that way on purpose - `keyring` in
    `secrets_store` is a deliberate fallback. Reporting those would give the
    audit a false-positive rate high enough that it gets ignored, which is how a
    gate stops being a gate.
    """
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Try):
            for handler in current.handlers:
                caught = handler.type
                if caught is None:                       # bare `except:`
                    return True
                names = []
                if isinstance(caught, ast.Tuple):
                    names = [element.id for element in caught.elts
                             if isinstance(element, ast.Name)]
                elif isinstance(caught, ast.Name):
                    names = [caught.id]
                if any(name in OPTIONAL_HANDLERS for name in names):
                    return True
        # Do not walk past a function boundary: a try in the caller does not
        # guard an import in a callee.
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                pass
        current = parents.get(current)
    return False


def imports_in(path):
    """Every import statement, with the line number that holds it.

    Import statements guarded by an ImportError handler are marked optional, so
    the audit can skip the packages that are meant to be absent.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    parents = parent_map(tree)
    package = package_of(path)

    for node in ast.walk(tree):
        optional = is_guarded(node, parents)
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, resolve(package, 0, alias.name), None, \
                    alias.name, optional
        elif isinstance(node, ast.ImportFrom):
            module = resolve(package, node.level, node.module or "")
            names = [alias.name for alias in node.names if alias.name != "*"]
            # `from x import y, z` is one statement, but a handler may guard
            # only the statement, so optionality applies to all names in it.
            yield node.lineno, module, names, node.module or "", optional


class ImportResolutionTests(unittest.TestCase):
    def test_every_imported_module_exists(self):
        problems = []
        optional_seen = []
        for path in source_files():
            for lineno, module, _names, _raw, optional in imports_in(path):
                if not module:
                    continue
                if optional:
                    optional_seen.append(module)
                    continue
                try:
                    found = importlib.util.find_spec(module)
                except (ImportError, AttributeError, ValueError) as exc:
                    problems.append(f"{path.relative_to(ROOT)}:{lineno} "
                                    f"cannot import {module!r} ({exc})")
                    continue
                if found is None:
                    problems.append(f"{path.relative_to(ROOT)}:{lineno} "
                                    f"no module named {module!r}")
        self.assertEqual(problems, [],
                         "unresolvable imports:\n  " + "\n  ".join(problems))
        # Sanity: the guard detection must actually be skipping something, or it
        # is silently disabling the whole audit.
        self.assertIn("keyring", optional_seen,
                      "optional-import detection found nothing; it may be broken")

    def test_every_imported_name_exists(self):
        """`from m import n` must find `n`, not just `m`.

        This is the half that actually broke: the module path was wrong, but a
        wrong *name* inside a right module fails identically at runtime.
        """
        problems = []
        for path in source_files():
            for lineno, module, names, _raw, optional in imports_in(path):
                if not module or not names or optional:
                    continue
                try:
                    imported = importlib.import_module(module)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{path.relative_to(ROOT)}:{lineno} "
                                    f"importing {module!r} raised "
                                    f"{type(exc).__name__}: {exc}")
                    continue
                for name in names:
                    if hasattr(imported, name):
                        continue
                    # `from package import submodule` is valid even though the
                    # attribute is not set until the submodule is imported, so
                    # check that before calling it a mistake.
                    try:
                        importlib.import_module(f"{module}.{name}")
                        continue
                    except Exception:  # noqa: BLE001
                        problems.append(f"{path.relative_to(ROOT)}:{lineno} "
                                        f"{module!r} has no attribute {name!r}")
        self.assertEqual(problems, [],
                         "imports that would fail at runtime:\n  "
                         + "\n  ".join(problems))

    def test_deferred_imports_are_covered(self):
        """The bug was inside a function; make sure the walk sees those."""
        target = ROOT / "bbpull" / "gui_qt" / "window.py"
        source = target.read_text(encoding="utf-8").splitlines()
        deferred = [line for line in imports_in(target)
                    if source[line[0] - 1].startswith((" ", "\t"))]
        self.assertTrue(deferred, "expected deferred imports in window.py")

    def test_guarded_imports_are_recognised(self):
        """`keyring` is optional on purpose and must not be reported."""
        store = ROOT / "bbpull" / "secrets_store.py"
        guarded = [module for _line, module, _n, _r, optional in imports_in(store)
                   if optional]
        self.assertIn("keyring", guarded)

    def test_unguarded_import_is_reported(self):
        """The guard must not swallow a genuine mistake."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text("from .nowhere import thing\n", encoding="utf-8")
            found = list(imports_in(sample))
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0][4], "an unguarded import must not be optional")

    def test_the_known_regression_is_fixed(self):
        """The exact line that shipped broken in v0.1.1."""
        source = (ROOT / "bbpull" / "gui_qt" / "window.py").read_text(
            encoding="utf-8")
        self.assertNotIn("from ..gui.wizard import", source)
        self.assertIn("from ..wizard import", source)


class ImportHygieneTests(unittest.TestCase):
    def test_no_module_imports_itself(self):
        """`from . import sibling` is legal; `from . import itself` is not.

        Compared against the module's own dotted name, not its package:
        `bbpull/announcements.py` doing `from . import bbml` resolves to the
        package `bbpull` and is perfectly fine.
        """
        for path in source_files():
            own = module_name_of(path)
            for lineno, module, _names, _raw, _optional in imports_in(path):
                self.assertNotEqual(
                    module, own,
                    f"{path.relative_to(ROOT)}:{lineno} imports itself")

    def test_no_wildcard_imports(self):
        """`import *` hides exactly the kind of mistake this file exists to catch."""
        offenders = []
        for path in source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if any(alias.name == "*" for alias in node.names):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], f"wildcard imports: {offenders}")


if __name__ == "__main__":
    unittest.main()
