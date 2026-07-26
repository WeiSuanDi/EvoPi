"""Plugin file-system discovery and importlib loading.

Mirrors Pi's extension pattern: scan ``~/.evopi/plugins/`` (global) and
``<project>/.evopi/plugins/`` (local), load ``.py`` modules, find ``Plugin``
implementations, validate dependencies.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from evopi.plugins.protocol import Plugin

_logger = logging.getLogger(__name__)

_PLUGIN_DIR_NAME = "plugins"


# =============================================================================
# File-system discovery
# =============================================================================


def discover_plugin_paths(
    workspace: str | Path,
    root: str | Path | None = None,
) -> list[Path]:
    """Return absolute paths to all discovered plugin files.

    Discovery order (local wins on name collision):
    1. ``<workspace>/.evopi/plugins/`` — project-local
    2. ``~/.evopi/plugins/`` — global
    """
    discovered: list[Path] = []
    seen: set[str] = set()
    ws_path = Path(workspace).expanduser().resolve()

    local_dir = ws_path / ".evopi" / _PLUGIN_DIR_NAME
    for p in _scan_dir(local_dir):
        key = p.name if p.is_file() else p.name
        if key not in seen:
            seen.add(key)
            discovered.append(p)

    global_root = Path(root).expanduser().resolve() if root else Path.home() / ".evopi"
    global_dir = global_root / _PLUGIN_DIR_NAME
    for p in _scan_dir(global_dir):
        key = p.name if p.is_file() else p.name
        if key not in seen:
            seen.add(key)
            discovered.append(p)

    return discovered


def _scan_dir(directory: Path) -> list[Path]:
    """Scan one directory for plugin files (recursively)."""
    if not directory.exists():
        return []
    if not directory.is_dir():
        return []
    result: list[Path] = []
    try:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_"):
                result.append(entry)
            elif entry.is_dir() and not entry.name.startswith("_"):
                if (entry / "__init__.py").exists() or (entry / "plugin.py").exists():
                    result.append(entry)
    except OSError:
        pass
    return result


# =============================================================================
# Module loading
# =============================================================================


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

    entry = _resolve_entry_point(resolved)
    if entry is None:
        return None

    module = _import_module(entry)
    if module is None:
        return None

    return _find_plugin_instance(module)


def _resolve_entry_point(path: Path) -> Path | None:
    """Resolve a directory to its entry-point ``.py`` file."""
    if path.is_file():
        return path
    if path.is_dir():
        manifest = path / "plugin.py"
        if manifest.exists():
            return manifest
        init = path / "__init__.py"
        if init.exists():
            return init
    _logger.warning("Plugin path has no entry point: %s", path)
    return None


def _import_module(file_path: Path) -> object | None:
    """Import a ``.py`` file as a unique module and return the module object."""
    safe_name = file_path.stem.replace("-", "_").replace(".", "_")
    module_name = f"_evopi_plugin_{safe_name}_{hash(str(file_path)) & 0xFFFFFFFF}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(file_path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        _logger.exception("Failed to load plugin: %s", file_path)
        return None


def _find_plugin_instance(module: object) -> Plugin | None:
    """Find the first :class:`Plugin` subclass in *module* and instantiate it."""
    for name in dir(module):
        obj = getattr(module, name)
        if not (isinstance(obj, type) and issubclass(obj, Plugin) and obj is not Plugin):
            continue
        try:
            instance = obj()
            # Verify meta is accessible (not an unimplemented abstract property)
            _ = instance.meta
            return instance
        except Exception:
            _logger.exception("Failed to instantiate Plugin subclass in %r", module)
            return None
    _logger.warning("No Plugin subclass found in %r", module)
    return None


# =============================================================================
# PluginLoader — discovery + validation
# =============================================================================


class PluginLoader:
    """Discovers, loads, and validates plugins from the filesystem.

    Does **not** call ``register()`` — that is the harness's responsibility.
    """

    def __init__(
        self,
        workspace: str | Path,
        root: str | Path | None = None,
        extra_paths: list[str | Path] | None = None,
        *,
        discover_defaults: bool = True,
    ) -> None:
        self._workspace = Path(workspace)
        self._plugins: list[Plugin] = []
        self._source_paths: dict[str, Path] = {}
        self._errors: list[str] = []
        self._invalid_plugins: set[str] = set()

        if discover_defaults:
            for path in discover_plugin_paths(workspace, root):
                self._try_load(path)
        for ep in (extra_paths or []):
            self._try_load(Path(ep).expanduser().resolve())

        self._validate_dependencies()

    # -- internal -------------------------------------------------------------

    def _try_load(self, path: Path) -> None:
        plugin = load_plugin(path)
        if plugin is None:
            self._errors.append(f"Failed to load plugin at {path}")
            return
        name = plugin.meta.name
        if name in self._source_paths:
            self._errors.append(f"Duplicate plugin name '{name}' — first occurrence kept")
            return
        self._plugins.append(plugin)
        self._source_paths[name] = path
        manifest_root = _find_manifest_root(path)
        if manifest_root is not None:
            from evopi.plugins.candidates import (
                PluginCandidateError,
                review_plugin,
            )

            try:
                manifest = review_plugin(manifest_root).candidate.manifest
            except PluginCandidateError as exc:
                self._invalid_plugins.add(name)
                self._errors.append(
                    f"Plugin '{name}' approved manifest is invalid: {exc}"
                )
            else:
                if (
                    plugin.meta.name != manifest.name
                    or plugin.meta.version != manifest.version
                    or plugin.meta.dependencies != manifest.dependencies
                ):
                    self._invalid_plugins.add(name)
                    self._errors.append(
                        f"Plugin '{name}' runtime metadata does not match its "
                        "approved manifest"
                    )

    def _validate_dependencies(self) -> None:
        names = {p.meta.name for p in self._plugins}
        for plugin in self._plugins:
            for dep in plugin.meta.dependencies:
                if dep not in names:
                    self._invalid_plugins.add(plugin.meta.name)
                    self._errors.append(
                        f"Plugin '{plugin.meta.name}' requires '{dep}' which is not loaded"
                    )

    # -- public API -----------------------------------------------------------

    @property
    def plugins(self) -> list[Plugin]:
        """Loaded :class:`Plugin` instances (dependency-validated)."""
        return list(self._plugins)

    @property
    def errors(self) -> list[str]:
        """Errors encountered during discovery, loading, or validation."""
        return list(self._errors)

    def add_error(self, message: str) -> None:
        """Append an error message (used by wire_plugins for register failures)."""
        self._errors.append(message)

    def source_of(self, plugin_name: str) -> Path | None:
        """Return the filesystem path from which *plugin_name* was loaded."""
        return self._source_paths.get(plugin_name)

    def is_loaded(self, name: str) -> bool:
        """Return ``True`` if a plugin named *name* is loaded."""
        return any(p.meta.name == name for p in self._plugins)

    def is_valid(self, name: str) -> bool:
        """Return whether static dependency validation permits activation."""

        return name not in self._invalid_plugins


def _find_manifest_root(path: Path) -> Path | None:
    resolved = path.expanduser().resolve()
    start = resolved if resolved.is_dir() else resolved.parent
    for directory in (start, *start.parents):
        if (directory / "evopi-plugin.json").is_file():
            return directory
    return None


__all__ = ["PluginLoader", "discover_plugin_paths", "load_plugin"]
