"""Plugin file-system discovery and importlib loading.

Mirrors Pi's :file:`loader.ts`: scan ``~/.evopi/plugins/`` (global) and
``<project>/.evopi/plugins/`` (local), load ``.py`` modules, find ``Plugin``
implementations.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from evopi.plugins.protocol import Plugin

_logger = logging.getLogger(__name__)

_PLUGIN_DIR_NAME = "plugins"


def discover_plugins(
    workspace: str | Path,
    root: str | Path | None = None,
) -> list[Path]:
    """Return absolute paths to all discovered plugin files.

    Discovery order (same as Pi):
    1. ``<workspace>/.evopi/plugins/`` — project-local
    2. ``~/.evopi/plugins/`` — global
    """
    discovered: list[Path] = []
    seen: set[str] = set()
    ws_path = Path(workspace).expanduser().resolve()

    # 1. Project-local
    local_dir = ws_path / ".evopi" / _PLUGIN_DIR_NAME
    for p in _scan_dir(local_dir):
        if str(p) not in seen:
            seen.add(str(p))
            discovered.append(p)

    # 2. Global
    global_root = Path(root).expanduser().resolve() if root else Path.home() / ".evopi"
    global_dir = global_root / _PLUGIN_DIR_NAME
    for p in _scan_dir(global_dir):
        if str(p) not in seen:
            seen.add(str(p))
            discovered.append(p)

    return discovered


def load_plugin(path: str | Path) -> Plugin | None:
    """Import a ``.py`` file and return the first :class:`Plugin` found.

    Supports:
    - Single ``.py`` file
    - Directory with ``__init__.py`` (Python package)
    - Directory with ``plugin.py`` (manifest entry point)
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        _logger.warning("Plugin path does not exist: %s", resolved)
        return None

    # Determine the module file to import
    if resolved.is_dir():
        manifest = resolved / "plugin.py"
        if manifest.exists():
            resolved = manifest
        else:
            resolved = resolved / "__init__.py"
            if not resolved.exists():
                _logger.warning("Plugin directory has no entry point: %s", path)
                return None

    module_name = f"_evopi_plugin_{resolved.stem}_{hash(str(resolved)) & 0xFFFFFFFF}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(resolved))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception:
        _logger.exception("Failed to load plugin: %s", resolved)
        return None

    # Find the first Plugin implementation in the module
    for name in dir(module):
        obj = getattr(module, name)
        if (
            isinstance(obj, type)
            and issubclass(obj, Plugin)
            and obj is not Plugin
            and hasattr(obj, "meta")
        ):
            instance = obj()
            meta = instance.meta
            # Ensure source_path is set
            object.__setattr__(meta, "source_path", str(resolved))
            return instance

    _logger.warning("No Plugin subclass found in %s", resolved)
    return None


def _scan_dir(directory: Path) -> list[Path]:
    """Scan one directory for plugin files."""
    if not directory.exists():
        return []
    result: list[Path] = []
    try:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_"):
                result.append(entry)
            elif entry.is_dir() and not entry.name.startswith("_"):
                # Directory with __init__.py or plugin.py
                if (entry / "__init__.py").exists() or (entry / "plugin.py").exists():
                    result.append(entry)
    except OSError:
        pass
    return result


__all__ = ["discover_plugins", "load_plugin"]
