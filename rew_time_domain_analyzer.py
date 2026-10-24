#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analiza czasowa REWDO v20.31 — 90 BW48 Fast.

Moduł ocenia rzeczywiście zmierzone warianty na podstawie migawki REW API,
która zawiera natywną odpowiedź częstotliwościową, group delay oraz impuls.
Nie projektuje filtrów na samym impulsie i nie udaje pomiaru waterfall REW.
Z impulsu wylicza porównawcze, pasmowe krzywe zaniku energii i późną energię.
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from rew_api_client import (
    decode_frequency_response,
    decode_group_delay,
    decode_impulse_response,
    is_rew_api_snapshot,
    read_snapshot,
)
from rew_mdat import infer_mdat_measurement_role


BANDS: Tuple[Tuple[str, float, float], ...] = (
    ("25–45 Hz", 25.0, 45.0),
    ("45–65 Hz", 45.0, 65.0),
    ("65–85 Hz", 65.0, 85.0),
    ("80–110 Hz", 80.0, 110.0),
    ("110–160 Hz", 110.0, 160.0),
)


def _fmt(value: Optional[float], digits: int = 1, suffix: str = "") -> str:
    if value is None or not math.isfinite(float(value)):
        return "—"
    return f"{float(value):.{digits}f}{suffix}"


def _normalise_text(value: str) -> str:
    text = str(value or "").lower()
    replacements = str.maketrans("ąćęłńóśźż", "acelnoszz")
    text = text.translate(replacements)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _unwrap_interp(target: np.ndarray, freq: np.ndarray, phase_deg: np.ndarray) -> np.ndarray:
    unwrapped = np.unwrap(np.deg2rad(np.asarray(phase_deg, dtype=float)))
    return np.rad2deg(np.interp(target, freq, unwrapped))


def _band_mean(freq: np.ndarray, values: np.ndarray, lo: float, hi: float) -> Optional[float]:
    mask = (freq >= lo) & (freq <= hi) & np.isfinite(values)
    if np.count_nonzero(mask) < 3:
        return None
    return float(np.nanmean(values[mask]))


def _band_percentile(freq: np.ndarray, values: np.ndarray, lo: float, hi: float, q: float) -> Optional[float]:
    mask = (freq >= lo) & (freq <= hi) & np.isfinite(values)
    if np.count_nonzero(mask) < 3:
        return None
    return float(np.nanpercentile(values[mask], q))


def _choose_anchor(freq: np.ndarray) -> Optional[Tuple[float, float]]:
    maximum = float(np.nanmax(freq)) if freq.size else 0.0
    minimum = float(np.nanmin(freq)) if freq.size else 1e9
    choices = (
        (500.0, 1000.0),
        (250.0, 500.0),
        (220.0, 250.0),
        (200.0, 220.0),
        (170.0, 200.0),
    )
    for lo, hi in choices:
        if minimum <= lo and maximum >= hi:
            return lo, hi
    return None


def _smooth_cosine_band(freq: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Łagodna maska pasmowa do porównawczej analizy zaniku.

    Nie jest to filtr implementowany w miniDSP. Maska służy wyłącznie do
    wydzielenia energii pasma z odpowiedzi impulsowej.
    """
    freq = np.asarray(freq, dtype=float)
    mask = np.zeros_like(freq)
    lo0 = max(0.1, lo * 0.78)
    lo1 = lo
    hi1 = hi
    hi2 = hi * 1.22
    rise = (freq >= lo0) & (freq < lo1)
    core = (freq >= lo1) & (freq <= hi1)
    fall = (freq > hi1) & (freq <= hi2)
    if lo1 > lo0:
        x = (freq[rise] - lo0) / (lo1 - lo0)
        mask[rise] = 0.5 - 0.5 * np.cos(np.pi * x)
    mask[core] = 1.0
    if hi2 > hi1:
        x = (freq[fall] - hi1) / (hi2 - hi1)
        mask[fall] = 0.5 + 0.5 * np.cos(np.pi * x)
    return mask


def _band_limited_signal(samples: np.ndarray, sample_rate: float, lo: float, hi: float) -> np.ndarray:
    signal = np.asarray(samples, dtype=float)
    n = int(signal.size)
    if n < 32:
        return signal.copy()
    # Zero-padding ogranicza zawijanie odpowiedzi przy filtracji FFT.
    nfft = 1 << int(math.ceil(math.log2(max(64, n * 2))))
    spectrum = np.fft.rfft(signal, n=nfft)
    freq = np.fft.rfftfreq(nfft, d=1.0 / sample_rate)
    spectrum *= _smooth_cosine_band(freq, lo, hi)
    filtered = np.fft.irfft(spectrum, n=nfft)[:n]
    return np.asarray(filtered, dtype=float)


def _moving_average(values: np.ndarray, points: int) -> np.ndarray:
    points = max(1, int(points))
    if points <= 1:
        return np.asarray(values, dtype=float)
    kernel = np.ones(points, dtype=float) / float(points)
    return np.convolve(np.asarray(values, dtype=float), kernel, mode="same")


def _first_below(time_ms: np.ndarray, curve_db: np.ndarray, threshold_db: float) -> Optional[float]:
    indices = np.where(np.isfinite(curve_db) & (curve_db <= threshold_db))[0]
    if indices.size == 0:
        return None
    return float(time_ms[int(indices[0])])


@dataclass
class BandDecay:
    label: str
    low_hz: float
    high_hz: float
    peak_offset_ms: float
    t10_ms: Optional[float]
    t20_ms: Optional[float]
    t30_ms: Optional[float]
    late_100_db: Optional[float]
    late_200_db: Optional[float]
    early_50_percent: Optional[float]
    usable_duration_ms: float
    noise_limited: bool = False


def analyse_band_decay(
    samples: np.ndarray,
    time_s: np.ndarray,
    sample_rate: float,
    low_hz: float,
    high_hz: float,
    *,
    max_time_ms: float = 600.0,
) -> BandDecay:
    filtered = _band_limited_signal(samples, sample_rate, low_hz, high_hz)
    centre = math.sqrt(low_hz * high_hz)
    smooth_points = max(3, int(round(sample_rate / max(centre, 1.0) * 0.5)))
    energy = _moving_average(filtered * filtered, smooth_points)

    original_peak = int(np.argmax(np.abs(samples)))
    search_after = int(round(0.120 * sample_rate))
    search_lo = max(0, original_peak - int(round(0.020 * sample_rate)))
    search_hi = min(energy.size, original_peak + search_after)
    if search_hi <= search_lo:
        peak = original_peak
    else:
        peak = search_lo + int(np.argmax(energy[search_lo:search_hi]))

    max_points = int(round(max_time_ms / 1000.0 * sample_rate))
    stop = min(energy.size, peak + max_points + 1)
    segment = np.asarray(energy[peak:stop], dtype=float)
    if segment.size < 16:
        return BandDecay(
            label=f"{low_hz:g}–{high_hz:g} Hz", low_hz=low_hz, high_hz=high_hz,
            peak_offset_ms=(peak - original_peak) / sample_rate * 1000.0,
            t10_ms=None, t20_ms=None, t30_ms=None, late_100_db=None,
            late_200_db=None, early_50_percent=None,
            usable_duration_ms=segment.size / sample_rate * 1000.0,
            noise_limited=True,
        )

    # Szacowanie tła z ostatnich 15% i odjęcie jego energii. Nie zastępuje
    # pełnego algorytmu Lundeby/REW, ale chroni wskaźniki porównawcze przed
    # sztucznym ogonem wynikającym z szumu.
    tail_count = max(8, int(round(segment.size * 0.15)))
    noise_floor = float(np.nanmedian(segment[-tail_count:]))
    corrected = np.maximum(segment - noise_floor, 0.0)
    total = float(np.sum(corrected))
    if not math.isfinite(total) or total <= 1e-30:
        corrected = np.maximum(segment, 0.0)
        total = float(np.sum(corrected))
    reverse = np.cumsum(corrected[::-1])[::-1]
    if reverse.size == 0 or reverse[0] <= 1e-30:
        curve_db = np.full(segment.size, np.nan)
    else:
        curve_db = 10.0 * np.log10(np.maximum(reverse / reverse[0], 1e-12))
    rel_time_ms = np.arange(segment.size, dtype=float) / sample_rate * 1000.0

    def late_db(after_ms: float) -> Optional[float]:
        index = int(round(after_ms / 1000.0 * sample_rate))
        if index >= corrected.size or total <= 1e-30:
            return None
        ratio = float(np.sum(corrected[index:]) / total)
        return 10.0 * math.log10(max(ratio, 1e-12))

    i50 = min(corrected.size, int(round(0.050 * sample_rate)))
    early50 = float(np.sum(corrected[:i50]) / total * 100.0) if total > 1e-30 else None
    noise_limited = bool(np.nanmin(curve_db) > -25.0) if np.any(np.isfinite(curve_db)) else True

    return BandDecay(
        label=f"{low_hz:g}–{high_hz:g} Hz",
        low_hz=low_hz,
        high_hz=high_hz,
        peak_offset_ms=(peak - original_peak) / sample_rate * 1000.0,
        t10_ms=_first_below(rel_time_ms, curve_db, -10.0),
        t20_ms=_first_below(rel_time_ms, curve_db, -20.0),
        t30_ms=_first_below(rel_time_ms, curve_db, -30.0),
        late_100_db=late_db(100.0),
        late_200_db=late_db(200.0),
        early_50_percent=early50,
        usable_duration_ms=float(rel_time_ms[-1]) if rel_time_ms.size else 0.0,
        noise_limited=noise_limited,
    )


@dataclass
class TimeMeasurement:
    name: str
    role: str
    freq: np.ndarray
    spl_db: np.ndarray
    phase_deg: np.ndarray
    gd_freq: Optional[np.ndarray] = None
    gd_ms: Optional[np.ndarray] = None
    gd_source: str = "brak"
    time_s: Optional[np.ndarray] = None
    impulse: Optional[np.ndarray] = None
    sample_rate_hz: Optional[float] = None
    timing_reference: str = ""
    bands: List[BandDecay] = field(default_factory=list)
    gd_anchor_ms: Optional[float] = None
    gd_p90_45_110_ms: Optional[float] = None
    gd_median_80_110_ms: Optional[float] = None
    composite_late100_db: Optional[float] = None
    composite_t20_ms: Optional[float] = None
    composite_early50_percent: Optional[float] = None


def _derive_group_delay(freq: np.ndarray, phase_deg: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    freq = np.asarray(freq, dtype=float)
    phase = np.unwrap(np.deg2rad(np.asarray(phase_deg, dtype=float)))
    if freq.size < 5:
        return freq.copy(), np.full(freq.size, np.nan)
    gd_s = -np.gradient(phase, freq) / (2.0 * np.pi)
    # Lekka mediana ruchoma bez SciPy, aby ograniczyć różniczkowanie szumu.
    window = 7
    padded = np.pad(gd_s, (window // 2, window // 2), mode="edge")
    smoothed = np.asarray([np.nanmedian(padded[i:i + window]) for i in range(gd_s.size)], dtype=float)
    return freq.copy(), smoothed * 1000.0


def _gd_metrics(item: TimeMeasurement) -> None:
    if item.gd_freq is None or item.gd_ms is None or item.gd_freq.size < 5:
        return
    freq = item.gd_freq
    gd = item.gd_ms
    anchor_band = None
    for lo, hi in ((160.0, 220.0), (140.0, 180.0), (110.0, 160.0)):
        if np.nanmin(freq) <= lo and np.nanmax(freq) >= hi:
            anchor_band = (lo, hi)
            break
    anchor = _band_mean(freq, gd, *(anchor_band or (110.0, 160.0)))
    if anchor is None:
        anchor = float(np.nanmedian(gd[np.isfinite(gd)]))
    relative = gd - anchor
    item.gd_anchor_ms = anchor
    item.gd_p90_45_110_ms = _band_percentile(freq, relative, 45.0, 110.0, 90.0)
    item.gd_median_80_110_ms = _band_percentile(freq, relative, 80.0, 110.0, 50.0)


def _aggregate_time_metrics(item: TimeMeasurement) -> None:
    fast_bands = [b for b in item.bands if b.high_hz <= 110.1]
    late = [b.late_100_db for b in fast_bands if b.late_100_db is not None]
    t20 = [b.t20_ms for b in fast_bands if b.t20_ms is not None]
    early = [b.early_50_percent for b in fast_bands if b.early_50_percent is not None]
    item.composite_late100_db = float(np.nanmean(late)) if late else None
    item.composite_t20_ms = float(np.nanmedian(t20)) if t20 else None
    item.composite_early50_percent = float(np.nanmean(early)) if early else None


def load_time_measurements(path: Path, max_time_ms: float) -> Tuple[Dict[str, Any], List[TimeMeasurement], List[str]]:
    if not is_rew_api_snapshot(path):
        raise ValueError(
            "Analiza czasowa wymaga migawki *.rewapi.json utworzonej z zaznaczonymi opcjami "
            "Pobierz group delay i Pobierz impuls. Import MDAT pozostaje dostępny dla pozostałych trybów."
        )
    payload = read_snapshot(path)
    warnings: List[str] = []

    state = payload.get("measurementState", {})
    if not isinstance(state, Mapping):
        state = {}
    if state.get("timingReferenceConsistent") is not True:
        warnings.append(
            "Migawka nie zawiera potwierdzenia wspólnej metody timing reference. "
            "Porównanie czasu nadejścia, impulsu i group delay może być nieważne."
        )
    if state.get("microphoneStationary") is not True:
        warnings.append(
            "Migawka nie zawiera potwierdzenia nieruchomego mikrofonu. "
            "Różnice decay mogą wynikać ze zmiany pozycji pomiarowej."
        )
    acquisition = payload.get("acquisition", {})
    if not isinstance(acquisition, Mapping):
        acquisition = {}
    if acquisition.get("impulseRequested") is not True:
        warnings.append("Migawka nie potwierdza pobrania impulsu z API REW.")
    if acquisition.get("groupDelayRequested") is not True:
        warnings.append(
            "Migawka nie potwierdza pobrania group delay z API REW; "
            "brakujące dane zostaną oszacowane z fazy."
        )

    items: List[TimeMeasurement] = []
    for index, raw in enumerate(payload.get("measurements", []), start=1):
        if not isinstance(raw, Mapping):
            continue
        summary = raw.get("summary", {})
        if not isinstance(summary, Mapping):
            summary = {}
        name = str(summary.get("title") or summary.get("name") or f"Pomiar {index}")
        role = str(raw.get("role") or infer_mdat_measurement_role(name) or "Nieprzypisany")
        response = raw.get("frequencyResponse")
        if not isinstance(response, Mapping):
            warnings.append(f"{name}: brak frequencyResponse — pominięto.")
            continue
        try:
            fr = decode_frequency_response(response)
        except Exception as exc:
            warnings.append(f"{name}: nie można odczytać SPL/fazy: {exc}")
            continue
        item = TimeMeasurement(
            name=name,
            role=role,
            freq=np.asarray(fr["frequency_hz"], dtype=float),
            spl_db=np.asarray(fr["spl_db"], dtype=float),
            phase_deg=np.asarray(fr["phase_deg"], dtype=float),
        )

        gd_payload = raw.get("groupDelay")
        if isinstance(gd_payload, Mapping):
            try:
                gd = decode_group_delay(gd_payload)
                item.gd_freq = np.asarray(gd["frequency_hz"], dtype=float)
                item.gd_ms = np.asarray(gd["group_delay_ms"], dtype=float)
                item.gd_source = "API REW"
            except Exception as exc:
                warnings.append(f"{name}: błąd group delay API: {exc}; użyto pochodnej fazy.")
        if item.gd_freq is None:
            item.gd_freq, item.gd_ms = _derive_group_delay(item.freq, item.phase_deg)
            item.gd_source = "pochodna fazy"
        _gd_metrics(item)

        impulse_payload = raw.get("impulseResponse")
        if isinstance(impulse_payload, Mapping):
            try:
                ir = decode_impulse_response(impulse_payload)
                item.time_s = np.asarray(ir["time_s"], dtype=float)
                item.impulse = np.asarray(ir["samples"], dtype=float)
                item.sample_rate_hz = float(ir["sample_rate_hz"])
                item.timing_reference = str(ir["timing_reference"])
                duration_ms = (float(item.time_s[-1] - item.time_s[0]) * 1000.0) if item.time_s.size > 1 else 0.0
                if duration_ms < min(300.0, max_time_ms * 0.75):
                    warnings.append(
                        f"{name}: impuls ma tylko {duration_ms:.0f} ms; "
                        "ocena późnej energii po 200 ms i T30 może być niepełna."
                    )
                for label, lo, hi in BANDS:
                    result = analyse_band_decay(
                        item.impulse,
                        item.time_s,
                        item.sample_rate_hz,
                        lo,
                        hi,
                        max_time_ms=max_time_ms,
                    )
                    result.label = label
                    item.bands.append(result)
                _aggregate_time_metrics(item)
            except Exception as exc:
                warnings.append(f"{name}: nie można przeanalizować impulsu: {exc}")
        else:
            detail = raw.get("impulseResponseError")
            warnings.append(f"{name}: brak impulsu" + (f" ({detail})" if detail else "") + ".")
        items.append(item)
    if not items:
        raise ValueError("Migawka nie zawiera żadnego możliwego do analizy pomiaru.")

    references = sorted({x.timing_reference.strip() for x in items if x.timing_reference.strip()})
    if len(references) > 1:
        warnings.append(
            "Impulsy zawierają różne oznaczenia timing reference: " + ", ".join(references) + ". "
            "Porównuj tylko pomiary wykonane tą samą metodą referencji czasowej."
        )
    missing_impulse = [x.name for x in items if x.impulse is None]
    if missing_impulse:
        warnings.append(
            f"Brak impulsu dla {len(missing_impulse)} z {len(items)} pomiarów; "
            "nie mogą one otrzymać pełnej oceny szybkości."
        )
    return payload, items, warnings



ROLE_ORDER: Tuple[str, ...] = ("L", "R", "L+R")
ROLE_LABELS: Dict[str, str] = {"L": "L strona", "R": "R strona", "L+R": "L+R"}
MIN_REFERENCE_REPEATS = 3
TIMING_ANCHOR_TOLERANCE_MS = 2.0
REPEATABILITY_FLOORS: Dict[str, Dict[str, float]] = {
    "L": {"late": 0.8, "t20": 10.0, "gd": 6.0},
    "R": {"late": 0.8, "t20": 25.0, "gd": 6.0},
    "L+R": {"late": 0.8, "t20": 12.0, "gd": 6.0},
}


@dataclass
class RepeatabilityProfile:
    role: str
    measurements: List[TimeMeasurement]
    centre: TimeMeasurement
    count: int
    anchor_median_ms: Optional[float]
    anchor_range_ms: Optional[float]
    anchor_outliers: List[str]
    late_range_db: Optional[float]
    t20_range_ms: Optional[float]
    gd_range_ms: Optional[float]
    late_threshold_db: float
    t20_threshold_ms: float
    gd_threshold_ms: float
    reliable: bool


@dataclass
class RoleComparison:
    role: str
    candidate: TimeMeasurement
    reference: TimeMeasurement
    repeatability: RepeatabilityProfile
    spl: Dict[str, Optional[float] | str]
    late_improvement_db: Optional[float]
    t20_improvement_ms: Optional[float]
    gd_improvement_ms: Optional[float]
    anchor_delta_ms: Optional[float]
    timing_consistent: bool
    significant_good: Dict[str, bool]
    significant_bad: Dict[str, bool]


@dataclass
class VariantResult:
    variant_id: str
    display_name: str
    roles: Dict[str, RoleComparison]
    missing_roles: List[str]
    verdict: str
    fast_index: float
    reasons: List[str]


def _canonical_role(item: TimeMeasurement) -> str:
    raw_role = str(item.role or "").lower().strip()
    raw_name = str(item.name or "").lower()
    combined = raw_role + " " + raw_name
    if "l+r" in combined or re.search(r"(?:^|[_\s-])lr(?:[_\s-]|$)", combined):
        return "L+R"
    role_n = _normalise_text(raw_role)
    name_n = _normalise_text(raw_name)
    if role_n.startswith("l r") or role_n.startswith("lr"):
        return "L+R"
    if role_n.startswith("l ") or role_n == "l" or " l strona" in " " + name_n or " l str" in " " + name_n:
        return "L"
    if role_n.startswith("r ") or role_n == "r" or " r strona" in " " + name_n or " r str" in " " + name_n:
        return "R"
    if "l sum" in role_n or "l sum" in name_n:
        return "L"
    if "r sum" in role_n or "r sum" in name_n:
        return "R"
    return str(item.role or "Nieprzypisany")


def _strip_measurement_role(name: str) -> str:
    value = re.sub(r"^\s*\d+(?:[_\s.-]+)", "", str(name or "").strip())
    patterns = (
        r"(?:[_\s-]+)(?:L\+R|LR)(?:[_\s-]*(?:SUM|STRONA|STR))?$",
        r"(?:[_\s-]+)L(?:[_\s-]*(?:SUM|STRONA|STR))$",
        r"(?:[_\s-]+)R(?:[_\s-]*(?:SUM|STRONA|STR))$",
    )
    for pattern in patterns:
        updated = re.sub(pattern, "", value, flags=re.IGNORECASE)
        if updated != value:
            value = updated
            break
    return value.strip(" _-")


def _variant_id(name: str) -> str:
    return _strip_measurement_role(name) or str(name or "WARIANT")


def _configuration_fingerprint(name: str) -> str:
    value = _strip_measurement_role(name).lower()
    value = re.sub(r"g\s*-\s*(\d+(?:[.,]\d+)?)", lambda m: " gminus" + m.group(1).replace(",", ".").replace(".", "p") + " ", value)
    value = re.sub(r"g\s*\+\s*(\d+(?:[.,]\d+)?)", lambda m: " gplus" + m.group(1).replace(",", ".").replace(".", "p") + " ", value)
    tokens = _normalise_text(value).split()
    ignored = {"ref", "reference", "referencja", "baseline", "slot", "slot2", "powtorzenie"}
    kept: List[str] = []
    skip_next_repeat_number = False
    for token in tokens:
        if token in ignored or re.fullmatch(r"slot\d+", token):
            continue
        if token == "repeat":
            skip_next_repeat_number = True
            continue
        if re.fullmatch(r"repeat\d+", token):
            continue
        if skip_next_repeat_number and token.isdigit():
            skip_next_repeat_number = False
            continue
        skip_next_repeat_number = False
        kept.append(token)
    return " ".join(kept)


def _is_reference_like(name: str) -> bool:
    text = _normalise_text(name)
    return any(token in text.split() for token in ("ref", "reference", "referencja", "baseline"))


def _select_reference_family(
    items: Sequence[TimeMeasurement], query: str
) -> Tuple[Dict[str, List[TimeMeasurement]], str, List[str]]:
    query_n = _normalise_text(query)
    warnings: List[str] = []
    if not query_n:
        raise ValueError(
            "Tryb 10 wymaga jawnego fragmentu nazwy referencji. "
            "Przykład: REF_SLOT2_90BW48_G0."
        )

    literal = [x for x in items if query_n in _normalise_text(x.name)]
    seeds = list(literal)
    if not seeds:
        query_fp_tokens = set(_configuration_fingerprint(query).split())
        families: Dict[str, List[TimeMeasurement]] = {}
        for item in items:
            if not _is_reference_like(item.name):
                continue
            fp = _configuration_fingerprint(item.name)
            fp_tokens = set(fp.split())
            if query_fp_tokens and query_fp_tokens.issubset(fp_tokens):
                families.setdefault(fp, []).append(item)
        if len(families) == 1:
            family_fp, seeds = next(iter(families.items()))
            warnings.append(
                f"Wzorzec `{query}` nie wystąpił literalnie. Jawnie dopasowano rodzinę referencji `{family_fp}`. "
                "Sprawdź tabelę powtarzalności."
            )
        else:
            examples = ", ".join(x.name for x in items[:8])
            raise ValueError(
                f"Nie znaleziono jednoznacznej referencji dla `{query}`. "
                f"Wpisz fragment rzeczywistych nazw, np. `REF_SLOT2_90BW48_G0`. Przykładowe pomiary: {examples}"
            )

    seed = sorted(seeds, key=lambda x: (0 if _is_reference_like(x.name) else 1, x.name))[0]
    family_fp = _configuration_fingerprint(seed.name)
    family_items = [x for x in items if _is_reference_like(x.name) and _configuration_fingerprint(x.name) == family_fp]
    if not family_items:
        family_items = seeds

    by_role: Dict[str, List[TimeMeasurement]] = {role: [] for role in ROLE_ORDER}
    for item in family_items:
        role = _canonical_role(item)
        if role in by_role:
            by_role[role].append(item)
    missing = [role for role in ROLE_ORDER if not by_role[role]]
    if missing:
        raise ValueError(
            "Rodzina referencji nie zawiera pełnego zestawu L, R i L+R. Braki: "
            + ", ".join(ROLE_LABELS[x] for x in missing)
        )
    counts = ", ".join(f"{role}={len(by_role[role])}" for role in ROLE_ORDER)
    warnings.append(f"Wybrana rodzina referencji: `{family_fp}` ({counts}).")
    return by_role, family_fp, warnings


def _finite_values(values: Iterable[Optional[float]]) -> List[float]:
    return [float(x) for x in values if x is not None and math.isfinite(float(x))]


def _value_range(values: Iterable[Optional[float]]) -> Optional[float]:
    finite = _finite_values(values)
    if len(finite) < 2:
        return None
    return float(max(finite) - min(finite))


def _median_optional(values: Iterable[Optional[float]]) -> Optional[float]:
    finite = _finite_values(values)
    return float(np.nanmedian(finite)) if finite else None


def _aggregate_measurements(items: Sequence[TimeMeasurement], name: str, role: str) -> TimeMeasurement:
    if not items:
        raise ValueError(f"Nie można agregować pustej grupy {role}.")
    base = max(items, key=lambda x: x.freq.size)
    axis = np.asarray(base.freq, dtype=float)
    spl_stack = [np.interp(axis, x.freq, x.spl_db) for x in items]
    phase_stack = [_unwrap_interp(axis, x.freq, x.phase_deg) for x in items]
    result = TimeMeasurement(
        name=name,
        role=ROLE_LABELS.get(role, role),
        freq=axis,
        spl_db=np.nanmedian(np.vstack(spl_stack), axis=0),
        phase_deg=np.nanmedian(np.vstack(phase_stack), axis=0),
    )
    result.composite_late100_db = _median_optional(x.composite_late100_db for x in items)
    result.composite_t20_ms = _median_optional(x.composite_t20_ms for x in items)
    result.composite_early50_percent = _median_optional(x.composite_early50_percent for x in items)
    result.gd_p90_45_110_ms = _median_optional(x.gd_p90_45_110_ms for x in items)
    result.gd_median_80_110_ms = _median_optional(x.gd_median_80_110_ms for x in items)
    result.gd_anchor_ms = _median_optional(x.gd_anchor_ms for x in items)
    result.gd_source = "mediana " + "/".join(sorted({x.gd_source for x in items if x.gd_source}))
    result.timing_reference = " | ".join(sorted({x.timing_reference for x in items if x.timing_reference}))
    return result


def _repeatability_profile(role: str, items: Sequence[TimeMeasurement]) -> RepeatabilityProfile:
    centre = _aggregate_measurements(items, f"REFERENCJA_MEDIANA_{role}", role)
    anchors = [(x.name, x.gd_anchor_ms) for x in items if x.gd_anchor_ms is not None and math.isfinite(float(x.gd_anchor_ms))]
    anchor_values = [float(x[1]) for x in anchors]
    anchor_median = float(np.nanmedian(anchor_values)) if anchor_values else None
    anchor_outliers: List[str] = []
    if anchor_median is not None and len(anchor_values) >= 3:
        anchor_outliers = [name for name, value in anchors if abs(float(value) - anchor_median) > TIMING_ANCHOR_TOLERANCE_MS]
    anchor_range = _value_range(anchor_values)
    late_range = _value_range(x.composite_late100_db for x in items)
    t20_range = _value_range(x.composite_t20_ms for x in items)
    gd_range = _value_range(x.gd_p90_45_110_ms for x in items)
    floors = REPEATABILITY_FLOORS[role]
    late_threshold = max(floors["late"], 1.05 * float(late_range or 0.0))
    t20_threshold = max(floors["t20"], 1.05 * float(t20_range or 0.0))
    gd_threshold = max(floors["gd"], 1.05 * float(gd_range or 0.0))
    reliable = len(items) >= MIN_REFERENCE_REPEATS
    return RepeatabilityProfile(
        role=role,
        measurements=list(items),
        centre=centre,
        count=len(items),
        anchor_median_ms=anchor_median,
        anchor_range_ms=anchor_range,
        anchor_outliers=anchor_outliers,
        late_range_db=late_range,
        t20_range_ms=t20_range,
        gd_range_ms=gd_range,
        late_threshold_db=late_threshold,
        t20_threshold_ms=t20_threshold,
        gd_threshold_ms=gd_threshold,
        reliable=reliable,
    )


def compare_spl(candidate: TimeMeasurement, reference: TimeMeasurement) -> Dict[str, Optional[float] | str]:
    lo = max(float(candidate.freq[0]), float(reference.freq[0]))
    hi = min(float(candidate.freq[-1]), float(reference.freq[-1]))
    mask = (reference.freq >= lo) & (reference.freq <= hi)
    axis = reference.freq[mask]
    if axis.size < 16:
        return {"anchor": "brak wspólnej osi", "body_100_160_db": None, "body_125_220_db": None,
                "foundation_40_80_db": None, "kick_60_90_db": None}
    cand = np.interp(axis, candidate.freq, candidate.spl_db)
    ref = np.interp(axis, reference.freq, reference.spl_db)
    anchor_band = _choose_anchor(axis)
    offset = 0.0
    anchor_label = "brak — poziom bez wyrównania"
    if anchor_band is not None:
        mask_anchor = (axis >= anchor_band[0]) & (axis <= anchor_band[1])
        if np.count_nonzero(mask_anchor) >= 3:
            offset = float(np.nanmean(ref[mask_anchor] - cand[mask_anchor]))
            anchor_label = f"{anchor_band[0]:g}–{anchor_band[1]:g} Hz"
    delta = cand + offset - ref
    return {
        "anchor": anchor_label,
        "body_100_160_db": _band_mean(axis, delta, 100.0, 160.0),
        "body_125_220_db": _band_mean(axis, delta, 125.0, 220.0),
        "foundation_40_80_db": _band_mean(axis, delta, 40.0, 80.0),
        "kick_60_90_db": _band_mean(axis, delta, 60.0, 90.0),
    }


def _difference(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
        return None
    return float(a - b)


def classify_candidate(item: TimeMeasurement, reference: TimeMeasurement, spl_cmp: Mapping[str, Any]) -> Tuple[str, float, Dict[str, Optional[float]]]:
    """Klasyfikacja pojedynczej roli pozostawiona dla zgodności testów/modułów.

    Główny raport v20.29 nie używa jej do ostatecznej decyzji. Decyzja jest
    liczona zbiorczo dla wariantu L/R/L+R i względem rozrzutu powtórzeń.
    """
    if item is reference:
        return "REFERENCJA", 50.0, {"late_improvement_db": 0.0, "t20_improvement_ms": 0.0, "gd_improvement_ms": 0.0}
    body1 = spl_cmp.get("body_100_160_db")
    body2 = spl_cmp.get("body_125_220_db")
    late_improvement = _difference(reference.composite_late100_db, item.composite_late100_db)
    t20_improvement = _difference(reference.composite_t20_ms, item.composite_t20_ms)
    gd_improvement = _difference(reference.gd_p90_45_110_ms, item.gd_p90_45_110_ms)
    body_gate_fail = (
        (isinstance(body1, (int, float)) and math.isfinite(float(body1)) and float(body1) < -1.0)
        or (isinstance(body2, (int, float)) and math.isfinite(float(body2)) and float(body2) < -0.8)
    )
    temporal_good = (
        (late_improvement is not None and late_improvement >= 1.0)
        or (t20_improvement is not None and t20_improvement >= 15.0)
    )
    temporal_bad = (
        (late_improvement is not None and late_improvement <= -1.0)
        or (t20_improvement is not None and t20_improvement <= -15.0)
    )
    gd_bad = gd_improvement is not None and gd_improvement < -3.0
    if body_gate_fail:
        verdict = "ODRZUĆ — UTRATA BODY"
    elif temporal_good and not gd_bad:
        verdict = "POTENCJALNIE LEPSZY"
    elif temporal_bad or gd_bad:
        verdict = "GORSZY CZASOWO"
    else:
        verdict = "ZMIANA BOCZNA / DO ODSŁUCHU"
    score = 50.0
    if late_improvement is not None:
        score += 4.0 * float(np.clip(late_improvement, -5.0, 5.0))
    if t20_improvement is not None:
        score += 0.15 * float(np.clip(t20_improvement, -80.0, 80.0))
    if gd_improvement is not None:
        score += 0.6 * float(np.clip(gd_improvement, -15.0, 15.0))
    return verdict, float(np.clip(score, 0.0, 100.0)), {
        "late_improvement_db": late_improvement,
        "t20_improvement_ms": t20_improvement,
        "gd_improvement_ms": gd_improvement,
    }


def _compare_role(candidate: TimeMeasurement, profile: RepeatabilityProfile) -> RoleComparison:
    reference = profile.centre
    spl = compare_spl(candidate, reference)
    late = _difference(reference.composite_late100_db, candidate.composite_late100_db)
    t20 = _difference(reference.composite_t20_ms, candidate.composite_t20_ms)
    gd = _difference(reference.gd_p90_45_110_ms, candidate.gd_p90_45_110_ms)
    anchor_delta = None
    if candidate.gd_anchor_ms is not None and profile.anchor_median_ms is not None:
        anchor_delta = abs(float(candidate.gd_anchor_ms) - float(profile.anchor_median_ms))
    timing_consistent = anchor_delta is None or anchor_delta <= TIMING_ANCHOR_TOLERANCE_MS
    significant_good = {
        "late": late is not None and late >= profile.late_threshold_db,
        "t20": t20 is not None and t20 >= profile.t20_threshold_ms,
        "gd": gd is not None and gd >= profile.gd_threshold_ms,
    }
    significant_bad = {
        "late": late is not None and late <= -profile.late_threshold_db,
        "t20": t20 is not None and t20 <= -profile.t20_threshold_ms,
        "gd": gd is not None and gd <= -profile.gd_threshold_ms,
    }
    return RoleComparison(
        role=profile.role,
        candidate=candidate,
        reference=reference,
        repeatability=profile,
        spl=spl,
        late_improvement_db=late,
        t20_improvement_ms=t20,
        gd_improvement_ms=gd,
        anchor_delta_ms=anchor_delta,
        timing_consistent=timing_consistent,
        significant_good=significant_good,
        significant_bad=significant_bad,
    )


def _body_gate(role_cmp: RoleComparison) -> bool:
    body1 = role_cmp.spl.get("body_100_160_db")
    body2 = role_cmp.spl.get("body_125_220_db")
    limit1 = -1.0 if role_cmp.role == "L+R" else -1.2
    limit2 = -0.8 if role_cmp.role == "L+R" else -1.0
    return bool(
        (isinstance(body1, (int, float)) and math.isfinite(float(body1)) and float(body1) < limit1)
        or (isinstance(body2, (int, float)) and math.isfinite(float(body2)) and float(body2) < limit2)
    )


def _variant_fast_index(roles: Mapping[str, RoleComparison]) -> float:
    score = 50.0
    weights = {"L": 0.25, "R": 0.25, "L+R": 0.50}
    for role, cmp in roles.items():
        w = weights.get(role, 0.0)
        for value, threshold, factor in (
            (cmp.late_improvement_db, cmp.repeatability.late_threshold_db, 6.0),
            (cmp.t20_improvement_ms, cmp.repeatability.t20_threshold_ms, 5.0),
            (cmp.gd_improvement_ms, cmp.repeatability.gd_threshold_ms, 3.0),
        ):
            if value is not None and threshold > 0:
                score += w * factor * float(np.clip(value / threshold, -2.0, 2.0))
        for key in ("body_100_160_db", "body_125_220_db"):
            value = cmp.spl.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)) and value < 0:
                score += w * 10.0 * max(float(value), -2.0)
        if not cmp.timing_consistent:
            score -= 10.0 * w
    return float(np.clip(score, 0.0, 100.0))


def _classify_variant(variant_id: str, roles: Dict[str, RoleComparison], missing: List[str]) -> VariantResult:
    reasons: List[str] = []
    if missing:
        return VariantResult(
            variant_id=variant_id,
            display_name=variant_id,
            roles=roles,
            missing_roles=missing,
            verdict="NIEPEŁNY ZESTAW L/R/L+R",
            fast_index=_variant_fast_index(roles),
            reasons=["Brakuje: " + ", ".join(ROLE_LABELS[x] for x in missing)],
        )
    if any(not cmp.repeatability.reliable for cmp in roles.values()):
        return VariantResult(
            variant_id=variant_id,
            display_name=variant_id,
            roles=roles,
            missing_roles=[],
            verdict="BRAK KALIBRACJI POWTARZALNOŚCI",
            fast_index=_variant_fast_index(roles),
            reasons=[f"Do wiarygodnego progu potrzeba co najmniej {MIN_REFERENCE_REPEATS} powtórzeń referencji dla L, R i L+R."],
        )
    inconsistent = [role for role, cmp in roles.items() if not cmp.timing_consistent]
    if inconsistent:
        return VariantResult(
            variant_id=variant_id,
            display_name=variant_id,
            roles=roles,
            missing_roles=[],
            verdict="DANE CZASOWE NIESPÓJNE",
            fast_index=_variant_fast_index(roles),
            reasons=[
                "Anchor group delay różni się od stabilnej referencji o ponad "
                f"{TIMING_ANCHOR_TOLERANCE_MS:.1f} ms dla: " + ", ".join(ROLE_LABELS[x] for x in inconsistent)
            ],
        )
    body_fail_roles = [role for role, cmp in roles.items() if _body_gate(cmp)]
    if body_fail_roles:
        return VariantResult(
            variant_id=variant_id,
            display_name=variant_id,
            roles=roles,
            missing_roles=[],
            verdict="ODRZUĆ — UTRATA BODY",
            fast_index=_variant_fast_index(roles),
            reasons=["Przekroczono limit ochrony 100–220 Hz dla: " + ", ".join(ROLE_LABELS[x] for x in body_fail_roles)],
        )

    any_bad = {role: any(cmp.significant_bad.values()) for role, cmp in roles.items()}
    lr_good = any(roles["L+R"].significant_good.values())
    side_good = any(any(roles[role].significant_good.values()) for role in ("L", "R"))
    if lr_good and (any_bad["L"] or any_bad["R"]):
        reasons.append("L+R wygląda lepiej, ale co najmniej jedna strona pogarsza się ponad rozrzut referencji.")
        verdict = "ODRZUĆ — NIESPÓJNOŚĆ L/R"
    elif any(any_bad.values()):
        reasons.append("Co najmniej jedna metryka pogarsza się bardziej niż naturalny rozrzut powtórzeń.")
        verdict = "GORSZY CZASOWO"
    elif lr_good and side_good:
        reasons.append("Poprawa przekracza rozrzut referencji w L+R i co najmniej po jednej stronie, bez istotnego pogorszenia drugiej strony.")
        verdict = "POTENCJALNIE LEPSZY"
    else:
        any_good = any(any(cmp.significant_good.values()) for cmp in roles.values())
        if any_good:
            reasons.append("Pojedyncza poprawa nie została potwierdzona jednocześnie przez L+R i stronę L/R.")
            verdict = "ZMIANA BOCZNA / DO ODSŁUCHU"
        else:
            reasons.append("Różnice mieszczą się w naturalnym rozrzucie trzech powtórzeń referencji.")
            verdict = "BRAK POTWIERDZONEJ RÓŻNICY"
    return VariantResult(
        variant_id=variant_id,
        display_name=variant_id,
        roles=roles,
        missing_roles=[],
        verdict=verdict,
        fast_index=_variant_fast_index(roles),
        reasons=reasons,
    )


def analyse_variants(
    items: Sequence[TimeMeasurement], reference_query: str
) -> Tuple[Dict[str, RepeatabilityProfile], List[VariantResult], str, List[str], List[TimeMeasurement]]:
    reference_by_role, family_fp, warnings = _select_reference_family(items, reference_query)
    profiles = {role: _repeatability_profile(role, reference_by_role[role]) for role in ROLE_ORDER}
    reference_items = [item for group in reference_by_role.values() for item in group]
    reference_ids = {id(x) for x in reference_items}

    variants_raw: Dict[str, Dict[str, List[TimeMeasurement]]] = {}
    for item in items:
        if id(item) in reference_ids:
            continue
        role = _canonical_role(item)
        if role not in ROLE_ORDER:
            warnings.append(f"{item.name}: nie rozpoznano roli L/R/L+R — pominięto w werdykcie wariantu.")
            continue
        key = _variant_id(item.name)
        variants_raw.setdefault(key, {}).setdefault(role, []).append(item)

    results: List[VariantResult] = []
    for key, role_groups in variants_raw.items():
        role_comparisons: Dict[str, RoleComparison] = {}
        for role, group in role_groups.items():
            candidate = _aggregate_measurements(group, key + "__" + role, role)
            role_comparisons[role] = _compare_role(candidate, profiles[role])
        missing = [role for role in ROLE_ORDER if role not in role_comparisons]
        results.append(_classify_variant(key, role_comparisons, missing))
    results.sort(key=lambda x: (-x.fast_index, x.variant_id))
    return profiles, results, family_fp, warnings, reference_items


def _write_csv(path: Path, rows: Iterable[Sequence[Any]], header: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(header)
        writer.writerows(rows)


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out.extend("| " + " | ".join(str(x) for x in row) + " |" for row in rows)
    return "\n".join(out)


def _role_summary(cmp: Optional[RoleComparison]) -> str:
    if cmp is None:
        return "brak"
    marks = []
    for key, label in (("late", "late"), ("t20", "T20"), ("gd", "GD")):
        if cmp.significant_good[key]:
            marks.append(label + " +")
        elif cmp.significant_bad[key]:
            marks.append(label + " −")
    return ", ".join(marks) if marks else "w rozrzucie"


def write_reports(
    out_dir: Path,
    source: Path,
    payload: Mapping[str, Any],
    items: Sequence[TimeMeasurement],
    reference_query: str,
    warnings: Sequence[str],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    profiles, variants, family_fp, analysis_warnings, reference_items = analyse_variants(items, reference_query)
    all_warnings = list(warnings) + list(analysis_warnings)

    repeat_rows: List[List[Any]] = []
    repeat_md: List[List[str]] = []
    for role in ROLE_ORDER:
        p = profiles[role]
        repeat_rows.append([
            role, p.count, p.anchor_median_ms, p.anchor_range_ms, " | ".join(p.anchor_outliers),
            p.late_range_db, p.t20_range_ms, p.gd_range_ms,
            p.late_threshold_db, p.t20_threshold_ms, p.gd_threshold_ms,
            "TAK" if p.reliable else "NIE",
        ])
        repeat_md.append([
            ROLE_LABELS[role], str(p.count), _fmt(p.late_range_db, 2, " dB"),
            _fmt(p.t20_range_ms, 1, " ms"), _fmt(p.gd_range_ms, 1, " ms"),
            _fmt(p.late_threshold_db, 2, " dB"), _fmt(p.t20_threshold_ms, 1, " ms"),
            _fmt(p.gd_threshold_ms, 1, " ms"), "TAK" if p.reliable else "NIE",
        ])
    _write_csv(
        out_dir / "02_POWTARZALNOSC_REFERENCJI.csv",
        repeat_rows,
        [
            "rola", "liczba_powtorzen", "anchor_mediana_ms", "anchor_rozstep_ms", "odstajace_anchory",
            "rozrzut_poznej_energii_db", "rozrzut_t20_ms", "rozrzut_gd_ms",
            "prog_poznej_energii_db", "prog_t20_ms", "prog_gd_ms", "wiarygodna_powtarzalnosc",
        ],
    )

    variant_rows: List[List[Any]] = []
    variant_md: List[List[str]] = []
    role_rows: List[List[Any]] = []
    for variant in variants:
        lr = variant.roles.get("L+R")
        body = lr.spl.get("body_125_220_db") if lr else None
        foundation = lr.spl.get("foundation_40_80_db") if lr else None
        kick = lr.spl.get("kick_60_90_db") if lr else None
        variant_rows.append([
            variant.variant_id, variant.verdict, variant.fast_index,
            body, foundation, kick,
            _role_summary(variant.roles.get("L")), _role_summary(variant.roles.get("R")), _role_summary(lr),
            " | ".join(variant.reasons), " | ".join(variant.missing_roles),
        ])
        variant_md.append([
            variant.variant_id, variant.verdict, _fmt(variant.fast_index, 1),
            _fmt(body, 2, " dB"), _role_summary(variant.roles.get("L")),
            _role_summary(variant.roles.get("R")), _role_summary(lr),
        ])
        for role in ROLE_ORDER:
            cmp = variant.roles.get(role)
            if cmp is None:
                continue
            role_rows.append([
                variant.variant_id, role, cmp.candidate.name, cmp.reference.name,
                cmp.spl.get("body_100_160_db"), cmp.spl.get("body_125_220_db"),
                cmp.spl.get("foundation_40_80_db"), cmp.spl.get("kick_60_90_db"),
                cmp.late_improvement_db, cmp.t20_improvement_ms, cmp.gd_improvement_ms,
                cmp.repeatability.late_threshold_db, cmp.repeatability.t20_threshold_ms, cmp.repeatability.gd_threshold_ms,
                cmp.anchor_delta_ms, "TAK" if cmp.timing_consistent else "NIE",
                _role_summary(cmp),
            ])
    _write_csv(
        out_dir / "03_WARIANTY_ZBIORCZO.csv",
        variant_rows,
        [
            "wariant", "werdykt_zbiorczy", "indeks_fast", "body_125_220_delta_db_LR",
            "fundament_40_80_delta_db_LR", "kick_60_90_delta_db_LR",
            "status_L", "status_R", "status_LR", "uzasadnienie", "brakujace_role",
        ],
    )
    _write_csv(
        out_dir / "04_SZCZEGOLY_L_R_LR.csv",
        role_rows,
        [
            "wariant", "rola", "pomiar_agregowany", "referencja_mediana",
            "body_100_160_delta_db", "body_125_220_delta_db", "fundament_40_80_delta_db", "kick_60_90_delta_db",
            "poprawa_poznej_energii_db", "poprawa_t20_ms", "poprawa_gd_ms",
            "prog_poznej_energii_db", "prog_t20_ms", "prog_gd_ms",
            "roznica_anchora_ms", "timing_spojny", "istotnosc",
        ],
    )

    decay_rows = []
    for item in items:
        for band in item.bands:
            decay_rows.append([
                item.name, _canonical_role(item), band.label, band.low_hz, band.high_hz,
                band.peak_offset_ms, band.t10_ms, band.t20_ms, band.t30_ms,
                band.late_100_db, band.late_200_db, band.early_50_percent,
                band.usable_duration_ms, "TAK" if band.noise_limited else "NIE",
            ])
    _write_csv(
        out_dir / "05_DECAY_PASMA.csv",
        decay_rows,
        [
            "pomiar", "rola", "pasmo", "low_hz", "high_hz", "offset_szczytu_pasma_ms",
            "czas_do_minus10_db_ms", "czas_do_minus20_db_ms", "czas_do_minus30_db_ms",
            "pozna_energia_po_100ms_db", "pozna_energia_po_200ms_db", "energia_0_50ms_percent",
            "analizowany_czas_ms", "ograniczenie_szumem",
        ],
    )

    gd_rows = []
    for item in items:
        if item.gd_freq is None or item.gd_ms is None:
            continue
        anchor = item.gd_anchor_ms or 0.0
        relative = item.gd_ms - anchor
        for label, lo, hi in BANDS:
            gd_rows.append([
                item.name, _canonical_role(item), label,
                _band_mean(item.gd_freq, relative, lo, hi),
                _band_percentile(item.gd_freq, relative, lo, hi, 90.0),
                _band_percentile(item.gd_freq, relative, lo, hi, 95.0),
                item.gd_source, anchor,
            ])
    _write_csv(
        out_dir / "06_GROUP_DELAY_PASMA.csv",
        gd_rows,
        ["pomiar", "rola", "pasmo", "gd_srednia_wzgledem_anchor_ms", "gd_p90_ms", "gd_p95_ms", "zrodlo", "anchor_absolutny_ms"],
    )

    best = [x for x in variants if x.verdict == "POTENCJALNIE LEPSZY"]
    lines = [
        "# Analiza czasowa / 90 BW48 Fast — werdykt zbiorczy v20.31",
        "",
        f"- Źródło: `{source.name}`",
        f"- Rodzina referencji: **{family_fp}**",
        f"- Zapytanie referencji: `{reference_query}`",
        f"- Referencyjne pomiary: **{len(reference_items)}**",
        f"- Ocenione warianty: **{len(variants)}**",
        "",
        "## Werdykt",
        "",
    ]
    if best:
        lines.append("Warianty, których poprawa przekracza rozrzut referencji w L+R oraz co najmniej po jednej stronie, bez pogorszenia drugiej strony:")
        for result in best:
            lines.append(f"- **{result.variant_id}** — FAST {result.fast_index:.1f}/100.")
    else:
        lines.append("Żaden wariant nie wykazał potwierdzonej poprawy ponad naturalny rozrzut powtórzeń referencji. Slot 2 należy zachować.")
    lines += [
        "",
        "## Powtarzalność referencji i progi istotności",
        "",
        _markdown_table(
            ["Rola", "N", "Rozrzut late", "Rozrzut T20", "Rozrzut GD", "Próg late", "Próg T20", "Próg GD", "Wiarygodna"],
            repeat_md,
        ),
        "",
        "Progi są liczone osobno dla L, R i L+R jako większa z wartości: konserwatywne minimum albo 105% rzeczywistego rozrzutu referencji.",
        "",
        "## Warianty — jeden werdykt dla L/R/L+R",
        "",
        _markdown_table(
            ["Wariant", "Decyzja", "FAST", "Body L+R", "L", "R", "L+R"],
            variant_md or [["—", "brak kandydatów", "—", "—", "—", "—", "—"]],
        ),
        "",
        "## Zasady decyzji",
        "",
        "- **POTENCJALNIE LEPSZY**: L+R oraz co najmniej jedna strona poprawiają się ponad rozrzut referencji, druga strona nie pogarsza się istotnie, a body pozostaje chronione.",
        "- **BRAK POTWIERDZONEJ RÓŻNICY**: różnice mieszczą się w naturalnej zmienności trzech powtórzeń.",
        "- **ODRZUĆ — NIESPÓJNOŚĆ L/R**: L+R wygląda lepiej, ale L lub R pogarsza się ponad próg.",
        "- **DANE CZASOWE NIESPÓJNE**: anchor group delay odbiega od stabilnej referencji o ponad 2 ms.",
        "- **BRAK KALIBRACJI POWTARZALNOŚCI**: mniej niż trzy pomiary referencji dla którejkolwiek roli.",
        "- **ODRZUĆ — UTRATA BODY**: przekroczony limit ochrony 100–220 Hz.",
    ]
    anchor_outliers = [(role, name) for role, p in profiles.items() for name in p.anchor_outliers]
    if anchor_outliers:
        lines += ["", "## Ostrzeżenie: odstające anchory referencji", ""]
        for role, name in anchor_outliers:
            lines.append(f"- {ROLE_LABELS[role]}: `{name}` — nie traktuj jego bezwzględnego group delay jako wspólnej ramy czasu.")
    if all_warnings:
        lines += ["", "## Ostrzeżenia jakości danych", ""] + [f"- {w}" for w in all_warnings]
    (out_dir / "01_WERDYKT_90BW48_FAST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    method = """# Metoda i jakość danych — analiza czasowa REWDO v20.31

## Najważniejsza zmiana

Tryb nie klasyfikuje już pojedynczych pomiarów L, R lub L+R jako osobnych kandydatów. Najpierw wyznacza naturalny rozrzut co najmniej trzech powtórzeń referencji, a następnie wydaje jeden werdykt dla kompletnego wariantu L/R/L+R.

## Kontrole

1. Jawny wybór rodziny referencji; brak cichego wyboru pierwszego pomiaru.
2. Osobne progi istotności dla L, R i L+R.
3. Wymagane potwierdzenie poprawy w L+R oraz po co najmniej jednej stronie.
4. Blokada, gdy jedna strona pogarsza się ponad rozrzut referencji.
5. Kontrola zgodności bezwzględnego anchoru group delay z tolerancją 2 ms.
6. Twarda ochrona body 100–220 Hz.

## Ograniczenie

Pasmowy decay pozostaje porównawczym wskaźnikiem energii wyliczonym z impulsu. Nie jest laboratoryjnym RT60 ani kopią Waterfall REW. Wiarygodność zależy od identycznego timing reference, pozycji mikrofonu, poziomu sweepu i zakresu pomiaru.
"""
    (out_dir / "07_JAKOSC_DANYCH_I_METODA.md").write_text(method, encoding="utf-8")

    instruction = """# Pomiary do projektu 90 BW48 Fast — v20.31

1. Dla referencji wykonaj co najmniej trzy pełne powtórzenia: L strona, R strona i L+R.
2. Nazwy referencji powinny mieć wspólną konfigurację, np. `REF_SLOT2_90BW48_G0...`, a powtórzenia mogą zawierać `REPEAT2`, `REPEAT3`.
3. Jako fragment referencji wpisz `REF_SLOT2_90BW48_G0`.
4. Dla każdego kandydata wykonaj komplet L, R i L+R z identycznym rdzeniem nazwy.
5. Używaj nieruchomego mikrofonu, tej samej metody acoustic timing reference i tego samego poziomu sweepu.
6. Zaznacz w API pobieranie impulsu i group delay.
7. Wariant jest potencjalnie lepszy dopiero wtedy, gdy poprawa przekracza rozrzut referencji w L+R i co najmniej po jednej stronie, bez pogorszenia drugiej strony.
"""
    (out_dir / "08_INSTRUKCJA_POMIAROW_90BW48_FAST.md").write_text(instruction, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="REWDO v20.31 — analiza czasowa / 90 BW48 Fast z kalibracją powtarzalności")
    parser.add_argument("--snapshot", "--mdat", dest="snapshot", required=True, help="Migawka *.rewapi.json z impulsem i group delay")
    parser.add_argument("--out", required=True, help="Folder wyników")
    parser.add_argument("--reference", default="REF_SLOT2_90BW48_G0", help="Jawny fragment nazwy rodziny referencji")
    parser.add_argument("--max-time-ms", type=float, default=600.0, help="Maksymalny analizowany ogon impulsu")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    source = Path(args.snapshot).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not source.exists():
        print(f"BŁĄD: nie istnieje plik: {source}", file=sys.stderr)
        return 2
    try:
        payload, items, warnings = load_time_measurements(source, max_time_ms=float(args.max_time_ms))
        write_reports(out, source, payload, items, str(args.reference or ""), warnings)
    except Exception as exc:
        print(f"BŁĄD ANALIZY CZASOWEJ: {exc}", file=sys.stderr)
        return 2
    print("Analiza czasowa zakończona z kalibracją powtarzalności L/R/L+R.")
    print(f"Wyniki: {out}")
    print("Główny raport: 01_WERDYKT_90BW48_FAST.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
