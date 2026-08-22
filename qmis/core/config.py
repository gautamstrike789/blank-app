"""Runtime settings loader.

Precedence: environment variable > ``settings.yaml`` > built-in default.
Everything is plain data so the settings page can display and edit it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

_ENV_OVERRIDES = {
    "database.url": "QMIS_DATABASE_URL",
    "storage.inbox": "QMIS_INBOX",
    "storage.archive": "QMIS_ARCHIVE",
    "storage.backend": "QMIS_STORAGE_BACKEND",
    "notifications.enabled": "QMIS_NOTIFICATIONS_ENABLED",
    "auth.mode": "QMIS_AUTH_MODE",
    "auth.demo_role": "QMIS_DEMO_ROLE",
}

_DEFAULTS: dict[str, Any] = {
    "database": {"url": None},
    "storage": {
        "backend": "local",
        "inbox": "data/inbox",
        "archive": "data/processed",
        "archive_after_load": True,
    },
    "evaluation": {
        "grain": "weekly",
        "trailing_window": 4,
        "rolling_window": 4,
        "rolling_levels": ["ba", "team", "owner"],
        "confidence": 0.90,
        "quality_score_bands": [[90, "Excellent"], [75, "Healthy"], [60, "Attention required"], [0, "Critical"]],
    },
    "alerts": {"family_group_min_size": 3, "max_alerts_per_digest": 15},
    "notifications": {
        "enabled": False,
        "send_severities": ["RED", "ORANGE"],
        "send_improvements": True,
        "max_improvements": 3,
        "repeat_after_periods": 3,
        "channels": [],
    },
    "auth": {"mode": "header", "header_name": "X-Forwarded-Email", "demo_role": "admin"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "1"):
        return True
    if lowered in ("false", "no", "0"):
        return False
    return text


class Settings:
    """Dotted-path access over the merged settings tree."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def get(self, path: str, default: Any = None) -> Any:
        env = _ENV_OVERRIDES.get(path)
        if env and os.environ.get(env) is not None:
            return _coerce(os.environ[env])
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        """A whole settings block, with environment overrides already applied.

        Returning the raw dict here silently defeated QMIS_INBOX and friends:
        callers that took a section never saw the override that callers using
        get() did.
        """
        value = self._data.get(name, {})
        out = dict(value) if isinstance(value, dict) else {}
        prefix = f"{name}."
        for path in _ENV_OVERRIDES:
            if not path.startswith(prefix):
                continue
            key = path[len(prefix) :]
            if "." in key:
                continue
            override = self.get(path, "__missing__")
            if override != "__missing__":
                out[key] = override
        return out

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    @property
    def score_bands(self) -> list[tuple[float, str]]:
        raw = self.get("evaluation.quality_score_bands") or []
        return [(float(floor), str(label)) for floor, label in raw]


_SETTINGS: Settings | None = None


def load_settings(path: str | Path | None = None, reload: bool = False) -> Settings:
    global _SETTINGS
    if _SETTINGS is not None and not reload and path is None:
        return _SETTINGS
    target = Path(path or SETTINGS_FILE)
    raw = {}
    if target.exists():
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    _SETTINGS = Settings(_deep_merge(_DEFAULTS, raw))
    return _SETTINGS
