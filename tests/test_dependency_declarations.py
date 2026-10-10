"""Dependency declarations match reality (CI S4, A1 F21-F23, A2 F-26).

Three drifts this guards against:

* A module imports a third-party package that pyproject.toml does not
  declare. `pip install .` then builds an image that cannot boot
  (argon2, bcrypt, joserfc, sqlcipher3, fastapi were all undeclared).
* pyproject declares a package nothing imports (authlib: its advisories
  were unreachable but still shipped and still flagged by scanners).
* A floor drifts back below the first release with no known HIGH or
  CRITICAL advisory (cryptography, python-multipart, joserfc, Pillow).

The scan is static (ast), so lazily imported and function-local imports
count, and nothing is executed. pkgutil finds the first-party packages,
importlib.metadata maps import names to distributions.
"""

from __future__ import annotations

import ast
import functools
import pkgutil
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

# Root-level directories that never hold first-party runtime source.
SKIP_ROOT_DIRS = {
    "tests",
    "web",
    "node_modules",
    "artifacts",
    "docs",
    "schemas",
    "workflows",
    "infra",
    "video_factory.egg-info",
}

# Declared runtime dependencies that no module imports, with the reason.
# Anything else declared but never imported fails the test (authlib did).
DECLARED_BUT_NOT_IMPORTED = {
    "uvicorn": "ASGI server started by the Dockerfile / systemd CMD, never imported",
    "python-multipart": (
        "FastAPI imports it itself to parse Form(...) and UploadFile; "
        "without it those routes raise at import time"
    ),
}

# First release with no known HIGH or CRITICAL advisory (OSV, 2026-10-10).
# A floor below these re-admits a vulnerable version to `pip install`.
SECURITY_FLOORS = {
    "cryptography": "50.0.0",  # GHSA-g6cj-pr64-35w5, GHSA-537c-gmf6-5ccf, GHSA-r6ph-v2qm-q3c2
    "python-multipart": "0.0.31",  # GHSA-pp6c-gr5w-3c5g, GHSA-wp53-j4wj-2cfg, GHSA-5rvq-cxj2-64vf
    "joserfc": "1.6.8",  # GHSA-gg9x-qcx2-xmrh, GHSA-w5r5-m38g-f9f9
    "sentry-sdk": "2.8.0",  # LOW only, GHSA-g92j-qhmh-64v2; kept so the floors audit is clean
}
SECURITY_FLOORS_OPTIONAL = {
    "pillow": "12.3.0",  # postprocess extra; many HIGH fixed in 12.3.0
}


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(spec: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    assert m, f"unparseable requirement: {spec!r}"
    return _canon(m.group(1))


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group(0)))
    return tuple(parts)


def _floor(spec: str) -> tuple[int, ...] | None:
    m = re.search(r">=\s*([0-9][0-9A-Za-z.]*)", spec)
    return _version_tuple(m.group(1)) if m else None


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def _runtime_specs() -> list[str]:
    return list(_pyproject()["project"]["dependencies"])


def _runtime_names() -> set[str]:
    return {_requirement_name(s) for s in _runtime_specs()}


def _optional_names() -> set[str]:
    extras = _pyproject()["project"].get("optional-dependencies", {})
    return {
        _requirement_name(s)
        for group, specs in extras.items()
        if group != "dev"
        for s in specs
    }


def _source_roots() -> list[Path]:
    """Root-level entries holding first-party python source."""
    roots = []
    for entry in sorted(ROOT.iterdir()):
        if entry.name.startswith(".") or entry.name in SKIP_ROOT_DIRS:
            continue
        if entry.is_dir() and any(entry.rglob("*.py")):
            roots.append(entry)
    return roots


def _first_party_names() -> set[str]:
    # pkgutil sees packages with __init__.py and top-level modules; the
    # directory scan adds namespace packages (api/ has no __init__.py).
    names = {m.name for m in pkgutil.iter_modules([str(ROOT)])}
    names |= {p.name for p in _source_roots()}
    return names


def _source_files() -> list[Path]:
    files = [p for p in sorted(ROOT.glob("*.py"))]
    for root in _source_roots():
        files.extend(sorted(root.rglob("*.py")))
    return [f for f in files if "__pycache__" not in f.parts]


def _imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


@functools.lru_cache(maxsize=None)
def _third_party_imports() -> dict[str, list[str]]:
    stdlib = set(sys.stdlib_module_names)
    first_party = _first_party_names()
    found: dict[str, list[str]] = {}
    for path in _source_files():
        for mod in _imported_top_level(path):
            if mod in stdlib or mod in first_party:
                continue
            found.setdefault(mod, []).append(str(path.relative_to(ROOT)))
    return found


@functools.lru_cache(maxsize=None)
def _module_to_distributions() -> dict[str, list[str]]:
    return metadata.packages_distributions()


def _distributions_for(module: str) -> set[str]:
    return {_canon(d) for d in _module_to_distributions().get(module, [])}


def test_scan_finds_the_code_under_test() -> None:
    """Guard the guard: an empty scan would make the real tests vacuous."""
    rel = {str(p.relative_to(ROOT)) for p in _source_files()}
    assert "api/main.py" in rel
    assert "core/repo.py" in rel
    assert not any(r.startswith("tests/") for r in rel)
    imports = _third_party_imports()
    assert {"fastapi", "httpx", "cryptography"} <= set(imports)


def test_every_third_party_import_is_a_declared_dependency() -> None:
    declared = _runtime_names() | _optional_names()
    missing: list[str] = []
    for module, files in sorted(_third_party_imports().items()):
        dists = _distributions_for(module)
        if not dists:
            missing.append(
                f"{module} (imported by {files[0]}): not installed here, so it "
                "cannot be mapped to a distribution; install the project "
                "dependencies and declare it in pyproject.toml"
            )
        elif not dists & declared:
            missing.append(
                f"{module} (imported by {files[0]}): distribution "
                f"{sorted(dists)} is not declared in pyproject.toml"
            )
    assert not missing, "undeclared third-party imports:\n  " + "\n  ".join(missing)


def test_every_declared_runtime_dependency_is_used() -> None:
    used: set[str] = set()
    for module in _third_party_imports():
        used |= _distributions_for(module)
    unused = sorted(
        name
        for name in _runtime_names()
        if name not in used and name not in DECLARED_BUT_NOT_IMPORTED
    )
    assert not unused, (
        f"declared in pyproject.toml but imported by no module: {unused}. "
        "Drop the pin (A1 F23 did this for authlib) or add it to "
        "DECLARED_BUT_NOT_IMPORTED with the reason."
    )


def test_not_imported_allowlist_has_no_stale_entries() -> None:
    declared = _runtime_names()
    stale = sorted(n for n in DECLARED_BUT_NOT_IMPORTED if n not in declared)
    assert not stale, f"allowlisted but no longer declared: {stale}"
    used: set[str] = set()
    for module in _third_party_imports():
        used |= _distributions_for(module)
    now_imported = sorted(n for n in DECLARED_BUT_NOT_IMPORTED if n in used)
    assert not now_imported, (
        f"{now_imported} are imported now; remove them from DECLARED_BUT_NOT_IMPORTED"
    )


def test_authlib_stays_dropped() -> None:
    assert "authlib" not in _runtime_names() | _optional_names()


def test_security_floors_exclude_vulnerable_releases() -> None:
    specs = {_requirement_name(s): s for s in _runtime_specs()}
    for extra in _pyproject()["project"].get("optional-dependencies", {}).values():
        specs.update({_requirement_name(s): s for s in extra})
    wrong = []
    for name, floor in {**SECURITY_FLOORS, **SECURITY_FLOORS_OPTIONAL}.items():
        spec = specs.get(name)
        declared_floor = _floor(spec) if spec else None
        if declared_floor is None or declared_floor < _version_tuple(floor):
            wrong.append(f"{name}: declared {spec!r}, need >={floor}")
    assert not wrong, "floors below the first advisory-free release:\n  " + "\n  ".join(wrong)


def test_installed_versions_satisfy_declared_floors() -> None:
    """The suite must run against versions the metadata admits to."""
    behind = []
    for spec in _runtime_specs():
        floor = _floor(spec)
        if floor is None:
            continue
        name = _requirement_name(spec)
        try:
            installed_text = metadata.version(name)
        except metadata.PackageNotFoundError:
            behind.append(f"{name}: declared {spec!r} but not installed")
            continue
        if _version_tuple(installed_text) < floor:
            behind.append(f"{name}: installed {installed_text}, declared {spec!r}")
    assert not behind, "environment older than pyproject floors:\n  " + "\n  ".join(behind)
