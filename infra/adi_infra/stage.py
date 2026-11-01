"""Stage pure-Python bundles for assets (no Docker needed at synth time)."""
from __future__ import annotations

import shutil
from pathlib import Path

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def stage(out: Path, packages: list[Path]) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for pkg in packages:
        if not (pkg / "__init__.py").exists():
            raise FileNotFoundError(f"{pkg} is not a Python package")
        shutil.copytree(pkg, out / pkg.name, ignore=IGNORE)
    return out
