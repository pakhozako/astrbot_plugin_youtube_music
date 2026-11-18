"""Create test-local directory links without following them during cleanup."""

import os
import unittest
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def directory_link(link: Path, target: Path):
    parent = link.parent.resolve(strict=True)
    resolved_target = target.resolve(strict=True)
    if resolved_target == parent or not resolved_target.is_relative_to(parent):
        raise ValueError("directory link target must stay inside the test directory")
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        raise unittest.SkipTest(f"directory links are unavailable: {type(exc).__name__}") from exc
    try:
        yield link
    finally:
        if link.is_symlink():
            link.unlink()
        elif link.is_junction():
            link.rmdir()
