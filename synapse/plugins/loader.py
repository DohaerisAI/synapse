from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .registry import PluginRegistry
from .types import PluginManifest, PluginRecord


def load_plugin(manifest: PluginManifest, registry: PluginRegistry) -> PluginRecord:
    record = PluginRecord(
        plugin_id=manifest.id,
        manifest=manifest,
        loaded=False,
    )
    entry = manifest.entry_point
    if not entry:
        record.error = "no entry_point specified"
        registry.register(record)
        return record

    entry_path = Path(entry)
    if entry_path.is_dir():
        init_file = entry_path / "__init__.py"
        if init_file.exists():
            entry = str(init_file)
        else:
            record.loaded = True
            registry.register(record)
            return record

    if entry_path.is_file() and entry.endswith(".py"):
        try:
            module_name = f"_plugin_{manifest.id.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(module_name, entry)
            if spec is None or spec.loader is None:
                record.error = f"cannot load module from {entry}"
                registry.register(record)
                return record
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            record.loaded = True
        except Exception as error:
            record.error = str(error)
    elif "." in entry:
        try:
            importlib.import_module(entry)
            record.loaded = True
        except Exception as error:
            record.error = str(error)
    else:
        record.error = f"unrecognized entry_point: {entry}"

    registry.register(record)
    return record


def load_all(manifests: list[PluginManifest], registry: PluginRegistry) -> list[PluginRecord]:
    return [load_plugin(manifest, registry) for manifest in manifests]
