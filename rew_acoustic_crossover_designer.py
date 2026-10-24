#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Akustyczny selektor zwrotnicy miniDSP SHD — REWDO v20.23.

Projektuje warianty HPF/LPF z rzeczywistych pomiarów RAW L/R/P main + L/R/P sub.
Wersja v20.23 zachowuje poprawki po audycie i ujednolica nazwy oraz skale metryk: twardą bramkę VERIFIED RAW,
poprawne rozpoznawanie DIRAC_OFF, deduplikację fizycznie identycznych filtrów,
scoring względem efektywnej częstotliwości przecięcia, neutralny opis lokalnego
mismatchu sub/main, twardy limit delayów SHD oraz rozdzielenie średnicy od
ciężaru basu.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

from rew_common import Measurement
from rew_mdat import load_optimizer_measurements_from_mdat
from rew_api_client import is_rew_api_snapshot, read_snapshot, verify_measurement_state, verify_snapshot_response_integrity
from rew_scoring import measurement_to_complex, common_grid, apply_delay_and_polarity, db, dip_depth, roughness
from rew_filter_generator import make_sections, freq_response, write_minidsp_biquads, write_csv as write_biquad_csv
from rew_metric_registry import side_integration_from_construct_dip, metric_label, write_metric_registry
from rew_gain_bridge import analyze_gain_bridge, write_gain_bridge_report
from rew_gain_state import GainState, build_gain_state
from rew_candidate_safety import (
    CandidateSafetyResult,
    InputIntegrityError,
    NoSafeCandidateError,
    combine_required_state_safety,
    evaluate_candidate_safety,
    write_quarantine,
)
from rew_reference_registry import reference_relative_delays

APP_VERSION = "v20.31 Akustyczny selektor zwrotnicy — Candidate Safety Gate"


@dataclass
class AcousticCandidate:
    rank: int
    code: str
    xo_hz: float
    effective_xo_hz: float
    filter_label: str
    main_fc_hz: float
    sub_fc_hz: float
    l_rel_ms: float
    r_rel_ms: float
    l_side_score: float
    r_side_score: float
    side_min: float
    side_mean: float
    lr_sum_score: float
    body_score: float
    authority_score: float
    kick_score: float
    phase_energy_score: float
    null_risk: float
    dirac_burden: float
    acoustic_score: float
    label: str
    reason: str
    shd_main_l_ms: float
    shd_main_r_ms: float
    shd_sub_l_ms: float
    shd_sub_r_ms: float
    l_sub_gain_db: float
    r_sub_gain_db: float
    l_suggested_gain_db: float
    r_suggested_gain_db: float
    gain_mode: str
    gain_limit_warning: str
    low_bass_sub_delta_db: float
    xo_sub_delta_db: float
    body_sub_delta_db: float
    dirac_bass_strategy: str
    dirac_bass_note: str
    decision_class: str
    recommendation: str
    family_key: str
    global_gain_l_db: float = 0.0
    global_gain_r_db: float = 0.0
    final_gain_l_db: float = 0.0
    final_gain_r_db: float = 0.0
    gain_state_name: str = "RAW"
    state_metrics: Dict[str, Dict[str, object]] = field(default_factory=dict)
    candidate_safe_to_export: bool = False
    safety_reasons: Tuple[str, ...] = field(default_factory=tuple)


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))


def _safe_mean(values: np.ndarray) -> float:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return float("nan")
    return float(np.nanmean(v))


def _band(freq: np.ndarray, low: float, high: float) -> np.ndarray:
    return (freq >= float(low)) & (freq <= float(high))


def _score_side(sum_resp: np.ndarray, main_resp: np.ndarray, sub_resp: np.ndarray, freq: np.ndarray, xo: float) -> Tuple[float, float, float]:
    """Sklejenie jednej strony we wspólnej skali v1.

    Od v20.23 tryby 8 i 9 używają dokładnie tej samej transformacji
    konstruktywności/dołku co tryby 1/3/5/6. Dopisek raportu wskazuje jednak,
    że tutaj jest to SYMULACJA PROJEKTU.
    """
    m = _band(freq, max(35.0, xo - 32.0), min(180.0, xo + 42.0))
    if np.count_nonzero(m) < 8:
        m = _band(freq, 55.0, 145.0)
    if np.count_nonzero(m) < 8:
        return 0.0, 0.0, 30.0
    sdb = db(sum_resp)[m]
    comp = np.maximum(db(main_resp)[m], db(sub_resp)[m])
    construct = _safe_mean(sdb - comp)
    dip = dip_depth(sdb)
    one = side_integration_from_construct_dip(construct, construct, dip, dip)
    return float(one["l_score"]), float(construct), float(dip)


def _score_lr_sum(l_sum: np.ndarray, r_sum: np.ndarray, freq: np.ndarray, xo: float) -> Tuple[float, float, float]:
    lr = l_sum + r_sum
    m = _band(freq, max(35.0, xo - 35.0), min(180.0, xo + 45.0))
    if np.count_nonzero(m) < 8:
        m = _band(freq, 55.0, 145.0)
    if np.count_nonzero(m) < 8:
        return 0.0, 99.0, 99.0
    lr_db = db(lr)[m]
    comp = np.maximum(db(l_sum)[m], db(r_sum)[m])
    construct = _safe_mean(lr_db - comp)
    dip = dip_depth(lr_db)
    asym = abs(_safe_mean(db(l_sum)[m]) - _safe_mean(db(r_sum)[m]))
    score = 78.0 + construct * 7.0 - dip * 8.5 - max(0.0, asym - 1.5) * 5.0
    null_risk = _clip(dip * 11.0 + max(0.0, -construct) * 12.0 + max(0.0, asym - 2.0) * 5.0)
    return _clip(score), float(null_risk), float(dip)


def _score_body(lr_sum: np.ndarray, freq: np.ndarray) -> Tuple[float, str]:
    """Ocena ciągłości niższej średnicy niezależna od ciężaru 25–100 Hz.

    v20.17 nie kalibruje body do arbitralnego celu 60–100 -> 100–160 = -4 dB,
    bo taki model premiował zbyt szczupłe warianty. Sprawdzamy przede wszystkim,
    czy pasma 100–160 i 160–220 Hz tworzą ciągłą, nierozchwianą średnicę.
    Referencja 90 BW48 pozostaje punktem barwy w POST-DIRAC, nie wzorcem ciężaru basu.
    """
    m100 = _band(freq, 100.0, 160.0)
    m160 = _band(freq, 160.0, 220.0)
    m125 = _band(freq, 125.0, 220.0)
    if min(np.count_nonzero(m100), np.count_nonzero(m160)) < 4:
        return 50.0, "brak pełnego pasma 100–220 Hz do oceny ciągłości średnicy"
    y = db(lr_sum)
    mid1 = _safe_mean(y[m100])
    mid2 = _safe_mean(y[m160])
    slope = mid2 - mid1
    local_rough = roughness(y[m125]) if np.count_nonzero(m125) >= 8 else 2.0
    # Delikatny spadek ku 220 Hz jest naturalny; karzemy głównie gwałtowną dziurę/garb.
    score = 86.0 - abs(slope + 0.7) * 8.0 - max(0.0, local_rough - 2.0) * 6.0
    if score < 45:
        note = "ryzyko przerwania ciągłości lub sztucznego pogrubienia 100–220 Hz"
    elif score < 68:
        note = "ciągłość body do kontroli wokalem i instrumentami akustycznymi"
    else:
        note = "ciągłość niższej średnicy względnie bezpieczna"
    return _clip(score), note


def _score_authority_and_kick(lr_sum: np.ndarray, freq: np.ndarray) -> Tuple[float, float]:
    """Dwie osobne metryki: autorytet 40–80 Hz i kopnięcie 60–100 Hz.

    Nie premiujemy samej ilości basu. Ocena dotyczy ciągłości i braku lokalnej
    zapadłości/garbu względem sąsiedniego pasma 90–125 Hz.
    """
    y = db(lr_sum)
    m40 = _band(freq, 40.0, 80.0)
    m60 = _band(freq, 60.0, 100.0)
    m90 = _band(freq, 90.0, 125.0)
    if min(np.count_nonzero(m40), np.count_nonzero(m60), np.count_nonzero(m90)) < 4:
        return 50.0, 50.0
    ref = _safe_mean(y[m90])
    authority_delta = _safe_mean(y[m40]) - ref
    kick_delta = _safe_mean(y[m60]) - ref
    authority = 100.0 - abs(authority_delta + 1.0) * 12.0
    kick = 100.0 - abs(kick_delta + 0.2) * 16.0
    return _clip(authority), _clip(kick)

def _phase_energy_score(l_sum: np.ndarray, r_sum: np.ndarray, freq: np.ndarray, xo: float) -> float:
    m = _band(freq, max(45.0, xo - 25.0), min(160.0, xo + 35.0))
    if np.count_nonzero(m) < 6:
        return 50.0
    ph_l = np.unwrap(np.angle(l_sum[m]))
    ph_r = np.unwrap(np.angle(r_sum[m]))
    diff = np.rad2deg(np.angle(np.exp(1j * (ph_l - ph_r))))
    med_abs = float(np.nanmedian(np.abs(diff)))
    return _clip(100.0 - med_abs * 0.55)


def _dirac_burden_risk(l_sum: np.ndarray, r_sum: np.ndarray, freq: np.ndarray, xo: float) -> float:
    """Ryzyko pracy Diraca po usunięciu łagodnego trendu tonalnego.

    v20.16 często saturuje do 100, bo karała szeroki span będący częściowo
    naturalnym nachyleniem targetu. v20.17 ocenia lokalne reszty, dołki i
    nierówność w okolicy efektywnego XO.
    """
    lr = l_sum + r_sum
    m = _band(freq, max(35.0, xo - 40.0), min(220.0, xo + 90.0))
    if np.count_nonzero(m) < 12:
        return 50.0
    x = np.log(np.asarray(freq[m], dtype=float))
    y = db(lr)[m]
    try:
        coef = np.polyfit(x, y, 1)
        resid = y - np.polyval(coef, x)
    except Exception:
        resid = y - np.nanmedian(y)
    rr = roughness(resid)
    dp = dip_depth(resid)
    span = float(np.nanpercentile(resid, 95) - np.nanpercentile(resid, 5))
    # Skala v20.26 nie saturuje automatycznie do 100 dla typowych nierówności
    # pomieszczenia. 0 oznacza mały przewidywany wysiłek, 100 — skrajnie trudny
    # przypadek. Jest to wskaźnik porównawczy, nie prognoza liczby dB korekcji.
    risk = 5.0 + 3.0 * rr + 2.0 * max(0.0, dp - 3.0) + 1.0 * max(0.0, span - 8.0)
    return _clip(risk)


def _smooth_db(values: np.ndarray, width: int = 31) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.size < 5:
        return arr
    n = max(3, min(int(width), int(arr.size // 4) * 2 + 1))
    if n % 2 == 0:
        n += 1
    kernel = np.ones(n, dtype=float) / float(n)
    return np.convolve(arr, kernel, mode="same")


def _effective_xo_hz(
    lm: np.ndarray, rm: np.ndarray, ls: np.ndarray, rs: np.ndarray,
    freq: np.ndarray, main_fc: float, sub_fc: float,
) -> float:
    """Akustyczny punkt zbliżenia średnich magnitud main/sub po filtrach.

    Gdy pomiar nie daje sensownego przecięcia, wracamy do geometrycznego środka
    elektrycznych częstotliwości HPF/LPF. Dzięki temu identyczny fizyczny filtr
    nie zmienia wyniku tylko dlatego, że powstał z innej etykiety pętli XO.
    """
    lo = max(45.0, min(main_fc, sub_fc) - 25.0)
    hi = min(150.0, max(main_fc, sub_fc) + 35.0)
    m = _band(freq, lo, hi)
    fallback = float(math.sqrt(max(1.0, main_fc) * max(1.0, sub_fc)))
    if np.count_nonzero(m) < 12:
        return fallback
    main_db = 0.5 * (db(lm)[m] + db(rm)[m])
    sub_db = 0.5 * (db(ls)[m] + db(rs)[m])
    diff = np.abs(_smooth_db(main_db - sub_db, 21))
    idx = int(np.nanargmin(diff))
    f = np.asarray(freq[m], dtype=float)
    if idx <= 1 or idx >= f.size - 2 or not np.isfinite(diff[idx]) or diff[idx] > 15.0:
        return fallback
    return float(f[idx])

def _map_relative_to_shd(l_rel_ms: float, r_rel_ms: float, limit: float = 30.0) -> Tuple[float, float, float, float]:
    """Mapuj relacje sub-main do 0..limit ms albo przerwij jako niewykonalne."""
    vals = [0.0, 0.0, float(l_rel_ms), float(r_rel_ms)]
    span = max(vals) - min(vals)
    if span > float(limit) + 1e-9:
        raise ValueError(
            f"Relacje delayów wymagają zakresu {span:.2f} ms, większego niż limit SHD {limit:.2f} ms."
        )
    off = -min(vals)
    mapped = [v + off for v in vals]
    if any(v < -1e-9 or v > float(limit) + 1e-9 for v in mapped):
        raise ValueError("Nie udało się bezpiecznie odwzorować delayów do zakresu SHD 0–30 ms.")
    return tuple(float(v) for v in mapped)  # type: ignore[return-value]

def _map_relative_to_shd_main_anchor(
    l_rel_ms: float,
    r_rel_ms: float,
    anchor_main_ms: float = 30.0,
    limit: float = 30.0,
) -> Tuple[float, float, float, float, str]:
    """Czytelny zapis delayów SHD z równymi mainami zakotwiczonymi np. na 30 ms.

    To nie zmienia relacji sub-main; jest tylko alternatywnym sposobem zapisania
    tych samych relacji w SHD, preferowanym w systemie użytkownika.
    """
    ml = float(anchor_main_ms)
    mr = float(anchor_main_ms)
    sl = ml + float(l_rel_ms)
    sr = mr + float(r_rel_ms)
    vals = [ml, mr, sl, sr]
    warning = ""
    if any(v < -1e-9 or v > limit + 1e-9 for v in vals):
        warning = (
            f"wariant main={anchor_main_ms:.2f} ms wychodzi poza zakres 0..{limit:.2f} ms; "
            "użyj zapisu minimal-offset z raportu"
        )
    return ml, mr, sl, sr, warning


def _audit_is_verified_raw(status: str, decision: str) -> bool:
    return str(status or "").upper() in {"RAW_VERIFIED_BY_NAMES", "RAW_VERIFIED_BY_API_CONFIRMATION", "RAW_VERIFIED_BY_API_NATIVE"} and "EXPORT DOZWOLONY" in str(decision or "").upper()


def _audit_is_diagnostic_only(status: str, decision: str) -> bool:
    return not _audit_is_verified_raw(status, decision)

def _parse_xo_list(txt: str) -> List[float]:
    if not txt:
        return list(np.arange(76.0, 101.0, 2.0))
    vals: List[float] = []
    for part in re.split(r"[,;\s]+", str(txt).strip()):
        if not part:
            continue
        vals.append(float(part.replace(",", ".")))
    return vals





def _reference_relative_delays(path: Optional[Path]) -> Optional[Tuple[float, float]]:
    """Użyj wyłącznie zweryfikowanej, bezkonfliktowej referencji."""
    if path is None or not Path(path).exists():
        return None
    companion = Path(path).with_name("USER_REFERENCE_SLOT4MAX_90BW48.yaml")
    companions = [companion] if companion.exists() and companion.resolve() != Path(path).resolve() else []
    return reference_relative_delays(Path(path), companion_paths=companions)

def _audit_input_for_acoustic_design(mdat_path: Path, roles: Dict[str, str]) -> Tuple[str, str, str]:
    """Bramka bezpieczeństwa projektu.

    Eksport jest dozwolony tylko wtedy, gdy nazwy jednoznacznie wskazują:
    RAW + XOVER_OFF + REL120 + DELAY0 oraz nie wskazują DIRAC_ON/POST_DIRAC.
    To nadal nie potwierdza faktycznego stanu urządzenia — potwierdza wyłącznie nazwy.
    """
    names = " ".join([str(mdat_path.name)] + [str(v) for v in roles.values() if v])
    low = names.lower()
    missing_roles = [r for r in ["L main", "R main", "L sub", "R sub"] if not roles.get(r)]
    if missing_roles:
        return (
            "NIEPEŁNY PLIK",
            "EKSPORT ZABLOKOWANY",
            "Brakuje wymaganych ról: " + ", ".join(missing_roles) + ". Tryb wymaga osobnych RAW: L main, R main, L sub, R sub.",
        )

    # Migawka API zapisuje jawne potwierdzenie ustawień, których REW nie może
    # odczytać z miniDSP. To potwierdzenie zastępuje wymaganie kodowania całego
    # stanu SHD w nazwach pomiarów.
    if is_rew_api_snapshot(mdat_path):
        try:
            payload = read_snapshot(mdat_path)
            verified, missing_state = verify_measurement_state(payload.get("measurementState"))
            response_ok, response_problems = verify_snapshot_response_integrity(payload)
        except Exception as exc:
            return ("BŁĄD MIGAWKI API", "EKSPORT ZABLOKOWANY", f"Nie można sprawdzić potwierdzenia RAW: {exc}")
        explicit_bad = any(x in re.sub(r"[^a-z0-9]+", "_", low) for x in [
            "dirac_on", "diracon", "post_dirac", "postdirac", "after_dirac"
        ])
        if explicit_bad:
            return (
                "SPRZECZNE DANE API",
                "EKSPORT ZABLOKOWANY",
                "Nazwy pomiarów wskazują DIRAC_ON/POST_DIRAC mimo potwierdzenia RAW. Zmień nazwy lub wybierz właściwe pomiary.",
            )
        if verified and not response_ok:
            return (
                "API_DANE_NIEZWERYFIKOWANE",
                "TYLKO DIAGNOSTYKA — EKSPORT ZABLOKOWANY",
                "Stan RAW potwierdzono, ale odpowiedź API nie jest natywna i bez wygładzania: " + "; ".join(response_problems) + ". Utwórz migawkę ponownie w v20.31.",
            )
        if verified and response_ok:
            state = payload.get("measurementState", {})
            parity = payload.get("parityCheck", {})
            parity_note = ""
            if isinstance(parity, dict):
                parity_note = f" Kontrola API–MDAT: {parity.get('status', '—')}."
            return (
                "RAW_VERIFIED_BY_API_NATIVE",
                "EXPORT DOZWOLONY",
                "Użytkownik potwierdził stan RAW, a REWDO pobrał natywną odpowiedź częstotliwościową z jawnym brakiem wygładzania i bez wymuszania PPO. "
                f"Gainy: main {float(state.get('mainGainDb', 0.0)):.2f} dB, sub {float(state.get('subGainDb', 0.0)):.2f} dB." + parity_note,
            )
        return (
            "RAW_API_NIEPOTWIERDZONY",
            "TYLKO DIAGNOSTYKA — EKSPORT ZABLOKOWANY",
            "Migawka API nie zawiera kompletnego potwierdzenia stanu RAW. Braki: " + ", ".join(missing_state) + ". Utwórz migawkę ponownie i zaznacz warunki RAW.",
        )
    compact = re.sub(r"[^a-z0-9]+", "_", low)
    has_raw = "raw" in low
    has_xover_off = any(x in compact for x in ["xover_off", "xoveroff", "crossover_off"])
    has_rel120 = any(x in compact for x in ["rel120", "rel_120"])
    has_delay0 = bool(re.search(r"(?:^|_)delay_?0(?:p0+)?(?:_|$)", compact)) or "delay0" in compact
    has_dirac_off = any(x in compact for x in ["dirac_off", "diracoff"])
    has_dirac_on_or_post = any(x in compact for x in [
        "dirac_on", "diracon", "post_dirac", "postdirac", "po_dirac", "after_dirac"
    ])
    has_lr_sum = any(x in low for x in ["l+r", "l + r", "l/p", "l / p", "l+p", "l + p"])
    has_filter_name = bool(re.search(r"(?i)(?:^|[\s_\-])(?:\d{2,3})(?:\s*hz)?[\s_\-]*(?:bw|lr)\s*(?:24|48)?", names))
    if has_dirac_on_or_post or has_lr_sum:
        return (
            "ZŁY TYP WEJŚCIA",
            "UŻYJ TRYBU 2 POST-DIRAC — EKSPORT ZABLOKOWANY",
            "W nazwach wykryto DIRAC_ON/POST_DIRAC albo pełne L+R/L+P. DIRAC_OFF nie jest błędem i jest traktowane jako znacznik pozytywny.",
        )
    if has_filter_name and not has_xover_off:
        return (
            "POMIAR ISTNIEJĄCEJ ZWROTNICY",
            "TYLKO DIAGNOSTYKA — EKSPORT ZABLOKOWANY",
            "W nazwach wykryto aktywny filtr/crossover. Nie projektuj nowego filtra na materiale już przefiltrowanym.",
        )
    if has_raw and has_xover_off and has_rel120 and has_delay0:
        dirac_note = " Wykryto również DIRAC_OFF." if has_dirac_off else ""
        return (
            "RAW_VERIFIED_BY_NAMES",
            "EXPORT DOZWOLONY",
            "Nazwy są zgodne z RAW XOVER_OFF REL120 DELAY0." + dirac_note + " Aplikacja nie potrafi jednak zweryfikować faktycznych ustawień SHD — sprawdź je przed pomiarem.",
        )
    missing = []
    if not has_raw:
        missing.append("RAW")
    if not has_xover_off:
        missing.append("XOVER_OFF")
    if not has_rel120:
        missing.append("REL120")
    if not has_delay0:
        missing.append("DELAY0")
    return (
        "RAW_NIEZWERYFIKOWANY",
        "TYLKO DIAGNOSTYKA — EKSPORT ZABLOKOWANY",
        "Nazwy nie potwierdzają wszystkich wymaganych znaczników: " + ", ".join(missing or ["niejednoznaczny zestaw"]) + ". Wykonaj/oznacz pomiar: RAW_XOVER_OFF_REL120_DELAY0, Dirac OFF, PEQ OFF.",
    )

def _raw_sub_gain_delta_db(main_resp: np.ndarray, sub_resp: np.ndarray, freq: np.ndarray, xo: float) -> float:
    """Lokalna różnica magnitudy main-sub przy efektywnym XO; NIE globalny gain suba."""
    m = _band(freq, max(45.0, xo - 18.0), min(150.0, xo + 18.0))
    if np.count_nonzero(m) < 5:
        m = _band(freq, 70.0, 120.0)
    if np.count_nonzero(m) < 5:
        return 0.0
    return float(_safe_mean(db(main_resp)[m]) - _safe_mean(db(sub_resp)[m]))


def _limited_sub_gain_db(main_resp: np.ndarray, sub_resp: np.ndarray, freq: np.ndarray, xo: float, lo: float = -18.0, hi: float = 6.0) -> Tuple[float, bool, float]:
    """Ograniczona diagnostyka lokalnego mismatchu przy XO; NIE rekomendacja globalnego gainu."""
    raw = _raw_sub_gain_delta_db(main_resp, sub_resp, freq, xo)
    limited = float(max(lo, min(hi, raw)))
    limited_flag = bool(raw < lo - 1e-6 or raw > hi + 1e-6 or abs(limited - lo) < 1e-6 or abs(limited - hi) < 1e-6)
    return limited, limited_flag, raw


def _mean_lr_band_delta_db(main_l: np.ndarray, main_r: np.ndarray, sub_l: np.ndarray, sub_r: np.ndarray, freq: np.ndarray, low: float, high: float) -> float:
    """Średnia przewaga subów nad mainami w paśmie; dodatnia = suby mocniejsze."""
    m = _band(freq, low, high)
    if np.count_nonzero(m) < 5:
        return 0.0
    main_db = 0.5 * (_safe_mean(db(main_l)[m]) + _safe_mean(db(main_r)[m]))
    sub_db = 0.5 * (_safe_mean(db(sub_l)[m]) + _safe_mean(db(sub_r)[m]))
    return float(sub_db - main_db)


def _dirac_bass_strategy_profile(
    lm: np.ndarray, rm: np.ndarray, ls0: np.ndarray, rs0: np.ndarray, freq: np.ndarray, xo: float,
    lm_low_ref: Optional[np.ndarray] = None, rm_low_ref: Optional[np.ndarray] = None,
) -> Tuple[float, float, float, str, str]:
    """Rozdziel zapas niskiego basu od lokalnej dominacji suba przy XO.

    v20.17 nie używa już resztkowej energii suba 120–180 Hz po LPF48 jako
    wskaźnika body, bo ta wartość opisuje głównie stromość filtra. Trzecia
    wartość jest pozostawiona wyłącznie dla zgodności wewnętrznej i zastępowana
    później realną relacją całkowitej sumy 125–220 vs 75–125 Hz.
    """
    lm_low = lm_low_ref if lm_low_ref is not None else lm
    rm_low = rm_low_ref if rm_low_ref is not None else rm
    low_delta = _mean_lr_band_delta_db(lm_low, rm_low, ls0, rs0, freq, 25.0, 60.0)
    xo_low = max(55.0, float(xo) - 22.0)
    xo_high = min(135.0, float(xo) + 22.0)
    xo_delta = _mean_lr_band_delta_db(lm, rm, ls0, rs0, freq, xo_low, xo_high)

    if xo_delta >= 8.0:
        strategy = "WYSOKI SUB RYZYKOWNY PRZY XO"
        note = "Lokalna magnituda subów wyraźnie dominuje przy efektywnym XO. To nie jest automatyczne polecenie globalnego ściszenia; sprawdź sumę fazową, target Diraca i POST-DIRAC."
    elif low_delta >= 4.0 and xo_delta <= 4.0:
        strategy = "ZAPAS NISKIEGO BASU DO CIĘCIA DIRACA"
        note = "Wyższy poziom subów leży głównie w 25–60 Hz, a nie przy XO. Może to być użyteczny zapas do cięcia modów."
    elif low_delta <= -4.0 and xo_delta <= -4.0:
        strategy = "SUBY PRAWDOPODOBNIE ZA NISKO"
        note = "Suby są słabsze od mainów zarówno w niskim basie, jak i przy XO. To może zabierać autorytet, ale decyzję potwierdź po Diracu."
    elif xo_delta >= 4.0:
        strategy = "SUBY WYŻEJ — KONTROLA XO WYMAGANA"
        note = "Przy XO jest lokalny nadmiar magnitudy. Oceniaj go razem z fazą i sumą, nie jako globalny trim."
    else:
        strategy = "UMIARKOWANA STRATEGIA BASU"
        note = "Nie widać skrajnej dominacji subów przy efektywnym XO. Finalny balans ustal po Diracu i odsłuchu."
    return float(low_delta), float(xo_delta), float("nan"), strategy, note

def _decision_class(score: float, side_min: float, lr_score: float, body: float, null_risk: float, burden: float, gain_warning: str) -> Tuple[str, str]:
    """Krótka decyzja użytkowa; nie udajemy rekomendacji przy słabym wyniku."""
    hard = []
    if score < 60:
        hard.append("wynik akustyczny poniżej 60/100")
    if side_min < 55:
        hard.append("słaba jedna strona sub/main")
    if body < 45:
        hard.append("ryzyko przerwania ciągłości korpusu / dolnej średnicy")
    if burden >= 80:
        hard.append("bardzo wysoki ciężar Diraca")
    if null_risk >= 55:
        hard.append("wysokie ryzyko dziury L+P")
    if gain_warning:
        hard.append("problem poziomu subów")
    if hard:
        return "DIAGNOSTYKA", "Nie traktuj jako rekomendacji; najpierw sprawdź: " + "; ".join(hard) + "."
    if score >= 80 and side_min >= 78 and lr_score >= 70 and body >= 60 and null_risk <= 25 and burden <= 45:
        return "MOCNY KANDYDAT", "Warto wgrać jako wariant do Dirac + POST-DIRAC 3-punktowy."
    if score >= 70:
        return "KANDYDAT WARUNKOWY", "Warto sprawdzić po Diracu, ale nadal wymaga pomiaru L+P posL/MLP/posP."
    return "OSTROŻNIE", "Tylko test kontrolny; nie traktuj jako finalnej zwrotnicy."


def _simulate_candidate(
    freq: np.ndarray,
    lm_raw: np.ndarray, rm_raw: np.ndarray, ls_raw: np.ndarray, rs_raw: np.ndarray,
    fs: float, xo: float, filter_label: str, main_fc: float, sub_fc: float,
    l_rel_ms: float, r_rel_ms: float,
    gain_mode: str = "suggested",
) -> AcousticCandidate:
    """Symulacja wariantu.

    gain_mode:
    - fixed:     nie licz ani nie stosuj auto gainu, raportuj 0/0;
    - suggested: licz lokalny mismatch XO, ale NIE stosuj go w symulacji;
    - applied:   zastosuj ograniczony trim w symulacji jak w v20.09.
    """
    gain_mode = (gain_mode or "suggested").strip().lower()
    if gain_mode not in {"fixed", "suggested", "applied"}:
        gain_mode = "suggested"
    main_sec = make_sections("HP", fs, main_fc, filter_label)
    sub_sec = make_sections("LP", fs, sub_fc, filter_label)
    hm = freq_response(main_sec, fs, freq)
    hs = freq_response(sub_sec, fs, freq)

    lm = lm_raw * hm
    rm = rm_raw * hm
    ls0 = ls_raw * hs
    rs0 = rs_raw * hs

    effective_xo = _effective_xo_hz(lm, rm, ls0, rs0, freq, main_fc, sub_fc)
    low_delta, xo_delta, _legacy_body_delta, bass_strategy, bass_note = _dirac_bass_strategy_profile(
        lm, rm, ls0, rs0, freq, effective_xo, lm_low_ref=lm_raw, rm_low_ref=rm_raw
    )

    l_suggested, l_limited, l_raw_gain = _limited_sub_gain_db(lm, ls0, freq, effective_xo)
    r_suggested, r_limited, r_raw_gain = _limited_sub_gain_db(rm, rs0, freq, effective_xo)
    if gain_mode == "fixed":
        l_gain_db = 0.0
        r_gain_db = 0.0
        gain_warning = ""
    elif gain_mode == "suggested":
        # W symulacji zachowujemy realny poziom RAW, a trim tylko raportujemy.
        l_gain_db = 0.0
        r_gain_db = 0.0
        flags = []
        if l_limited:
            flags.append(f"L lokalny mismatch XO {l_suggested:.1f} dB osiąga limit; raw {l_raw_gain:.1f} dB")
        elif abs(l_suggested) >= 10.0:
            flags.append(f"L lokalny mismatch XO {l_suggested:.1f} dB jest bardzo duży")
        if r_limited:
            flags.append(f"R lokalny mismatch XO {r_suggested:.1f} dB osiąga limit; raw {r_raw_gain:.1f} dB")
        elif abs(r_suggested) >= 10.0:
            flags.append(f"R lokalny mismatch XO {r_suggested:.1f} dB jest bardzo duży")
        gain_warning = "; ".join(flags)
    else:
        l_gain_db = l_suggested
        r_gain_db = r_suggested
        flags = []
        if l_limited:
            flags.append(f"L zastosowany trim {l_gain_db:.1f} dB osiąga limit; raw {l_raw_gain:.1f} dB")
        elif abs(l_gain_db) >= 10.0:
            flags.append(f"L zastosowany trim {l_gain_db:.1f} dB jest bardzo duży")
        if r_limited:
            flags.append(f"R zastosowany trim {r_gain_db:.1f} dB osiąga limit; raw {r_raw_gain:.1f} dB")
        elif abs(r_gain_db) >= 10.0:
            flags.append(f"R zastosowany trim {r_gain_db:.1f} dB jest bardzo duży")
        gain_warning = "; ".join(flags)

    ls = apply_delay_and_polarity(ls0 * (10.0 ** (l_gain_db / 20.0)), freq, l_rel_ms, False)
    rs = apply_delay_and_polarity(rs0 * (10.0 ** (r_gain_db / 20.0)), freq, r_rel_ms, False)
    lsum = lm + ls
    rsum = rm + rs
    lscore, lconstruct, ldip = _score_side(lsum, lm, ls, freq, effective_xo)
    rscore, rconstruct, rdip = _score_side(rsum, rm, rs, freq, effective_xo)
    lr_score, null_risk, lr_dip = _score_lr_sum(lsum, rsum, freq, effective_xo)
    body, body_note = _score_body(lsum + rsum, freq)
    authority, kick = _score_authority_and_kick(lsum + rsum, freq)
    phase_score = _phase_energy_score(lsum, rsum, freq, effective_xo)
    burden = _dirac_burden_risk(lsum, rsum, freq, effective_xo)
    total_db = db(lsum + rsum)
    m_body = _band(freq, 125.0, 220.0)
    m_kick = _band(freq, 75.0, 125.0)
    body_delta = (_safe_mean(total_db[m_body]) - _safe_mean(total_db[m_kick])) if np.count_nonzero(m_body) >= 4 and np.count_nonzero(m_kick) >= 4 else float("nan")
    side_min = min(lscore, rscore)
    side_mean = (lscore + rscore) / 2.0
    score = (
        0.25 * side_min +
        0.15 * side_mean +
        0.18 * lr_score +
        0.14 * body +
        0.08 * authority +
        0.08 * kick +
        0.06 * phase_score +
        0.06 * (100.0 - burden)
    )
    if null_risk > 45:
        score -= (null_risk - 45.0) * 0.25
    score = _clip(score)

    label = "kandydat akustyczny"
    if side_min >= 78 and lr_score >= 70 and body >= 60 and null_risk <= 25 and score >= 70:
        label = "mocny kandydat akustyczny"
    elif score < 60 or side_min < 60 or lr_score < 55 or body < 45:
        label = "tylko diagnostycznie / ostrożnie"

    decision, recommendation = _decision_class(score, side_min, lr_score, body, null_risk, burden, gain_warning)
    reason = (
        f"Sklejenie stron — SYMULACJA PROJEKTU {lscore:.1f}/{rscore:.1f}, jakość sumy L+P {lr_score:.1f}, "
        f"ciągłość korpusu {body:.1f} ({body_note}), autorytet fundamentu {authority:.1f}, impuls {kick:.1f}, "
        f"efektywne XO {effective_xo:.1f} Hz, symulowane ryzyko dziury L+P {null_risk:.1f}, "
        f"ciężar Diraca {burden:.1f}, tryb gain {gain_mode}, "
        f"zastosowany poziom L/R {l_gain_db:.1f}/{r_gain_db:.1f} dB, "
        f"lokalne niedopasowanie poziomu przy XO L/R {l_suggested:.1f}/{r_suggested:.1f} dB, "
        f"strategia basu: {bass_strategy}."
    )
    if gain_warning:
        reason += " UWAGA: " + gain_warning + "."
    ml, mr, sl, sr = _map_relative_to_shd(l_rel_ms, r_rel_ms)
    family_key = f"{filter_label}_M{main_fc:.3f}_S{sub_fc:.3f}"
    return AcousticCandidate(
        rank=0, code="", xo_hz=xo, effective_xo_hz=effective_xo, filter_label=filter_label, main_fc_hz=main_fc, sub_fc_hz=sub_fc,
        l_rel_ms=l_rel_ms, r_rel_ms=r_rel_ms, l_side_score=lscore, r_side_score=rscore,
        side_min=side_min, side_mean=side_mean, lr_sum_score=lr_score, body_score=body,
        authority_score=authority, kick_score=kick,
        phase_energy_score=phase_score, null_risk=null_risk, dirac_burden=burden,
        acoustic_score=score, label=label, reason=reason,
        shd_main_l_ms=ml, shd_main_r_ms=mr, shd_sub_l_ms=sl, shd_sub_r_ms=sr,
        l_sub_gain_db=l_gain_db, r_sub_gain_db=r_gain_db,
        l_suggested_gain_db=l_suggested, r_suggested_gain_db=r_suggested,
        gain_mode=gain_mode, gain_limit_warning=gain_warning,
        low_bass_sub_delta_db=low_delta, xo_sub_delta_db=xo_delta, body_sub_delta_db=body_delta,
        dirac_bass_strategy=bass_strategy, dirac_bass_note=bass_note,
        decision_class=decision, recommendation=recommendation, family_key=family_key,
    )

def design_acoustic_crossovers(
    mdat_path: Path,
    out_dir: Path,
    fs: float = 96000.0,
    xo_values: Optional[List[float]] = None,
    filters: Optional[List[str]] = None,
    l_rel_center: float = -9.6,
    r_rel_center: float = -10.9,
    rel_radius: float = 1.5,
    rel_step: float = 0.5,
    top_n: int = 8,
    gain_mode: str = "suggested",
    shd_anchor_main: float = 30.0,
    user_reference_path: Optional[Path] = None,
    refine_radius: float = 0.4,
    refine_step: float = 0.1,
    gain_bridge_path: Optional[Path] = None,
    gain_bridge_target_delta_db: float = 6.0,
    raw_sub_trim_db: float = -6.0,
    dirac_sub_trim_db: Optional[float] = -4.0,
    listening_sub_trim_db: Optional[float] = 0.0,
    allow_unverified_gain_override: bool = False,
    effective_xo_min_hz: float = 70.0,
    effective_xo_max_hz: float = 105.0,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> List[AcousticCandidate]:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_metric_registry(out_dir)
    lm, ls, rm, rs, project = load_optimizer_measurements_from_mdat(mdat_path)
    input_audit = _audit_input_for_acoustic_design(Path(mdat_path), project.role_to_name)
    diagnostic_only = _audit_is_diagnostic_only(input_audit[0], input_audit[1])
    freq = common_grid([lm, rm, ls, rs], points=2200)
    # Zakres projektowania zwrotnicy: nie potrzebujemy 20k, ale zostawiamy body do 240 Hz.
    freq = freq[(freq >= 20.0) & (freq <= 500.0)]
    if freq.size < 80:
        raise ValueError("Za mało punktów w zakresie 20–500 Hz do projektowania zwrotnicy akustycznej.")
    lm_raw = measurement_to_complex(lm, freq)
    rm_raw = measurement_to_complex(rm, freq)
    ls_raw = measurement_to_complex(ls, freq)
    rs_raw = measurement_to_complex(rs, freq)
    ls_raw_measured = ls_raw.copy()
    rs_raw_measured = rs_raw.copy()

    if not np.isfinite(float(gain_bridge_target_delta_db)):
        raise ValueError("Stan projektu względem RAW musi być skończony.")
    if not float(effective_xo_min_hz) < float(effective_xo_max_hz):
        raise ValueError("effective_xo_min musi być mniejsze od effective_xo_max.")
    if gain_bridge_path is None and not allow_unverified_gain_override:
        (out_dir / "DESIGN_GAIN_STATE.md").write_text(
            "# Stan gainu użyty w projekcie\n\n"
            "**BRAK GAIN BRIDGE — projekt i eksport zatrzymane.**\n\n"
            "Podaj `--gain-bridge PLIK.mdat` albo jawny `--allow-unverified-gain-override` "
            "wyłącznie do diagnostyki bez eksportu miniDSP.\n",
            encoding="utf-8",
        )
        raise InputIntegrityError("Brak Gain Bridge i brak jawnego override diagnostycznego.")
    if listening_sub_trim_db is not None:
        full_delta = float(listening_sub_trim_db) - float(raw_sub_trim_db)
        lo_delta, hi_delta = sorted((0.0, full_delta))
        if not lo_delta - 1e-9 <= float(gain_bridge_target_delta_db) <= hi_delta + 1e-9:
            raise ValueError(
                f"Stan projektu {float(gain_bridge_target_delta_db):+.2f} dB względem RAW jest poza "
                f"zakresem GainState {lo_delta:+.2f}..{hi_delta:+.2f} dB."
            )

    bridge_audit = None
    gain_state: GainState
    applied_l_db = float(gain_bridge_target_delta_db)
    applied_r_db = float(gain_bridge_target_delta_db)

    if gain_bridge_path is not None:
        bridge_audit = analyze_gain_bridge(Path(mdat_path), Path(gain_bridge_path), expected_gain_db=6.0)
        write_gain_bridge_report(bridge_audit, out_dir)
        if bridge_audit.status != "VERIFIED":
            raise ValueError("Gain bridge: " + bridge_audit.reason)
        gain_state = build_gain_state(
            raw_db=float(raw_sub_trim_db),
            dirac_db=dirac_sub_trim_db,
            listening_db=listening_sub_trim_db,
            source="GAIN_BRIDGE",
            verified=True,
            bridge_path=gain_bridge_path,
            bridge_left_delta_db=bridge_audit.left.gain_db,
            bridge_right_delta_db=bridge_audit.right.gain_db,
        )
        target_absolute_db = float(raw_sub_trim_db) + float(gain_bridge_target_delta_db)
        full_nominal_delta = (
            float(listening_sub_trim_db) - float(raw_sub_trim_db)
            if listening_sub_trim_db is not None else 0.0
        )
        if abs(full_nominal_delta) < 1e-12:
            raise ValueError("GainState: stan RAW i odsłuchowy nie mogą być identyczne przy użyciu bridge.")
        fraction = float(gain_bridge_target_delta_db) / full_nominal_delta
        applied_l_db = bridge_audit.left.gain_db * fraction
        applied_r_db = bridge_audit.right.gain_db * fraction
        ls_raw *= np.power(10.0, applied_l_db / 20.0)
        rs_raw *= np.power(10.0, applied_r_db / 20.0)
        (out_dir / "DESIGN_GAIN_STATE.md").write_text(
            "\n".join([
                "# Stan gainu użyty w projekcie",
                "",
                f"- RAW: **{float(raw_sub_trim_db):+.2f} dB**",
                f"- Pełny mostek do 0 dB: **L {bridge_audit.left.gain_db:+.2f} dB / R {bridge_audit.right.gain_db:+.2f} dB**",
                f"- Stan pomiaru Diraca: **{dirac_sub_trim_db if dirac_sub_trim_db is not None else 'BRAK'} dB**",
                f"- Stan odsłuchowy: **{listening_sub_trim_db if listening_sub_trim_db is not None else 'BRAK'} dB**",
                f"- Zastosowany stan projektu względem RAW: **{float(gain_bridge_target_delta_db):+.2f} dB**",
                f"- Realne skalowanie użyte w obliczeniach: **L {applied_l_db:+.2f} dB / R {applied_r_db:+.2f} dB**",
                f"- Hash Gain Bridge: `{gain_state.bridge_sha256}`",
                "",
                "Dla workflow Slot 4 wartość +2 dB odpowiada pomiarowi Diraca przy -4 dB. "
                "Pełne +6 dB odpowiada odsłuchowi przy 0 dB i służy jako osobny test obciążenia korekcji.",
            ]),
            encoding="utf-8",
        )
    else:
        gain_state = build_gain_state(
            raw_db=float(raw_sub_trim_db),
            dirac_db=dirac_sub_trim_db,
            listening_db=listening_sub_trim_db,
            source="MANUAL_UNVERIFIED_OVERRIDE" if allow_unverified_gain_override else "MISSING_GAIN_BRIDGE",
            verified=False,
        )
        ls_raw *= np.power(10.0, applied_l_db / 20.0)
        rs_raw *= np.power(10.0, applied_r_db / 20.0)
        (out_dir / "DESIGN_GAIN_STATE.md").write_text(
            "\n".join([
                "# Stan gainu użyty w projekcie",
                "",
                f"- RAW: **{float(raw_sub_trim_db):+.2f} dB**",
                f"- Dirac: **{dirac_sub_trim_db if dirac_sub_trim_db is not None else 'BRAK'} dB**",
                f"- Odsłuch: **{listening_sub_trim_db if listening_sub_trim_db is not None else 'BRAK'} dB**",
                f"- Zastosowane ręczne skalowanie L/R: **{applied_l_db:+.2f}/{applied_r_db:+.2f} dB**",
                "- Status: **NIEZWERYFIKOWANY — tylko diagnostyka, eksport zablokowany**",
            ]) + "\n",
            encoding="utf-8",
        )

    xo_values = xo_values or [80.0, 82.0, 84.0, 86.0, 88.0, 90.0, 92.0, 94.0, 96.0]
    filters = filters or ["BW48", "LR48"]
    offsets = [-4.0, 0.0, 4.0]

    ref_rel = _reference_relative_delays(user_reference_path)
    reference_anchor_verified = ref_rel is not None
    if ref_rel is not None:
        l_rel_center, r_rel_center = ref_rel
    (out_dir / "REFERENCE_REGISTRY_STATUS.md").write_text(
        "# Status referencji delayu\n\n"
        + ("- **VERIFIED** — użyto bezkonfliktowej referencji z dowodem pomiarowym.\n"
           if reference_anchor_verified else
           "- **BLOCKED** — brak zweryfikowanej, bezkonfliktowej referencji. Wartości centrum skanu są tylko priorem konfiguracyjnym i nie mogą odblokować eksportu.\n"),
        encoding="utf-8",
    )

    # Fizycznie identyczne pary HPF/LPF liczymy tylko raz. Etykieta nominalnego XO
    # nie bierze już udziału w tożsamości ani w oknach scoringu.
    physical_pairs: List[Tuple[str, float, float]] = []
    seen_pairs = set()
    for flt in filters:
        for xo in xo_values:
            for moff in offsets:
                for soff in offsets:
                    main_fc = max(45.0, float(xo) + moff)
                    sub_fc = max(45.0, float(xo) + soff)
                    sig = (str(flt).upper(), round(main_fc, 6), round(sub_fc, 6))
                    if sig in seen_pairs:
                        continue
                    seen_pairs.add(sig)
                    physical_pairs.append((str(flt).upper(), main_fc, sub_fc))

    rel_offsets = np.arange(-float(rel_radius), float(rel_radius) + 1e-9, float(rel_step))
    rows_by_sig: Dict[Tuple[object, ...], AcousticCandidate] = {}
    total = int(len(physical_pairs) * len(rel_offsets) * len(rel_offsets))
    done = 0
    report_every = max(1, min(500, total // 20 if total else 1))
    if progress_callback:
        progress_callback(0, total, "start skanu unikalnych filtrów")
    for flt, main_fc, sub_fc in physical_pairs:
        nominal_center = float(math.sqrt(main_fc * sub_fc))
        for dl in rel_offsets:
            for dr in rel_offsets:
                lrel = float(l_rel_center + dl)
                rrel = float(r_rel_center + dr)
                # Twarda walidacja reprezentowalności w SHD 0..30 ms.
                try:
                    _map_relative_to_shd(lrel, rrel)
                    cand = _simulate_candidate(
                        freq, lm_raw, rm_raw, ls_raw, rs_raw, fs,
                        nominal_center, flt, main_fc, sub_fc, lrel, rrel,
                        gain_mode=gain_mode,
                    )
                except ValueError:
                    cand = None
                if cand is not None:
                    sig = (flt, round(main_fc, 4), round(sub_fc, 4), round(lrel, 4), round(rrel, 4), gain_mode)
                    prev = rows_by_sig.get(sig)
                    if prev is None or cand.acoustic_score > prev.acoustic_score:
                        rows_by_sig[sig] = cand
                done += 1
                if progress_callback and (done % report_every == 0 or done == total):
                    progress_callback(done, total, f"{flt} M{main_fc:.0f}/S{sub_fc:.0f}")

    rows = sorted(rows_by_sig.values(), key=lambda c: c.acoustic_score, reverse=True)

    # Drugi etap: dokładny refine delayów 0,1 ms wokół najlepszych rodzin.
    if rows and refine_step > 0 and refine_radius > 0:
        seeds: List[AcousticCandidate] = []
        seen_family = set()
        for c in rows:
            if c.family_key in seen_family:
                continue
            seen_family.add(c.family_key)
            seeds.append(c)
            if len(seeds) >= min(6, max(2, len(filters) * 2)):
                break
        fine = np.arange(-float(refine_radius), float(refine_radius) + 1e-9, float(refine_step))
        refine_total = len(seeds) * len(fine) * len(fine)
        refine_done = 0
        if progress_callback:
            progress_callback(0, refine_total, "refine delayów")
        for seed in seeds:
            for dl in fine:
                for dr in fine:
                    lrel = float(seed.l_rel_ms + dl)
                    rrel = float(seed.r_rel_ms + dr)
                    try:
                        _map_relative_to_shd(lrel, rrel)
                        cand = _simulate_candidate(
                            freq, lm_raw, rm_raw, ls_raw, rs_raw, fs,
                            float(math.sqrt(seed.main_fc_hz * seed.sub_fc_hz)), seed.filter_label,
                            seed.main_fc_hz, seed.sub_fc_hz, lrel, rrel, gain_mode=gain_mode,
                        )
                    except ValueError:
                        cand = None
                    if cand is not None:
                        sig = (cand.filter_label, round(cand.main_fc_hz, 4), round(cand.sub_fc_hz, 4), round(lrel, 4), round(rrel, 4), gain_mode)
                        prev = rows_by_sig.get(sig)
                        if prev is None or cand.acoustic_score > prev.acoustic_score:
                            rows_by_sig[sig] = cand
                    refine_done += 1
                    if progress_callback and (refine_done % max(1, refine_total // 10) == 0 or refine_done == refine_total):
                        progress_callback(refine_done, refine_total, "refine delayów 0,1 ms")
        rows = sorted(rows_by_sig.values(), key=lambda c: c.acoustic_score, reverse=True)

    # v20.10: grupowanie rodzin. Pokazujemy najlepszy wariant z danej rodziny
    # XO/filter/HPF/LPF, żeby top lista nie była zasypana prawie identycznymi filtrami
    # różniącymi się wyłącznie delayem. Pełna lista pozostaje w CSV 99.
    best_by_family: Dict[str, AcousticCandidate] = {}
    for c in rows:
        if c.family_key not in best_by_family:
            best_by_family[c.family_key] = c
    grouped = sorted(best_by_family.values(), key=lambda c: c.acoustic_score, reverse=True)

    # v20.16: globalny TOP nie może ukrywać całej rodziny filtra.
    # Najpierw bierzemy klasyczny TOP, a następnie dokładamy najlepszego
    # przedstawiciela każdej rodziny z listy --filters (np. BW48 i LR48),
    # nawet jeśli nie zmieścił się w globalnym TOP. Dzięki temu w raporcie
    # i w eksporcie biquadów zawsze widać najlepszy BW oraz najlepszy LR.
    selected_seed = list(grouped[:top_n])
    requested_filters = []
    for flt in filters:
        u = str(flt).strip().upper()
        if u and u not in requested_filters:
            requested_filters.append(u)
    for flt in requested_filters:
        fam_best = next((c for c in grouped if c.filter_label.upper() == flt), None)
        if fam_best is not None:
            selected_seed.append(fam_best)

    selected: List[AcousticCandidate] = []
    seen_selected = set()
    for c in selected_seed:
        sig = (c.family_key, round(c.l_rel_ms, 3), round(c.r_rel_ms, 3), c.gain_mode)
        if sig in seen_selected:
            continue
        seen_selected.add(sig)
        selected.append(c)

    code_prefix = "DIAG" if diagnostic_only else "AKU"
    for i, c in enumerate(selected, start=1):
        c.rank = i
        c.code = (
            f"{code_prefix}_{i:02d}_{int(round(c.effective_xo_hz))}_{c.filter_label}_"
            f"M{int(round(c.main_fc_hz))}_S{int(round(c.sub_fc_hz))}_"
            f"L{c.l_rel_ms:+.1f}_R{c.r_rel_ms:+.1f}"
        ).replace("+", "p").replace("-", "m").replace(".", "p")
    gain_comparison: List[AcousticCandidate] = []
    if selected:
        b = selected[0]
        for gm in ["fixed", "suggested", "applied"]:
            gc = _simulate_candidate(
                freq, lm_raw, rm_raw, ls_raw, rs_raw, fs,
                b.xo_hz, b.filter_label, b.main_fc_hz, b.sub_fc_hz,
                b.l_rel_ms, b.r_rel_ms, gain_mode=gm,
            )
            gc.rank = b.rank
            gc.code = b.code + "__" + gm
            gain_comparison.append(gc)

    input_integrity_ok = not diagnostic_only and bool(project.phase_metrics_allowed)
    safety_results: List[CandidateSafetyResult] = []
    safe_selected: List[AcousticCandidate] = []
    for candidate in selected:
        candidate.global_gain_l_db = float(applied_l_db)
        candidate.global_gain_r_db = float(applied_r_db)
        candidate.final_gain_l_db = float(applied_l_db + candidate.l_sub_gain_db)
        candidate.final_gain_r_db = float(applied_r_db + candidate.r_sub_gain_db)
        candidate.gain_state_name = f"RAW{float(raw_sub_trim_db):+.1f}_DELTA{float(gain_bridge_target_delta_db):+.1f}"

        required_state_results: Dict[str, CandidateSafetyResult] = {}
        candidate.state_metrics = {}
        for state_name in ("raw", "dirac", "listening"):
            try:
                state_ls, state_rs = gain_state.scale_complex_subs(ls_raw_measured, rs_raw_measured, state_name)
            except ValueError:
                continue
            state_candidate = _simulate_candidate(
                freq, lm_raw, rm_raw, state_ls, state_rs, fs,
                candidate.xo_hz, candidate.filter_label,
                candidate.main_fc_hz, candidate.sub_fc_hz,
                candidate.l_rel_ms, candidate.r_rel_ms,
                gain_mode=candidate.gain_mode,
            )
            global_l = gain_state.side_delta_db(state_name, "L")
            global_r = gain_state.side_delta_db(state_name, "R")
            state_gate = evaluate_candidate_safety(
                state_candidate,
                input_integrity_ok=input_integrity_ok,
                phase_integrity_ok=bool(project.phase_metrics_allowed),
                gain_state_verified=bool(gain_state.verified),
                gain_state_complete=bool(gain_state.complete),
                reference_anchor_verified=reference_anchor_verified,
                effective_xo_min_hz=float(effective_xo_min_hz),
                effective_xo_max_hz=float(effective_xo_max_hz),
            )
            candidate.state_metrics[state_name] = {
                "global_gain_l_db": global_l,
                "global_gain_r_db": global_r,
                "local_gain_l_db": state_candidate.l_sub_gain_db,
                "local_gain_r_db": state_candidate.r_sub_gain_db,
                "final_gain_l_db": global_l + state_candidate.l_sub_gain_db,
                "final_gain_r_db": global_r + state_candidate.r_sub_gain_db,
                "effective_xo_hz": state_candidate.effective_xo_hz,
                "side_min": state_candidate.side_min,
                "body_score": state_candidate.body_score,
                "lr_sum_score": state_candidate.lr_sum_score,
                "decision_class": state_candidate.decision_class,
                "safe_to_export": state_gate.candidate_safe_to_export,
                "reasons": list(state_gate.reasons),
            }
            if state_name in {"dirac", "listening"}:
                required_state_results[state_name] = state_gate

        base_gate = evaluate_candidate_safety(
            candidate,
            input_integrity_ok=input_integrity_ok,
            phase_integrity_ok=bool(project.phase_metrics_allowed),
            gain_state_verified=bool(gain_state.verified),
            gain_state_complete=bool(gain_state.complete),
            reference_anchor_verified=reference_anchor_verified,
            effective_xo_min_hz=float(effective_xo_min_hz),
            effective_xo_max_hz=float(effective_xo_max_hz),
        )
        combined_gate = combine_required_state_safety(base_gate, required_state_results)
        candidate.candidate_safe_to_export = combined_gate.candidate_safe_to_export
        candidate.safety_reasons = combined_gate.reasons
        safety_results.append(combined_gate)
        if combined_gate.candidate_safe_to_export:
            safe_selected.append(candidate)

    write_reports(out_dir, selected, rows, fs, mdat_path, project.role_to_name, input_audit, gain_comparison, shd_anchor_main=shd_anchor_main, diagnostic_only=diagnostic_only)
    if not input_integrity_ok:
        write_diagnostic_guard(out_dir, selected, input_audit)
        write_quarantine(out_dir, selected, safety_results, mode="STANDARD_CROSSOVER_INPUT_BLOCK")
        raise InputIntegrityError("Input Integrity Gate nie przeszedł; eksport został zablokowany.")
    if not safe_selected:
        write_quarantine(out_dir, selected, safety_results, mode="STANDARD_CROSSOVER_NO_SAFE_CANDIDATE")
        raise NoSafeCandidateError("Analiza zakończona: brak kandydata przechodzącego Candidate Safety Gate.")
    write_biquad_exports(out_dir, safe_selected, fs, shd_anchor_main=shd_anchor_main)
    return selected


def write_biquad_exports(out_dir: Path, selected: List[AcousticCandidate], fs: float, shd_anchor_main: float = 30.0) -> None:
    for c in selected:
        vdir = out_dir / c.code
        vdir.mkdir(parents=True, exist_ok=True)
        main_sections = make_sections("HP", fs, c.main_fc_hz, c.filter_label)
        sub_sections = make_sections("LP", fs, c.sub_fc_hz, c.filter_label)
        write_minidsp_biquads(vdir / "MAIN_C1_HPF_MINIDSP_BIQUADS.txt", main_sections, f"{c.code} — MAIN/C1 HPF {c.main_fc_hz:g} Hz {c.filter_label}")
        write_minidsp_biquads(vdir / "SUB_REL_LPF_MINIDSP_BIQUADS.txt", sub_sections, f"{c.code} — SUB/REL LPF {c.sub_fc_hz:g} Hz {c.filter_label}")
        write_biquad_csv(vdir / "MAIN_C1_HPF_coefficients.csv", main_sections, fs, c.main_fc_hz, c.filter_label)
        write_biquad_csv(vdir / "SUB_REL_LPF_coefficients.csv", sub_sections, fs, c.sub_fc_hz, c.filter_label)
        a_ml, a_mr, a_sl, a_sr, a_warn = _map_relative_to_shd_main_anchor(c.l_rel_ms, c.r_rel_ms, shd_anchor_main)
        readme = f"""# {c.code}

## Co to jest

Wariant zwrotnicy wygenerowany z rzeczywistego pomiaru RAW C1 + REL. To nie jest zwykły filtr z listy, tylko wynik symulacji akustycznej na realnym SPL/fazie.

## Parametry filtra

- XO elektryczny środek: **{c.xo_hz:.1f} Hz**
- XO efektywny z pomiaru: **{c.effective_xo_hz:.1f} Hz**
- Typ filtra: **{c.filter_label}**
- Main/C1 HPF: **{c.main_fc_hz:.1f} Hz**
- Sub/REL LPF: **{c.sub_fc_hz:.1f} Hz**
- Relacja sub-main L/R: **{c.l_rel_ms:.2f} / {c.r_rel_ms:.2f} ms**

## Wartości delay do SHD

- L main: **{c.shd_main_l_ms:.2f} ms**
- R main: **{c.shd_main_r_ms:.2f} ms**
- L sub: **{c.shd_sub_l_ms:.2f} ms**
- R sub: **{c.shd_sub_r_ms:.2f} ms**

### Alternatywny zapis z main = {shd_anchor_main:.2f} ms

- L main: **{a_ml:.2f} ms**
- R main: **{a_mr:.2f} ms**
- L sub: **{a_sl:.2f} ms**
- R sub: **{a_sr:.2f} ms**
- Ostrzeżenie wariantu main-anchor: **{a_warn or "brak"}**

- Polaryzacja: **OFF/OFF**
- Tryb gainu w symulacji: **{c.gain_mode}**
- Globalny gain stanu L/R: **{c.global_gain_l_db:+.2f}/{c.global_gain_r_db:+.2f} dB**
- Lokalna korekta XO L/R: **{c.l_sub_gain_db:+.2f}/{c.r_sub_gain_db:+.2f} dB**
- Końcowy efektywny gain L/R: **{c.final_gain_l_db:+.2f}/{c.final_gain_r_db:+.2f} dB**
- Lokalny mismatch magnitudy przy XO: **L {c.l_suggested_gain_db:.1f} dB, R {c.r_suggested_gain_db:.1f} dB**
- **Nie stosuj tej wartości automatycznie jako globalnego gainu subów.**
- Ostrzeżenie gainu: **{c.gain_limit_warning or "brak"}**

## Format biquadów miniDSP SHD

Pliki `*_MINIDSP_BIQUADS.txt` są w v20.17 zapisane bezpośrednio pod pole `Crossover > Advanced Mode > Biquad Programming` w miniDSP SHD Studio: bez komentarzy, bez notacji naukowej i z poprawionym znakiem `a1/a2`. Po `Process and Apply` wykres musi pokazać HPF dla C1 i LPF dla REL-i.

## Strategia basu Diraca

- Strategia: **{c.dirac_bass_strategy}**
- Sub vs main 25–60 Hz: **{c.low_bass_sub_delta_db:+.1f} dB**
- Sub vs main okolica XO: **{c.xo_sub_delta_db:+.1f} dB**
- Suma 125–220 vs 75–125 Hz: **{c.body_sub_delta_db:+.1f} dB**
- Komentarz: {c.dirac_bass_note}

## Wynik symulacji akustycznej

- Ocena akustyczna: **{c.acoustic_score:.1f}/100** — {c.label}
- Klasa decyzji: **{c.decision_class}**
- Rekomendacja: {c.recommendation}
- Sklejenie stron — SYMULACJA PROJEKTU: **{c.l_side_score:.1f}/{c.r_side_score:.1f}**, min **{c.side_min:.1f}**
- Jakość sumy stereo L+P — SYMULACJA PROJEKTU: **{c.lr_sum_score:.1f}/100**
- Ciągłość korpusu 100–220 Hz — SYMULACJA PROJEKTU: **{c.body_score:.1f}/100**
- Autorytet 40–80 Hz: **{c.authority_score:.1f}/100**
- Kopnięcie 60–100 Hz: **{c.kick_score:.1f}/100**
- Symulowane ryzyko dziury L+P — SYMULACJA PROJEKTU: **{c.null_risk:.1f}/100**
- Symulowane obciążenie korekcji Dirac — SYMULACJA PROJEKTU: **{c.dirac_burden:.1f}/100**

## Jak testować

1. Nie traktuj wariantu jako finalnego, jeżeli klasa decyzji to `DIAGNOSTYKA`.
2. Wgraj biquady HPF/LPF do miniDSP tylko jako wariant testowy.
3. Ustaw delaye powyżej.
4. Lokalny mismatch przy XO traktuj jako diagnostykę. Nie przenoś go automatycznie na globalny gain REL-i.
5. Uruchom Dirac.
6. Zmierz POST-DIRAC L+P w posL / MLP / posP.
7. Odsłuchaj wokal, fortepian, kontrabas i stabilność sceny.
"""
        (vdir / "README_WARIANTU_AKUSTYCZNEGO.md").write_text(readme, encoding="utf-8")


def write_diagnostic_guard(out_dir: Path, selected: List[AcousticCandidate], input_audit: Tuple[str, str, str]) -> None:
    """Tworzy jawny marker, że w trybie diagnostycznym nie wyeksportowano biquadów do wgrania."""
    status, decision, note = input_audit
    guard_dir = out_dir / "DIAGNOSTYKA_NIE_WGRYWAC"
    guard_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# DIAGNOSTYKA — NIE WGRYWAĆ DO miniDSP",
        "",
        "Tryb 8 policzył warianty tylko diagnostycznie. Eksport biquadów został celowo zablokowany, bo plik wejściowy nie wygląda na czysty RAW XOVER_OFF REL120 DELAY0.",
        "",
        "## Audyt wejścia",
        "",
        f"- Status: **{status}**",
        f"- Decyzja: **{decision}**",
        f"- Komentarz: {note}",
        "",
        "## Co zrobić zamiast wgrywania tych wyników",
        "",
        "1. Wykonaj czysty RAW: Dirac OFF, crossover OFF, PEQ OFF, delay 0.",
        "2. Zmierz osobno L main, R main, L sub, R sub.",
        "3. REL ustaw wysoko / około 120 Hz, żeby pomiar obejmował realny zapas subów.",
        "4. Dopiero taki plik podaj do trybu 8 jako projektowanie zwrotnicy.",
        "",
        "## Warianty policzone tylko informacyjnie",
        "",
    ]
    for c in selected:
        lines.append(f"- `{c.code}` — efektywne XO {c.effective_xo_hz:.1f} Hz, {c.filter_label}, wynik selektora — SYMULACJA PROJEKTU {c.acoustic_score:.1f}/100, klasa {c.decision_class}")
    (guard_dir / "README_DIAGNOSTYKA_NIE_WGRYWAC.md").write_text("\n".join(lines) + "\n", encoding="utf-8")




def write_reports(out_dir: Path, selected: List[AcousticCandidate], all_rows: List[AcousticCandidate], fs: float, mdat_path: Path, roles: Dict[str, str], input_audit: Tuple[str, str, str], gain_comparison: List[AcousticCandidate], shd_anchor_main: float = 30.0, diagnostic_only: bool = False) -> None:
    def row_dict(c: AcousticCandidate) -> Dict[str, object]:
        return {
            "rank": c.rank, "code": c.code, "electrical_center_hz": c.xo_hz, "effective_xo_hz": c.effective_xo_hz, "filter": c.filter_label,
            "main_hpf_hz": c.main_fc_hz, "sub_lpf_hz": c.sub_fc_hz,
            "l_rel_sub_main_ms": c.l_rel_ms, "r_rel_sub_main_ms": c.r_rel_ms,
            "acoustic_score_0_100": c.acoustic_score, "label": c.label,
            "l_side_score_0_100": c.l_side_score, "r_side_score_0_100": c.r_side_score,
            "side_min_0_100": c.side_min, "lr_sum_score_0_100": c.lr_sum_score,
            "body_continuity_score_0_100": c.body_score, "authority_40_80_score_0_100": c.authority_score,
            "kick_60_100_score_0_100": c.kick_score, "phase_energy_score_0_100": c.phase_energy_score,
            "null_risk_0_100": c.null_risk, "dirac_burden_0_100": c.dirac_burden,
            "shd_l_main_ms": c.shd_main_l_ms, "shd_r_main_ms": c.shd_main_r_ms,
            "shd_l_sub_ms": c.shd_sub_l_ms, "shd_r_sub_ms": c.shd_sub_r_ms,
            "shd_main_anchor_ms": shd_anchor_main,
            "shd_anchor_l_main_ms": _map_relative_to_shd_main_anchor(c.l_rel_ms, c.r_rel_ms, shd_anchor_main)[0],
            "shd_anchor_r_main_ms": _map_relative_to_shd_main_anchor(c.l_rel_ms, c.r_rel_ms, shd_anchor_main)[1],
            "shd_anchor_l_sub_ms": _map_relative_to_shd_main_anchor(c.l_rel_ms, c.r_rel_ms, shd_anchor_main)[2],
            "shd_anchor_r_sub_ms": _map_relative_to_shd_main_anchor(c.l_rel_ms, c.r_rel_ms, shd_anchor_main)[3],
            "shd_anchor_warning": _map_relative_to_shd_main_anchor(c.l_rel_ms, c.r_rel_ms, shd_anchor_main)[4],
            "l_sub_gain_db": c.l_sub_gain_db, "r_sub_gain_db": c.r_sub_gain_db,
            "global_gain_l_db": c.global_gain_l_db, "global_gain_r_db": c.global_gain_r_db,
            "local_gain_l_db": c.l_sub_gain_db, "local_gain_r_db": c.r_sub_gain_db,
            "final_gain_l_db": c.final_gain_l_db, "final_gain_r_db": c.final_gain_r_db,
            "gain_state_name": c.gain_state_name,
            "candidate_safe_to_export": c.candidate_safe_to_export,
            "candidate_safety_reasons": " | ".join(c.safety_reasons),
            "gain_state_metrics_json": json.dumps(c.state_metrics, ensure_ascii=False, sort_keys=True),
            "l_local_xo_mismatch_db": c.l_suggested_gain_db, "r_local_xo_mismatch_db": c.r_suggested_gain_db,
            "gain_mode": c.gain_mode, "gain_limit_warning": c.gain_limit_warning,
            "low_bass_sub_delta_db": c.low_bass_sub_delta_db,
            "xo_sub_delta_db": c.xo_sub_delta_db,
            "sum_body_125_220_vs_75_125_db": c.body_sub_delta_db,
            "dirac_bass_strategy": c.dirac_bass_strategy,
            "dirac_bass_note": c.dirac_bass_note,
            "decision_class": c.decision_class, "recommendation": c.recommendation,
            "family_key": c.family_key, "reason": c.reason,
        }

    def _display_code(c: AcousticCandidate) -> str:
        if c.code:
            return c.code
        return (
            f"effXO {c.effective_xo_hz:.1f} Hz {c.filter_label} "
            f"M{c.main_fc_hz:.0f}/S{c.sub_fc_hz:.0f} "
            f"L{c.l_rel_ms:.1f}/R{c.r_rel_ms:.1f}"
        )

    def _family_tops(rows: List[AcousticCandidate]) -> List[AcousticCandidate]:
        best_by_filter: Dict[str, AcousticCandidate] = {}
        for c in sorted(rows, key=lambda x: x.acoustic_score, reverse=True):
            key = c.filter_label.upper()
            if key not in best_by_filter:
                best_by_filter[key] = c
        order = {"BW48": 0, "LR48": 1, "BW24": 2, "LR24": 3}
        return sorted(best_by_filter.values(), key=lambda c: (order.get(c.filter_label.upper(), 99), c.filter_label.upper()))

    def _family_delta_note(fams: List[AcousticCandidate]) -> List[str]:
        if len(fams) < 2:
            return ["Dostępna jest tylko jedna rodzina filtra, więc nie ma porównania BW/LR."]
        best_family = max(fams, key=lambda c: c.acoustic_score)
        if diagnostic_only or best_family.acoustic_score < 60 or all(c.decision_class == "DIAGNOSTYKA" for c in fams):
            return [
                "Brak wiarygodnego zwycięzcy rodzin filtrów: materiał wejściowy albo wyniki są diagnostyczne. Różnice punktowe pokazują tylko zachowanie symulacji, nie rekomendację do wgrania.",
                f"Najwyższy wynik wśród rodzin: {best_family.filter_label} {best_family.acoustic_score:.1f}/100 — to nadal nie przesądza o wyborze BW/LR.",
            ]
        lines = []
        for c in fams:
            delta = c.acoustic_score - best_family.acoustic_score
            if c is best_family:
                lines.append(f"{c.filter_label}: najlepsza rodzina w tej symulacji, wynik selektora — SYMULACJA PROJEKTU {c.acoustic_score:.1f}/100.")
            else:
                if abs(delta) < 5.0:
                    lines.append(f"{c.filter_label}: tylko {abs(delta):.1f} pkt za {best_family.filter_label}; praktycznie remis, decyzja wymaga POST-DIRAC i odsłuchu.")
                else:
                    lines.append(f"{c.filter_label}: {abs(delta):.1f} pkt za {best_family.filter_label}; nie oznacza odrzucenia, tylko słabszy wynik projektanta w tym RAW.")
        return lines

    family_tops = _family_tops(all_rows)

    fieldnames = list(row_dict(selected[0] if selected else all_rows[0]).keys())
    with (out_dir / "00_TOP_WARIANTY_AKUSTYCZNE.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in selected:
            w.writerow(row_dict(c))
    with (out_dir / "99_WSZYSTKIE_KANDYDATY_AKUSTYCZNE.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in all_rows:
            w.writerow(row_dict(c))

    with (out_dir / "05_POROWNANIE_RODZIN_FILTROW.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in family_tops:
            w.writerow(row_dict(c))

    audit_status, audit_decision, audit_note = input_audit
    lines = [
        "# 01 — Co teraz zrobić: akustyczny selektor zwrotnicy",
        "",
        f"Wersja: **{APP_VERSION}**",
        "",
        "## Audyt pliku wejściowego",
        "",
        f"- Status pliku: **{audit_status}**",
        f"- Decyzja wejścia: **{audit_decision}**",
        f"- Komentarz: {audit_note}",
        "",
        "## Najkrócej",
        "",
        "To jest tryb selekcji akustycznej zwrotnicy z pomiaru RAW. Aplikacja bierze realne pomiary `L main`, `R main`, `L sub`, `R sub`, symuluje filtry HPF/LPF na rzeczywistym SPL/fazie i dopiero z tego wybiera warianty do miniDSP.",
        "",
        "Moduł wybiera standardowe kaskady BW/LR i ich pary HPF/LPF na podstawie realnego RAW. Nie optymalizuje dowolnych Q ani all-passów; finalna decyzja wymaga Diraca, POST-DIRAC i odsłuchu.",
        "",
        "## Werdykt ogólny",
        "",
    ]
    best = selected[0] if selected else None
    if "NIE PROJEKTUJ" in audit_decision or "TRYBU 2" in audit_decision:
        lines += [
            "**NIE UŻYWAJ TEGO PLIKU DO PROJEKTOWANIA ZWROTNICY.**",
            "",
            "Plik wejściowy nie wygląda na czysty RAW main/sub. Wygenerowane warianty traktuj wyłącznie jako diagnostykę parsera lub istniejącej konfiguracji.",
            "",
        ]
    elif "TYLKO DIAGNOSTYKA" in audit_decision:
        lines += [
            "**TRYB DIAGNOSTYCZNY — PLIK WYGLĄDA NA POMIAR ISTNIEJĄCEJ ZWROTNICY.**",
            "",
            "Nie projektuj docelowego nowego filtra na materiale już przefiltrowanym. Do projektowania użyj RAW XOVER_OFF REL120 DELAY0.",
            "",
        ]

    if best and best.acoustic_score < 60:
        lines += [
            "**BRAK MOCNEGO KANDYDATA AKUSTYCZNEGO.**",
            "",
            "Najlepszy wynik jest poniżej 60/100, więc top warianty traktuj jako diagnostykę, a nie jako gotową rekomendację filtra. Najpierw sprawdź poziom REL-i, prawą/lewą stronę integracji i dopiero potem testuj wybrane biquady po Diracu.",
            "",
        ]
    elif best:
        lines += [
            f"Najlepszy wariant: **{best.code}** — {best.acoustic_score:.1f}/100, klasa **{best.decision_class}**.",
            "",
        ]
    lines += [
        "## Porównanie rodzin filtrów BW/LR",
        "",
        "Globalny TOP pokazuje najlepsze warianty ogólnie, a aplikacja osobno aplikacja osobno pokazuje też najlepszego przedstawiciela każdej rodziny filtra. To zapobiega sytuacji, w której np. LR48 wypycha BW48 z tabeli i wygląda, jakby BW w ogóle nie był liczony.",
        "",
        "| Rodzina | Najlepszy wariant | Wynik selektora — SYMULACJA PROJEKTU | Klasa | Efektywne XO | Sklejenie stron — SYMULACJA PROJEKTU | Jakość sumy L+P — SYMULACJA PROJEKTU | Ciągłość korpusu — SYMULACJA PROJEKTU | Autorytet | Impuls | Symulowane ryzyko dziury L+P | Symulowane obciążenie Dirac | Różnica do lidera |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if family_tops:
        leader = max(family_tops, key=lambda c: c.acoustic_score)
        for fc in family_tops:
            delta = fc.acoustic_score - leader.acoustic_score
            lines.append(
                f"| {fc.filter_label} | `{_display_code(fc)}` | {fc.acoustic_score:.1f} | {fc.decision_class} | {fc.effective_xo_hz:.1f} | {fc.l_side_score:.1f}/{fc.r_side_score:.1f} | {fc.lr_sum_score:.1f} | {fc.body_score:.1f} | {fc.authority_score:.1f} | {fc.kick_score:.1f} | {fc.null_risk:.1f} | {fc.dirac_burden:.1f} | {delta:+.1f} |"
            )
        lines += ["", *_family_delta_note(family_tops), ""]

    lines += [
        "## Co testować jako pierwsze",
        "",
    ]
    for c in selected[:4]:
        lines.append(f"{c.rank}. **{c.code}** — efektywne XO {c.effective_xo_hz:.1f} Hz, {c.filter_label}, main {c.main_fc_hz:.1f} Hz / sub {c.sub_fc_hz:.1f} Hz, wynik selektora — SYMULACJA PROJEKTU {c.acoustic_score:.1f}/100 — **{c.decision_class}**. {c.recommendation}")
    if gain_comparison:
        lines += [
            "",
            "## Porównanie trybów gainu dla najlepszego wariantu",
            "",
            "| Tryb poziomu | Wynik selektora — SYMULACJA PROJEKTU | Klasa | Sklejenie stron — SYMULACJA PROJEKTU | Jakość sumy L+P — SYMULACJA PROJEKTU | Ciągłość korpusu — SYMULACJA PROJEKTU | Symulowane ryzyko dziury L+P | Symulowane obciążenie Dirac | Sub 25–60 / XO / suma korpusu | Strategia | Niedopasowanie XO L/R | Komentarz |",
            "|---|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---|",
        ]
        for g in gain_comparison:
            lines.append(
                f"| {g.gain_mode} | {g.acoustic_score:.1f} | {g.decision_class} | {g.l_side_score:.1f}/{g.r_side_score:.1f} | {g.lr_sum_score:.1f} | {g.body_score:.1f} | {g.null_risk:.1f} | {g.dirac_burden:.1f} | {g.low_bass_sub_delta_db:+.1f}/{g.xo_sub_delta_db:+.1f}/{g.body_sub_delta_db:+.1f} | {g.dirac_bass_strategy} | {g.l_suggested_gain_db:.1f}/{g.r_suggested_gain_db:.1f} | {g.gain_limit_warning or 'brak'} |"
            )
        lines += [
            "",
            "Jak czytać: `suggested` pokazuje wyłącznie lokalne niedopasowanie poziomu sub/main przy efektywnym XO i nie stosuje go w symulacji. `applied` stosuje tę różnicę testowo. Nie traktuj tej wartości jako zalecanego globalnego poziomu REL-i. O wyniku decydują też suma fazowa, 25–60 Hz, impuls 60–100 Hz i ciągłość 125–220 Hz.",
        ]

    if best:
        lines += [
            "",
            "## Strategia basu Diraca — nie chodzi o samo ściszenie subów",
            "",
            f"- Ocena: **{best.dirac_bass_strategy}**",
            f"- Sub vs main 25–60 Hz: **{best.low_bass_sub_delta_db:+.1f} dB**",
            f"- Sub vs main okolica XO: **{best.xo_sub_delta_db:+.1f} dB**",
            f"- Suma 125–220 vs 75–125 Hz: **{best.body_sub_delta_db:+.1f} dB**",
            f"- Komentarz: {best.dirac_bass_note}",
            "",
            "Interpretacja: wyższy poziom subów może być korzystny, jeżeli daje głównie zapas niskiego basu do cięcia przez Diraca. Jest ryzykowny, jeżeli lokalna dominacja pojawia się przy efektywnym XO; ciągłość 125–220 Hz oceniamy już z całkowitej sumy, nie z resztkowego suba po LPF.",
        ]

    lines += [
        "",
        "## Kalibracja muzyczna",
        "",
        "- 90 BW48 jest referencją **barwy, tekstury głosu i instrumentów akustycznych**, nie wzorcem ciężaru 25–60 Hz.",
        "- Kandydat może mieć lżejszy i szybszy bas niż 90 BW48, o ile nie traci ciągłości 100–220 Hz, kopnięcia 60–100 Hz i naturalności po Diracu.",
        "- Lokalny mismatch sub/main przy XO nie jest poleceniem globalnego ściszenia subwooferów.",
        "",
        "## Workflow",
        "",
        "1. Jeżeli najlepszy wynik jest poniżej 60/100, nie traktuj top 1 jako rekomendacji — to diagnostyka.",
        "2. Porównaj tryby gainu: `suggested` pokazuje lokalny mismatch przy XO bez stosowania; `applied` stosuje go tylko jako test; `fixed` nie wyrównuje magnitudy. Nie przepisuj mismatchu jako globalnego poziomu subów.",
        "3. Jeżeli lokalne niedopasowanie poziomu przy XO dochodzi do limitu, najpierw sprawdź realny gain REL-i albo poziom wyjść SHD.",
        "4. Wybierz maksymalnie 2–3 różne rodziny wariantów, nie kilka prawie identycznych topów.",
        "5. Wgraj biquady HPF/LPF z folderu danego wariantu jako test, nie finalną decyzję.",
        "6. Wpisz delaye SHD podane w `README_WARIANTU_AKUSTYCZNEGO.md`.",
        "7. Uruchom Dirac.",
        "8. Zmierz POST-DIRAC L+P w `posL`, `MLP`, `posP`.",
        "9. Dopiero potem oceń odsłuchowo.",
        "",
        "## Ważne ograniczenie",
        "",
        "To jest akustyczny selektor standardowych kaskad IIR BW48/LR48 z doborem częstotliwości i delayów. Nie projektuje dowolnych Q, all-passów ani krzywej docelowej Dirac. Wybiera kandydatów na podstawie realnego pomiaru akustycznego.",
        "",
    ]
    if diagnostic_only:
        cleaned: List[str] = []
        for line in lines:
            if line == "To jest tryb selekcji akustycznej zwrotnicy z pomiaru RAW. Aplikacja bierze realne pomiary `L main`, `R main`, `L sub`, `R sub`, symuluje filtry HPF/LPF na rzeczywistym SPL/fazie i dopiero z tego wybiera warianty do miniDSP.":
                line = "To jest **tryb diagnostyczny**, bo audyt wejścia nie potwierdził czystego RAW do projektowania. Aplikacja policzyła warianty informacyjnie, ale nie wolno traktować ich jako gotowej zwrotnicy."
            elif line == "Moduł wybiera standardowe kaskady BW/LR i ich pary HPF/LPF na podstawie realnego RAW. Nie optymalizuje dowolnych Q ani all-passów; finalna decyzja wymaga Diraca, POST-DIRAC i odsłuchu.":
                line = "Eksport biquadów został zablokowany. W folderze wynikowym znajdziesz tylko raporty i marker `DIAGNOSTYKA_NIE_WGRYWAC`."
            elif line == "## Co testować jako pierwsze":
                line = "## Warianty policzone diagnostycznie — NIE WGRYWAĆ"
            elif "Wgraj biquady HPF/LPF z folderu danego wariantu jako test" in line:
                line = "5. Nie wgrywaj biquadów z tego przebiegu. Najpierw wykonaj czysty RAW XOVER_OFF REL120 DELAY0."
            elif line == "6. Wpisz delaye SHD podane w `README_WARIANTU_AKUSTYCZNEGO.md`.":
                line = "6. Delaye traktuj tylko jako informację diagnostyczną; brak folderów wariantów do importu."
            elif line == "7. Uruchom Dirac.":
                line = "7. Dirac uruchom dopiero po zbudowaniu wariantu z czystego RAW."
            cleaned.append(line)
        lines = cleaned
        lines.insert(0, "**UWAGA: DIAGNOSTYKA — NIE WGRYWAĆ DO miniDSP.**")
        lines.insert(1, "")
    (out_dir / "01_CO_TERAZ_ZROBIC_AKUSTYCZNA_ZWROTNICA.md").write_text("\n".join(lines), encoding="utf-8")

    audit_md = [
        "# 04 — Audyt pliku wejściowego i gainu subów",
        "",
        f"Plik: `{mdat_path.name}`",
        "",
        "## Audyt wejścia",
        "",
        f"- Status: **{audit_status}**",
        f"- Decyzja: **{audit_decision}**",
        f"- Komentarz: {audit_note}",
        "",
        "## Rozpoznane role",
        "",
        "| Rola | Nazwa w MDAT |",
        "|---|---|",
    ]
    for role in ["L main", "R main", "L sub", "R sub"]:
        audit_md.append(f"| {role} | `{roles.get(role, '')}` |")
    if gain_comparison:
        audit_md += [
            "",
            "## Porównanie gainu dla najlepszego wariantu",
            "",
            "| Tryb | Wynik | Klasa | Dodatkowy gain XO L/R | Mismatch XO L/R | Strategia basu | Sub 25–60/XO/suma body | Ostrzeżenie |",
            "|---|---:|---|---:|---:|---|---:|---|",
        ]
        for g in gain_comparison:
            audit_md.append(f"| {g.gain_mode} | {g.acoustic_score:.1f} | {g.decision_class} | {g.l_sub_gain_db:.1f}/{g.r_sub_gain_db:.1f} | {g.l_suggested_gain_db:.1f}/{g.r_suggested_gain_db:.1f} | {g.dirac_bass_strategy} | {g.low_bass_sub_delta_db:+.1f}/{g.xo_sub_delta_db:+.1f}/{g.body_sub_delta_db:+.1f} | {g.gain_limit_warning or 'brak'} |")
        audit_md += [
            "",
            "## Wniosek praktyczny",
            "",
            "Tryb `applied` jest wyłącznie eksperymentem diagnostycznym. Duży lokalny mismatch przy XO nie oznacza automatycznie, że suby mają grać ciszej globalnie. Najpierw sprawdź 25–60 Hz, sumę fazową przy XO, target Diraca i odsłuch.",
        ]
    (out_dir / "04_AUDYT_PLIKU_I_GAINU.md").write_text("\n".join(audit_md), encoding="utf-8")

    fam_md = [
        "# 05 — Porównanie rodzin filtrów BW/LR",
        "",
        "Ten raport pokazuje najlepszego przedstawiciela każdej rodziny filtra liczonej w akustycznym selektorze zwrotnicy. Jest uzupełnieniem globalnego TOP, a nie osobnym rankingiem odsłuchowym.",
        "",
        "## Tabela rodzin",
        "",
        "| Rodzina | Najlepszy wariant | Wynik selektora — SYMULACJA PROJEKTU | Klasa | HPF main | LPF sub | Efektywne XO | Rel. L/R | Sklejenie stron — SYMULACJA PROJEKTU | Jakość sumy L+P — SYMULACJA PROJEKTU | Ciągłość korpusu — SYMULACJA PROJEKTU | Autorytet | Kick | Symulowane ryzyko dziury L+P | Symulowane obciążenie Dirac | Mismatch XO | Strategia basu |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    if family_tops:
        leader = max(family_tops, key=lambda c: c.acoustic_score)
        for fc in family_tops:
            fam_md.append(
                f"| {fc.filter_label} | `{_display_code(fc)}` | {fc.acoustic_score:.1f} | {fc.decision_class} | {fc.main_fc_hz:.1f} | {fc.sub_fc_hz:.1f} | {fc.effective_xo_hz:.1f} | {fc.l_rel_ms:.1f}/{fc.r_rel_ms:.1f} | {fc.l_side_score:.1f}/{fc.r_side_score:.1f} | {fc.lr_sum_score:.1f} | {fc.body_score:.1f} | {fc.authority_score:.1f} | {fc.kick_score:.1f} | {fc.null_risk:.1f} | {fc.dirac_burden:.1f} | {fc.l_suggested_gain_db:.1f}/{fc.r_suggested_gain_db:.1f} | {fc.dirac_bass_strategy} |"
            )
        fam_md += [
            "",
            "## Różnice punktowe",
            "",
            *_family_delta_note(family_tops),
            "",
            "## Jak czytać",
            "",
            "- Jeżeli globalny TOP zawiera tylko LR48, to nie oznacza, że BW48 nie był liczony. Oznacza tylko, że najlepsze warianty LR48 miały wyższy wynik projektanta w tej konkretnej symulacji.",
            "- Jeżeli najlepszy BW48 ma klasę `DIAGNOSTYKA`, nie traktuj go jako gotowego filtra; ale nadal możesz go porównać jako rodzinę bliską Twoim preferencjom i wcześniejszym odsłuchom.",
            "- `Wynik selektora standardowych filtrów — SYMULACJA PROJEKTU` jest lokalną skalą trybu 8. Nie porównuj go 1:1 z innymi wynikami głównymi.",
            "- Różnica kilku punktów między rodzinami jest słaba; różnica kilkunastu punktów jest już sygnałem, że przy tym RAW jedna rodzina lepiej radzi sobie z integracją/gainem.",
        ]
    (out_dir / "05_POROWNANIE_RODZIN_FILTROW.md").write_text("\n".join(fam_md), encoding="utf-8")

    table = [
        "# 02 — Top warianty akustycznej zwrotnicy",
        "",
        f"Plik RAW: `{mdat_path.name}`",
        "",
        "## Role pomiarów użyte do symulacji",
        "",
        "| Rola | Nazwa w MDAT |",
        "|---|---|",
    ]
    for role in ["L main", "R main", "L sub", "R sub"]:
        table.append(f"| {role} | `{roles.get(role, '')}` |")
    table += [
        "",
        "## Najlepsze warianty według rodzin filtrów",
        "",
        "Ta sekcja odpowiada na pytanie: gdzie jest najlepszy BW48 i najlepszy LR48. Globalna tabela niżej może nadal być zdominowana przez jedną rodzinę, ale nie ukrywa już drugiej.",
        "",
        "| Rodzina | Najlepszy wariant | Wynik selektora — SYMULACJA PROJEKTU | Klasa | HPF main | LPF sub | Efektywne XO | Sklejenie stron — SYMULACJA PROJEKTU | Jakość sumy L+P — SYMULACJA PROJEKTU | Ciągłość korpusu — SYMULACJA PROJEKTU | Autorytet | Kick | Różnica do lidera |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if family_tops:
        leader = max(family_tops, key=lambda c: c.acoustic_score)
        for fc in family_tops:
            delta = fc.acoustic_score - leader.acoustic_score
            table.append(
                f"| {fc.filter_label} | `{_display_code(fc)}` | {fc.acoustic_score:.1f} | {fc.decision_class} | {fc.main_fc_hz:.1f} | {fc.sub_fc_hz:.1f} | {fc.effective_xo_hz:.1f} | {fc.l_side_score:.1f}/{fc.r_side_score:.1f} | {fc.lr_sum_score:.1f} | {fc.body_score:.1f} | {fc.authority_score:.1f} | {fc.kick_score:.1f} | {delta:+.1f} |"
            )
        table += ["", "Pełny opis porównania rodzin zapisano w `05_POROWNANIE_RODZIN_FILTROW.md`."]

    table += [
        "",
        "## Tabela globalna TOP",
        "",
        "| # | Kod | Klasa | HPF main | LPF sub | Efektywne XO | Filtr | Rel. L/R | Dodatkowy gain XO | Mismatch XO | Strategia basu | Sub 25–60/XO/suma korpusu | Wynik selektora — SYMULACJA PROJEKTU | Sklejenie stron — SYMULACJA PROJEKTU | Jakość sumy L+P — SYMULACJA PROJEKTU | Ciągłość korpusu — SYMULACJA PROJEKTU | Autorytet | Kick | Symulowane ryzyko dziury L+P | Symulowane obciążenie Dirac |",
        "|---:|---|---|---:|---:|---:|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in selected:
        table.append(
            f"| {c.rank} | `{c.code}` | {c.decision_class} | {c.main_fc_hz:.1f} | {c.sub_fc_hz:.1f} | {c.effective_xo_hz:.1f} | {c.filter_label} | {c.l_rel_ms:.1f}/{c.r_rel_ms:.1f} | {c.l_sub_gain_db:.1f}/{c.r_sub_gain_db:.1f} | {c.l_suggested_gain_db:.1f}/{c.r_suggested_gain_db:.1f} | {c.dirac_bass_strategy} | {c.low_bass_sub_delta_db:+.1f}/{c.xo_sub_delta_db:+.1f}/{c.body_sub_delta_db:+.1f} | {c.acoustic_score:.1f} | {c.l_side_score:.1f}/{c.r_side_score:.1f} | {c.lr_sum_score:.1f} | {c.body_score:.1f} | {c.authority_score:.1f} | {c.kick_score:.1f} | {c.null_risk:.1f} | {c.dirac_burden:.1f} |"
        )
    table += [
        "",
        "## Jak czytać",
        "",
        "- **Wynik selektora standardowych filtrów — SYMULACJA PROJEKTU**: lokalna skala trybu 8. Nie porównuj jej 1:1 z wynikiem muzycznym rankingu RAW ani wynikiem projektanta biquadów.",
        "- **Sklejenie stron — SYMULACJA PROJEKTU**: wspólna skala v1; identyczny wzór jak w trybach 1/3/5/6.",
        "- **Jakość sumy stereo L+P — SYMULACJA PROJEKTU**: lokalna ocena sumy gotowych stron; nie jest metryką sklejenia main–sub.",
        "- **Ciągłość korpusu 100–220 Hz — SYMULACJA PROJEKTU**: lokalna ocena ciągłości niższej średnicy; nie jest skalą Podparcia dolnej średnicy z rankingu RAW.",
        "- **Symulowane ryzyko dziury L+P — SYMULACJA PROJEKTU**: mniej znaczy lepiej; nie jest rzeczywistym pomiarem po Dirac.",
        "- **Symulowane obciążenie korekcji Dirac — SYMULACJA PROJEKTU**: mniej znaczy lepiej; skala jest wspólna z trybem 9, lecz nie z predykcją RAW.",
        "- **Dodatkowy gain XO**: korekta lokalna ponad stan bazowy zapisany w `DESIGN_GAIN_STATE.md`. W trybie `suggested` i `fixed` będzie 0/0.",
        "- **Mismatch XO**: informacja, o ile suby musiałyby zostać skorygowane względem mainów wokół XO. Jeśli dochodzi do limitu, to sygnał do analizy, ale nie automatyczny nakaz ściszenia całego basu.",
        "- **Strategia basu**: rozdziela zapas 25–60 Hz od lokalnej dominacji przy efektywnym XO. Ciągłość 125–220 Hz pochodzi z całkowitej sumy.",
        "- **Sub 25–60 / niedopasowanie poziomu przy XO / suma korpusu**: pierwsza wartość to przewaga subów nad mainami w 25–60 Hz; druga to lokalna różnica magnitudy przy efektywnym XO; trzecia opisuje ciągłość pełnej sumy 125–220 względem 75–125 Hz. Tylko pierwsze dwie są relacją sub/main.",
        "- **Klasa**: `MOCNY KANDYDAT`, `KANDYDAT WARUNKOWY`, `OSTROŻNIE` albo `DIAGNOSTYKA`. Przy `DIAGNOSTYKA` nie wgrywaj jako finalny filtr.",
        "",
    ]
    if diagnostic_only:
        table = [
            "**UWAGA: DIAGNOSTYKA — NIE WGRYWAĆ DO miniDSP.**",
            "",
            "Ten ranking powstał z pliku, który audyt oznaczył jako nieodpowiedni do projektowania docelowej zwrotnicy. Eksport biquadów został zablokowany.",
            "",
        ] + table
    (out_dir / "02_TOP_WARIANTY_AKUSTYCZNE.md").write_text("\n".join(table), encoding="utf-8")

    import_lines = [
        "# 03 — Import do miniDSP SHD: warianty akustyczne",
        "",
        "Dla każdego wariantu użyj plików:",
        "",
        "- `MAIN_C1_HPF_MINIDSP_BIQUADS.txt` — HPF dla C1,",
        "- `SUB_REL_LPF_MINIDSP_BIQUADS.txt` — LPF dla REL-i.",
        "",
        "## Zasada",
        "",
        "Nie łącz tych biquadów z aktywnym standardowym crossoverem z listy, chyba że świadomie testujesz sumę filtrów. Najbezpieczniej: użyć biquadów jako właściwej zwrotnicy i po imporcie wykonać Dirac.",
        "",
        "**v20.17:** pliki `*_MINIDSP_BIQUADS.txt` są zapisane w konwencji miniDSP SHD Studio: odwrócony znak `a1/a2`, zapis dziesiętny bez `e-06`, bez komentarzy oraz z dopełnieniem do 8 sekcji. Poprzednie eksporty v20.15 mogły rysować w konsoli błędny HPF/LPF.",
        "",
        "Jeżeli wariant ma klasę `DIAGNOSTYKA` albo lokalny mismatch XO suba przy limicie, traktuj pliki biquadów jako materiał testowy. W v20.17 sprawdź dodatkowo strategię basu: zapas w 25–60 Hz może być celowy, ale dominacja suba przy XO lub zaburzona ciągłość sumy 125–220 Hz wymaga korekty poziomu, filtra albo targetu Diraca.",
        "",
    ]
    if diagnostic_only:
        import_lines = [
            "# 03 — Import do miniDSP SHD: zablokowany",
            "",
            "**DIAGNOSTYKA — NIE WGRYWAĆ DO miniDSP.**",
            "",
            "Plik wejściowy nie przeszedł audytu jako czysty RAW XOVER_OFF REL120 DELAY0, dlatego v20.23 celowo nie tworzy folderów z biquadami do importu.",
            "",
            "Do projektowania wykonaj nowy pomiar: Dirac OFF, crossover OFF, PEQ OFF, delay 0, osobno L main / R main / L sub / R sub.",
            "",
        ]
    (out_dir / "03_IMPORT_MINIDSP_SHD_AKUSTYCZNE.md").write_text("\n".join(import_lines), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="REWDO v20.31 — akustyczny selektor z Candidate Safety Gate")
    ap.add_argument("--mdat", required=True, help="RAW .mdat z pomiarami L_MAIN, L_SUB, R_MAIN/P_MAIN, R_SUB/P_SUB, najlepiej Dirac OFF")
    ap.add_argument("--out", required=True, help="Folder wyników")
    ap.add_argument("--fs", type=float, default=96000.0, help="Częstotliwość próbkowania biquadów")
    ap.add_argument("--xo-list", default="80,82,84,86,88,90,92,94,96", help="Lista XO do symulacji")
    ap.add_argument("--filters", default="BW48,LR48", help="Lista filtrów do symulacji")
    ap.add_argument("--l-rel", type=float, default=-9.6, help="Środek relacji L sub-main [ms]")
    ap.add_argument("--r-rel", type=float, default=-10.9, help="Środek relacji R sub-main [ms]")
    ap.add_argument("--rel-radius", type=float, default=1.5, help="Promień szukania wokół relacji referencji [ms]")
    ap.add_argument("--rel-step", type=float, default=0.5, help="Krok relacji delay [ms]")
    ap.add_argument("--top", type=int, default=8, help="Ile wariantów eksportować")
    ap.add_argument("--gain-mode", choices=["fixed", "suggested", "applied"], default="suggested",
                    help="Diagnostyka lokalnego mismatchu sub/main przy XO: fixed=bez wyrównania, suggested=tylko pokaż mismatch, applied=zastosuj mismatch testowo; nie jest to zalecany globalny gain subów")
    ap.add_argument("--shd-anchor-main", type=float, default=30.0,
                    help="Alternatywny czytelny zapis delayów: L/R main zakotwiczone na tej wartości, np. 30 ms")
    ap.add_argument("--user-reference", default=None,
                    help="Opcjonalny USER_REFERENCE_90BW48.yaml; z realnych delayów wyznacza centrum skanu relacji")
    ap.add_argument("--refine-radius", type=float, default=0.4, help="Promień dokładnego refine delayów [ms]")
    ap.add_argument("--refine-step", type=float, default=0.1, help="Krok dokładnego refine delayów [ms]")
    ap.add_argument("--gain-bridge", default=None,
                    help="Opcjonalny MDAT z L_SUM_SUB0 i R_SUM_SUB0; skaluje RAW -6 dB do realnego poziomu odsłuchu")
    ap.add_argument("--gain-bridge-target-delta-db", type=float, default=6.0,
                    help="Stan projektu względem RAW -6 dB: 2 = pomiar Diraca przy -4 dB, 6 = odsłuch 0 dB")
    ap.add_argument("--raw-sub-trim-db", type=float, default=-6.0, help="Rzeczywisty stan subów podczas RAW")
    ap.add_argument("--dirac-sub-trim-db", type=float, default=-4.0, help="Stan subów użyty podczas pomiaru Dirac")
    ap.add_argument("--listening-sub-trim-db", type=float, default=0.0, help="Stan subów podczas odsłuchu")
    ap.add_argument("--allow-unverified-gain-override", action="store_true",
                    help="Jawny override tylko diagnostyczny; nie odblokowuje eksportu miniDSP")
    ap.add_argument("--effective-xo-min", type=float, default=70.0, help="Dolny twardy limit akustycznego XO")
    ap.add_argument("--effective-xo-max", type=float, default=105.0, help="Górny twardy limit akustycznego XO")
    args = ap.parse_args(argv)
    print("REWDO", APP_VERSION)
    print("RAW:", args.mdat)
    print("Folder wyników:", args.out)
    def _progress(done: int, total: int, message: str) -> None:
        pct = (100.0 * float(done) / float(total)) if total else 100.0
        print(f"[postęp] {done}/{total} ({pct:.1f}%) — {message}", flush=True)

    try:
        selected = design_acoustic_crossovers(
            Path(args.mdat), Path(args.out), fs=float(args.fs),
            xo_values=_parse_xo_list(args.xo_list),
            filters=[x.strip().upper() for x in args.filters.split(",") if x.strip()],
            l_rel_center=float(args.l_rel), r_rel_center=float(args.r_rel),
            rel_radius=float(args.rel_radius), rel_step=float(args.rel_step),
            top_n=int(args.top), gain_mode=str(args.gain_mode),
            shd_anchor_main=float(args.shd_anchor_main),
            user_reference_path=Path(args.user_reference) if args.user_reference else None,
            refine_radius=float(args.refine_radius), refine_step=float(args.refine_step),
            gain_bridge_path=Path(args.gain_bridge) if args.gain_bridge else None,
            gain_bridge_target_delta_db=float(args.gain_bridge_target_delta_db),
            raw_sub_trim_db=float(args.raw_sub_trim_db),
            dirac_sub_trim_db=float(args.dirac_sub_trim_db),
            listening_sub_trim_db=float(args.listening_sub_trim_db),
            allow_unverified_gain_override=bool(args.allow_unverified_gain_override),
            effective_xo_min_hz=float(args.effective_xo_min),
            effective_xo_max_hz=float(args.effective_xo_max),
            progress_callback=_progress,
        )
    except NoSafeCandidateError as exc:
        print("")
        print("BRAK BEZPIECZNEGO KANDYDATA:", exc)
        print("Zapisano QUARANTINE_DIAGNOSTIC.json/csv i NIE_WGRYWAC_DO_MINIDSP.txt.")
        return 3
    except InputIntegrityError as exc:
        print("")
        print("BŁĄD INTEGRALNOŚCI WEJŚCIA:", exc)
        return 2
    except Exception as exc:
        print("")
        print("BŁĄD: nie można zaprojektować filtrów z tego pliku RAW.")
        print(str(exc))
        print("")
        print("Ten tryb wymaga jednego pliku .mdat z pomiarami RAW bez Dirac:")
        print("  - L_MAIN oraz L_SUB")
        print("  - R_MAIN/P_MAIN oraz R_SUB/P_SUB")
        print("")
        print("Przykładowe nazwy do testu filtrów:")
        print("  L_MAIN_RAW")
        print("  L_SUB_RAW")
        print("  R_MAIN_RAW")
        print("  R_SUB_RAW")
        print("")
        print("Jeżeli plik zawiera gotowe pomiary L+P po Diracu, użyj trybu 2 POST-DIRAC zamiast trybu 8.")
        return 2

    print("[OK] Wygenerowano warianty akustyczne:")
    for c in selected[:5]:
        print(f" {c.rank}. {c.code} | {c.decision_class} | wynik {c.acoustic_score:.1f} | {c.reason}")
    print("[OK] Główny plik: 01_CO_TERAZ_ZROBIC_AKUSTYCZNA_ZWROTNICA.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
