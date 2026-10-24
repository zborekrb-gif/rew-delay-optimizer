# -*- coding: utf-8 -*-
"""Bezpieczny zapis ostatnich ustawień GUI.

Preferencje nie są wymagane do pracy aplikacji. Gdy plik JSON jest uszkodzony
albo nie można go zapisać, aplikacja ma działać dalej z wartościami domyślnymi.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

PREFS_FILENAME = "rew_delay_optimizer_last_config.json"


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def preferences_path() -> Path:
    return application_dir() / PREFS_FILENAME


def load_app_preferences() -> Dict[str, Any]:
    path = preferences_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_app_preferences(data: Dict[str, Any]) -> Path:
    path = preferences_path()
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return path
