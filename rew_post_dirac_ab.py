#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POST-DIRAC A/B Analyzer v20.31.

Analizuje realne pomiary slotów po Dirac zapisane w jednym projekcie REW .mdat.
To jest osobne narzędzie od rankingu RAW: nie symuluje crossovera, tylko porównuje
rzeczywiście zmierzone presety, np. 90_BW48_slot2 vs 82_BW48_INV_po Dirac.
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from rew_common import Measurement
from rew_scoring import db, wrap_phase_deg
from rew_mdat import (
    _java_serialized_strings_from_bytes,
    _scan_java_serialized_float_arrays,
    _looks_like_rew_spl_array,
    _looks_like_rew_phase_array,
    _infer_mdat_freq_axis,
)
from rew_metric_registry import write_metric_registry
from rew_measurement_source import load_all_measurements


@dataclass
class ImportedAnyMeasurement:
    name: str
    position: int
    measurement: Measurement


@dataclass
class PresetGroup:
    key: str
    label: str
    xo_hz: Optional[int]
    filter_name: str
    mic_position: str = "MLP"
    mic_position_explicit: bool = False
    sum_measurement: Optional[Measurement] = None
    left_measurement: Optional[Measurement] = None
    right_measurement: Optional[Measurement] = None
    source_names: str = ""
    label_source: str = "measurement_name"
    generic_sum_binding: bool = False
    binding_warning: str = ""


def _md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def _parse_xo_filter(name: str) -> Tuple[Optional[int], str]:
    m = re.search(r"(?i)(\d{2,3})\s*(?:hz)?\s*[_\-\s]*(BW|LR)\s*(24|48)?", str(name))
    if not m:
        return None, ""
    xo = int(m.group(1))
    filt = str(m.group(2)).upper() + (str(m.group(3)) if m.group(3) else "")
    return xo, filt


def _is_component_measurement_name(name: str) -> bool:
    """Czy nazwa wygląda jak składowa techniczna, a nie pełny slot POST-DIRAC.

    POST-DIRAC A/B porównuje gotowe presety/sloty po Dirac. Nazwy typu
    L_main, R_main, L_sub, R_sub pochodzą z pomiarów diagnostycznych składowych.
    Nie wolno traktować L_sub/R_sub jako lewego/prawego kanału presetu, bo wtedy
    raport liczy parę subwooferów zamiast całego ustawienia odsłuchowego.
    """
    s = str(name or "").lower().replace(" ", "_")
    return bool(re.search(r"(^|[_\-])([lr])[_\-]?(main|sub)(?:[_\-]|$)", s))


def _tokenize_measurement_name_for_role(name: str) -> List[str]:
    """Tokeny nazwy do rozpoznania roli pomiaru POST-DIRAC.

    Ważne: nazwa typu ``90 Hz BW48 slot2 L`` nie jest pełnym slotem L+P,
    tylko lewym kanałem diagnostycznym. W v20.07 słowo ``slot`` było
    rozpoznawane zbyt wcześnie i przez to L/R trafiały do tabeli decyzji
    jako osobne pełne L+P.
    """
    raw = str(name or "").lower()
    raw = raw.replace("ł", "l").replace("ó", "o").replace("ą", "a").replace("ę", "e").replace("ś", "s").replace("ć", "c").replace("ń", "n").replace("ż", "z").replace("ź", "z")
    return [t for t in re.split(r"[^a-z0-9]+", raw) if t]



def _normalize_pl_ascii(text: str) -> str:
    return (str(text or "").lower()
            .replace("ł", "l").replace("ó", "o").replace("ą", "a")
            .replace("ę", "e").replace("ś", "s").replace("ć", "c")
            .replace("ń", "n").replace("ż", "z").replace("ź", "z"))


def _mic_position_from_name(name: str) -> Tuple[str, bool]:
    """Rozpoznaj pozycję mikrofonu L/MLP/P w pomiarze POST-DIRAC.

    To NIE jest kanał L/R. Chodzi o położenie mikrofonu w okolicy głowy:
    L = lewa pozycja głowy, MLP = środek, P = prawa pozycja głowy.
    Dla zgodności wstecznej brak oznaczenia traktujemy jako MLP, ale z flagą
    explicit=False, żeby nie udawać pełnego pomiaru 3-punktowego.
    """
    raw = _normalize_pl_ascii(name)
    # Jawny MLP / środek.
    if re.search(r"(^|[^a-z0-9])(mlp|sweet\s*spot|sweetspot|center|centre|srodek|pos\s*m|pos_m|pozycja\s*m|punkt\s*m)([^a-z0-9]|$)", raw):
        return "MLP", True
    # Prawa pozycja mikrofonu. Obsługujemy P oraz R, bo użytkownik może pisać
    # po polsku P albo po angielsku R/right.
    if re.search(r"(^|[^a-z0-9])(pos|poz|pozycja|punkt|mic|mikrofon)[_\-\s]*(p|r|prawa|right)([^a-z0-9]|$)", raw):
        return "P", True
    if re.search(r"(^|[^a-z0-9])(p|r|prawa|right)[_\-\s]*(pos|poz|pozycja|punkt|mic|mikrofon)([^a-z0-9]|$)", raw):
        return "P", True
    # Lewa pozycja jest sprawdzana po prawej. W przeciwnym razie litera `l`
    # z nazwy pełnej sumy `L+R`, występująca po `right-pos`, może fałszywie
    # przechwycić prawą pozycję jako lewą.
    if re.search(r"(^|[^a-z0-9])(pos|poz|pozycja|punkt|mic|mikrofon)[_\-\s]*(l|lewa|left)([^a-z0-9]|$)", raw):
        return "L", True
    if re.search(r"(^|[^a-z0-9])(l|lewa|left)[_\-\s]*(pos|poz|pozycja|punkt|mic|mikrofon)([^a-z0-9]|$)", raw):
        return "L", True
    return "MLP", False


def _position_sort_key(pos: str) -> int:
    return {"L": 0, "MLP": 1, "P": 2}.get(str(pos or "").upper(), 9)

def _has_explicit_lr_sum_name(name: str) -> bool:
    s = str(name or "")
    low = s.lower()
    # Nie mylić filtra LR48 z sumą L+R. Szukamy jawnego plusa/slasha albo słowa stereo.
    if re.search(r"(?i)(^|[^a-z0-9])l\s*[+&/]\s*r([^a-z0-9]|$)", s):
        return True
    if re.search(r"(?i)(^|[^a-z0-9])l\s*[+&/]\s*p([^a-z0-9]|$)", s):
        return True
    if any(tok in low for tok in ["l+r", "l + r", "l/r", "l / r", "l/p", "l / p", "l+p", "l + p", "l&p", "l & p"]):
        return True
    toks = _tokenize_measurement_name_for_role(s)
    if "stereo" in toks or "sum" in toks or "suma" in toks:
        if any(t in toks for t in ["l", "left", "lewy", "lewa"]) and any(t in toks for t in ["r", "right", "p", "prawy", "prawa"]):
            return True
    return False


def _is_rew_technical_metadata_name(name: str) -> bool:
    """Odrzuć techniczne opisy routingu zapisane obok nazwy pomiaru.

    REW serializuje wpisy ``EXCL:``/``INCL:`` dotyczące urządzenia, kanału
    timing reference i poziomu.  Nie są to nazwy pomiarów.  Szczególnie wpis
    ``EXCL: ..., L+R, ...`` wyglądał wcześniej jak pełna suma stereo i dzielił
    segment MDAT w niewłaściwym miejscu.
    """
    value = str(name or "").lstrip("\ufeff \t\r\n")
    return bool(re.match(r"(?i)^(?:excl|incl)\s*:", value))


def _role_from_name(name: str) -> str:
    if _is_rew_technical_metadata_name(name):
        return "other"
    low = str(name).lower()
    if _is_component_measurement_name(name):
        return "component"

    # v20.07: najpierw jawna suma L+R/L+P, potem kanały L/R, a dopiero na końcu
    # slot bez oznaczenia kanału. Inaczej pomiary ``slot2 L`` i ``slot2 R`` były
    # fałszywie uznawane za pełne L+P.
    if _has_explicit_lr_sum_name(name):
        return "sum"

    toks = _tokenize_measurement_name_for_role(name)
    has_l = any(t in toks for t in ["l", "left", "lewy", "lewa"])
    has_r = any(t in toks for t in ["r", "right", "p", "prawy", "prawa"])
    if has_l and not has_r:
        return "left"
    if has_r and not has_l:
        return "right"
    if has_l and has_r:
        return "sum"

    # Slot bez oznaczenia kanału traktujemy jako pełny pomiar L+P, bo REW często
    # zapisuje gotowy slot po Dirac jako ``90 Hz BW48 slot2``.
    if re.search(r"(?i)(^|[_\-\s])slot\s*\d*", low):
        return "sum"
    return "other"


def _is_candidate_measurement_name(name: str) -> bool:
    # v20.29a: wpisy EXCL:/INCL: są metadanymi REW, nawet jeżeli zawierają
    # token L+R. Muszą wypaść przed rozpoznawaniem kanałów i segmentacją MDAT.
    if _is_rew_technical_metadata_name(name):
        return False
    low = str(name).lower()
    explicit_sum = _has_explicit_lr_sum_name(name)
    # v20.07: L+P / L/P to pełny polski zapis stereo, tak samo jak L+R / L/R.
    # Nie można odrzucać samego "/" globalnie, bo wtedy nazwa "Głośniki L/P"
    # wygląda jak ścieżka i wypada z POST-DIRAC. Odrzucamy slash tylko wtedy,
    # gdy nazwa nie jest jawną sumą stereo.
    candidate_tokens = ["bw", "lr", "slot", "dirac", "sub", "l+r", "l + r", "l/r", "l+p", "l + p", "l/p", "l & r", "l & p"]
    if not explicit_sum and not any(x in low for x in candidate_tokens):
        return False
    if low.endswith((".txt", ".wav", ".mdat")) or "\\" in low:
        return False
    if "/" in low and not explicit_sum:
        return False
    return _role_from_name(name) in {"sum", "left", "right"}


def _friendly_label_from_slot(name: str) -> str:
    xo, filt = _parse_xo_filter(name)
    label = str(name).strip()
    if xo and filt:
        label = f"{xo} Hz {filt}"
    low = str(name).lower()
    mslot = re.search(r"(?i)slot\s*(\d+)", name)
    if mslot:
        label += f" slot{mslot.group(1)}"
    if "inv" in low:
        label += " INV"
    if "po dirac" in low or "po_dirac" in low or "dirac on" in low or "dirac_on" in low or low.endswith("dirac"):
        label += " po Dirac"
    return label


def _is_generic_sum_name(name: str) -> bool:
    """Czy nazwa L+R/L+P pochodzi z urządzenia/REW, a nie z nazwy presetu.

    Przykłady problematyczne:
    ``EXCL: Głośniki (SHD Series), SPEAKER, L+R, volume: 1,000``
    ``Głośniki L+P`` albo ``Głośniki L/P`` bez informacji 90/96/102 i BW/LR.

    Taki pomiar może być poprawny, ale nie wolno z niego wnioskować, czy to 90,
    96 czy 102 Hz. Nazwę grupy trzeba wtedy związać z sąsiadującymi pomiarami L/R.
    """
    s = str(name or "")
    xo, filt = _parse_xo_filter(s)
    low = s.lower()
    if xo and filt:
        return False
    if not _has_explicit_lr_sum_name(s):
        return False
    generic_tokens = ["excl:", "speaker", "głośniki", "glosniki", "volume", "shd series"]
    return any(t in low for t in generic_tokens) or low.strip() in {"l+r", "l + r", "l/r", "l+p", "l + p", "l/p"}


def _infer_group_identity_from_lr(group: PresetGroup) -> None:
    """Ustal nazwę presetu z pary L/R, jeżeli pomiar L+P/L+R ma nazwę generyczną.

    To naprawia krytyczny przypadek, w którym dwa pomiary L+P/L+R w MDAT mają tę samą
    nazwę urządzenia, a dopiero kolejne pomiary L/R są nazwane np. 102_BW48_L/R
    i 90_BW48_L/R. Bez tej funkcji aplikacja może odwrócić referencję.
    """
    if not group.generic_sum_binding:
        return
    names = []
    if group.left_measurement is not None:
        names.append(group.left_measurement.name)
    if group.right_measurement is not None:
        names.append(group.right_measurement.name)
    if not names:
        group.binding_warning = "L+P/L+R ma nazwę generyczną i brak sąsiednich L/R do identyfikacji presetu"
        return

    parsed = [(_parse_xo_filter(n), n) for n in names]
    valid = [(xf, n) for xf, n in parsed if xf[0] is not None and xf[1]]
    if not valid:
        group.binding_warning = "L+P/L+R ma nazwę generyczną; L/R nie zawierają czytelnego XO/filtra"
        return

    # Preferuj zgodną parę L/R. Jeśli tylko jedna strona ma nazwę, też użyj jej,
    # ale zostaw ostrzeżenie.
    xo0, filt0 = valid[0][0]
    consistent = all(xf[0] == xo0 and xf[1] == filt0 for xf, _n in valid)
    if not consistent:
        group.binding_warning = "L+P/L+R ma nazwę generyczną; L/R wskazują różne XO/filtry — sprawdź nazwy pomiarów"
        return

    base_name = valid[0][1]
    label = _friendly_label_from_slot(base_name)
    # Zachowaj dodatkowe cechy, które mogły być tylko po jednej stronie.
    all_text = " ".join(names).lower()
    if "slot" not in label.lower():
        mslot = re.search(r"(?i)slot\s*(\d+)", " ".join(names))
        if mslot:
            label += f" slot{mslot.group(1)}"
    if "inv" in all_text and "inv" not in label.lower():
        label += " INV"
    if ("po dirac" in all_text or "po_dirac" in all_text or "dirac on" in all_text or "dirac_on" in all_text) and "dirac" not in label.lower():
        label += " po Dirac"

    group.xo_hz = int(xo0) if xo0 is not None else group.xo_hz
    group.filter_name = str(filt0 or group.filter_name)
    group.label = label
    group.key = re.sub(r"[^a-zA-Z0-9]+", "_", label.lower()).strip("_")
    group.label_source = "inferred_from_adjacent_LR"
    group.binding_warning = "L+P/L+R miał nazwę generyczną; preset przypisano po kolejności do sąsiednich L/R: " + ", ".join(names)


def _import_all_rew_measurements(path: str | Path) -> List[ImportedAnyMeasurement]:
    loaded = load_all_measurements(path)
    component_names = [item.name for item in loaded if _is_component_measurement_name(item.name)]
    imported = [
        ImportedAnyMeasurement(item.name, int(item.position), item.measurement)
        for item in loaded
        if _is_candidate_measurement_name(item.name)
    ]
    if imported:
        return imported
    if component_names:
        examples = ", ".join(component_names[:6])
        raise ValueError(
            "POST-DIRAC A/B wymaga pomiarów pełnych slotów po Dirac, np. 90_BW48_L+R albo 90_BW48_L/90_BW48_R. "
            "Wykryto pomiary składowe L_main/R_main/L_sub/R_sub, których nie wolno liczyć jako presetów POST-DIRAC. "
            f"Przykłady z pliku: {examples}"
        )
    raise ValueError("Nie znaleziono nazw pomiarów pełnych slotów POST-DIRAC w źródle MDAT/API.")


def _group_measurements(items: List[ImportedAnyMeasurement]) -> List[PresetGroup]:
    groups: List[PresetGroup] = []
    current: Optional[PresetGroup] = None
    for item in sorted(items, key=lambda x: int(x.position)):
        role = _role_from_name(item.name)
        xo, filt = _parse_xo_filter(item.name)
        if role == "sum":
            label = _friendly_label_from_slot(item.name)
            key = re.sub(r"[^a-zA-Z0-9]+", "_", label.lower()).strip("_")
            generic = _is_generic_sum_name(item.name)
            mic_pos, mic_explicit = _mic_position_from_name(item.name)
            current = PresetGroup(
                key=key,
                label=label,
                xo_hz=xo,
                filter_name=filt,
                mic_position=mic_pos,
                mic_position_explicit=mic_explicit,
                sum_measurement=item.measurement,
                source_names=item.name,
                label_source="generic_sum_name" if generic else "measurement_name",
                generic_sum_binding=generic,
                binding_warning=("L+P/L+R ma nazwę generyczną — nazwa presetu zostanie związana z sąsiednimi pomiarami L/R" if generic else ""),
            )
            groups.append(current)
        elif role == "component":
            # v20.07: składowe main/sub nigdy nie tworzą kanałów L/R presetu POST-DIRAC.
            continue
        elif role in {"left", "right"}:
            # Najczęściej L/R występują bezpośrednio po pomiarze slotu. To jest bezpieczniejsze
            # niż ślepe grupowanie po nazwie, bo nazwy L/R nie zawsze zawierają INV.
            target = current
            if target is None or (
                xo is not None
                and target.xo_hz is not None
                and xo != target.xo_hz
                and not target.generic_sum_binding
            ):
                label = _friendly_label_from_slot(item.name)
                key = re.sub(r"[^a-zA-Z0-9]+", "_", label.lower()).strip("_")
                mic_pos, mic_explicit = _mic_position_from_name(item.name)
                target = PresetGroup(key=key, label=label, xo_hz=xo, filter_name=filt, mic_position=mic_pos, mic_position_explicit=mic_explicit, source_names=item.name)
                groups.append(target)
                current = target
            if role == "left":
                target.left_measurement = item.measurement
            else:
                target.right_measurement = item.measurement
            target.source_names = (target.source_names + "; " + item.name).strip("; ")
            _infer_group_identity_from_lr(target)

    # Finalny pass: jeżeli dopiero po zakończeniu pętli mamy parę L/R, ustal etykietę.
    for g in groups:
        _infer_group_identity_from_lr(g)

    # Jeżeli nadal istnieją generyczne duplikaty L+R bez przypisania, oznacz je mocnym ostrzeżeniem.
    sum_names = {}
    for g in groups:
        if g.sum_measurement is not None:
            sum_names.setdefault(g.sum_measurement.name, 0)
            sum_names[g.sum_measurement.name] += 1
    for g in groups:
        if g.sum_measurement is not None and sum_names.get(g.sum_measurement.name, 0) > 1 and g.label_source != "inferred_from_adjacent_LR":
            g.binding_warning = (g.binding_warning + " | " if g.binding_warning else "") + "DUPLIKAT NAZWY L+R — sprawdź ręcznie, czy L+R należy do właściwych L/R"

    return [g for g in groups if g.sum_measurement is not None or (g.left_measurement is not None and g.right_measurement is not None)]

def _common_grid(measurements: Iterable[Measurement], low: float = 20.0, high: float = 20000.0, points: int = 6000) -> np.ndarray:
    ms = [m for m in measurements if m is not None]
    fmin = max(float(np.nanmin(m.freq)) for m in ms)
    fmax = min(float(np.nanmax(m.freq)) for m in ms)
    lo = max(float(low), fmin)
    hi = min(float(high), fmax)
    if hi <= lo:
        raise ValueError("Brak wspólnego zakresu częstotliwości dla pomiarów.")
    return np.geomspace(lo, hi, int(points))


def _interp_complex(m: Measurement, grid: np.ndarray) -> np.ndarray:
    z = (10.0 ** (np.asarray(m.spl_db, dtype=float) / 20.0)) * np.exp(1j * np.deg2rad(np.asarray(m.phase_deg, dtype=float)))
    real = np.interp(grid, np.asarray(m.freq, dtype=float), np.real(z))
    imag = np.interp(grid, np.asarray(m.freq, dtype=float), np.imag(z))
    return real + 1j * imag


def _band_mask(freq: np.ndarray, low: float, high: float) -> np.ndarray:
    return (freq >= float(low)) & (freq <= float(high))


def _mean_band(freq: np.ndarray, values: np.ndarray, low: float, high: float, default: float = float("nan")) -> float:
    mask = _band_mask(freq, low, high)
    if not np.any(mask):
        return float(default)
    vals = np.asarray(values)[mask]
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return float(default)
    return float(np.mean(vals))


def _mean_band_strict(freq: np.ndarray, values: np.ndarray, low: float, high: float, default: float = float("nan")) -> float:
    """Średnia pasma tylko wtedy, gdy pomiar obejmuje cały zakres.

    To zabezpiecza przed sytuacją typu pomiar do 250 Hz i próba wyliczenia
    pełni 120–300 Hz albo obecności 1–4 kHz z niepełnych danych.
    """
    freq = np.asarray(freq, dtype=float)
    if freq.size == 0 or float(np.nanmin(freq)) > float(low) or float(np.nanmax(freq)) < float(high):
        return float(default)
    return _mean_band(freq, values, low, high, default=default)


def _span_band(freq: np.ndarray, values: np.ndarray, low: float, high: float) -> float:
    mask = _band_mask(freq, low, high)
    vals = np.asarray(values)[mask]
    vals = vals[np.isfinite(vals)]
    if vals.size < 2:
        return float("nan")
    return float(np.percentile(vals, 95) - np.percentile(vals, 5))


def _std_band(freq: np.ndarray, values: np.ndarray, low: float, high: float) -> float:
    mask = _band_mask(freq, low, high)
    vals = np.asarray(values)[mask]
    vals = vals[np.isfinite(vals)]
    if vals.size < 2:
        return float("nan")
    return float(np.std(vals))


def _smooth(values: np.ndarray, n: int = 9) -> np.ndarray:
    if values.size < n or n <= 1:
        return values
    kernel = np.ones(int(n), dtype=float) / float(n)
    return np.convolve(values, kernel, mode="same")


def _phase_residual_metrics(freq: np.ndarray, z: np.ndarray, low: float, high: float) -> Tuple[float, float]:
    mask = _band_mask(freq, low, high)
    if np.count_nonzero(mask) < 10:
        return float("nan"), float("nan")
    ph = np.rad2deg(np.unwrap(np.angle(z[mask])))
    x = freq[mask]
    coef = np.polyfit(x, ph, 1)
    resid = ph - np.polyval(coef, x)
    return float(np.sqrt(np.mean(resid ** 2))), float(np.max(resid) - np.min(resid))


def _group_delay_metrics(freq: np.ndarray, z: np.ndarray) -> Tuple[float, float, float]:
    ph = np.unwrap(np.angle(z))
    gd_ms = -np.gradient(ph, freq) / (2.0 * math.pi) * 1000.0
    gd_ms = _smooth(gd_ms, 11)
    peak = float(np.percentile(gd_ms[_band_mask(freq, 75, 125)], 95))
    bg = float(np.median(gd_ms[_band_mask(freq, 160, 220)])) if np.any(_band_mask(freq, 160, 220)) else float(np.median(gd_ms))
    spread = float(np.percentile(gd_ms[_band_mask(freq, 75, 125)], 95) - np.percentile(gd_ms[_band_mask(freq, 75, 125)], 5))
    return peak, peak - bg, spread


def _phase_diff_deg(z1: np.ndarray, z2: np.ndarray) -> np.ndarray:
    return np.abs(wrap_phase_deg(np.rad2deg(np.angle(z1)) - np.rad2deg(np.angle(z2))))


def _interp_at(freq: np.ndarray, values: np.ndarray, hz: float) -> float:
    hz = float(hz)
    return float(np.interp(hz, freq, values))


def _metrics_for_group(group: PresetGroup) -> Dict[str, object]:
    ms: List[Measurement] = []
    if group.sum_measurement is not None:
        ms.append(group.sum_measurement)
    if group.left_measurement is not None:
        ms.append(group.left_measurement)
    if group.right_measurement is not None:
        ms.append(group.right_measurement)
    grid = _common_grid(ms, low=20, high=20000, points=7000)
    grid_min = float(np.nanmin(grid))
    grid_max = float(np.nanmax(grid))

    z_sum_measured = _interp_complex(group.sum_measurement, grid) if group.sum_measurement is not None else None
    z_l = _interp_complex(group.left_measurement, grid) if group.left_measurement is not None else None
    z_r = _interp_complex(group.right_measurement, grid) if group.right_measurement is not None else None
    z_sum_lr = (z_l + z_r) if z_l is not None and z_r is not None else None
    z_sum = z_sum_measured if z_sum_measured is not None else z_sum_lr
    if z_sum is None:
        raise ValueError(f"Preset {group.label}: brak L+R albo pary L/R.")

    spl = db(z_sum)
    mean_75_125 = _mean_band(grid, spl, 75, 125, 0.0)
    spl_norm = spl - mean_75_125
    peak_gd, excess_gd, spread_gd = _group_delay_metrics(grid, z_sum)
    phase_rms, phase_p2p = _phase_residual_metrics(grid, z_sum, 75, 125)

    dip = _mean_band(grid, spl_norm, 88, 104, 0.0) - 0.5 * (_mean_band(grid, spl_norm, 75, 88, 0.0) + _mean_band(grid, spl_norm, 104, 125, 0.0))
    xo = float(group.xo_hz or 90.0)

    mean_300_1000_strict = _mean_band_strict(grid, spl, 300, 1000)
    mean_120_300_strict = _mean_band_strict(grid, spl, 120, 300)
    mean_1_4k_strict = _mean_band_strict(grid, spl, 1000, 4000)
    mean_4_8k_strict = _mean_band_strict(grid, spl, 4000, 8000)

    out: Dict[str, object] = {
        "label": group.label,
        "xo_hz": "" if group.xo_hz is None else int(group.xo_hz),
        "filter": group.filter_name,
        "source_names": group.source_names,
        "pozycja_mikrofonu": group.mic_position,
        "pozycja_mikrofonu_jawna": "TAK" if group.mic_position_explicit else "NIE",
        "label_source": group.label_source,
        "generic_sum_binding": bool(group.generic_sum_binding),
        "binding_warning": group.binding_warning,
        "measurement_low_hz": grid_min,
        "measurement_high_hz": grid_max,
        "has_fullness_120_300": bool(grid_min <= 120 and grid_max >= 1000),
        "has_presence_1_4k": bool(grid_min <= 1000 and grid_max >= 4000),
        "has_glass_4_8k": bool(grid_min <= 4000 and grid_max >= 8000),
        "spl_span_75_125_db": _span_band(grid, spl_norm, 75, 125),
        "spl_std_75_125_db": _std_band(grid, spl_norm, 75, 125),
        "dip_88_104_vs_shoulders_db": float(dip),
        "level_95_vs_75_125_db": _interp_at(grid, spl_norm, 95.0),
        "gd_peak_75_125_ms": peak_gd,
        "gd_excess_75_125_ms": excess_gd,
        "gd_spread_75_125_ms": spread_gd,
        "phase_resid_rms_75_125_deg": phase_rms,
        "phase_resid_p2p_75_125_deg": phase_p2p,
        "band_25_60_vs_75_125_db": _mean_band(grid, spl, 25, 60, 0.0) - mean_75_125,
        "band_40_60_vs_75_125_db": _mean_band(grid, spl, 40, 60, 0.0) - mean_75_125,
        "band_60_90_vs_75_125_db": _mean_band(grid, spl, 60, 90, 0.0) - mean_75_125,
        "band_60_100_vs_75_125_db": _mean_band(grid, spl, 60, 100, 0.0) - mean_75_125,
        "band_90_120_vs_75_125_db": _mean_band(grid, spl, 90, 120, 0.0) - mean_75_125,
        "band_100_160_vs_75_125_db": _mean_band(grid, spl, 100, 160, 0.0) - mean_75_125,
        "band_125_160_vs_75_125_db": _mean_band(grid, spl, 125, 160, 0.0) - mean_75_125,
        "band_160_220_vs_75_125_db": _mean_band(grid, spl, 160, 220, 0.0) - mean_75_125,
        "body_125_220_vs_75_125_db": _mean_band(grid, spl, 125, 220, 0.0) - mean_75_125,
        "mid_texture_160_220_vs_125_160_db": _mean_band(grid, spl, 160, 220, float("nan")) - _mean_band(grid, spl, 125, 160, float("nan")),
        "fullness_120_300_vs_300_1000_db": mean_120_300_strict - mean_300_1000_strict,
        "presence_1_4k_vs_300_1000_db": mean_1_4k_strict - mean_300_1000_strict,
        "glass_4_8k_vs_1_4k_db": mean_4_8k_strict - mean_1_4k_strict,
        "limited_fullness_120_to_fmax_vs_75_125_db": (_mean_band(grid, spl, 120, min(300.0, grid_max), float("nan")) - mean_75_125) if grid_max >= 180 else float("nan"),
        "has_lr": bool(z_l is not None and z_r is not None),
    }

    if z_l is not None and z_r is not None:
        spl_l = db(z_l)
        spl_r = db(z_r)
        bal = np.abs(spl_l - spl_r)
        phdiff = _phase_diff_deg(z_l, z_r)
        sum_margin = db(z_l + z_r) - np.maximum(spl_l, spl_r)
        out.update({
            "lr_balance_mean_75_125_db": _mean_band(grid, bal, 75, 125, float("nan")),
            "lr_balance_max_75_125_db": float(np.percentile(bal[_band_mask(grid,75,125)], 95)),
            "lr_balance_at_xo_db": _interp_at(grid, bal, xo),
            "lr_phase_mean_75_125_deg": _mean_band(grid, phdiff, 75, 125, float("nan")),
            "lr_phase_at_xo_deg": _interp_at(grid, phdiff, xo),
            "sum_margin_mean_75_125_db": _mean_band(grid, sum_margin, 75, 125, float("nan")),
            "sum_margin_worst_75_125_db": float(np.percentile(sum_margin[_band_mask(grid,75,125)], 5)),
            "sum_margin_at_xo_db": _interp_at(grid, sum_margin, xo),
            "sum_margin_at_95_db": _interp_at(grid, sum_margin, 95.0),
            "sum_margin_at_100_db": _interp_at(grid, sum_margin, 100.0),
        })
    else:
        for k in ["lr_balance_mean_75_125_db", "lr_balance_max_75_125_db", "lr_balance_at_xo_db", "lr_phase_mean_75_125_deg", "lr_phase_at_xo_deg", "sum_margin_mean_75_125_db", "sum_margin_worst_75_125_db", "sum_margin_at_xo_db", "sum_margin_at_95_db", "sum_margin_at_100_db"]:
            out[k] = float("nan")

    # Użytkowy indeks — nie wyrok. Ma faworyzować realną muzykalność: ciało 125–220,
    # kontrola czasu, scena L/R i brak szklistości.
    body = float(out["body_125_220_vs_75_125_db"])
    presence = float(out["presence_1_4k_vs_300_1000_db"])
    glass = float(out["glass_4_8k_vs_1_4k_db"])
    # score sklejony z miękkich okien; pozostaje tylko pomocniczy.
    def window(v: float, target: float, good: float, zero: float) -> float:
        if not np.isfinite(v):
            return 50.0
        d = abs(v - target)
        if d <= good:
            return 100.0
        if d >= zero:
            return 0.0
        return 100.0 * (1.0 - (d - good) / (zero - good))
    body_score = window(body, target=1.5, good=2.5, zero=8.0)
    time_score = max(0.0, min(100.0, 100.0 - 1.8 * max(0.0, float(out["gd_excess_75_125_ms"]) - 18.0)))
    lr_score = max(0.0, min(100.0, 100.0 - 18.0 * max(0.0, float(out["lr_balance_mean_75_125_db"]) - 1.2))) if np.isfinite(float(out["lr_balance_mean_75_125_db"])) else 55.0
    spl_score = max(0.0, min(100.0, 100.0 - 10.0 * max(0.0, float(out["spl_span_75_125_db"]) - 3.0)))
    parts = [(0.34, body_score), (0.22, time_score), (0.20, lr_score), (0.14, spl_score)]
    if np.isfinite(presence):
        parts.append((0.06, window(presence, target=0.5, good=2.0, zero=7.0)))
    if np.isfinite(glass):
        parts.append((0.04, window(glass, target=-1.5, good=3.0, zero=10.0)))
    wsum = sum(w for w, _ in parts) or 1.0
    out["organic_midrange_index_0_100"] = float(np.clip(sum(w * v for w, v in parts) / wsum, 0, 100))
    return out


def _fmt(v: object, digits: int = 2, suffix: str = "") -> str:
    if v is None or str(v).strip().lower() in {"none", "nan", ""}:
        return "—"
    try:
        f = float(v)
        if not np.isfinite(f):
            return "—"
        return f"{f:.{digits}f}{suffix}"
    except Exception:
        return str(v)


def _score_cell(r: Dict[str, object], ref: Optional[Dict[str, object]]) -> str:
    if ref is None:
        return "DIAG"
    if r is ref:
        return "REF"
    return _fmt(r.get("reference_replacement_score_0_100"), 1)


def _v20_data_status(r: Dict[str, object]) -> str:
    status = str(r.get("pomiar_3pkt_status", "") or "").upper()
    if "PEŁNY" in status:
        return "POMIAR 3-PUNKTOWY"
    if "CZĘŚCIOWY" in status:
        return "POMIAR CZĘŚCIOWY"
    if np.isfinite(_safe_float_value(r, "model_odpornosci_v20_0_100")):
        return "POJEDYNCZY POMIAR MLP — OGRANICZONA PEWNOŚĆ"
    return "BRAK DANYCH"


def _short_slot_role(r: Dict[str, object], ref: Optional[Dict[str, object]]) -> str:
    label = (str(r.get("label", "")) + " " + str(r.get("source_names", ""))).lower()
    if ref is not None and r is ref:
        return "referencja tekstury"
    if "102" in label:
        return "kandydat warunkowy"
    if "100" in label:
        return "kandydat kompromisowy"
    if "96" in label or "98" in label:
        return "kandydat A/B"
    return "slot testowy"


def _short_slot_decision(r: Dict[str, object], ref: Optional[Dict[str, object]]) -> str:
    if ref is not None and r is ref:
        return "zostaw jako punkt odniesienia"
    cls = str(r.get("decision_class", "") or "").upper()
    if "BRAK REFERENCJI" in cls:
        return "tylko diagnostyka — dodaj 90 BW48"
    risk = _safe_float_value(r, "ryzyko_dziury_lp_po_dirac_0_100")
    body = _safe_float_value(r, "mid_texture_delta_vs_reference_db")
    if "NIE ZAST" in cls:
        return "A/B tylko ostrożnie"
    if np.isfinite(risk) and risk > 60:
        return "tylko diagnostycznie"
    if np.isfinite(body) and body < -1.2:
        return "A/B, sprawdź chudszą średnicę"
    if np.isfinite(body) and body > 2.0:
        return "A/B, sprawdź pogrubienie"
    return "do odsłuchu A/B"


def _short_main_risk(r: Dict[str, object]) -> str:
    risks: List[str] = []
    body = _safe_float_value(r, "mid_texture_delta_vs_reference_db")
    null = _safe_float_value(r, "ryzyko_dziury_lp_po_dirac_0_100")
    three = _safe_float_value(r, "odpornosc_3pkt_l_mlp_p_0_100")
    comfort = _safe_float_value(r, "listening_comfort_0_100")
    if np.isfinite(body) and body < -1.2:
        risks.append("możliwie chudsza średnica")
    elif np.isfinite(body) and body > 2.0:
        risks.append("możliwe pogrubienie/maskowanie")
    if np.isfinite(null) and null > 55:
        risks.append("ryzyko dziury L+P")
    if np.isfinite(three) and three < 55:
        risks.append("słaba odporność L/MLP/P")
    if np.isfinite(comfort) and comfort < 45:
        risks.append("komfort odsłuchu do sprawdzenia")
    if not risks:
        return "brak dużego ostrzeżenia — potwierdź odsłuchem"
    return "; ".join(risks[:3])


def _post_dirac_short_lines(rows: List[Dict[str, object]], ranked: List[Dict[str, object]], ref: Optional[Dict[str, object]], *, mdat: str) -> List[str]:
    main_candidate = next((r for r in ranked if not (ref is not None and r is ref)), ranked[0] if ranked else None)
    ref_label = _md_escape(ref.get("label", "brak") if ref is not None else "brak")
    cand_label = _md_escape(main_candidate.get("label", "brak") if main_candidate is not None else "brak")
    lines: List[str] = [
        "# 01 — Co teraz zrobić po POST-DIRAC",
        "",
        f"Plik: `{Path(mdat).name}`",
        "",
        "## Decyzja robocza",
        "",
        f"- Referencja tekstury: **{ref_label}**.",
        *( ["- Brak 90 BW48 w tym pliku: poniższy slot jest tylko diagnostyką, nie kandydatem ocenionym względem referencji."] if ref is None else [f"- Pierwszy kandydat do A/B: **{cand_label}**."] ),
        "- Nie zmieniaj referencji tylko dlatego, że jedna liczba jest wyższa. Najpierw sprawdź ostrzeżenia i odsłuch.",
        "",
        "## Krótka tabela slotów",
        "",
        "| Slot L+P | Rola | Decyzja | Główne ryzyko | Status danych v20 |",
        "|---|---|---|---|---|",
    ]
    for r in ranked[:6]:
        lines.append(
            f"| {_md_escape(r.get('label',''))} | {_short_slot_role(r, ref)} | {_short_slot_decision(r, ref)} | {_short_main_risk(r)} | {_v20_data_status(r)} |"
        )
    lines.extend([
        "",
        "## Co sprawdzić odsłuchowo",
        "",
        "1. Wokal żeński i męski — czy nie robi się chudszy, dalszy albo zbyt gładki.",
        "2. Fortepian / gitara / saksofon — czy jest masa i naturalny atak.",
        "3. Kontrabas / stopa — czy bas jest punktowy, ale nie suchy i nie rozlany.",
        "4. Centrum sceny — czy nie robi się rozdmuchane albo niestabilne.",
        "5. Zmęczenie po 30 minutach — czy nie pojawia się szklistość albo natarczywość.",
        "",
        "## Następny krok",
        "",
        *( ["Dodaj do tego samego MDAT pełny pomiar 90 BW48 L+P/L+R wykonany w tych samych warunkach. Dopiero wtedy uruchom analizę porównawczą."] if ref is None else ["Odsłuchaj najpierw referencję 90 vs główny kandydat. Dopiero potem porównuj kolejne sloty."] ),
        "Szczegóły techniczne są w `POST_DIRAC_AB_REPORT.md`, a liczby w `POST_DIRAC_AB_METRICS.csv`.",
    ])
    return lines


def _post_dirac_short_table_lines(ranked: List[Dict[str, object]], ref: Optional[Dict[str, object]]) -> List[str]:
    lines = [
        "# 02 — Tabela decyzji slotów L+P",
        "",
        "Ta tabela pokazuje tylko pełne sloty stereo L+P/L+R. Dane L/R są diagnostyką, nie osobnymi kandydatami do decyzji.",
        "",
        "| # | Slot L+P | Wynik zastąpienia 90 BW48 — POMIAR POST-DIRAC | Model odporności v20 — PREDYKCJA/POMIAR | Zmierzona odporność przestrzenna L/MLP/P | Zmierzona dziura L+P po Dirac | Tekstura 160–220 vs 90 | Cel lżejszego basu | Komfort | Klasa | Werdykt |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for i, r in enumerate(ranked[:8], 1):
        lines.append(
            f"| {i} | {_md_escape(r.get('label',''))} | {_score_cell(r, ref)} | {_fmt(r.get('model_odpornosci_v20_0_100'),1)} | {_fmt(r.get('odpornosc_3pkt_l_mlp_p_0_100'),1)} | {_fmt(r.get('ryzyko_dziury_lp_po_dirac_0_100'),1)} | {_fmt(r.get('mid_texture_delta_vs_reference_db'),2,' dB')} | {_fmt(r.get('bass_target_lighter_than_90_score_0_100'),1)} | {_fmt(r.get('listening_comfort_0_100'),1)} | {_md_escape(r.get('decision_class',''))} | {_md_escape(r.get('reference_verdict',''))} |"
        )
    return lines


def _post_dirac_three_point_lines(ranked: List[Dict[str, object]]) -> List[str]:
    lines = [
        "# 03 — Diagnostyka odporności 3-punktowej L/MLP/P",
        "",
        "Pełny status `POMIAR 3-PUNKTOWY` oznacza realny pomiar L+P w pozycjach posL, MLP i posP. `PREDYKCJA` pozostaje osobną klasą danych i nie jest liczbowo równoważna pomiarowi wielopunktowemu.",
        "",
        "| Slot | Status | Pozycje | Zmierzona odporność przestrzenna L/MLP/P — POMIAR POST-DIRAC | Rozrzut sumowania | Najgorsza zmierzona dziura L+P | Uwagi |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for r in ranked[:8]:
        lines.append(
            f"| {_md_escape(r.get('label',''))} | {_md_escape(r.get('pomiar_3pkt_status',''))} | {_md_escape(r.get('pomiar_3pkt_pozycje',''))} | {_fmt(r.get('odpornosc_3pkt_l_mlp_p_0_100'),1)} | {_fmt(r.get('rozrzut_sumowania_3pkt_db'),2,' dB')} | {_fmt(r.get('najgorsza_dziura_3pkt_db'),2,' dB')} | {_md_escape(r.get('uwagi_3pkt',''))} |"
        )
    return lines


def _post_dirac_lr_diag_lines(ranked: List[Dict[str, object]]) -> List[str]:
    lines = [
        "# 04 — Diagnostyka L/R",
        "",
        "Ta sekcja sprawdza, czy dobra suma L+P nie ukrywa problemu jednej strony. Nie służy do mnożenia kandydatów w tabeli decyzji.",
        "",
        "| Slot | L/R dostępne | Balans L/R 75–125 | Faza L/R przy XO | Sumowanie przy XO | Ostrzeżenie |",
        "|---|---|---:|---:|---:|---|",
    ]
    for r in ranked[:8]:
        warn = _short_main_risk(r)
        lines.append(
            f"| {_md_escape(r.get('label',''))} | {'TAK' if bool(r.get('has_lr')) else 'NIE'} | {_fmt(r.get('lr_balance_mean_75_125_db'),2,' dB')} | {_fmt(r.get('lr_phase_at_xo_deg'),1,'°')} | {_fmt(r.get('sum_margin_at_xo_db'),2,' dB')} | {_md_escape(warn)} |"
        )
    return lines


_CONFIDENCE_ORDER = {"NONE": -1, "VERY LOW": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _confidence_rank(label: object) -> int:
    return _CONFIDENCE_ORDER.get(str(label or "").strip().upper(), 0)


def _overall_decision_confidence(rows: List[Dict[str, object]]) -> Tuple[str, str]:
    """Najniższa pewność w projekcie decyduje o tym, czy wolno zmieniać referencję."""
    if not rows:
        return "LOW", "brak porównywanych presetów"
    ordered = sorted(rows, key=lambda r: _confidence_rank(r.get("decision_confidence")))
    worst = ordered[0]
    return str(worst.get("decision_confidence", "LOW") or "LOW"), str(worst.get("decision_confidence_reason", "") or "")


def _safe_float_value(r: Dict[str, object], key: str, default: float = float("nan")) -> float:
    try:
        v = float(r.get(key, default))
        return v if np.isfinite(v) else float(default)
    except Exception:
        return float(default)


def _midrange_safety_score(r: Dict[str, object], ref: Optional[Dict[str, object]]) -> float:
    """Ochrona średnicy względem realnej 90 BW48; bez referencji brak werdyktu."""
    if ref is None:
        return 50.0
    if r is ref:
        return 100.0
    score = 100.0
    body_delta = _safe_float_value(r, "mid_texture_delta_vs_reference_db")
    full_delta = _safe_float_value(r, "fullness_delta_vs_reference_db")
    presence_delta = _safe_float_value(r, "presence_delta_vs_reference_db")
    glass_delta = _safe_float_value(r, "glass_delta_vs_reference_db")
    profile_status = str(r.get("listening_profile_status", "")).lower()

    if np.isfinite(body_delta):
        if body_delta < -0.8:
            score -= min(45.0, (abs(body_delta) - 0.8) * 25.0 + 10.0)
        elif body_delta > 2.0:
            score -= min(35.0, (body_delta - 2.0) * 15.0 + 8.0)
        elif body_delta > 1.2:
            score -= min(12.0, (body_delta - 1.2) * 8.0)
    else:
        score -= 10.0

    if np.isfinite(full_delta) and full_delta < -0.8:
        score -= min(25.0, (abs(full_delta) - 0.8) * 12.0 + 6.0)
    if np.isfinite(presence_delta) and np.isfinite(body_delta) and body_delta > 1.5 and presence_delta < body_delta * 0.35:
        score -= min(20.0, (body_delta * 0.35 - presence_delta) * 8.0 + 6.0)
    if np.isfinite(glass_delta) and glass_delta > 1.5:
        score -= min(18.0, (glass_delta - 1.5) * 6.0 + 4.0)
    if profile_status in {"caution", "technical_control"}:
        score -= 18.0
    if _confidence_rank(r.get("decision_confidence")) <= _confidence_rank("LOW"):
        score -= 20.0
    return float(np.clip(score, 0.0, 100.0))


def _music_safety_decision(r: Dict[str, object], ref: Optional[Dict[str, object]], overall_confidence: str) -> Tuple[str, str, bool]:
    """Zwraca klasę decyzji. 90 BW48 jest referencją średnicy, nie ciężaru basu."""
    if ref is None:
        return "BRAK REFERENCJI 90 — TYLKO DIAGNOSTYKA", "w pliku nie ma dopasowanego pomiaru 90 BW48; nie liczono zastąpienia referencji", False
    if r is ref:
        return "REFERENCJA — ZOSTAW", "to jest punkt zerowy barwy/tekstury średnicy; jego ciężar basu nie jest celem", False

    reasons: List[str] = []
    hard_block = False
    score = _safe_float_value(r, "reference_replacement_score_0_100", 0.0)
    body_delta = _safe_float_value(r, "mid_texture_delta_vs_reference_db")
    full_delta = _safe_float_value(r, "fullness_delta_vs_reference_db")
    presence_delta = _safe_float_value(r, "presence_delta_vs_reference_db")
    glass_delta = _safe_float_value(r, "glass_delta_vs_reference_db")
    profile_status = str(r.get("listening_profile_status", "")).lower()

    if _confidence_rank(overall_confidence) <= _confidence_rank("LOW"):
        hard_block = True
        reasons.append("LOW confidence całego pomiaru — brak podstaw do zmiany referencji")
    if _confidence_rank(r.get("decision_confidence")) <= _confidence_rank("LOW"):
        hard_block = True
        reasons.append("ten preset ma niską pewność danych")
    if profile_status in {"caution", "technical_control"}:
        hard_block = True
        reasons.append("profil odsłuchowy oznacza ten wariant jako ostrożny/kontrolny")
    elif profile_status == "overall_reference_candidate":
        reasons.append("profil odsłuchowy: kandydat całościowy 102 — nie blokuj za samą odmienność od 90")
    elif profile_status == "compromise_candidate":
        reasons.append("profil odsłuchowy: kandydat kompromisowy między 90 a 102")
    if score < 70.0:
        if profile_status == "overall_reference_candidate":
            reasons.append(f"niska szansa zachowania pełnej tekstury 90 ({score:.1f}/100), ale wariant może być lepszy całościowo")
        else:
            hard_block = True
            reasons.append(f"szansa zastąpienia 90 tylko {score:.1f}/100")
    elif score < 78.0:
        reasons.append(f"wynik {score:.1f}/100 jest za mały na zmianę bez bardzo dobrego odsłuchu")

    if np.isfinite(body_delta) and body_delta < -0.8:
        hard_block = True
        reasons.append(f"tekstura 160–220 względem 125–160 niższa od referencji o {body_delta:.2f} dB")
    if np.isfinite(full_delta) and full_delta < -0.8:
        if profile_status == "overall_reference_candidate" and full_delta > -1.2:
            reasons.append(f"fullness 120–300 niższy od 90 o {full_delta:.2f} dB — może kosztować trochę tekstury, ale nie blokuje 102 jako presetu całościowego")
        else:
            hard_block = True
            reasons.append(f"fullness 120–300 niższy od referencji o {full_delta:.2f} dB")
    if np.isfinite(body_delta) and body_delta > 2.0:
        # Nadmiar body może brzmieć początkowo atrakcyjnie, ale zabić światło/presence.
        if not np.isfinite(presence_delta):
            hard_block = True
            reasons.append(f"tekstura niższej średnicy większa o {body_delta:.2f} dB, ale brak danych 1–4 kHz")
        elif presence_delta < body_delta * 0.35:
            hard_block = True
            reasons.append("niższa średnica rośnie szybciej niż presence — ryzyko maskowania blasku")
        else:
            reasons.append(f"tekstura niższej średnicy większa o {body_delta:.2f} dB — wymaga odsłuchu pod pogrubienie")
    if np.isfinite(glass_delta) and glass_delta > 1.5:
        hard_block = True
        reasons.append(f"glass 4–8 kHz wyższy od referencji o {glass_delta:.2f} dB")

    if hard_block:
        return "NIE ZASTĘPUJ TEKSTURY 90", "; ".join(reasons) if reasons else "ochrona tekstury referencji 90 BW48", False
    if profile_status == "overall_reference_candidate":
        return "KANDYDAT CAŁOŚCIOWY — A/B", "; ".join(reasons) if reasons else "może być presetem głównym, jeśli odsłuch potwierdzi scenę/energię/komfort", False
    if profile_status == "compromise_candidate":
        return "KANDYDAT KOMPROMISOWY — A/B", "; ".join(reasons) if reasons else "sprawdź jako złoty środek między 90 i 102", False
    if reasons:
        return "A/B TYLKO — OSTROŻNIE", "; ".join(reasons), False
    if _confidence_rank(overall_confidence) >= _confidence_rank("HIGH") and score >= 82.0:
        return "KANDYDAT WARUNKOWY", "może rywalizować z 90 Hz, ale dopiero po odsłuchu pianina/wokalu", False
    return "A/B TYLKO", "pomiar wskazuje kandydata, ale nie daje automatycznej zgody na zmianę referencji", False


def _decision_confidence_for_row(r: Dict[str, object]) -> Tuple[str, str]:
    try:
        hi = float(r.get("measurement_high_hz", float("nan")))
    except Exception:
        hi = float("nan")
    if not np.isfinite(hi):
        return "LOW", "brak pewnej informacji o zakresie pomiaru"
    if hi < 300:
        return "LOW", f"pomiar kończy się przy {hi:.0f} Hz — oceniono bas/body, ale brak pełnego fullness 120–300, presence 1–4 kHz i glass 4–8 kHz"
    if hi < 1000:
        return "LOW", f"pomiar kończy się przy {hi:.0f} Hz — brak niższej/pełnej średnicy 300–1000 Hz oraz presence/glow"
    if hi < 4000:
        return "MEDIUM", f"pomiar kończy się przy {hi:.0f} Hz — brak pełnej oceny presence 1–4 kHz i glass 4–8 kHz"
    if hi < 8000:
        return "MEDIUM", f"pomiar kończy się przy {hi:.0f} Hz — można ocenić presence, ale nie pełne ryzyko szklistości 4–8 kHz"
    return "HIGH", "pomiar obejmuje bas, body, presence 1–4 kHz i glass 4–8 kHz"


def _load_listening_profile_entries() -> List[Dict[str, str]]:
    """Lekki parser profilu odsłuchowego bez zależności od PyYAML."""
    path = Path(__file__).with_name("USER_LISTENING_PROFILE.yaml")
    default = [
        {"match": "90", "status": "midrange_reference", "note": "referencja tekstury/naturalności średnicy; nie jest automatycznie jedynym presetem głównym", "penalty": "0"},
        {"match": "96", "status": "listening_confirmed_candidate", "note": "kandydat odsłuchowo bez rozdmuchania sceny; lokalne ostrzeżenia XO traktować jako techniczne, do potwierdzenia basem/body", "penalty": "0"},
        {"match": "102", "status": "conditional_candidate", "note": "kandydat tylko warunkowo: poprzedni 102 slot4 był podejrzany o wadliwy pomiar/filtr Dirac, więc nie jest automatyczną referencją sceny", "penalty": "2"},
        {"match": "100", "status": "compromise_candidate", "note": "kompromis do sprawdzenia między naturalnością 90 a energią wyższego XO", "penalty": "0"},
        {"match": "82", "status": "technical_control", "note": "technicznie mocny bas/czas, ale odsłuchowo może być chudziej/dalej/gładziej", "penalty": "6"},
    ]
    if not path.exists():
        return default
    try:
        entries: List[Dict[str, str]] = []
        cur: Optional[Dict[str, str]] = None
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("- match:"):
                if cur:
                    entries.append(cur)
                cur = {"match": line.split(":", 1)[1].strip().strip('"\''), "status": "", "note": "", "penalty": "0"}
            elif cur is not None and ":" in line:
                k, v = line.split(":", 1)
                cur[k.strip()] = v.strip().strip('"\'')
        if cur:
            entries.append(cur)
        return entries or default
    except Exception:
        return default


def _apply_listening_profile(r: Dict[str, object], entries: List[Dict[str, str]], ref: Optional[Dict[str, object]]) -> float:
    text = (str(r.get("label", "")) + " " + str(r.get("source_names", ""))).lower()
    r["listening_profile_status"] = ""
    r["listening_profile_note"] = ""
    r["listening_profile_penalty"] = 0.0
    for e in entries:
        m = str(e.get("match", "")).lower().strip()
        if not m or m not in text:
            continue
        r["listening_profile_status"] = e.get("status", "")
        r["listening_profile_note"] = e.get("note", "")
        try:
            pen = float(e.get("penalty", "0") or 0)
        except Exception:
            pen = 0.0
        if ref is not None and r is ref:
            pen = 0.0
        r["listening_profile_penalty"] = pen
        return pen
    return 0.0


def _parse_simple_yaml_scalar(value: str) -> object:
    value = str(value).strip()
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    if value.lower() in {"null", "none", "~", ""}:
        return None
    value = value.strip('"\'')
    try:
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
            return float(value)
    except Exception:
        pass
    return value


def _load_reference_profile(filename: str | Path) -> Tuple[Dict[str, object], str]:
    """Lekki parser USER_REFERENCE_*.yaml bez zależności od PyYAML.

    v20.07: akceptuje zarówno plik w folderze aplikacji, jak i jawnie wybrany
    plik YAML z GUI. Dzięki temu wybór referencji SHD może pomagać również
    POST-DIRAC w rozpoznaniu referencji tekstury 90.

    v19.76: plik USER_REFERENCE_102BW48.yaml ma realnie wpływać na to,
    jak wyszukujemy drugą referencję. Jeżeli 102 nie występuje w pomiarze,
    raport ma uczciwie napisać, że dostępny jest tylko profil odsłuchowy.
    """
    raw_path = Path(filename)
    if raw_path.is_absolute() or raw_path.exists():
        path = raw_path
    else:
        path = Path(__file__).with_name(str(filename))
    if not path.exists():
        return {}, f"brak pliku {filename}"
    try:
        data: Dict[str, object] = {"__path__": str(path)}
        current_section = ""
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            if raw.startswith(" ") or raw.startswith("\t"):
                # Sekcje typu subjective_notes czytamy jako klucze pomocnicze,
                # ale nie są krytyczne dla logiki dopasowania referencji.
                line = raw.strip()
                if current_section and ":" in line:
                    k, v = line.split(":", 1)
                    data[f"{current_section}.{k.strip()}"] = _parse_simple_yaml_scalar(v)
                continue
            line = raw.strip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            key = k.strip()
            if v.strip() == "":
                current_section = key
                continue
            current_section = ""
            data[key] = _parse_simple_yaml_scalar(v)
        return data, "wczytano"
    except Exception as exc:
        return {}, f"błąd odczytu {filename}: {exc}"


def _row_matches_reference_profile(r: Dict[str, object], profile: Dict[str, object]) -> bool:
    if not profile:
        return False
    label_text = (str(r.get("label", "")) + " " + str(r.get("source_names", ""))).lower()
    name = str(profile.get("name") or "").strip().lower()
    if name and name.replace("_", " ") in label_text.replace("_", " "):
        return True
    xo = profile.get("crossover_hz")
    filt = str(profile.get("filter") or "").strip().upper()
    try:
        xo_i = int(xo) if xo is not None else None
    except Exception:
        xo_i = None
    if xo_i is not None:
        try:
            row_xo = int(r.get("xo_hz", r.get("xo", 0)) or 0)
        except Exception:
            row_xo = 0
        row_filt = str(r.get("filter_name", "") or "").strip().upper()
        if row_xo == xo_i and (not filt or row_filt == filt):
            return True
    return False


def _reference_profile_label(profile: Dict[str, object], fallback: str = "102 BW48") -> str:
    if profile.get("name"):
        return str(profile.get("name"))
    xo = profile.get("crossover_hz")
    filt = profile.get("filter")
    if xo and filt:
        return f"{xo}_{filt}"
    return fallback


def _find_reference(rows: List[Dict[str, object]], reference: Optional[str], profile: Optional[Dict[str, object]] = None) -> Optional[Dict[str, object]]:
    if reference:
        low = reference.lower()
        matches = [r for r in rows if low in str(r["label"]).lower() or low in str(r.get("source_names", "")).lower()]
        if matches:
            return matches[0]
    if profile:
        matches = [r for r in rows if _row_matches_reference_profile(r, profile)]
        if matches:
            return matches[0]
    # Najpierw 90 BW48 slot2, bo to referencja użytkownika.
    preferred = [r for r in rows if "90" in str(r["label"]) and "BW48" in str(r["label"]).upper()]
    if preferred:
        return preferred[0]
    return None


def _find_overall_reference(rows: List[Dict[str, object]], reference: Optional[str] = None, profile: Optional[Dict[str, object]] = None) -> Optional[Dict[str, object]]:
    """v19.76: druga referencja — najpierw według USER_REFERENCE_102BW48.yaml.

    90 BW48 zostaje punktem tekstury/naturalności średnicy, ale 102 może być
    punktem odniesienia dla sceny, energii, czystości basu i mniejszego zmęczenia.
    Jeżeli 102 nie ma w pliku POST-DIRAC, raport ma to jawnie pokazać.
    """
    if profile:
        matches = [r for r in rows if _row_matches_reference_profile(r, profile)]
        if matches:
            return matches[0]
    if reference:
        low = reference.lower()
        matches = [r for r in rows if low in str(r.get("label", "")).lower() or low in str(r.get("source_names", "")).lower()]
        if matches:
            return matches[0]
    preferred = [r for r in rows if "102" in str(r.get("label", "")) and "BW48" in str(r.get("label", "")).upper()]
    if preferred:
        return preferred[0]
    return None

def _fog_bloom_risk_score(r: Dict[str, object]) -> float:
    """v19.76: ryzyko mgiełki/bułkowatości.

    To nie jest prawda absolutna o barwie. To pomiarowa flaga: dużo ogona/time,
    duży span 75–125 Hz, słabsze sumowanie i ciężka niższa średnica mogą razem
    dawać mgiełkę, bułkowaty bas i mniejszą scenę.
    """
    gd = _safe_float_value(r, "gd_excess_75_125_ms", 60.0)
    span = _safe_float_value(r, "spl_span_75_125_db", 8.0)
    summ = _safe_float_value(r, "sum_margin_mean_75_125_db", 0.0)
    full = _safe_float_value(r, "fullness_120_300_vs_300_1000_db", 0.0)
    body = _safe_float_value(r, "body_125_220_vs_75_125_db", 0.0)
    lrbal = _safe_float_value(r, "lr_balance_mean_75_125_db", 2.0)
    risk = 0.0
    risk += max(0.0, gd - 35.0) * 0.75
    risk += max(0.0, span - 5.0) * 6.0
    risk += max(0.0, -summ) * 6.0
    risk += max(0.0, lrbal - 1.5) * 5.0
    # Dużo masy bez porządku może dawać bułkę; niewielka pełnia nie jest karana.
    risk += max(0.0, body - 2.0) * 5.0
    risk += max(0.0, full - 1.5) * 3.0
    # v19.76: profil odsłuchowy może kalibrować wskaźnik, bo "mgiełka" nie jest
    # czysto techniczną metryką GD. Użytkownik zgłosił mniej mgiełki w 102 mimo
    # gorszego GD, więc 102 nie może dostawać automatycznie czerwonej flagi.
    profile = str(r.get("listening_profile_status", "")).lower()
    if profile == "overall_reference_candidate":
        risk = min(risk, 28.0)
    elif profile == "midrange_reference":
        risk = max(risk, 42.0)
    elif profile == "compromise_candidate":
        risk = min(risk, 45.0)
    return float(np.clip(risk, 0.0, 100.0))


def _listening_comfort_score(r: Dict[str, object]) -> float:
    """v19.76: orientacyjny komfort dłuższego słuchania."""
    score = 76.0
    glass = _safe_float_value(r, "glass_4_8k_vs_1_4k_db")
    presence = _safe_float_value(r, "presence_1_4k_vs_300_1000_db")
    fog = _safe_float_value(r, "fog_bloom_risk_0_100", _fog_bloom_risk_score(r))
    gd = _safe_float_value(r, "gd_excess_75_125_ms", 60.0)
    if np.isfinite(glass):
        score -= max(0.0, glass - 0.5) * 8.0
    if np.isfinite(presence):
        score -= max(0.0, presence - 3.0) * 4.0
    score -= max(0.0, fog - 25.0) * 0.45
    score -= max(0.0, gd - 60.0) * 0.18
    profile = str(r.get("listening_profile_status", "")).lower()
    if profile == "overall_reference_candidate":
        score += 14.0
    if profile == "midrange_reference":
        # 90 jest pięknym punktem tekstury, ale użytkownik zgłosił mgiełkę i bułkowaty bas.
        score -= 6.0
    if profile == "compromise_candidate":
        score += 5.0
    return float(np.clip(score, 0.0, 100.0))


def _dual_reference_compromise_score(r: Dict[str, object], mid_ref: Optional[Dict[str, object]], overall_ref: Optional[Dict[str, object]]) -> float:
    """Wynik kompromisu: zachowaj teksturę 90, ale idź w czystość/scenę/komfort 102."""
    mid_safety = _safe_float_value(r, "midrange_safety_score_0_100", 60.0)
    fog = _safe_float_value(r, "fog_bloom_risk_0_100", _fog_bloom_risk_score(r))
    comfort = _safe_float_value(r, "listening_comfort_0_100", _listening_comfort_score(r))
    summ = _safe_float_value(r, "sum_margin_mean_75_125_db", 0.0)
    gd = _safe_float_value(r, "gd_excess_75_125_ms", 70.0)
    # bonus za bliskość 100 Hz jako naturalny kompromis między 90 i 102, ale nie dominujący.
    try:
        xo = int(r.get("xo_hz", r.get("xo", 0)) or 0)
    except Exception:
        xo = 0
    xo_bonus = max(0.0, 1.0 - abs(float(xo) - 100.0) / 8.0) * 5.0 if xo else 0.0
    tech = np.clip(52.0 + summ * 4.0 - max(0.0, gd - 45.0) * 0.35, 0.0, 100.0)
    score = 0.34 * mid_safety + 0.26 * comfort + 0.22 * (100.0 - fog) + 0.18 * tech + xo_bonus
    # Jeżeli mamy overall_ref=102, preferuj kandydatów, którzy nie są wyraźnie mniej komfortowi/czyści od 102.
    if overall_ref is not None and r is not overall_ref:
        fog_delta = _safe_float_value(r, "fog_delta_vs_overall_ref")
        comfort_delta = _safe_float_value(r, "comfort_delta_vs_overall_ref")
        if np.isfinite(fog_delta) and fog_delta > 18:
            score -= min(10.0, (fog_delta - 18) * 0.4)
        if np.isfinite(comfort_delta) and comfort_delta < -12:
            score -= min(10.0, abs(comfort_delta + 12) * 0.4)
    return float(np.clip(score, 0.0, 100.0))




def _score_between(value: float, good: float, bad: float, *, higher_is_better: bool = True) -> float:
    try:
        v = float(value)
    except Exception:
        return 50.0
    if not np.isfinite(v):
        return 50.0
    good = float(good)
    bad = float(bad)
    if higher_is_better:
        if v >= good:
            return 100.0
        if v <= bad:
            return 0.0
        return float(100.0 * (v - bad) / max(1e-9, good - bad))
    if v <= good:
        return 100.0
    if v >= bad:
        return 0.0
    return float(100.0 * (bad - v) / max(1e-9, bad - good))


def _v20_post_dirac_metrics(r: Dict[str, object]) -> Dict[str, object]:
    """v20.07 — polski model odporności dla realnego POST-DIRAC.

    W POST-DIRAC mamy już wynik rzeczywistego slotu, więc metryki są silniejsze
    niż predykcja z RAW. Nadal nie zastępują odsłuchu, bo pełna odporność
    przestrzenna wymaga kilku pozycji mikrofonu.
    """
    has_lr = bool(r.get("has_lr"))
    span = _safe_float_value(r, "spl_span_75_125_db", 8.0)
    std = _safe_float_value(r, "spl_std_75_125_db", 3.0)
    gd = _safe_float_value(r, "gd_excess_75_125_ms", 70.0)
    gd_spread = _safe_float_value(r, "gd_spread_75_125_ms", 30.0)
    phase_rms = _safe_float_value(r, "phase_resid_rms_75_125_deg", 55.0)
    phase_p2p = _safe_float_value(r, "phase_resid_p2p_75_125_deg", 180.0)
    summ_mean = _safe_float_value(r, "sum_margin_mean_75_125_db", 0.0)
    summ_worst = _safe_float_value(r, "sum_margin_worst_75_125_db", -3.0)
    summ_xo = _safe_float_value(r, "sum_margin_at_xo_db", -3.0)
    summ_95 = _safe_float_value(r, "sum_margin_at_95_db", -3.0)
    summ_100 = _safe_float_value(r, "sum_margin_at_100_db", -3.0)
    lr_phase = _safe_float_value(r, "lr_phase_at_xo_deg", 180.0)
    lr_phase_mean = _safe_float_value(r, "lr_phase_mean_75_125_deg", 150.0)
    lr_balance = _safe_float_value(r, "lr_balance_mean_75_125_db", 4.0)
    lr_balance_max = _safe_float_value(r, "lr_balance_max_75_125_db", 7.0)
    fog = _safe_float_value(r, "fog_bloom_risk_0_100", 60.0)
    comfort = _safe_float_value(r, "listening_comfort_0_100", 50.0)
    mid_safety = _safe_float_value(r, "midrange_safety_score_0_100", 60.0)

    span_score = _score_between(span, good=3.0, bad=10.0, higher_is_better=False)
    std_score = _score_between(std, good=1.0, bad=4.5, higher_is_better=False)
    gd_score = _score_between(gd, good=18.0, bad=85.0, higher_is_better=False)
    gd_spread_score = _score_between(gd_spread, good=8.0, bad=45.0, higher_is_better=False)
    phase_rms_score = _score_between(phase_rms, good=20.0, bad=90.0, higher_is_better=False)
    phase_p2p_score = _score_between(phase_p2p, good=70.0, bad=240.0, higher_is_better=False)
    summ_mean_score = _score_between(summ_mean, good=1.5, bad=-2.5, higher_is_better=True)
    summ_worst_score = _score_between(summ_worst, good=-0.5, bad=-6.0, higher_is_better=True)
    summ_xo_score = _score_between(summ_xo, good=1.0, bad=-5.0, higher_is_better=True)
    summ_95_score = _score_between(summ_95, good=1.0, bad=-5.0, higher_is_better=True)
    summ_100_score = _score_between(summ_100, good=1.0, bad=-5.0, higher_is_better=True)
    lr_phase_score = _score_between(abs(lr_phase), good=45.0, bad=150.0, higher_is_better=False) if has_lr else 50.0
    lr_phase_mean_score = _score_between(abs(lr_phase_mean), good=55.0, bad=160.0, higher_is_better=False) if has_lr else 50.0
    balance_score = _score_between(lr_balance, good=1.2, bad=5.5, higher_is_better=False) if has_lr else 50.0
    balance_max_score = _score_between(lr_balance_max, good=2.5, bad=8.0, higher_is_better=False) if has_lr else 50.0

    # Odporność przestrzenna: w pełni wiarygodna dopiero z wieloma pozycjami mikrofonu.
    spatial = float(np.clip(
        0.18 * span_score + 0.12 * std_score + 0.16 * summ_worst_score + 0.14 * balance_score
        + 0.14 * balance_max_score + 0.14 * lr_phase_mean_score + 0.12 * gd_spread_score,
        0.0, 100.0,
    ))

    # v20.07: jeżeli dostępny jest realny pomiar L/MLP/P, wzmacnia on ocenę odporności.
    three_point = _safe_float_value(r, "odpornosc_3pkt_l_mlp_p_0_100")
    three_point_null = _safe_float_value(r, "ryzyko_dziury_3pkt_0_100")
    if np.isfinite(three_point):
        spatial = float(np.clip(0.65 * three_point + 0.35 * spatial, 0.0, 100.0))

    # Obciążenie Diraca: im wyżej, tym gorzej.
    correction_quality = float(np.clip(
        0.18 * span_score + 0.16 * std_score + 0.16 * gd_score + 0.14 * phase_rms_score
        + 0.16 * summ_mean_score + 0.12 * summ_worst_score + 0.08 * mid_safety,
        0.0, 100.0,
    ))
    dirac_burden = float(np.clip(100.0 - correction_quality, 0.0, 100.0))

    # Ryzyko dziury L+P po Diracu: im wyżej, tym gorzej.
    null_score = float(np.clip(
        0.24 * (100.0 - summ_xo_score) + 0.20 * (100.0 - summ_worst_score)
        + 0.16 * (100.0 - summ_95_score) + 0.12 * (100.0 - summ_100_score)
        + 0.16 * (100.0 - lr_phase_score) + 0.12 * (100.0 - balance_score),
        0.0, 100.0,
    ))
    if np.isfinite(three_point_null):
        null_score = float(np.clip(max(null_score, three_point_null), 0.0, 100.0))

    phase_cont = float(np.clip(
        0.26 * phase_rms_score + 0.20 * phase_p2p_score + 0.18 * gd_spread_score
        + 0.16 * lr_phase_mean_score + 0.12 * summ_mean_score + 0.08 * summ_worst_score,
        0.0, 100.0,
    ))

    listening_cal = float(np.clip(
        0.24 * mid_safety + 0.22 * comfort + 0.20 * (100.0 - fog)
        + 0.16 * (100.0 - null_score) + 0.10 * phase_cont + 0.08 * (100.0 - dirac_burden),
        0.0, 100.0,
    ))

    robustness = float(np.clip(
        0.24 * spatial + 0.22 * (100.0 - null_score) + 0.18 * (100.0 - dirac_burden)
        + 0.18 * phase_cont + 0.18 * listening_cal,
        0.0, 100.0,
    ))

    if robustness >= 76 and null_score <= 35 and dirac_burden <= 40:
        label = "odporny preset po Diracu"
    elif robustness >= 62 and null_score <= 55:
        label = "warunkowo odporny preset"
    elif null_score > 65:
        label = "wysokie ryzyko dziury L+P"
    elif dirac_burden > 65:
        label = "duże obciążenie korekcji"
    else:
        label = "diagnostyczny — potrzebny odsłuch"

    notes: List[str] = []
    if np.isfinite(_safe_float_value(r, "odpornosc_3pkt_l_mlp_p_0_100")):
        notes.append("uwzględniono realną odporność 3-punktową L/MLP/P")
    if not has_lr:
        notes.append("brak osobnych L/R — ograniczona ocena sumy stereo")
    if null_score > 55:
        notes.append("sprawdź dziurę/kasowanie w L+P po Diracu")
    if dirac_burden > 55:
        notes.append("Dirac prawdopodobnie mocno pracuje w basie/XO")
    if spatial < 55:
        notes.append("niska odporność na przesunięcie głowy lub asymetrię pokoju")
    if phase_cont < 55:
        notes.append("słaba ciągłość fazy/energii 75–125 Hz")
    if not notes:
        notes.append("model v20 bez dużych ostrzeżeń; potwierdź odsłuchem")

    return {
        "odpornosc_przestrzenna_0_100": spatial,
        "odpornosc_3pkt_l_mlp_p_0_100": r.get("odpornosc_3pkt_l_mlp_p_0_100", float("nan")),
        "obciazenie_diraca_ryzyko_0_100": dirac_burden,
        "ryzyko_dziury_lp_po_dirac_0_100": null_score,
        "ciaglosc_fazy_energii_0_100": phase_cont,
        "ranking_skalibrowany_odsluchem_0_100": listening_cal,
        "model_odpornosci_v20_0_100": robustness,
        "etykieta_modelu_v20": label,
        "uwagi_modelu_v20": "; ".join(notes),
    }

def _row_diff(candidate: Dict[str, object], ref: Dict[str, object], key: str) -> float:
    try:
        return float(candidate[key]) - float(ref[key])
    except Exception:
        return float("nan")



def _base_slot_key_for_3point(r: Dict[str, object]) -> str:
    """Klucz slotu bez pozycji mikrofonu.

    Przy jasnym nazewnictwie label jest już zwykle bez posL/MLP/posP, bo
    _friendly_label_from_slot zostawia np. "90 Hz BW48 slot2". Ten fallback
    czyści też sytuacje, w których pozycja została w nazwie źródłowej.
    """
    label = str(r.get("label", "") or r.get("source_names", ""))
    low = _normalize_pl_ascii(label)
    low = re.sub(r"(^|[^a-z0-9])(mlp|sweetspot|sweet\s*spot|center|centre|srodek)([^a-z0-9]|$)", " ", low)
    low = re.sub(r"(^|[^a-z0-9])(pos|poz|pozycja|punkt|mic|mikrofon)[_\-\s]*(l|p|r|lewa|prawa|left|right)([^a-z0-9]|$)", " ", low)
    low = re.sub(r"(^|[^a-z0-9])(l|p|r|lewa|prawa|left|right)[_\-\s]*(pos|poz|pozycja|punkt|mic|mikrofon)([^a-z0-9]|$)", " ", low)
    low = re.sub(r"[^a-z0-9]+", "_", low).strip("_")
    return low or re.sub(r"[^a-z0-9]+", "_", str(r.get("label", "")).lower()).strip("_")


def _nan_range(values: List[float]) -> float:
    vals = [float(v) for v in values if np.isfinite(float(v))]
    if len(vals) < 2:
        return float("nan")
    return float(max(vals) - min(vals))


def _nan_min(values: List[float]) -> float:
    vals = [float(v) for v in values if np.isfinite(float(v))]
    return float(min(vals)) if vals else float("nan")


def _nan_max(values: List[float]) -> float:
    vals = [float(v) for v in values if np.isfinite(float(v))]
    return float(max(vals)) if vals else float("nan")


def _three_point_score_for_rows(pos_rows: List[Dict[str, object]]) -> Tuple[float, Dict[str, float], str]:
    """Ocena odporności L/MLP/P. Bazuje na pełnych pomiarach L+P w pozycjach głowy."""
    summ = [_safe_float_value(r, "sum_margin_mean_75_125_db") for r in pos_rows]
    worst_sum = [_safe_float_value(r, "sum_margin_worst_75_125_db") for r in pos_rows]
    dip = [_safe_float_value(r, "dip_88_104_vs_shoulders_db") for r in pos_rows]
    body = [_safe_float_value(r, "body_125_220_vs_75_125_db") for r in pos_rows]
    gd = [_safe_float_value(r, "gd_excess_75_125_ms") for r in pos_rows]
    span = [_safe_float_value(r, "spl_span_75_125_db") for r in pos_rows]

    summ_range = _nan_range(summ)
    body_range = _nan_range(body)
    worst_sum_min = _nan_min(worst_sum)
    worst_dip_min = _nan_min(dip)
    gd_max = _nan_max(gd)
    span_max = _nan_max(span)

    stability_sum = _score_between(summ_range, good=0.8, bad=4.0, higher_is_better=False)
    stability_body = _score_between(body_range, good=0.8, bad=4.0, higher_is_better=False)
    worst_sum_score = _score_between(worst_sum_min, good=-0.8, bad=-6.0, higher_is_better=True)
    worst_dip_score = _score_between(worst_dip_min, good=-0.5, bad=-6.0, higher_is_better=True)
    gd_score = _score_between(gd_max, good=25.0, bad=95.0, higher_is_better=False)
    span_score = _score_between(span_max, good=3.5, bad=11.0, higher_is_better=False)

    score = float(np.clip(
        0.24 * stability_sum + 0.18 * worst_sum_score + 0.18 * worst_dip_score
        + 0.16 * stability_body + 0.12 * gd_score + 0.12 * span_score,
        0.0, 100.0,
    ))
    null_risk_3pt = float(np.clip(
        0.35 * (100.0 - worst_sum_score) + 0.30 * (100.0 - worst_dip_score)
        + 0.20 * max(0.0, summ_range if np.isfinite(summ_range) else 0.0) * 12.0
        + 0.15 * max(0.0, span_max - 5.0 if np.isfinite(span_max) else 0.0) * 8.0,
        0.0, 100.0,
    ))
    notes = []
    if np.isfinite(summ_range) and summ_range > 2.5:
        notes.append("duży rozrzut sumowania L+P między pozycjami")
    if np.isfinite(worst_sum_min) and worst_sum_min < -3.0:
        notes.append("co najmniej jedna pozycja ma lokalne kasowanie L+P")
    if np.isfinite(worst_dip_min) and worst_dip_min < -3.0:
        notes.append("co najmniej jedna pozycja ma dziurę 88–104 Hz")
    if np.isfinite(body_range) and body_range > 2.5:
        notes.append("body 125–220 zmienia się mocno przy przesunięciu głowy")
    if not notes:
        notes.append("stabilność L/MLP/P bez dużych ostrzeżeń")
    return score, {
        "rozrzut_sumowania_3pkt_db": summ_range,
        "najgorsze_sumowanie_3pkt_db": worst_sum_min,
        "najgorsza_dziura_3pkt_db": worst_dip_min,
        "rozrzut_body_3pkt_db": body_range,
        "najgorsze_gd_3pkt_ms": gd_max,
        "najwiekszy_span_3pkt_db": span_max,
        "ryzyko_dziury_3pkt_0_100": null_risk_3pt,
    }, "; ".join(notes)


def _aggregate_three_point_rows(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Scal pomiary L+P z pozycji L/MLP/P do jednej decyzji slotu.

    Tabela decyzji ma mieć jeden wiersz na slot. Jeżeli w .mdat są trzy pełne
    pomiary L+P tego samego slotu z różnych pozycji mikrofonu, decyzja bazuje
    na MLP, a odporność 3-punktowa trafia do osobnych kolumn.
    """
    buckets: Dict[str, List[Dict[str, object]]] = {}
    order: List[str] = []
    for r in rows:
        key = _base_slot_key_for_3point(r)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)

    out_rows: List[Dict[str, object]] = []
    for key in order:
        items = buckets[key]
        # Agregujemy tylko wtedy, gdy są jawne pozycje mikrofonu albo duplikaty tego samego slotu.
        explicit_positions = [r for r in items if str(r.get("pozycja_mikrofonu_jawna", "NIE")).upper() == "TAK"]
        should_aggregate = len(items) > 1 and (len(explicit_positions) >= 1)
        if not should_aggregate:
            row = dict(items[0])
            row.setdefault("pomiar_3pkt_status", "BRAK — pojedynczy MLP lub brak jawnych pozycji")
            row.setdefault("pomiar_3pkt_liczba", 1)
            row.setdefault("pomiar_3pkt_pozycje", str(row.get("pozycja_mikrofonu", "MLP")))
            row.setdefault("odpornosc_3pkt_l_mlp_p_0_100", float("nan"))
            row.setdefault("uwagi_3pkt", "brak pełnego pomiaru L/MLP/P")
            out_rows.append(row)
            continue

        # Jeżeli są duplikaty tej samej pozycji, bierzemy ostatni pomiar danej pozycji i sygnalizujemy to w źródłach.
        by_pos: Dict[str, Dict[str, object]] = {}
        duplicate_positions: List[str] = []
        for r in sorted(items, key=lambda rr: _position_sort_key(str(rr.get("pozycja_mikrofonu", "MLP")))):
            pos = str(r.get("pozycja_mikrofonu", "MLP")).upper()
            if pos in by_pos:
                duplicate_positions.append(pos)
            by_pos[pos] = r
        base = by_pos.get("MLP") or by_pos.get("M") or items[0]
        row = dict(base)
        positions = [p for p in ["L", "MLP", "P"] if p in by_pos]
        pos_rows = [by_pos[p] for p in positions]
        complete = all(p in by_pos for p in ["L", "MLP", "P"])
        score, vals, note = _three_point_score_for_rows(pos_rows)
        row["label"] = str(base.get("label", items[0].get("label", "")))
        row["source_names"] = "; ".join(str(r.get("source_names", "")) for r in pos_rows if r.get("source_names"))
        row["pozycja_mikrofonu"] = "MLP"
        row["pozycja_mikrofonu_jawna"] = "TAK" if "MLP" in by_pos else str(base.get("pozycja_mikrofonu_jawna", "NIE"))
        row["pomiar_3pkt_status"] = "PEŁNY L/MLP/P" if complete else "CZĘŚCIOWY — brakuje " + ", ".join(p for p in ["L", "MLP", "P"] if p not in by_pos)
        row["pomiar_3pkt_liczba"] = len(pos_rows)
        row["pomiar_3pkt_pozycje"] = ", ".join(positions)
        row["pomiar_3pkt_kompletny"] = "TAK" if complete else "NIE"
        row["odpornosc_3pkt_l_mlp_p_0_100"] = score
        row["uwagi_3pkt"] = note + (("; duplikaty pozycji: " + ", ".join(sorted(set(duplicate_positions)))) if duplicate_positions else "")
        for k, v in vals.items():
            row[k] = v
        out_rows.append(row)
    return out_rows

def write_reports(rows: List[Dict[str, object]], out_dir: str | Path, *, mdat: str, reference: Optional[str] = None, texture_reference_yaml: Optional[str | Path] = None) -> Tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_metric_registry(out)
    csv_path = out / "POST_DIRAC_AB_METRICS.csv"
    md_path = out / "POST_DIRAC_AB_REPORT.md"

    texture_profile: Dict[str, object] = {}
    texture_profile_load_status = "nie podano pliku referencji tekstury"
    if texture_reference_yaml:
        texture_profile, texture_profile_load_status = _load_reference_profile(texture_reference_yaml)
    ref = _find_reference(rows, reference, texture_profile)
    # v19.76/v20.07: druga referencja odsłuchowa. 90 = tekstura średnicy, 102 = całość/scena/energia/komfort.
    overall_profile, overall_profile_load_status = _load_reference_profile("USER_REFERENCE_102BW48.yaml")
    overall_ref = _find_overall_reference(rows, None, overall_profile)
    overall_profile_label = _reference_profile_label(overall_profile, "102 BW48")
    if overall_ref is not None:
        overall_reference_status = "WYKRYTE W POMIARZE"
        overall_reference_status_note = f"{overall_profile_label} występuje w tym pliku POST-DIRAC i jest używane jako druga referencja pomiarowa."
    elif overall_profile:
        overall_reference_status = "TYLKO PROFIL ODSŁUCHOWY"
        overall_reference_status_note = f"{overall_profile_label} opisano w USER_REFERENCE_102BW48.yaml, ale nie znaleziono go w tym pliku POST-DIRAC. Delty vs 102 pozostają niewiarygodne/niedostępne."
    else:
        overall_reference_status = "BRAK"
        overall_reference_status_note = f"Nie wczytano drugiej referencji: {overall_profile_load_status}."
    profile_entries = _load_listening_profile_entries()
    for _r in rows:
        conf, reason = _decision_confidence_for_row(_r)
        _r["decision_confidence"] = conf
        _r["decision_confidence_reason"] = reason
    overall_confidence, overall_confidence_reason = _overall_decision_confidence(rows)
    # v20.17: 90 BW48 jest referencją barwy/tekstury średnicy, NIE wzorcem ciężaru basu.
    # Bas oceniamy osobno: kandydat może być lżejszy w 25–60 Hz, ale powinien
    # zachować impuls 60–90 Hz, przejście 90–120 Hz i ciągłość 125–220 Hz.
    if ref is not None:
        for r in rows:
            body_delta = _row_diff(r, ref, "body_125_220_vs_75_125_db")  # diagnostyka relacji body/bas
            texture_delta = _row_diff(r, ref, "mid_texture_160_220_vs_125_160_db")
            full_delta = _row_diff(r, ref, "fullness_120_300_vs_300_1000_db")
            presence_delta = _row_diff(r, ref, "presence_1_4k_vs_300_1000_db")
            glass_delta = _row_diff(r, ref, "glass_4_8k_vs_1_4k_db")
            deep_bass_delta = _row_diff(r, ref, "band_25_60_vs_75_125_db")
            kick_delta = _row_diff(r, ref, "band_60_90_vs_75_125_db")
            transition_delta = _row_diff(r, ref, "band_90_120_vs_75_125_db")
            gd_gain = -_row_diff(r, ref, "gd_excess_75_125_ms")
            sum_gain = _row_diff(r, ref, "sum_margin_mean_75_125_db")

            penalty = 0.0
            body_under_risk = "OK"
            body_overload_risk = "OK"
            body_glow_balance = "90 BW48 użyte tylko jako referencja średnicy"

            # Najmocniejsza ochrona: kształt 125–220 i pełnia/presence, nie poziom 25–100.
            if np.isfinite(texture_delta):
                if texture_delta < -0.7:
                    penalty += min(28.0, (abs(texture_delta) - 0.7) * 20.0 + 6.0)
                    body_under_risk = "UWAGA" if texture_delta > -1.5 else "WYSOKIE"
                    body_glow_balance = "ryzyko utraty tekstury głosu/instrumentów 160–220 Hz"
                elif texture_delta > 1.2:
                    penalty += min(18.0, (texture_delta - 1.2) * 12.0 + 4.0)
                    body_overload_risk = "UWAGA" if texture_delta < 2.0 else "WYSOKIE"
                    body_glow_balance = "ryzyko pogrubienia niższej średnicy"
            else:
                penalty += 6.0
                body_glow_balance = "brak pełnych danych do porównania tekstury 125–220 Hz"

            if np.isfinite(full_delta):
                if full_delta < -0.8:
                    penalty += min(16.0, (abs(full_delta) - 0.8) * 7.0 + 4.0)
                elif full_delta > 1.8:
                    penalty += min(12.0, (full_delta - 1.8) * 6.0 + 3.0)
            if np.isfinite(presence_delta) and np.isfinite(texture_delta) and texture_delta > 1.0 and presence_delta < texture_delta * 0.25:
                penalty += min(10.0, (texture_delta * 0.25 - presence_delta) * 5.0 + 2.0)
                body_glow_balance = "niższa średnica rośnie szybciej niż presence — możliwe maskowanie blasku"
            if np.isfinite(glass_delta) and glass_delta > 1.5:
                penalty += min(8.0, (glass_delta - 1.5) * 3.0 + 2.0)
                body_glow_balance = "więcej szklistości/natarczywości 4–8 kHz"

            profile_penalty = _apply_listening_profile(r, profile_entries, ref)
            penalty += max(0.0, profile_penalty)

            def _window(v: float, target: float, good: float, zero: float) -> float:
                if not np.isfinite(v):
                    return 50.0
                d = abs(v - target)
                if d <= good:
                    return 100.0
                if d >= zero:
                    return 0.0
                return 100.0 * (1.0 - (d - good) / max(1e-9, zero - good))

            # Cel basu niezależny od 90: około 1–2 dB lżej w dole, kick blisko 90.
            bass_light_score = float(np.clip(
                0.45 * _window(deep_bass_delta, target=-1.3, good=0.9, zero=4.0)
                + 0.35 * _window(kick_delta, target=0.0, good=0.8, zero=3.5)
                + 0.20 * _window(transition_delta, target=0.0, good=0.8, zero=3.0),
                0.0, 100.0,
            ))

            bonus = 0.0
            if np.isfinite(gd_gain) and gd_gain > 8.0:
                bonus += min(4.0, gd_gain * 0.12)
            if np.isfinite(sum_gain) and sum_gain > 1.0:
                bonus += min(4.0, sum_gain)
            bonus += max(-3.0, min(3.0, (bass_light_score - 50.0) * 0.06))

            r["mid_reference_status"] = "WYKRYTA — 90 BW48, tylko średnica/barwa"
            r["body_delta_vs_reference_db"] = body_delta
            r["mid_texture_delta_vs_reference_db"] = texture_delta
            r["fullness_delta_vs_reference_db"] = full_delta
            r["presence_delta_vs_reference_db"] = presence_delta
            r["glass_delta_vs_reference_db"] = glass_delta
            r["deep_bass_25_60_delta_vs_mid_ref_db"] = deep_bass_delta
            r["kick_60_90_delta_vs_mid_ref_db"] = kick_delta
            r["transition_90_120_delta_vs_mid_ref_db"] = transition_delta
            r["bass_target_lighter_than_90_score_0_100"] = bass_light_score
            r["time_gain_vs_reference_ms"] = gd_gain
            r["sum_gain_vs_reference_db"] = sum_gain
            r["body_under_risk"] = body_under_risk
            r["body_overload_risk"] = body_overload_risk
            r["body_glow_balance"] = body_glow_balance
            r["body_overload_penalty"] = max(0.0, penalty - max(0.0, profile_penalty))
            r["reference_preservation_penalty"] = penalty
            r["reference_replacement_score_0_100"] = float(np.clip(
                float(r.get("organic_midrange_index_0_100", 0.0)) - penalty + bonus, 0.0, 100.0
            ))
            if r is ref:
                r["reference_verdict"] = "referencja tekstury/średnicy; ciężki bas nie jest celem"
            elif np.isfinite(texture_delta) and texture_delta < -0.9:
                r["reference_verdict"] = "uwaga: słabsza tekstura średnicy niż 90 BW48"
            elif np.isfinite(texture_delta) and texture_delta > 1.5:
                r["reference_verdict"] = "więcej niższej średnicy — sprawdź pogrubienie"
            elif bass_light_score >= 70.0:
                r["reference_verdict"] = "średnica blisko 90, bas lżejszy/kontrolowany — kandydat A/B"
            else:
                r["reference_verdict"] = "średnica do porównania z 90; bas oceniany osobno"
    else:
        for r in rows:
            _apply_listening_profile(r, profile_entries, None)
            r["mid_reference_status"] = "BRAK — 90 BW48 nie występuje w pliku"
            r["body_delta_vs_reference_db"] = float("nan")
            r["mid_texture_delta_vs_reference_db"] = float("nan")
            r["fullness_delta_vs_reference_db"] = float("nan")
            r["presence_delta_vs_reference_db"] = float("nan")
            r["glass_delta_vs_reference_db"] = float("nan")
            r["deep_bass_25_60_delta_vs_mid_ref_db"] = float("nan")
            r["kick_60_90_delta_vs_mid_ref_db"] = float("nan")
            r["transition_90_120_delta_vs_mid_ref_db"] = float("nan")
            r["bass_target_lighter_than_90_score_0_100"] = float("nan")
            r["reference_replacement_score_0_100"] = float(r.get("organic_midrange_index_0_100", 0.0))
            r["reference_verdict"] = "BRAK REFERENCJI 90 — nie oceniono zastąpienia ani delt średnicy"

    # v19.76: dodatkowe metryki dwóch referencji liczymy etapami.
    # Najpierw metryki bazowe dla wszystkich presetów, potem delty vs 102,
    # potem safety średnicy, a dopiero na końcu kompromis 90↔102.
    for r in rows:
        r["overall_reference_status"] = overall_reference_status
        r["overall_reference_status_note"] = overall_reference_status_note
        r["overall_reference_profile"] = overall_profile_label
        r["fog_bloom_risk_0_100"] = _fog_bloom_risk_score(r)

    for r in rows:
        r["listening_comfort_0_100"] = _listening_comfort_score(r)

    for r in rows:
        if overall_ref is not None:
            r["fog_delta_vs_overall_ref"] = _safe_float_value(r, "fog_bloom_risk_0_100") - _safe_float_value(overall_ref, "fog_bloom_risk_0_100")
            r["comfort_delta_vs_overall_ref"] = _safe_float_value(r, "listening_comfort_0_100") - _safe_float_value(overall_ref, "listening_comfort_0_100")
            r["gd_delta_vs_overall_ref_ms"] = _row_diff(r, overall_ref, "gd_excess_75_125_ms")
            r["sum_delta_vs_overall_ref_db"] = _row_diff(r, overall_ref, "sum_margin_mean_75_125_db")
            r["presence_delta_vs_overall_ref_db"] = _row_diff(r, overall_ref, "presence_1_4k_vs_300_1000_db")
        else:
            r["fog_delta_vs_overall_ref"] = float("nan")
            r["comfort_delta_vs_overall_ref"] = float("nan")
            r["gd_delta_vs_overall_ref_ms"] = float("nan")
            r["sum_delta_vs_overall_ref_db"] = float("nan")
            r["presence_delta_vs_overall_ref_db"] = float("nan")

    # v19.67/v19.76: osobna blokada muzyczna. Score może wskazać kandydata A/B,
    # ale nie może samodzielnie wypchnąć referencji 90 Hz.
    for r in rows:
        safety_score = _midrange_safety_score(r, ref)
        decision_class, block_reason, auto_replace_allowed = _music_safety_decision(r, ref, overall_confidence)
        r["midrange_safety_score_0_100"] = safety_score
        r["decision_class"] = decision_class
        r["replacement_block_reason"] = block_reason
        r["auto_replace_allowed"] = "TAK" if auto_replace_allowed else "NIE"
        r["overall_measurement_confidence"] = overall_confidence
        r["overall_measurement_confidence_reason"] = overall_confidence_reason

    for r in rows:
        r["dual_reference_compromise_score_0_100"] = _dual_reference_compromise_score(r, ref, overall_ref)
        r.update(_v20_post_dirac_metrics(r))

    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    # kandydat 82 po Dirac, jeśli jest; potem najlepszy wg indeksu organicznego.
    candidate_82 = None
    for r in rows:
        low = str(r["label"]).lower() + " " + str(r.get("source_names", "")).lower()
        if "82" in low and "po dirac" in low:
            candidate_82 = r
            break
    best_organic = max(rows, key=lambda x: float(x.get("reference_replacement_score_0_100", x.get("organic_midrange_index_0_100", 0.0)))) if rows else None

    lines: List[str] = [
        "# Analizator A/B po Dirac — v20.31",
        "",
        f"Plik: `{Path(mdat).name}`",
        "",
        "Ten raport porównuje **realne pomiary pełnych slotów po Dirac**. Nie zastępuje rankingu RAW — sprawdza, co faktycznie wyszło po filtrach, delayach, polaryzacji, pomieszczeniu i Dirac.",
        "",
        "v20.23 odrzuca pliki diagnostyczne zawierające tylko składowe L_main/R_main/L_sub/R_sub, ponieważ z takich danych nie da się uczciwie policzyć pełnego presetu odsłuchowego.",
        "",
        *( ["**BRAK REFERENCJI 90 BW48 W TYM PLIKU — wyniki są wyłącznie diagnostyczne; aplikacja nie ocenia zastąpienia referencji ani delt średnicy.**", ""] if ref is None else ["**90 BW48 jest używane wyłącznie jako referencja barwy i tekstury średnicy. Bas 25–60 Hz jest oceniany osobno i może być celowo lżejszy.**", ""] ),
        "## Tabela decyzji slotów — tylko pełny wynik L+P",
        "",
        "Ta tabela pokazuje każdy preset jako **pełny slot stereo L+P**. Dane lewej i prawej strony nie są tu mieszane z decyzją — są niżej, w diagnostyce bezpieczeństwa.",
        "",
        "| # | Slot / preset L+P | Wynik zastąpienia referencji 90 BW48 — POMIAR POST-DIRAC | Model odporności — POMIAR POST-DIRAC | Zmierzona odporność przestrzenna L/MLP/P — POMIAR POST-DIRAC | Zmierzone ryzyko dziury L+P po Dirac | Sumowanie L+P 75–125 Hz — POMIAR | Dziura 88–104 Hz — POMIAR | Nadmiar opóźnienia grupowego L+P 75–125 Hz — POMIAR | Tekstura 160–220 Hz względem 90 BW48 | Bas 25–60 Hz względem 90 BW48 | Kopnięcie 60–90 Hz względem 90 BW48 | Cel lżejszego basu | Komfort odsłuchowy | Pewność | Klasa decyzji | Werdykt |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]

    ranked = sorted(rows, key=lambda x: float(x.get("reference_replacement_score_0_100", x.get("organic_midrange_index_0_100", 0.0))), reverse=True)
    for i, r in enumerate(ranked, 1):
        lines.append(
            f"| {i} | {_md_escape(r['label'])} | {_score_cell(r, ref)} | {_fmt(r.get('model_odpornosci_v20_0_100'),1)} | {_fmt(r.get('odpornosc_3pkt_l_mlp_p_0_100'),1)} | {_fmt(r.get('ryzyko_dziury_lp_po_dirac_0_100'),1)} | "
            f"{_fmt(r.get('sum_margin_mean_75_125_db'),2,' dB')} | {_fmt(r.get('dip_88_104_vs_shoulders_db'),2,' dB')} | {_fmt(r.get('gd_excess_75_125_ms'),1,' ms')} | "
            f"{_fmt(r.get('mid_texture_delta_vs_reference_db'),2,' dB')} | {_fmt(r.get('deep_bass_25_60_delta_vs_mid_ref_db'),2,' dB')} | {_fmt(r.get('kick_60_90_delta_vs_mid_ref_db'),2,' dB')} | {_fmt(r.get('bass_target_lighter_than_90_score_0_100'),1)} | {_fmt(r.get('listening_comfort_0_100'),1)} | "
            f"{_md_escape(r.get('decision_confidence',''))} | {_md_escape(r.get('decision_class',''))} | {_md_escape(r.get('reference_verdict',''))} |"
        )

    lines.extend([
        "",
        "## Raporty decyzyjne UX",
        "",
        "Ta sekcja wykorzystuje pełne pomiary stereo L+P wykonane w trzech pozycjach mikrofonu: L = lewa pozycja głowy, MLP = środek, P = prawa pozycja głowy. Decyzja nadal bazuje na MLP, ale slot nie powinien wygrać, jeżeli rozpada się po przesunięciu głowy.",
        "",
        "| Slot / preset | Status pomiaru przestrzennego | Pozycje | Zmierzona odporność przestrzenna L/MLP/P — POMIAR POST-DIRAC | Rozrzut sumowania | Najgorsze sumowanie | Najgorsza dziura 88–104 Hz | Rozrzut korpusu 125–220 Hz | Uwagi |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ])
    for r in ranked:
        lines.append(
            f"| {_md_escape(r.get('label',''))} | {_md_escape(r.get('pomiar_3pkt_status',''))} | {_md_escape(r.get('pomiar_3pkt_pozycje',''))} | {_fmt(r.get('odpornosc_3pkt_l_mlp_p_0_100'),1)} | "
            f"{_fmt(r.get('rozrzut_sumowania_3pkt_db'),2,' dB')} | {_fmt(r.get('najgorsze_sumowanie_3pkt_db'),2,' dB')} | {_fmt(r.get('najgorsza_dziura_3pkt_db'),2,' dB')} | "
            f"{_fmt(r.get('rozrzut_body_3pkt_db'),2,' dB')} | {_md_escape(r.get('uwagi_3pkt',''))} |"
        )

    lines.extend([
        "",
        "## Diagnostyka stron — nie tabela decyzji",
        "",
        "Te dane pilnują, czy dobra suma L+P nie ukrywa problemu jednej strony. Słaba lewa albo prawa strona może zablokować rekomendację, ale o porównaniu slotów decyduje tabela L+P powyżej.",
        "",
        "| Slot / preset | Dostępne L/R | Balans L/R 75–125 | Faza L/R przy XO | Sumowanie przy XO | Najgorsze sumowanie 75–125 | Ostrzeżenie diagnostyczne |",
        "|---|---|---:|---:|---:|---:|---|",
    ])
    for r in ranked:
        warn_parts = []
        try:
            if float(r.get('lr_balance_mean_75_125_db', float('nan'))) > 2.5:
                warn_parts.append('duża nierówność L/R')
        except Exception:
            pass
        try:
            if float(r.get('lr_phase_at_xo_deg', float('nan'))) > 120:
                warn_parts.append('duża różnica fazy L/R przy XO — sprawdź centrum A/B')
        except Exception:
            pass
        try:
            if float(r.get('sum_margin_at_xo_db', float('nan'))) < -1.0:
                warn_parts.append('lokalne kasowanie L+P przy XO — nie wyrok sceny')
        except Exception:
            pass
        try:
            if float(r.get('sum_margin_worst_75_125_db', float('nan'))) < -3.0:
                warn_parts.append('lokalna dziura L+P w 75–125 — sprawdź body/bas')
        except Exception:
            pass
        if not warn_parts:
            warn_parts.append('bez dużych ostrzeżeń L/R')
        lines.append(
            f"| {_md_escape(r.get('label',''))} | {'TAK' if bool(r.get('has_lr')) else 'NIE'} | {_fmt(r.get('lr_balance_mean_75_125_db'),2,' dB')} | "
            f"{_fmt(r.get('lr_phase_at_xo_deg'),1,'°')} | {_fmt(r.get('sum_margin_at_xo_db'),2,' dB')} | {_fmt(r.get('sum_margin_worst_75_125_db'),2,' dB')} | {_md_escape('; '.join(warn_parts))} |"
        )

    binding_warnings = [r for r in rows if str(r.get("binding_warning", "")).strip()]
    if binding_warnings:
        lines.extend([
            "",
            "## Kontrola przypisania L+R do presetów",
            "",
            "W pliku są pomiary L+P/L+R o nazwach generycznych REW/Windows. v20.07 nie traktuje ich jako nazw presetów; wiąże je z następującymi po nich pomiarami L/R. To chroni przed odwróceniem 90 ↔ 102.",
            "",
            "| Preset rozpoznany | Źródło etykiety | Ostrzeżenie / sposób przypisania |",
            "|---|---|---|",
        ])
        for wr in binding_warnings:
            lines.append(f"| {_md_escape(wr.get('label',''))} | {_md_escape(wr.get('label_source',''))} | {_md_escape(wr.get('binding_warning',''))} |")

    if ref is not None:
        ref_source_note = ""
        if texture_reference_yaml:
            ref_source_note = f" Wybrany plik YAML referencji tekstury: `{Path(texture_reference_yaml).name}` ({texture_profile_load_status})."
        lines.extend([
            "",
            f"## Referencja tekstury średnicy: **{_md_escape(ref['label'])}**",
            "",
            "To jest punkt odniesienia dla naturalności ludzkiego głosu i instrumentów akustycznych. Jego cięższy bas nie jest celem: kandydat może wygrać, jeśli zachowa teksturę 125–220 Hz, a jednocześnie ma lżejszy, szybszy dół i nie traci impulsu 60–90 Hz." + ref_source_note,
        ])
        if overall_ref is not None and overall_ref is not ref:
            lines.extend([
                "",
                f"## Punkt porównawczy 102 / kandydat warunkowy: **{_md_escape(overall_ref['label'])}**",
                "",
                "102 BW48 jest traktowane wyłącznie jako punkt porównawczy, nie automatyczna referencja sceny. Poprzedni problem 102/slot4 pokazuje, że 102 może być bardzo wrażliwe na pomiar i filtr Dirac.",
            ])
    if rows:
        lo_vals = [float(r.get("measurement_low_hz", float("nan"))) for r in rows if np.isfinite(float(r.get("measurement_low_hz", float("nan"))))]
        hi_vals = [float(r.get("measurement_high_hz", float("nan"))) for r in rows if np.isfinite(float(r.get("measurement_high_hz", float("nan"))))]
        if lo_vals and hi_vals:
            measured_txt = f"{min(lo_vals):.0f}–{max(hi_vals):.0f} Hz"
            common_limit_txt = f"najniższy górny limit presetów: {min(hi_vals):.0f} Hz"
        else:
            measured_txt = "brak danych"
            common_limit_txt = "brak danych o wspólnym limicie"
        worst_conf, reason = overall_confidence, overall_confidence_reason
        lines.extend([
            "",
            f"## Pewność danych: **{worst_conf}**",
            "",
            f"Zakres pomiarów: **{measured_txt}**; {common_limit_txt}. {reason}",
            "",
            "Przy niskiej pewności aplikacja może ocenić bas i korpus dolnej średnicy, ale nie potwierdzi blasku średnicy ani ryzyka szklistości. Wtedy odsłuch ma pierwszeństwo.",
        ])
    if ref is not None and candidate_82 is not None and candidate_82 is not ref:
        c = candidate_82
        lines.extend([
            "",
            f"## Dokładne porównanie: **{_md_escape(ref['label'])}** vs **{_md_escape(c['label'])}**",
            "",
            "| Wskaźnik | 90 / referencja | 82 po Dirac | Różnica 82 − 90 | Interpretacja |",
            "|---|---:|---:|---:|---|",
        ])
        comparisons = [
            ("Nadmiar opóźnienia grupowego 75–125 Hz", "gd_excess_75_125_ms", "ms", "ujemna różnica = 82 szybszy czasowo"),
            ("Szczyt opóźnienia grupowego 75–125 Hz", "gd_peak_75_125_ms", "ms", "ujemna różnica = mniej ogona"),
            ("Rozpiętość SPL 75–125 Hz", "spl_span_75_125_db", "dB", "ujemna różnica = równiejszy bas"),
            ("Dziura 88–104 Hz", "dip_88_104_vs_shoulders_db", "dB", "wyższa wartość = mniej zapadki"),
            ("Odchylenie fazy L+R RMS", "phase_resid_rms_75_125_deg", "°", "ujemna różnica = spokojniejsza faza L+R"),
            ("Balans L/R 75–125", "lr_balance_mean_75_125_db", "dB", "ujemna różnica = lepsza symetria sceny"),
            ("Różnica fazy L/R przy XO", "lr_phase_at_xo_deg", "°", "ujemna różnica = lepsze zgranie L/R przy XO"),
            ("Sumowanie 75–125", "sum_margin_mean_75_125_db", "dB", "dodatnia różnica = lepsze sumowanie"),
            ("Korpus 125–220 Hz względem 75–125 Hz", "body_125_220_vs_75_125_db", "dB", "dodatnia różnica = więcej ciała niższej średnicy"),
            ("Pełnia 120–300 Hz względem 300–1000 Hz", "fullness_120_300_vs_300_1000_db", "dB", "dodatnia różnica = większy fundament wokalu/instrumentów"),
            ("Obecność 1–4 kHz", "presence_1_4k_vs_300_1000_db", "dB", "dodatnia różnica = więcej blasku/obecności"),
            ("Szklistość 4–8 kHz", "glass_4_8k_vs_1_4k_db", "dB", "dodatnia różnica = większe ryzyko szklistości"),
        ]
        for label, key, unit, interp in comparisons:
            diff = _row_diff(c, ref, key)
            lines.append(f"| {label} | {_fmt(ref.get(key),2,' '+unit)} | {_fmt(c.get(key),2,' '+unit)} | {_fmt(diff,2,' '+unit)} | {interp} |")
        lines.extend([
            "",
            "### Wniosek dla tego porównania",
            "",
            "Jeżeli 82 po Dirac ma lepszy czas i sumowanie, ale słabszą pełnię 120–300 Hz, mniejszą obecność 1–4 kHz albo gorszy balans L/R, może brzmieć dokładnie tak, jak opisałeś: chudziej, dalej, gładziej i z mniejszą sceną. Wtedy 90 Hz zostaje lepszym presetem muzycznym mimo słabszego wykresu basowego.",
        ])

    lines.extend([
        "",
        "## Jak czytać wskaźniki — POMIAR POST-DIRAC",
        "",
        "- **Nadmiar opóźnienia grupowego 75–125 Hz**: nadwyżka opóźnienia grupowego w obszarze przejścia. Mniej zwykle znaczy szybciej, ale nie zawsze muzykalniej.",
        "- **Tekstura 160–220 Hz względem 125–160 Hz** i **Pełnia 120–300 Hz**: wsparcie niższej średnicy; to główne pasma porównywane z 90 BW48 dla głosu i instrumentów akustycznych.",
        "- **Obecność 1–4 kHz**: obszar blasku, artykulacji i obecności. Pokazuje się tylko wtedy, gdy .mdat zawiera wystarczający zakres częstotliwości.",
        "- **Szklistość 4–8 kHz**: ostrzeżenie przed szklistością. Jeśli raport pokazuje „—”, pomiar nie zawiera tego pasma i aplikacja nie zgaduje.",
        "- **Wynik zastąpienia referencji 90 BW48 — POMIAR POST-DIRAC** jest liczony tylko wtedy, gdy realny pomiar 90 BW48 znajduje się w tym samym pliku. Ocenia ochronę barwy/tekstury średnicy; bas 25–60 Hz ma osobny cel i może być lżejszy.",
        "- **Indeks organicznej średnicy — POMIAR POST-DIRAC** jest pomocniczy. Ostatecznie ma prowadzić do odsłuchu, nie zastępować go.",
    ])

    md_path.write_text("\n".join(lines), encoding="utf-8")
    (out / "99_RAPORT_TECHNICZNY_POST_DIRAC.md").write_text("\n".join(lines), encoding="utf-8")

    # v20.07: krótkie raporty decyzyjne — najważniejsze pliki do otwarcia po analizie.
    final_path = out / "FINAL_DECISION_REPORT.md"
    notes_path = out / "ODSLUCH_NOTATKI_TEMPLATE.csv"

    if rows:
        ranked_decision = sorted(rows, key=lambda x: float(x.get("reference_replacement_score_0_100", x.get("organic_midrange_index_0_100", 0.0))), reverse=True)
        candidate_ranked = [r for r in ranked_decision if not (ref is not None and r is ref)]
        best_decision = candidate_ranked[0] if candidate_ranked else (ref if ref is not None else ranked_decision[0])
        # najlepszy technicznie basowo/czasowo: mniej GD, mniej rozrzutu SPL, lepsze sumowanie
        def _tech_score(r):
            gd = float(r.get("gd_excess_75_125_ms", 999.0))
            spl_span = float(r.get("spl_span_75_125_db", 999.0))
            summ = float(r.get("sum_margin_mean_75_125_db", -99.0))
            lrbal = float(r.get("lr_balance_mean_75_125_db", 9.0)) if np.isfinite(float(r.get("lr_balance_mean_75_125_db", float("nan")))) else 5.0
            return -gd*1.2 - spl_span*3.0 + summ*5.0 - lrbal*2.0
        best_tech = max(rows, key=_tech_score)
        best_music_safe = max(candidate_ranked, key=lambda x: float(x.get("midrange_safety_score_0_100", 0.0))) if candidate_ranked else None
    else:
        ranked_decision = []
        candidate_ranked = []
        best_decision = None
        best_tech = None
        best_music_safe = None

    final_lines: List[str] = _post_dirac_short_lines(rows, ranked_decision, ref, mdat=mdat)
    table_lines = _post_dirac_short_table_lines(ranked_decision, ref)
    three_lines = _post_dirac_three_point_lines(ranked_decision)
    lr_lines = _post_dirac_lr_diag_lines(ranked_decision)

    (out / "01_CO_TERAZ_ZROBIC_POST_DIRAC.md").write_text("\n".join(final_lines), encoding="utf-8")
    (out / "02_TABELA_DECYZJI_SLOTOW.md").write_text("\n".join(table_lines), encoding="utf-8")
    (out / "03_DIAGNOSTYKA_3PUNKTOWA.md").write_text("\n".join(three_lines), encoding="utf-8")
    (out / "04_DIAGNOSTYKA_L_R.md").write_text("\n".join(lr_lines), encoding="utf-8")

    final_path.write_text("\n".join(final_lines), encoding="utf-8")

    with notes_path.open("w", newline="", encoding="utf-8-sig") as nf:
        w = csv.writer(nf, delimiter=";")
        w.writerow(["preset", "gestosc_srednicy_1_5", "blask_srednicy_1_5", "wielkosc_sceny_1_5", "naturalnosc_wokalu_1_5", "zmeczenie_ucha_1_5", "kontrola_basu_1_5", "uwagi"])
        for r in ranked_decision[:8]:
            w.writerow([r.get("label", ""), "", "", "", "", "", "", ""])

    return md_path, csv_path


def analyze_post_dirac_ab(source: str | Path, out: str | Path, *, reference: Optional[str] = None, texture_reference_yaml: Optional[str | Path] = None) -> List[Dict[str, object]]:
    items = _import_all_rew_measurements(source)
    groups = _group_measurements(items)
    if not groups:
        raise ValueError("Nie udało się zbudować żadnej grupy pełnych presetów POST-DIRAC z pomiarów.")
    ambiguous = [g for g in groups if "DUPLIKAT NAZWY L+R" in str(g.binding_warning or "") and g.label_source != "inferred_from_adjacent_LR"]
    if ambiguous:
        details = "; ".join(f"{g.label}: {g.source_names}" for g in ambiguous[:6])
        raise ValueError(
            "Nierozwiązany duplikat nazwy pełnej sumy L+R/L+P. v20.23 blokuje ranking zamiast zgadywać po kolejności. "
            "Nadaj każdemu slotowi unikalną nazwę zawierającą XO/filtr/slot albo dołącz jednoznaczne sąsiednie L/R. "
            f"Wykryto: {details}"
        )
    rows_raw = [_metrics_for_group(g) for g in groups]
    rows = _aggregate_three_point_rows(rows_raw)
    write_reports(rows, out, mdat=str(source), reference=reference, texture_reference_yaml=texture_reference_yaml)
    return rows


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="POST-DIRAC A/B Analyzer v20.31 — wspólne źródło MDAT/API bez dziedziczenia osi")
    p.add_argument("--source", "--mdat", dest="source", required=True, help="Źródło REW: .mdat albo .rewapi.json z pomiarami slotów")
    p.add_argument("--out", default="post_dirac_ab", help="Folder wynikowy")
    p.add_argument("--reference", default=None, help="Opcjonalny fragment nazwy referencji, np. 90_BW48_slot2")
    p.add_argument("--texture-reference-yaml", default=None, help="Opcjonalny plik YAML referencji tekstury 90 BW48 wybrany w GUI")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows = analyze_post_dirac_ab(args.source, args.out, reference=args.reference, texture_reference_yaml=args.texture_reference_yaml)
    except Exception as exc:
        print("BŁĄD POST-DIRAC A/B:", exc)
        return 2
    print("POST-DIRAC A/B gotowy.")
    print("Liczba porównanych presetów:", len(rows))
    print("Werdykt:", str(Path(args.out) / "FINAL_DECISION_REPORT.md"))
    print("Raport:", str(Path(args.out) / "POST_DIRAC_AB_REPORT.md"))
    print("CSV:", str(Path(args.out) / "POST_DIRAC_AB_METRICS.csv"))
    if rows:
        has_mid_ref = any(str(r.get("mid_reference_status", "")).startswith("WYKRYTA") for r in rows)
        candidates = [r for r in rows if str(r.get("decision_class", "")).upper() != "REFERENCJA — ZOSTAW"]
        if not has_mid_ref:
            print("BRAK REFERENCJI 90 BW48 W PLIKU — wygenerowano tylko diagnostykę bez oceny zastąpienia.")
        elif candidates:
            best = max(candidates, key=lambda x: float(x.get("reference_replacement_score_0_100", x.get("organic_midrange_index_0_100", 0.0))))
            print("Najwyższy wynik zastąpienia referencji 90 BW48 — POMIAR POST-DIRAC:", best.get("label"), f"{float(best.get('reference_replacement_score_0_100', best.get('organic_midrange_index_0_100',0.0))):.1f}/100")
            print("Klasa decyzji:", best.get("decision_class", ""))
        else:
            print("Brak osobnego kandydata — 90 BW48 zostaje referencją tekstury/średnicy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
