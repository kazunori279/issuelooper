"""pytest plugin for sweep_run.py's runs.jsonl: each pytest run in a sweep writes its own
junit XML, $SWEEP_JUNIT_PREFIX-<n>.xml (n counts up from 1), with per-test times.
Loaded with `-p sweep_junit` through PYTEST_ADDOPTS, with this directory on PYTHONPATH."""

import os

import pytest


@pytest.hookimpl(tryfirst=True)  # before pytest's own junitxml plugin reads the option
def pytest_configure(config):
    prefix = os.environ.get("SWEEP_JUNIT_PREFIX")
    if not prefix or getattr(config.option, "xmlpath", None):
        return  # not in a recorded sweep, or the project names its own XML
    n = 1
    while True:
        try:
            fd = os.open(f"{prefix}-{n}.xml", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            break
        except FileExistsError:
            n += 1
    config.option.xmlpath = f"{prefix}-{n}.xml"
