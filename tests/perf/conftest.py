"""Perf test fixtures."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--perf-baseline",
        action="store",
        default=None,
        help="Path to baseline JSON for comparison",
    )
