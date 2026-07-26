"""Discovering processor plugins.

Three ways to make a processor available, in increasing order of how much the
operator has to do:

1. **Entry points** (``eeg_mcp.processors``) -- ``pip install`` a package and its
   processors appear. This is the distribution path.
2. **``EEG_MCP_PLUGINS``** -- a comma-separated list of ``module:ClassName``.
   For code that lives in your own project and is not packaged.
3. **``EEG_MCP_PLUGIN_DIR``** -- a directory of ``.py`` files. Drop a file in
   and restart. The quickest path while developing.

**Why an agent cannot simply name an import path.** Loading a module executes
it. A tool that accepted an arbitrary dotted path from an agent would be
arbitrary code execution driven by a language model. So discovery is entirely
operator-controlled -- through the environment of the server process -- and the
agent may only attach processors that are already in the registry.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
from pathlib import Path
from typing import Any

from .base import Processor

ENTRY_POINT_GROUP = "eeg_mcp.processors"

_registry: dict[str, type[Processor]] = {}
_sources: dict[str, str] = {}
_load_errors: list[dict[str, str]] = []
_loaded = False


def _register(cls: type[Processor], source: str) -> None:
    if not isinstance(cls, type) or not issubclass(cls, Processor):
        return
    if cls is Processor or inspect.isabstract(cls):
        return
    name = getattr(cls, "name", "") or ""
    if not name:
        _load_errors.append({
            "source": source,
            "error": f"{cls.__qualname__} has no 'name'; it cannot be referenced.",
        })
        return
    if name in _registry and _registry[name] is not cls:
        _load_errors.append({
            "source": source,
            "error": f"Name '{name}' already registered by {_sources.get(name)}; "
                     f"{cls.__qualname__} was skipped. Rename one of them.",
        })
        return
    _registry[name] = cls
    _sources[name] = source


def _load_builtins() -> None:
    from . import builtins as builtin_module

    for _attr, obj in vars(builtin_module).items():
        if isinstance(obj, type) and issubclass(obj, Processor):
            _register(obj, "builtin")


def _load_entry_points() -> None:
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - Python < 3.8
        return
    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover - older API shape
        found = entry_points().get(ENTRY_POINT_GROUP, [])
    for entry in found:
        try:
            _register(entry.load(), f"entry_point:{entry.value}")
        except Exception as exc:
            _load_errors.append({
                "source": f"entry_point:{getattr(entry, 'value', entry)}",
                "error": f"{type(exc).__name__}: {exc}",
            })


def _load_env_plugins() -> None:
    raw = os.environ.get("EEG_MCP_PLUGINS", "").strip()
    if not raw:
        return
    for spec in (s.strip() for s in raw.split(",") if s.strip()):
        try:
            if ":" not in spec:
                raise ValueError(
                    f"'{spec}' must be 'module:ClassName' (or "
                    "'module:' to load every Processor in the module)"
                )
            module_name, _, class_name = spec.partition(":")
            module = importlib.import_module(module_name)
            if class_name:
                _register(getattr(module, class_name), f"env:{spec}")
            else:
                for obj in vars(module).values():
                    _register(obj, f"env:{module_name}")
        except Exception as exc:
            _load_errors.append({
                "source": f"env:{spec}", "error": f"{type(exc).__name__}: {exc}"
            })


def _load_plugin_dir() -> None:
    raw = os.environ.get("EEG_MCP_PLUGIN_DIR", "").strip()
    if not raw:
        return
    directory = Path(raw).expanduser()
    if not directory.is_dir():
        _load_errors.append({
            "source": f"dir:{directory}", "error": "not a directory"
        })
        return
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"_eeg_mcp_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            for obj in vars(module).values():
                _register(obj, f"dir:{path.name}")
        except Exception as exc:
            _load_errors.append({
                "source": f"dir:{path.name}", "error": f"{type(exc).__name__}: {exc}"
            })


def load(force: bool = False) -> None:
    """Populate the registry. Idempotent unless ``force``."""
    global _loaded
    if _loaded and not force:
        return
    if force:
        _registry.clear()
        _sources.clear()
        _load_errors.clear()
    _load_builtins()
    _load_entry_points()
    _load_env_plugins()
    _load_plugin_dir()
    _loaded = True


def available() -> list[dict[str, Any]]:
    """Every discovered processor, with where it came from."""
    load()
    out = []
    for name, cls in sorted(_registry.items()):
        out.append({
            "name": name,
            "description": getattr(cls, "description", ""),
            "streaming": getattr(cls, "streaming", False),
            "uses": getattr(cls, "uses", "filtered"),
            "params": {k: {"default": d, "doc": doc}
                       for k, (d, doc) in getattr(cls, "params", {}).items()},
            "source": _sources.get(name, "?"),
        })
    return out


def load_errors() -> list[dict[str, str]]:
    """Plugins that failed to load, so a typo is visible rather than silent."""
    load()
    return list(_load_errors)


def create(name: str, params: dict[str, Any] | None = None) -> Processor:
    """Instantiate a discovered processor by name."""
    load()
    key = str(name).strip()
    if key not in _registry:
        raise ValueError(
            f"Unknown processor '{name}'. Available: "
            f"{', '.join(sorted(_registry)) or 'none'}. To add your own, set "
            "EEG_MCP_PLUGINS or EEG_MCP_PLUGIN_DIR in the server environment "
            "and restart -- processors cannot be loaded from a tool call."
        )
    return _registry[key](**(params or {}))
