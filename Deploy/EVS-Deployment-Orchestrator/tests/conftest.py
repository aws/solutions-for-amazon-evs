"""Pytest bootstrap: make the repo's modules importable.

Two quirks this handles:
- ``spec-generator/`` has a hyphen, so it isn't importable as a package.
  We load its modules under the alias ``spec_generator`` so tests can
  ``from spec_generator import cidr``.
- ``orchestrator/evs_environment`` modules are imported directly by path
  for the parity tests.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_GEN_DIR = REPO_ROOT / "spec-generator"


def _load_module(alias: str, path: Path):
    """Load a single file as a module under ``alias`` (idempotent)."""
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


# Expose spec-generator modules as spec_generator.<name>.
import types

_pkg = types.ModuleType("spec_generator")
_pkg.__path__ = [str(SPEC_GEN_DIR)]
sys.modules.setdefault("spec_generator", _pkg)

for _name in ("cidr", "constants", "password_rules"):
    _mod = _load_module(f"spec_generator.{_name}", SPEC_GEN_DIR / f"{_name}.py")
    setattr(_pkg, _name, _mod)
