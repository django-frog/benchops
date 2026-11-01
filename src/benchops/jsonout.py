"""Machine-readable output (`--json`).

In JSON mode stdout carries exactly one JSON document, so a script or an AI
agent can parse it. Everything a command prints for people (progress, the
plan, hook and build output, warnings) goes to stderr instead.
"""

import json
import os
import sys
from contextlib import contextmanager, redirect_stdout

SCHEMA_VERSION = 1


@contextmanager
def human_output_to_stderr():
    """Send everything written to stdout inside the block to stderr: Python
    writes (Rich, print) through sys.stdout, and child processes that
    inherited file descriptor 1 through a dup2. Restored on exit."""
    sys.stdout.flush()
    try:
        saved = os.dup(1)
    except OSError:  # no usable fd 1 (e.g. an embedded interpreter)
        saved = None
    if saved is not None:
        os.dup2(2, 1)
    try:
        with redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stderr.flush()
        if saved is not None:
            os.dup2(saved, 1)
            os.close(saved)


def emit(document: dict) -> None:
    """Write one JSON document to stdout."""
    sys.stdout.write(json.dumps({"schema": SCHEMA_VERSION, **document}, indent=2, default=str) + "\n")
    sys.stdout.flush()
