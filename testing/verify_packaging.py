"""Pre-flight checks for publishing to PyPI and the MCP registry.

Written after a real failure: ``server.json`` carried a 186-character
description against the registry's 100-character limit, which was only
discovered when the registry rejected the submission with HTTP 422 -- *after*
the PyPI upload had already succeeded and could not be repeated.

Everything here is cheap and offline-ish, and it runs before the publish jobs so
that class of failure is caught in seconds rather than halfway through a
release. The registry schema is fetched live so the checks track upstream rather
than a stale copy; if the network is unavailable the schema check reports SKIP
instead of failing, since a local run should not depend on it.

Run:  python testing/verify_packaging.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSED, FAILED, SKIPPED = 0, 0, 0


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")
    return bool(condition)


def skip(name: str, why: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {name}  ({why})")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_versions() -> str:
    print("\n[versions] one release, one number")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    init = (ROOT / "eeg_mcp" / "__init__.py").read_text(encoding="utf-8")
    server = load_json(ROOT / "server.json")

    found = {
        "pyproject.toml": re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1),
        "eeg_mcp/__init__.py": re.search(r'__version__ = "([^"]+)"', init).group(1),
        "server.json:version": server["version"],
        "server.json:packages[0]": server["packages"][0]["version"],
    }
    unique = set(found.values())
    check("version identical in all four locations", len(unique) == 1,
          ", ".join(f"{k}={v}" for k, v in found.items()) if len(unique) > 1
          else next(iter(unique)))
    return next(iter(unique)) if len(unique) == 1 else ""


def verify_registry_schema() -> None:
    """Validate server.json against the schema the registry actually enforces."""
    print("\n[registry] server.json against the live schema")
    server = load_json(ROOT / "server.json")
    schema_url = server.get("$schema")
    if not check("server.json declares $schema", bool(schema_url)):
        return

    try:
        with urllib.request.urlopen(schema_url, timeout=20) as response:
            schema = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        skip("schema validation", f"could not fetch {schema_url}: {exc}")
        return

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        skip("schema validation", "pip install jsonschema (in the dev extra)")
        return

    errors = list(Draft202012Validator(schema).iter_errors(server))
    if errors:
        for err in errors:
            field = ".".join(str(p) for p in err.path) or "(root)"
            actual = (f"{len(err.instance)} chars" if isinstance(err.instance, str)
                      else repr(err.instance)[:40])
            check(f"server.json field '{field}'", False,
                  f"{err.validator}={err.validator_value}, got {actual}")
    else:
        check("server.json validates against the registry schema", True,
              f"{len(server)} top-level fields")

    # Called out separately because it is the limit that actually bit us, and a
    # generic "schema invalid" message buries it.
    description = server.get("description", "")
    check("description within the registry's 100-char limit",
          len(description) <= 100, f"{len(description)} chars")


def verify_ownership_marker(version: str) -> None:
    """The registry proves namespace ownership via a marker in the README."""
    print("\n[ownership] mcp-name marker")
    server = load_json(ROOT / "server.json")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected = f"<!-- mcp-name: {server['name']} -->"

    first_line = readme.splitlines()[0] if readme else ""
    check("README's first line is the mcp-name marker",
          first_line.strip() == expected, repr(first_line[:70]))
    check("marker matches server.json name", expected in readme, server["name"])
    check("name uses the io.github.<owner>/<repo> form",
          re.fullmatch(r"io\.github\.[\w.-]+/[\w.-]+", server["name"]) is not None,
          server["name"])


def verify_package_metadata(version: str) -> None:
    print("\n[pypi] package metadata")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    server = load_json(ROOT / "server.json")

    check("server.json identifier matches the PyPI distribution name",
          server["packages"][0]["identifier"] == "eeg-mcp",
          server["packages"][0]["identifier"])
    check("registryType is pypi",
          server["packages"][0]["registryType"] == "pypi")
    check("transport is stdio",
          server["packages"][0].get("transport", {}).get("type") == "stdio")

    for required in ("[project.scripts]", "readme = ", "license = ",
                     "requires-python"):
        check(f"pyproject declares {required.strip()}", required in pyproject)

    # Per-version classifiers drive PyPI's sidebar and search facets; without
    # them the pyversions badge renders an uninformative bare "python: 3".
    versions = re.findall(r'"Programming Language :: Python :: (\d+\.\d+)"', pyproject)
    check("per-version Python classifiers present", len(versions) >= 3,
          ", ".join(versions) or "none")

    for f in ("LICENSE", "NOTICE", "CHANGELOG.md", "MANIFEST.in"):
        check(f"{f} present", (ROOT / f).exists())

    if version:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        check(f"CHANGELOG documents {version}", f"[{version}]" in changelog)


def main() -> int:
    print(f"packaging pre-flight for {ROOT.name}")
    version = verify_versions()
    verify_registry_schema()
    verify_ownership_marker(version)
    verify_package_metadata(version)

    print(f"\n{'=' * 60}")
    print(f"  PACKAGING: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    print(f"{'=' * 60}")
    if FAILED:
        print("\n  Fix these before tagging -- a PyPI upload cannot be repeated,")
        print("  so a registry rejection after a successful upload is awkward to")
        print("  recover from.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
