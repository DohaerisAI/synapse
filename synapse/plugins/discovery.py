from __future__ import annotations

import json
from pathlib import Path

from .types import PluginManifest


def discover_plugins(*search_paths: Path) -> list[PluginManifest]:
    manifests: list[PluginManifest] = []
    seen: set[str] = set()
    for base in search_paths:
        if not base.exists():
            continue
        for manifest_path in sorted(base.glob("*/plugin.json")):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = PluginManifest.model_validate(data)
                if manifest.id not in seen:
                    if not manifest.entry_point:
                        manifest.entry_point = str(manifest_path.parent)
                    manifests.append(manifest)
                    seen.add(manifest.id)
            except Exception:
                continue
    return manifests
