"""Versioned user projects. Labels are never scientific identifiers."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
STEPS = ("Pomiary", "Dobór ustawień", "Porównanie i odsłuch")
STAGES = ("Nieustalony", "Bazowe", "Zmiana poziomu", "Przed Dirac", "Po Dirac")
ROLES = ("Nieprzypisany", "L main", "R main", "L sub", "R sub", "L sum", "R sum", "L+R")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid():
    return uuid.uuid4().hex


def number(value, label="Wartość", *, low=None, high=None):
    if value is None or not str(value).strip():
        raise ValueError(f"{label}: wpisz wartość.")
    try:
        result = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"{label}: wpisz liczbę.") from None
    if not math.isfinite(result):
        raise ValueError(f"{label}: NaN/Inf nie jest dopuszczalne.")
    if low is not None and result < low or high is not None and result > high:
        raise ValueError(f"{label}: dozwolony zakres {low}–{high}.")
    return result


def file_path(value):
    if value is None or not str(value).strip():
        raise ValueError("Wskaż plik pomiarowy.")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Nie znaleziono pliku: {path}")
    return path


def sha256(path):
    h = hashlib.sha256()
    with file_path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def user_dir():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return base / "REWDO"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def new_project():
    return {
        "schema": SCHEMA, "id": uid(), "revision": 1, "name": "Mój system 2.2",
        "sources": [], "configurations": [], "active_configuration": None,
        "gains": {state: {"L": None, "R": None, "origin": ""} for state in ("raw", "dirac", "listening")},
        "system": {"speakers": "Dynaudio Confidence C1 Platinum", "subs": "2 × REL S/510", "dsp": "miniDSP SHD Studio", "dirac_low": None, "dirac_high": None},
        "analysis": {"fc_low": 80.0, "fc_high": 100.0, "fc_step": 2.0, "families": ["BW48", "LR48"], "confirmed": False},
        "ui": {"step": 0, "compare_stage": "Po Dirac"},
        "runs": [], "listening": [], "reference_evidence": None,
    }


def scientific_state(project):
    p = copy.deepcopy(project)
    for key in ("name", "ui", "runs", "listening", "revision", "reference_evidence"):
        p.pop(key, None)
    for config in p["configurations"]:
        config.pop("label", None)
        config.pop("created", None)
    for source in p["sources"]:
        for key in ("path", "name", "checks"):
            source.pop(key, None)
        for trace in source["traces"]:
            trace.pop("name", None)
    return p


def fingerprint(project):
    return digest(scientific_state(project))


def changed(project):
    project["revision"] += 1
    project["reference_evidence"] = None


def active_config(project):
    return next((c for c in project["configurations"] if c["key"] == project["active_configuration"]), None)


def put_measurement_preset(project, label):
    identity = uid()
    config = {"id": identity, "version": 1, "key": identity+":1", "label": str(label).strip() or "Preset pomiarowy",
        "settings": {"fc":None, "filter":"BW48", "l_main_ms":None, "r_main_ms":None, "l_sub_ms":None, "r_sub_ms":None, "polarity":["OFF","OFF"]},
        "console_confirmed":False, "created":utc_now()}
    project["configurations"].append(config)
    if not project["active_configuration"]:
        project["active_configuration"] = config["key"]
    changed(project)
    return config


def put_config(project, values, label, *, console_confirmed=False, parent=None):
    settings = {k: number(values.get(k), k, low=0, high=30) for k in ("l_main_ms", "r_main_ms", "l_sub_ms", "r_sub_ms")}
    settings["fc"] = number(values.get("fc"), "Częstotliwość [Hz]", low=20, high=200)
    settings["filter"] = str(values.get("filter", "BW48"))
    if settings["filter"] not in ("BW48", "LR48", "BW24", "LR24"):
        raise ValueError("Nieobsługiwana rodzina referencji.")
    settings["polarity"] = ["OFF", "OFF"]
    previous = next((c for c in project["configurations"] if c["key"] == parent), None)
    identity = previous["id"] if previous else uid()
    version = max((c["version"] for c in project["configurations"] if c["id"]==identity),default=0)+1
    config = {"id": identity, "version": version, "key": f"{identity}:{version}", "label": label.strip() or "Moje ustawienie",
              "settings": settings, "console_confirmed": bool(console_confirmed), "created": utc_now()}
    project["configurations"].append(config)
    project["active_configuration"] = config["key"]
    changed(project)
    return config


def validate_project(project):
    if project.get("schema") != SCHEMA:
        raise ValueError("Nieobsługiwana wersja projektu.")
    keys = [c["key"] for c in project["configurations"]]
    if len(set(keys)) != len(keys):
        raise ValueError("Projekt zawiera powtórzone ID konfiguracji.")
    if project["active_configuration"] is not None and active_config(project) is None:
        raise ValueError("Nie znaleziono aktywnego ustawienia odniesienia.")
    for state, values in project["gains"].items():
        for side in ("L", "R"):
            if values[side] is not None:
                number(values[side], f"{state}/{side}", low=-60, high=12)
    digest(project)  # rejects NaN anywhere, also in restored results


def save_project(path, project):
    validate_project(project)
    atomic_json(path, project)
    atomic_json(user_dir() / "preferences.json", {"last_project": str(Path(path).resolve())})


def load_project(path):
    p = json.loads(file_path(path).read_text(encoding="utf-8"))
    validate_project(p)
    return p


def verify_sources(project):
    for source in project["sources"]:
        if sha256(source["path"]) != source["sha256"]:
            raise ValueError(f"Źródło {source['name']} zmieniło zawartość. Wczytaj je jako nową sesję.")


def relocate(source, path):
    candidate = file_path(path)
    if sha256(candidate) != source["sha256"]:
        raise ValueError("To inny plik. Nazwa nie wystarcza — zawartość musi być identyczna.")
    source["path"] = str(candidate)


def common_gains(project):
    out = {}
    for state, values in project["gains"].items():
        if not values["origin"].strip():
            raise ValueError(f"Poziom {state}: podaj źródło ustawień, np. konsola miniDSP, sesja i data.")
        left, right = (number(values[s], f"Poziom {state}/{s} [dB]", low=-60, high=12) for s in ("L", "R"))
        if abs(left - right) > 1e-9:
            raise ValueError("Zapisano różne poziomy L/R. Obecne projektanty obsługują wspólny poziom; nie przeliczono go po cichu. Porównanie pomiarów pozostaje dostępne.")
        out[state] = left
    delta = out["listening"] - out["raw"]
    if not 0 < delta <= 12:
        raise ValueError("Obecna kontrola zmiany poziomu wymaga wzrostu RAW→odsłuch w zakresie (0, 12] dB. Niezmienione lub malejące poziomy nie mają jeszcze zweryfikowanej ścieżki projektowania.")
    if not out["raw"] <= out["dirac"] <= out["listening"]:
        raise ValueError("Poziom pomiaru Dirac musi znajdować się w zweryfikowanym zakresie RAW→odsłuch.")
    return out


def result_is_current(project, result):
    return result.get("fingerprint") == fingerprint(project) and result.get("status") not in ("cancelled", "error", "running")
