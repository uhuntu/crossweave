"""The xweave forwarding package: the alternate spelling, pinned.

`packaging/xweave/` ships to PyPI as its own project whose only job is
to hold the name and install crossweave. A forwarding package rots
silently -- a bumped crossweave, an edited dependency, a renamed
directory -- so this file pins the contract: the name it holds, the
package it forwards to, and the version mirror (xweave==N means
crossweave N, so the two bump together or the pin fails).
"""

import os
import re

import pytest

from crossweave import __version__ as crossweave_version

# Relative to the repo root, not the cwd: several test modules chdir into
# tmp_path, and the pin must read the same files wherever it runs from.
PKG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "packaging", "xweave")


def _read(name):
    with open(os.path.join(PKG_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _field(text, key):
    m = re.search(rf'^{key} = "(.*?)"$', text, re.M)
    return m.group(1) if m else None


def test_the_forwarding_package_pins():
    pyproject = _read("pyproject.toml")
    assert _field(pyproject, "name") == "xweave"
    deps = re.search(r"^dependencies = \[(.*?)\]$", pyproject, re.S | re.M)
    assert deps and "crossweave>=" in deps.group(1), \
        "xweave must depend on crossweave -- that forwarding is its whole job"
    # the mirror: xweave==N means crossweave N. Two version lines drift
    # apart the first time one is bumped and the other forgotten, and a
    # placeholder pointing at a version that was never published fails
    # to install at all.
    assert _field(pyproject, "version") == crossweave_version
    assert _field(_read("xweave/__init__.py"), "__version__") == crossweave_version


def test_the_pypi_page_names_the_real_project():
    """The forwarding package's README must say what it forwards to --
    a placeholder that doesn't is a dead end for anyone who lands on it."""
    readme = _read("README.md")
    assert "crossweave" in readme and "github.com/uhuntu/crossweave" in readme
    assert "cw" in readme
