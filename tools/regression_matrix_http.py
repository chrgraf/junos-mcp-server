#!/usr/bin/env python3
"""Compatibility wrapper.

The selftest scripts were moved to regression_selftest/.
This wrapper keeps the old path working.
"""

from __future__ import annotations

import os
import runpy
import sys


def _run() -> None:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    runpy.run_module("regression_selftest.regression_matrix_http", run_name="__main__")


if __name__ == "__main__":
    _run()
