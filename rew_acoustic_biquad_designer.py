#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REWDO v20.31 — projektant biquadów z jawnym GainState i bramką eksportu.

Rzeczywisty projektant niestandardowych zwrotnic IIR dla miniDSP SHD Studio.
W odróżnieniu od trybu 8 nie wybiera tylko standardowego BW48/LR48. Każda
z czterech sekcji HPF i LPF może mieć własną częstotliwość oraz Q. Projektant:

- pracuje na osobnych pomiarach RAW L main / R main / L sub / R sub;
- używa wyłącznie stabilnych sekcji RBJ 2. rzędu;
- blokuje filtry z rezonansem, niemonotonicznym zboczem lub słabym stopbandem;
- porównuje wynik do realnej referencji 90 BW48 albo do referencji odtworzonej
  z RAW i USER_REFERENCE_90BW48.yaml;
- sprawdza odporność na ±0,2 ms i ±0,5 dB;
- generuje trzy profile odsłuchowo-techniczne;
- ocenia, czy docelowy kształt można uzyskać głównie przez tłumienie, bez szerokiego podbijania;
- skaluje zespolone odpowiedzi subów między stanami RAW, Dirac i odsłuchowym
  wyłącznie przez jawny, zweryfikowany Gain Bridge;
- sprawdza tego samego kandydata w stanie Dirac i odsłuchowym przed eksportem;
- eksportuje osiem sekcji w konwencji miniDSP SHD Studio 96 kHz.

To nadal jest projekt z pomiaru w jednym układzie/pozycji. Finalny wybór wymaga
wgrania filtra, Dirac Live, pomiaru POST-DIRAC L+P w posL/MLP/posP i odsłuchu.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from rew_mdat import load_optimizer_measurements_from_mdat
from rew_scoring import measurement_to_complex, common_grid, apply_delay_and_polarity, db
from rew_filter_generator import (
    Biquad,
    make_sections,
    make_custom_sections,
    freq_response,
    write_minidsp_biquads,
    biquad_pole_radii,
    make_allpass_section,
)
from rew_cut_only import CutOnlyMetrics, evaluate_cut_only
from rew_metric_registry import metric_label, write_metric_registry
from rew_empirical_calibration import EmpiricalCalibration, load_empirical_calibration, write_calibration_summary
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
from rew_acoustic_crossover_designer import (
    _audit_input_for_acoustic_design,
    _audit_is_diagnostic_only,
    _reference_relative_delays,
    _map_relative_to_shd,
    _map_relative_to_shd_main_anchor,
    _effective_xo_hz,
    _score_side,
    _score_lr_sum,
    _score_body,
    _score_authority_and_kick,
    _phase_energy_score,
    _dirac_burden_risk,
    _decision_class,
    _band,
    _safe_mean,
)

APP_VERSION = "v20.31 Projektant biquadów — GainState i Candidate Safety Gate"
PROFILES = ("REFERENCE_SAFE", "KICK_CONTROL", "CLEAN_BODY")
PROFILE_TITLES = {
    "REFERENCE_SAFE": "Ochrona średnicy i fundamentu — charakter 90 BW48 bez kopiowania ciężkiego basu",
    "KICK_CONTROL": "Impuls i kontrola — energia bez rozlewania",
    "CLEAN_BODY": "Czysty korpus — lżejszy bas, zachowana niższa średnica",
}


@dataclass(frozen=True)
class FilterShape:
    main_fc: Tuple[float, ...]
    main_q: Tuple[float, ...]
    sub_fc: Tuple[float, ...]
    sub_q: Tuple[float, ...]

    def signature(self, decimals_fc: int = 2, decimals_q: int = 3) -> Tuple[object, ...]:
        return (
            *(round(v, decimals_fc) for v in self.main_fc),
            *(round(v, decimals_q) for v in self.main_q),
            *(round(v, decimals_fc) for v in self.sub_fc),
            *(round(v, decimals_q) for v in self.sub_q),
        )


@dataclass
class SafetyMetrics:
    safe: bool
    peak_db: float
    monotonic_violation_db: float
    passband_ripple_db: float
    hp_stop_db: float
    lp_stop_db: float
    max_pole_radius: float
    fc_spread_hz: float
    reason: str


@dataclass(frozen=True)
class AllPassSpec:
    enabled: bool = False
    fc_hz: float = 90.0
    q: float = 0.70

    def label(self) -> str:
        return "OFF" if not self.enabled else f"{self.fc_hz:.2f} Hz / Q {self.q:.3f}"


@dataclass
class AllPassComparison:
    profile: str
    off: "CustomCandidate"
    on: "CustomCandidate"
    l_spec: AllPassSpec
    r_spec: AllPassSpec
    score_delta: float
    recommended: bool
    reason: str
    l_group_delay_peak_ms: float
    r_group_delay_peak_ms: float


@dataclass
class CustomCandidate:
    shape: FilterShape
    l_rel_ms: float
    r_rel_ms: float
    origin: str
    effective_xo_hz: float
    l_side_score: float
    r_side_score: float
    l_side_score_raw: float
    r_side_score_raw: float
    side_min: float
    side_mean: float
    lr_sum_score: float
    body_score: float
    authority_score: float
    kick_score: float
    phase_energy_score: float
    l_main_sub_phase_score: float
    r_main_sub_phase_score: float
    main_sub_phase_min: float
    l_main_sub_phase_median_deg: float
    r_main_sub_phase_median_deg: float
    technical_score: float
    technical_score_raw: float
    tonal_score_reference: float
    tonal_score_kick: float
    tonal_score_clean: float
    null_risk: float
    dirac_burden: float
    reference_score: float
    reference_rms_db: float
    bass_delta_vs_ref_db: float
    body_delta_db: float
    cut_only_score: float
    cut_only_safe: bool
    boost_need_mean_db: float
    boost_need_p95_db: float
    deficit_share_pct: float
    cut_headroom_db: float
    foundation_margin_db: float
    mass_margin_db: float
    body_margin_db: float
    cut_only_reason: str
    safety: SafetyMetrics
    nominal_scores: Dict[str, float]
    nominal_scores_raw: Dict[str, float]
    empirical_calibration_used: bool
    empirical_confidence: str
    empirical_uncertainty_penalty: float
    empirical_reason: str
    l_allpass: AllPassSpec = field(default_factory=AllPassSpec)
    r_allpass: AllPassSpec = field(default_factory=AllPassSpec)
    spatial_count: int = 0
    spatial_technical_min: float = float("nan")
    spatial_technical_mean: float = float("nan")
    robust_scores: Dict[str, float] = field(default_factory=dict)
    final_scores: Dict[str, float] = field(default_factory=dict)
    rank: int = 0
    code: str = ""
    decision_class: str = ""
    recommendation: str = ""
    global_gain_l_db: float = 0.0
    global_gain_r_db: float = 0.0
    local_gain_l_db: float = 0.0
    local_gain_r_db: float = 0.0
    final_gain_l_db: float = 0.0
    final_gain_r_db: float = 0.0
    gain_state_name: str = "RAW"
    state_metrics: Dict[str, Dict[str, object]] = field(default_factory=dict)
    candidate_safe_to_export: bool = False
    safety_reasons: Tuple[str, ...] = field(default_factory=tuple)

    def score(self, profile: str, robust: bool = True) -> float:
        p = profile.upper()
        if robust and p in self.final_scores:
            return float(self.final_scores[p])
        return float(self.nominal_scores.get(p, 0.0))


@dataclass
class EvaluationContext:
    freq: np.ndarray
    fs: float
    lm_raw: np.ndarray
    rm_raw: np.ndarray
    ls_raw: np.ndarray
    rs_raw: np.ndarray
    ref_sum: np.ndarray
    ref_source: str
    empirical_calibration: Optional[EmpiricalCalibration] = None
    delay_refinement_enabled: bool = True
    raw_sub_trim_db: float = -6.0
    raw_sub_trim_is_metadata: bool = False
    gain_state_name: str = "RAW"
    global_gain_l_db: float = 0.0
    global_gain_r_db: float = 0.0
    cut_only_enabled: bool = True
    search_frequencies: Optional[List[float]] = None
    prepare_import_files: bool = True


@dataclass
class SearchSettings:
    generations: int
    parents: int
    children_per_parent: int
    initial_random_per_seed: int
    fc_sigmas: Tuple[float, ...]
    q_sigmas: Tuple[float, ...]
    delay_sigmas: Tuple[float, ...]


SEARCH_LEVELS: Dict[str, SearchSettings] = {
    "quick": SearchSettings(3, 10, 6, 24, (2.4, 1.1, 0.45), (0.11, 0.060, 0.025), (0.22, 0.10, 0.05)),
    "normal": SearchSettings(4, 16, 8, 48, (3.2, 1.6, 0.75, 0.35), (0.15, 0.090, 0.050, 0.025), (0.30, 0.16, 0.08, 0.04)),
    "deep": SearchSettings(5, 24, 10, 80, (4.0, 2.2, 1.1, 0.55, 0.25), (0.18, 0.120, 0.075, 0.040, 0.020), (0.36, 0.22, 0.12, 0.06, 0.03)),
}


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))


def _interp_db(freq: np.ndarray, values_db: np.ndarray, at_hz: float) -> float:
    return float(np.interp(float(at_hz), np.asarray(freq, dtype=float), np.asarray(values_db, dtype=float)))


def _shape_from_standard(kind_label: str, fs: float, main_fc: float, sub_fc: float) -> FilterShape:
    m = make_sections("HP", fs, main_fc, kind_label)
    s = make_sections("LP", fs, sub_fc, kind_label)
    return FilterShape(
        main_fc=tuple(float(main_fc) for _ in m),
        main_q=tuple(float(x.q) for x in m),
        sub_fc=tuple(float(sub_fc) for _ in s),
        sub_q=tuple(float(x.q) for x in s),
    )


def _sections(shape: FilterShape, fs: float) -> Tuple[List[Biquad], List[Biquad]]:
    main = make_custom_sections("HP", fs, shape.main_fc, shape.main_q)
    sub = make_custom_sections("LP", fs, shape.sub_fc, shape.sub_q)
    return main, sub


def _filter_safety(shape: FilterShape, fs: float, main: Sequence[Biquad], sub: Sequence[Biquad]) -> SafetyMetrics:
    f = np.geomspace(10.0, 1000.0, 1400)
    hm = freq_response(main, fs, f)
    hs = freq_response(sub, fs, f)
    md = db(hm)
    sd = db(hs)

    peak = max(0.0, float(np.nanmax(md)), float(np.nanmax(sd)))
    # HP powinien rosnąć, LP maleć. Tolerancja usuwa szum numeryczny.
    dm = np.diff(md)
    ds = np.diff(sd)
    monotonic = float(np.sum(np.maximum(0.0, -dm - 0.025)) + np.sum(np.maximum(0.0, ds - 0.025)))

    main_pb = _band(f, 250.0, 500.0)
    sub_pb = _band(f, 20.0, 35.0)
    ripple_main = float(np.nanpercentile(md[main_pb], 95) - np.nanpercentile(md[main_pb], 5)) if np.count_nonzero(main_pb) else 99.0
    ripple_sub = float(np.nanpercentile(sd[sub_pb], 95) - np.nanpercentile(sd[sub_pb], 5)) if np.count_nonzero(sub_pb) else 99.0
    ripple = max(ripple_main, ripple_sub)

    main_center = float(np.exp(np.mean(np.log(np.maximum(1e-9, np.asarray(shape.main_fc))))))
    sub_center = float(np.exp(np.mean(np.log(np.maximum(1e-9, np.asarray(shape.sub_fc))))))
    hp_stop = _interp_db(f, md, max(10.0, main_center / 2.0))
    lp_stop = _interp_db(f, sd, min(1000.0, sub_center * 2.0))

    pole_radii = [r for bq in list(main) + list(sub) for r in biquad_pole_radii(bq)]
    max_pole = max(pole_radii) if pole_radii else 1.0
    fc_spread = max(
        max(shape.main_fc) - min(shape.main_fc),
        max(shape.sub_fc) - min(shape.sub_fc),
    )

    problems: List[str] = []
    if peak > 0.35:
        problems.append(f"rezonans {peak:.2f} dB > 0,35 dB")
    if monotonic > 0.75:
        problems.append(f"niemonotoniczne zbocze {monotonic:.2f} dB")
    if ripple > 0.35:
        problems.append(f"ripple passbandu {ripple:.2f} dB")
    if hp_stop > -28.0:
        problems.append(f"HPF za słaby w pół oktawy: {hp_stop:.1f} dB")
    if lp_stop > -28.0:
        problems.append(f"LPF za słaby w pół oktawy: {lp_stop:.1f} dB")
    if max_pole >= 0.999999:
        problems.append(f"biegun zbyt blisko 1: {max_pole:.8f}")
    if fc_spread > 18.0:
        problems.append(f"zbyt duży rozrzut fc sekcji: {fc_spread:.1f} Hz")
    return SafetyMetrics(
        safe=not problems,
        peak_db=peak,
        monotonic_violation_db=monotonic,
        passband_ripple_db=ripple,
        hp_stop_db=hp_stop,
        lp_stop_db=lp_stop,
        max_pole_radius=max_pole,
        fc_spread_hz=fc_spread,
        reason="; ".join(problems) if problems else "brak naruszeń twardych bramek",
    )


def _reference_similarity(candidate_sum: np.ndarray, ref_sum: np.ndarray, freq: np.ndarray) -> Tuple[float, float, float]:
    c = db(candidate_sum)
    r = db(ref_sum)
    anchor = _band(freq, 160.0, 220.0)
    if np.count_nonzero(anchor) < 5:
        anchor = _band(freq, 125.0, 220.0)
    if np.count_nonzero(anchor) < 5:
        return 50.0, 3.0, 0.0
    c0 = c - _safe_mean(c[anchor])
    r0 = r - _safe_mean(r[anchor])
    mid = _band(freq, 100.0, 220.0)
    upper_bass = _band(freq, 75.0, 100.0)
    low = _band(freq, 40.0, 80.0)
    if np.count_nonzero(mid) < 8:
        return 50.0, 3.0, 0.0
    err_mid = float(np.sqrt(np.nanmean((c0[mid] - r0[mid]) ** 2)))
    err_ub = float(np.sqrt(np.nanmean((c0[upper_bass] - r0[upper_bass]) ** 2))) if np.count_nonzero(upper_bass) >= 4 else err_mid
    rms = 0.72 * err_mid + 0.28 * err_ub
    score = _clip(100.0 - 18.0 * rms)
    if np.count_nonzero(low) >= 4:
        cand_rel = _safe_mean(c[low]) - _safe_mean(c[anchor])
        ref_rel = _safe_mean(r[low]) - _safe_mean(r[anchor])
        bass_delta = float(cand_rel - ref_rel)
    else:
        bass_delta = 0.0
    return score, float(rms), bass_delta


def _main_sub_phase_score(
    main_resp: np.ndarray,
    sub_resp: np.ndarray,
    freq: np.ndarray,
    xo: float,
) -> Tuple[float, float]:
    """Bezpośrednia zgodność fazowa main–sub dla jednej strony.

    Ocena obejmuje okolice efektywnego punktu podziału i tylko te punkty, w
    których obie gałęzie mają istotny udział. Dzięki temu nie mylimy zgodności
    fazy lewej i prawej sumy z rzeczywistym sklejeniem maina i subwoofera.
    """
    f = np.asarray(freq, dtype=float)
    m = _band(f, max(45.0, xo - 32.0), min(160.0, xo + 38.0))
    if np.count_nonzero(m) < 8:
        return 50.0, 90.0
    main_db = db(main_resp)
    sub_db = db(sub_resp)
    # Analizujemy punkty, w których słabsza gałąź nie jest niżej niż 14 dB od
    # silniejszej. Poza tym zakresem różnica fazy ma małe znaczenie dla sumy.
    active = m & (np.abs(main_db - sub_db) <= 14.0)
    if np.count_nonzero(active) < 8:
        active = m
    diff_deg = np.abs(np.rad2deg(np.angle(main_resp[active] * np.conj(sub_resp[active]))))
    med = float(np.nanmedian(diff_deg))
    p80 = float(np.nanpercentile(diff_deg, 80))
    score = _clip(100.0 - 0.50 * med - 0.18 * max(0.0, p80 - med))
    return score, med


def _technical_integration_score(
    *,
    side_min: float,
    side_mean: float,
    lr: float,
    main_sub_phase_min: float,
    main_sub_phase_mean: float,
    phase_lr: float,
    null_risk: float,
    burden: float,
    safety: SafetyMetrics,
) -> float:
    """Ocena technicznej jakości integracji, niezależna od tonalnego profilu."""
    if not safety.safe:
        return 0.0
    score = (
        0.24 * side_min +
        0.10 * side_mean +
        0.18 * lr +
        0.22 * main_sub_phase_min +
        0.08 * main_sub_phase_mean +
        0.04 * phase_lr +
        0.07 * (100.0 - null_risk) +
        0.07 * (100.0 - burden)
    )
    return _clip(score)


def _tonal_profile_scores(
    *,
    body: float,
    authority: float,
    kick: float,
    ref: float,
    cut_only: CutOnlyMetrics,
    cut_only_enabled: bool,
    bass_delta_vs_ref: float,
    effective_xo_hz: float,
) -> Dict[str, float]:
    """Ocena tonalna oddzielona od integracji fazowo-czasowej.

    Referencja 90 BW48 chroni głównie korpus i dolną średnicę. Metryka
    korekcji przez tłumienie jest obliczana względem własnego celu względnego,
    a nie przez bezpośrednie zestawienie RAW -6 dB z pomiarem po Dirac/0 dB.
    """
    cut = cut_only.score if cut_only_enabled else 70.0
    ref_safe = 0.34 * body + 0.12 * authority + 0.10 * kick + 0.28 * ref + 0.16 * cut
    kick_control = 0.16 * body + 0.28 * authority + 0.30 * kick + 0.08 * ref + 0.18 * cut
    clean_body = 0.40 * body + 0.08 * authority + 0.10 * kick + 0.24 * ref + 0.18 * cut

    # Nie kopiujemy ciężkiego basu referencji, ale też nie premiujemy szerokiej
    # utraty fundamentu. Są to łagodne kary tonalne, nie bramki eksportu.
    if bass_delta_vs_ref > 1.0:
        ref_safe -= (bass_delta_vs_ref - 1.0) * 2.2
        kick_control -= (bass_delta_vs_ref - 1.0) * 2.8
        clean_body -= (bass_delta_vs_ref - 0.4) * 3.0
    if bass_delta_vs_ref < -4.0:
        ref_safe -= (-4.0 - bass_delta_vs_ref) * 1.8
        kick_control -= (-4.0 - bass_delta_vs_ref) * 2.2
        clean_body -= (-4.5 - bass_delta_vs_ref) * 1.2
    if effective_xo_hz > 100.0:
        ref_safe -= (effective_xo_hz - 100.0) * 0.8
        clean_body -= (effective_xo_hz - 100.0) * 0.6
    return {
        "REFERENCE_SAFE": _clip(ref_safe),
        "KICK_CONTROL": _clip(kick_control),
        "CLEAN_BODY": _clip(clean_body),
    }


def _profile_scores(
    *,
    technical: float,
    tonal: Dict[str, float],
    null_risk: float,
    safety: SafetyMetrics,
) -> Dict[str, float]:
    """Wynik łączny: 65% integracji technicznej, 35% charakteru tonalnego."""
    if not safety.safe:
        return {p: 0.0 for p in PROFILES}
    common_penalty = max(0.0, null_risk - 65.0) * 0.12
    return {
        p: _clip(0.65 * technical + 0.35 * tonal[p] - common_penalty)
        for p in PROFILES
    }

def _allpass_response(spec: AllPassSpec, fs: float, freq: np.ndarray, *, section: int = 5) -> np.ndarray:
    if not spec.enabled:
        return np.ones_like(freq, dtype=complex)
    ap = make_allpass_section(fs, spec.fc_hz, spec.q, section=section)
    return freq_response([ap], fs, freq)


def _allpass_peak_group_delay_ms(spec: AllPassSpec, fs: float) -> float:
    if not spec.enabled:
        return 0.0
    f = np.geomspace(25.0, 250.0, 1200)
    h = _allpass_response(spec, fs, f)
    phase = np.unwrap(np.angle(h))
    omega = 2.0 * np.pi * f
    gd_s = -np.gradient(phase, omega)
    band = (f >= 40.0) & (f <= 180.0)
    return float(np.nanmax(gd_s[band]) * 1000.0)


def _allpass_spec_safe(spec: AllPassSpec, fs: float) -> Tuple[bool, str, float]:
    if not spec.enabled:
        return True, "OFF", 0.0
    if not 50.0 <= spec.fc_hz <= 150.0:
        return False, "fc poza zakresem 50–150 Hz", 99.0
    if not 0.35 <= spec.q <= 1.25:
        return False, "Q poza bezpiecznym zakresem 0,35–1,25", 99.0
    try:
        bq = make_allpass_section(fs, spec.fc_hz, spec.q, section=5)
        pole = max(biquad_pole_radii(bq))
        peak_gd = _allpass_peak_group_delay_ms(spec, fs)
    except Exception as exc:
        return False, str(exc), 99.0
    if pole >= 0.999999:
        return False, f"biegun {pole:.8f}", peak_gd
    if peak_gd > 9.0:
        return False, f"peak group delay {peak_gd:.2f} ms > 9 ms", peak_gd
    # Magnitude must remain essentially unity.
    f = np.geomspace(20.0, 500.0, 500)
    mag = db(freq_response([bq], fs, f))
    err = float(np.nanmax(np.abs(mag)))
    if err > 0.005:
        return False, f"błąd amplitudy {err:.4f} dB", peak_gd
    return True, "SAFE", peak_gd



def _evaluate(
    ctx: EvaluationContext,
    shape: FilterShape,
    l_rel_ms: float,
    r_rel_ms: float,
    origin: str,
    *,
    common_sub_gain_db: float = 0.0,
    l_allpass: Optional[AllPassSpec] = None,
    r_allpass: Optional[AllPassSpec] = None,
    l_invert: bool = False,
    r_invert: bool = False,
) -> CustomCandidate:
    main, sub = _sections(shape, ctx.fs)
    safety = _filter_safety(shape, ctx.fs, main, sub)
    hm = freq_response(main, ctx.fs, ctx.freq)
    hs = freq_response(sub, ctx.fs, ctx.freq)

    lm = ctx.lm_raw * hm
    rm = ctx.rm_raw * hm
    gain = 10.0 ** (float(common_sub_gain_db) / 20.0)
    l_ap = l_allpass or AllPassSpec()
    r_ap = r_allpass or AllPassSpec()
    ls0 = ctx.ls_raw * hs * gain * _allpass_response(l_ap, ctx.fs, ctx.freq)
    rs0 = ctx.rs_raw * hs * gain * _allpass_response(r_ap, ctx.fs, ctx.freq)
    effective_xo = _effective_xo_hz(
        lm, rm, ls0, rs0, ctx.freq,
        float(np.exp(np.mean(np.log(shape.main_fc)))),
        float(np.exp(np.mean(np.log(shape.sub_fc)))),
    )
    ls = apply_delay_and_polarity(ls0, ctx.freq, l_rel_ms, bool(l_invert))
    rs = apply_delay_and_polarity(rs0, ctx.freq, r_rel_ms, bool(r_invert))
    lsum = lm + ls
    rsum = rm + rs
    total = lsum + rsum

    lscore_raw, _, _ = _score_side(lsum, lm, ls, ctx.freq, effective_xo)
    rscore_raw, _, _ = _score_side(rsum, rm, rs, ctx.freq, effective_xo)
    lr_score, null_risk, _ = _score_lr_sum(lsum, rsum, ctx.freq, effective_xo)
    body, _ = _score_body(total, ctx.freq)
    authority, kick = _score_authority_and_kick(total, ctx.freq)
    phase = _phase_energy_score(lsum, rsum, ctx.freq, effective_xo)
    l_ms_phase, l_ms_phase_deg = _main_sub_phase_score(lm, ls, ctx.freq, effective_xo)
    r_ms_phase, r_ms_phase_deg = _main_sub_phase_score(rm, rs, ctx.freq, effective_xo)
    main_sub_phase_min = min(l_ms_phase, r_ms_phase)
    main_sub_phase_mean = 0.5 * (l_ms_phase + r_ms_phase)
    burden = _dirac_burden_risk(lsum, rsum, ctx.freq, effective_xo)
    ref_score, ref_rms, bass_delta = _reference_similarity(total, ctx.ref_sum, ctx.freq)
    cut_only = evaluate_cut_only(db(total), db(ctx.ref_sum), ctx.freq)

    total_db = db(total)
    m_body = _band(ctx.freq, 125.0, 220.0)
    m_kick = _band(ctx.freq, 75.0, 125.0)
    body_delta = (
        _safe_mean(total_db[m_body]) - _safe_mean(total_db[m_kick])
        if np.count_nonzero(m_body) >= 4 and np.count_nonzero(m_kick) >= 4
        else float("nan")
    )
    side_min_raw = min(lscore_raw, rscore_raw)
    side_mean_raw = 0.5 * (lscore_raw + rscore_raw)
    technical_raw = _technical_integration_score(
        side_min=side_min_raw,
        side_mean=side_mean_raw,
        lr=lr_score,
        main_sub_phase_min=main_sub_phase_min,
        main_sub_phase_mean=main_sub_phase_mean,
        phase_lr=phase,
        null_risk=null_risk,
        burden=burden,
        safety=safety,
    )
    tonal_scores = _tonal_profile_scores(
        body=body,
        authority=authority,
        kick=kick,
        ref=ref_score,
        cut_only=cut_only,
        cut_only_enabled=ctx.cut_only_enabled,
        bass_delta_vs_ref=bass_delta,
        effective_xo_hz=effective_xo,
    )
    raw_scores = _profile_scores(
        technical=technical_raw,
        tonal=tonal_scores,
        null_risk=null_risk,
        safety=safety,
    )

    cal = ctx.empirical_calibration
    empirical_used = bool(cal is not None and cal.enabled and not origin.startswith("standard_"))
    empirical_penalty = 0.0
    empirical_reason = "czysta symulacja — brak aktywnej kalibracji empirycznej"
    empirical_confidence = "BRAK"
    if empirical_used and cal is not None:
        weight = float(cal.weight)
        lscore = _clip(lscore_raw + weight * cal.custom_side_bias_l)
        rscore = _clip(rscore_raw + weight * cal.custom_side_bias_r)
        side_min = min(lscore, rscore)
        side_mean = 0.5 * (lscore + rscore)
        technical_components = _technical_integration_score(
            side_min=side_min,
            side_mean=side_mean,
            lr=lr_score,
            main_sub_phase_min=main_sub_phase_min,
            main_sub_phase_mean=main_sub_phase_mean,
            phase_lr=phase,
            null_risk=null_risk,
            burden=burden,
            safety=safety,
        )
        technical_bias_target = _clip(technical_raw + weight * cal.custom_technical_bias)
        technical = _clip(0.65 * technical_components + 0.35 * technical_bias_target)
        scores = _profile_scores(
            technical=technical,
            tonal=tonal_scores,
            null_risk=null_risk,
            safety=safety,
        )
        empirical_penalty = float(cal.uncertainty_penalty)
        scores = {p: _clip(v - empirical_penalty) for p, v in scores.items()}
        empirical_confidence = cal.confidence_label
        empirical_reason = (
            f"korekta realnymi pomiarami: waga {weight:.2f}, bias sklejenia L/P "
            f"{cal.custom_side_bias_l:+.2f}/{cal.custom_side_bias_r:+.2f} pkt, "
            f"kara niepewności {empirical_penalty:.2f} pkt"
        )
    else:
        lscore = float(lscore_raw)
        rscore = float(rscore_raw)
        side_min = float(side_min_raw)
        side_mean = float(side_mean_raw)
        technical = float(technical_raw)
        scores = dict(raw_scores)
        if cal is not None and cal.enabled and origin.startswith("standard_"):
            empirical_confidence = "NISKA — STANDARD TYLKO OSTRZEŻENIE"
            empirical_reason = "standardowy BW/LR nie jest punktowo korygowany; jedna walidacja 96 BW48 obniża pewność i wymusza PRE-DIRAC"
    decision, rec = _decision_class(
        max(scores.values()), side_min, lr_score, body, null_risk, burden,
        "" if safety.safe else safety.reason,
    )
    return CustomCandidate(
        shape=shape,
        l_rel_ms=float(l_rel_ms),
        r_rel_ms=float(r_rel_ms),
        origin=origin,
        effective_xo_hz=float(effective_xo),
        l_side_score=float(lscore),
        r_side_score=float(rscore),
        l_side_score_raw=float(lscore_raw),
        r_side_score_raw=float(rscore_raw),
        side_min=float(side_min),
        side_mean=float(side_mean),
        lr_sum_score=float(lr_score),
        body_score=float(body),
        authority_score=float(authority),
        kick_score=float(kick),
        phase_energy_score=float(phase),
        l_main_sub_phase_score=float(l_ms_phase),
        r_main_sub_phase_score=float(r_ms_phase),
        main_sub_phase_min=float(main_sub_phase_min),
        l_main_sub_phase_median_deg=float(l_ms_phase_deg),
        r_main_sub_phase_median_deg=float(r_ms_phase_deg),
        technical_score=float(technical),
        technical_score_raw=float(technical_raw),
        tonal_score_reference=float(tonal_scores["REFERENCE_SAFE"]),
        tonal_score_kick=float(tonal_scores["KICK_CONTROL"]),
        tonal_score_clean=float(tonal_scores["CLEAN_BODY"]),
        null_risk=float(null_risk),
        dirac_burden=float(burden),
        reference_score=float(ref_score),
        reference_rms_db=float(ref_rms),
        bass_delta_vs_ref_db=float(bass_delta),
        body_delta_db=float(body_delta),
        cut_only_score=float(cut_only.score),
        cut_only_safe=bool(cut_only.safe),
        boost_need_mean_db=float(cut_only.boost_mean_db),
        boost_need_p95_db=float(cut_only.boost_p95_db),
        deficit_share_pct=float(cut_only.deficit_share_pct),
        cut_headroom_db=float(cut_only.cut_headroom_db),
        foundation_margin_db=float(cut_only.foundation_margin_db),
        mass_margin_db=float(cut_only.mass_margin_db),
        body_margin_db=float(cut_only.body_margin_db),
        cut_only_reason=str(cut_only.reason),
        safety=safety,
        nominal_scores=scores,
        nominal_scores_raw=raw_scores,
        empirical_calibration_used=bool(empirical_used),
        empirical_confidence=str(empirical_confidence),
        empirical_uncertainty_penalty=float(empirical_penalty),
        empirical_reason=str(empirical_reason),
        l_allpass=l_ap,
        r_allpass=r_ap,
        decision_class=decision,
        recommendation=rec,
    )


def _load_reference_sum(
    reference_mdat: Optional[Path],
    freq: np.ndarray,
    *,
    raw: Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    fs: float,
    l_rel_ref: float,
    r_rel_ref: float,
    reference_settings: Optional[Dict[str, object]] = None,
) -> Tuple[np.ndarray, str]:
    if reference_mdat is not None and Path(reference_mdat).exists():
        try:
            lm, ls, rm, rs, project = load_optimizer_measurements_from_mdat(reference_mdat)
            ref = (
                measurement_to_complex(lm, freq) + measurement_to_complex(ls, freq) +
                measurement_to_complex(rm, freq) + measurement_to_complex(rs, freq)
            )
            names = ", ".join(project.role_to_name.values())
            return ref, f"realny MDAT 90 BW48: {Path(reference_mdat).name} ({names})"
        except Exception as exc:
            ref_error = str(exc)
    else:
        ref_error = "plik referencyjny nie został wskazany"

    lm_raw, rm_raw, ls_raw, rs_raw = raw
    ref_fc = float(reference_settings["fc"]) if reference_settings else 90.0
    ref_filter = str(reference_settings["filter"]) if reference_settings else "BW48"
    main = make_sections("HP", fs, ref_fc, ref_filter)
    sub = make_sections("LP", fs, ref_fc, ref_filter)
    hm = freq_response(main, fs, freq)
    hs = freq_response(sub, fs, freq)
    lm = lm_raw * hm
    rm = rm_raw * hm
    ls = apply_delay_and_polarity(ls_raw * hs, freq, l_rel_ref, False)
    rs = apply_delay_and_polarity(rs_raw * hs, freq, r_rel_ref, False)
    return lm + rm + ls + rs, f"odtworzona referencja {ref_fc:g} {ref_filter} z RAW; powód braku realnego MDAT: {ref_error}"


def _candidate_signature(c: CustomCandidate) -> Tuple[object, ...]:
    return (*c.shape.signature(), round(c.l_rel_ms, 2), round(c.r_rel_ms, 2))


def _mutate_shape(
    rng: np.random.Generator,
    parent: CustomCandidate,
    fc_sigma: float,
    q_sigma: float,
    delay_sigma: float,
) -> Tuple[FilterShape, float, float]:
    def mutate_branch(fcs: Sequence[float], qs: Sequence[float]) -> Tuple[Tuple[float, ...], Tuple[float, ...]]:
        f_arr = np.asarray(fcs, dtype=float)
        q_arr = np.asarray(qs, dtype=float)
        common_shift = rng.normal(0.0, fc_sigma * 0.55)
        individual = rng.normal(0.0, fc_sigma, size=f_arr.size)
        new_f = np.clip(f_arr + common_shift + individual, 74.0, 106.0)
        new_q = np.clip(q_arr * np.exp(rng.normal(0.0, q_sigma, size=q_arr.size)), 0.38, 3.50)
        order = np.argsort(new_f)
        return tuple(float(v) for v in new_f[order]), tuple(float(v) for v in new_q[order])

    mf, mq = mutate_branch(parent.shape.main_fc, parent.shape.main_q)
    sf, sq = mutate_branch(parent.shape.sub_fc, parent.shape.sub_q)
    lrel = float(np.clip(parent.l_rel_ms + rng.normal(0.0, delay_sigma), -30.0, 30.0))
    rrel = float(np.clip(parent.r_rel_ms + rng.normal(0.0, delay_sigma), -30.0, 30.0))
    return FilterShape(mf, mq, sf, sq), lrel, rrel


def _top_union(candidates: Sequence[CustomCandidate], count: int) -> List[CustomCandidate]:
    out: List[CustomCandidate] = []
    seen = set()
    per_profile = max(3, int(math.ceil(count / len(PROFILES))))
    for profile in PROFILES:
        for c in sorted(candidates, key=lambda x: x.score(profile, robust=False), reverse=True)[:per_profile]:
            sig = _candidate_signature(c)
            if sig not in seen:
                seen.add(sig)
                out.append(c)
    if len(out) < count:
        for c in sorted(candidates, key=lambda x: max(x.nominal_scores.values()), reverse=True):
            sig = _candidate_signature(c)
            if sig not in seen:
                seen.add(sig)
                out.append(c)
            if len(out) >= count:
                break
    return out[:count]


def _robustness(ctx: EvaluationContext, candidate: CustomCandidate) -> None:
    cases = [
        (0.0, 0.0, 0.0),
        (+0.2, 0.0, 0.0), (-0.2, 0.0, 0.0),
        (0.0, +0.2, 0.0), (0.0, -0.2, 0.0),
        (+0.2, +0.2, 0.0), (-0.2, -0.2, 0.0),
        (0.0, 0.0, +0.5), (0.0, 0.0, -0.5),
    ]
    scores: Dict[str, List[float]] = {p: [] for p in PROFILES}
    for dl, dr, gain in cases:
        try:
            x = _evaluate(
                ctx, candidate.shape,
                candidate.l_rel_ms + dl,
                candidate.r_rel_ms + dr,
                origin=candidate.origin,
                common_sub_gain_db=gain,
                l_allpass=candidate.l_allpass,
                r_allpass=candidate.r_allpass,
            )
        except Exception:
            for p in PROFILES:
                scores[p].append(0.0)
            continue
        for p in PROFILES:
            scores[p].append(x.nominal_scores[p])
    for p in PROFILES:
        arr = np.asarray(scores[p], dtype=float)
        robust = float(np.nanpercentile(arr, 20)) if arr.size else 0.0
        candidate.robust_scores[p] = robust
        candidate.final_scores[p] = _clip(0.78 * candidate.nominal_scores[p] + 0.22 * robust)


def _standard_seed_pool(
    ctx: EvaluationContext,
    l_center: float,
    r_center: float,
    progress: Optional[Callable[[str], None]],
) -> List[CustomCandidate]:
    fcs = ctx.search_frequencies or [82.0, 86.0, 90.0, 94.0, 98.0]
    delays = [-0.8, -0.4, 0.0, 0.4, 0.8]
    pool: List[CustomCandidate] = []
    total = 0
    for label in ("BW48", "LR48"):
        for mf in fcs:
            for sf in fcs:
                if abs(mf - sf) <= 8.0:
                    total += len(delays) * len(delays)
    done = 0
    for label in ("BW48", "LR48"):
        for mf in fcs:
            for sf in fcs:
                if abs(mf - sf) > 8.0:
                    continue
                shape = _shape_from_standard(label, ctx.fs, mf, sf)
                for dl in delays:
                    for dr in delays:
                        try:
                            _map_relative_to_shd(l_center + dl, r_center + dr)
                            c = _evaluate(ctx, shape, l_center + dl, r_center + dr, f"standard_{label}")
                            if c.safety.safe:
                                pool.append(c)
                        except Exception:
                            pass
                        done += 1
                        if progress and done % max(1, total // 8) == 0:
                            progress(f"Sprawdzanie standardowych filtrów BW/LR: {done}/{total}")
    return pool


def _delay_refinement_objective(c: CustomCandidate) -> float:
    """Cel końcowego dogęszczenia opóźnień po znalezieniu kształtu filtra.

    Realne pomiary pokazały, że wynik profilu może pozostawać podobny mimo
    wyraźnie gorszej słabszej strony. Dlatego dogęszczenie mocniej chroni
    najsłabsze sklejenie, a dopiero później wynik tonalny.
    """
    return float(
        max(c.nominal_scores.values())
        + 0.28 * c.side_min
        + 0.12 * c.technical_score
        + 0.05 * c.lr_sum_score
        + 0.04 * c.main_sub_phase_min
    )


def _refine_delays_from_real_validation(
    ctx: EvaluationContext,
    candidates: Sequence[CustomCandidate],
    *,
    progress: Optional[Callable[[str], None]] = None,
) -> List[CustomCandidate]:
    """Deterministycznie dogęść opóźnienia niezależnie od korekty punktowej.

    Skan wykonuje naprzemiennie L→P aż do zbieżności. Każda pełna iteracja
    kończy się ponownym sprawdzeniem prawej strony, co usuwa asymetrię v20.26.
    """
    cal = ctx.empirical_calibration
    if cal is None or not ctx.delay_refinement_enabled or not cal.delay_refinement_enabled:
        return list(candidates)
    seeds = _top_union(list(candidates), 9)
    refined: Dict[Tuple[object, ...], CustomCandidate] = {_candidate_signature(c): c for c in candidates}
    coarse_r = float(cal.delay_coarse_radius_ms)
    coarse_s = float(cal.delay_coarse_step_ms)
    fine_r = float(cal.delay_fine_radius_ms)
    fine_s = float(cal.delay_fine_step_ms)
    max_iter = max(1, int(cal.delay_max_iterations))
    eps = max(0.0, float(cal.delay_improvement_epsilon))

    def scan_axis(best: CustomCandidate, axis: str, radius: float, step: float, origin: str) -> CustomCandidate:
        centre = best.l_rel_ms if axis == "L" else best.r_rel_ms
        values = np.arange(centre - radius, centre + radius + step * 0.25, step)
        out = best
        out_obj = _delay_refinement_objective(out)
        for value in values:
            lv = float(value) if axis == "L" else float(out.l_rel_ms)
            rv = float(value) if axis == "R" else float(out.r_rel_ms)
            try:
                _map_relative_to_shd(lv, rv)
                cand = _evaluate(ctx, best.shape, lv, rv, origin)
            except Exception:
                continue
            cand_obj = _delay_refinement_objective(cand)
            if cand.safety.safe and cand_obj > out_obj + eps:
                out = cand
                out_obj = cand_obj
        return out

    def iterate_to_convergence(best: CustomCandidate, radius: float, step: float, label: str) -> CustomCandidate:
        for iteration in range(1, max_iter + 1):
            before = _delay_refinement_objective(best)
            old_l, old_r = best.l_rel_ms, best.r_rel_ms
            best = scan_axis(best, "L", radius, step, f"{best.origin}_{label}_L_{iteration}")
            best = scan_axis(best, "R", radius, step, f"{best.origin}_{label}_R_{iteration}")
            after = _delay_refinement_objective(best)
            moved = abs(best.l_rel_ms-old_l) > step*0.25 or abs(best.r_rel_ms-old_r) > step*0.25
            if (after - before) <= eps or not moved:
                break
        return best

    for idx, seed in enumerate(seeds, start=1):
        best = iterate_to_convergence(seed, coarse_r, coarse_s, "delay_coarse")
        best = iterate_to_convergence(best, fine_r, fine_s, "delay_fine")
        # Ostatnia kontrola P po finalnym L jest celowa.
        best = scan_axis(best, "R", fine_r, fine_s, seed.origin + "_delay_final_R")

        sig = _candidate_signature(best)
        old_c = refined.get(sig)
        if old_c is None or _delay_refinement_objective(best) > _delay_refinement_objective(old_c):
            refined[sig] = best
        if progress:
            progress(
                f"dokładne strojenie opóźnień {idx}/{len(seeds)}: "
                f"{seed.l_rel_ms:.2f}/{seed.r_rel_ms:.2f} → {best.l_rel_ms:.2f}/{best.r_rel_ms:.2f} ms"
            )
    return list(refined.values())


def search_custom_filters(
    ctx: EvaluationContext,
    *,
    l_center: float,
    r_center: float,
    level: str,
    random_seed: int,
    progress: Optional[Callable[[str], None]] = None,
) -> Tuple[List[CustomCandidate], List[CustomCandidate], CustomCandidate]:
    settings = SEARCH_LEVELS[level]
    standard = _standard_seed_pool(ctx, l_center, r_center, progress)
    if not standard:
        raise ValueError("Żadna standardowa zwrotnica BW48/LR48 nie spełniła warunków bezpieczeństwa. Sprawdź pomiary bez zwrotnicy DSP i opóźnienia ustawienia odniesienia.")
    baseline = max(standard, key=lambda c: c.nominal_scores["REFERENCE_SAFE"])

    seed_parents: List[CustomCandidate] = []
    for label in ("standard_BW48", "standard_LR48"):
        family = [c for c in standard if c.origin == label]
        seed_parents.extend(_top_union(family, 4))
    seed_parents.extend(_top_union(standard, 8))
    seed_parents = _top_union(seed_parents, 12)

    rng = np.random.default_rng(int(random_seed))
    all_custom: Dict[Tuple[object, ...], CustomCandidate] = {}

    # Pierwsze szerokie otoczenie każdej rodziny.
    for parent in seed_parents:
        for _ in range(settings.initial_random_per_seed):
            shape, lr, rr = _mutate_shape(rng, parent, 3.8, 0.17, 0.32)
            try:
                _map_relative_to_shd(lr, rr)
                c = _evaluate(ctx, shape, lr, rr, f"custom_from_{parent.origin}")
            except Exception:
                continue
            if not c.safety.safe:
                continue
            sig = _candidate_signature(c)
            old = all_custom.get(sig)
            if old is None or max(c.nominal_scores.values()) > max(old.nominal_scores.values()):
                all_custom[sig] = c
    if progress:
        progress(f"pierwsza populacja custom: {len(all_custom)} bezpiecznych filtrów")

    parents = _top_union(list(all_custom.values()) or standard, settings.parents)
    for gen in range(settings.generations):
        fc_sigma = settings.fc_sigmas[min(gen, len(settings.fc_sigmas) - 1)]
        q_sigma = settings.q_sigmas[min(gen, len(settings.q_sigmas) - 1)]
        d_sigma = settings.delay_sigmas[min(gen, len(settings.delay_sigmas) - 1)]
        children: List[CustomCandidate] = []
        for parent in parents:
            for _ in range(settings.children_per_parent):
                shape, lr, rr = _mutate_shape(rng, parent, fc_sigma, q_sigma, d_sigma)
                try:
                    _map_relative_to_shd(lr, rr)
                    c = _evaluate(ctx, shape, lr, rr, f"custom_gen{gen + 1}")
                except Exception:
                    continue
                if not c.safety.safe:
                    continue
                sig = _candidate_signature(c)
                old = all_custom.get(sig)
                if old is None or max(c.nominal_scores.values()) > max(old.nominal_scores.values()):
                    all_custom[sig] = c
                    children.append(c)
        parents = _top_union(list(all_custom.values()), settings.parents)
        if progress:
            progress(
                f"generacja {gen + 1}/{settings.generations}: +{len(children)} nowych, "
                f"łącznie {len(all_custom)} bezpiecznych"
            )

    custom = list(all_custom.values())
    if not custom:
        raise ValueError("Optymalizator nie znalazł żadnego customowego filtra przechodzącego bramki bezpieczeństwa.")

    # v20.26: realne pomiary pokazały, że po znalezieniu dobrego kształtu trzeba
    # jeszcze deterministycznie dogęścić delaye. Nie stosujemy stałego offsetu.
    custom = _refine_delays_from_real_validation(ctx, custom, progress=progress)

    # Pełna odporność tylko dla najlepszej rozsądnej puli, żeby nie mnożyć czasu.
    robust_pool = _top_union(custom, min(36, max(18, settings.parents * 2)))
    for i, c in enumerate(robust_pool, start=1):
        _robustness(ctx, c)
        if progress and (i % 6 == 0 or i == len(robust_pool)):
            progress(f"test odporności ±0,2 ms / ±0,5 dB: {i}/{len(robust_pool)}")

    # Pozostałym dajemy konserwatywny final = nominal - 4, żeby raport CSV miał pełną skalę,
    # ale nie mogły wyprzedzić kandydatów realnie sprawdzonych pod kątem odporności.
    robust_ids = {id(c) for c in robust_pool}
    for c in custom:
        if id(c) not in robust_ids:
            for p in PROFILES:
                c.robust_scores[p] = max(0.0, c.nominal_scores[p] - 8.0)
                c.final_scores[p] = max(0.0, c.nominal_scores[p] - 4.0)
    return standard, custom, baseline



def search_custom_filters_repeated(
    ctx: EvaluationContext,
    *,
    l_center: float,
    r_center: float,
    level: str,
    random_seed: int,
    repeats: int = 2,
    progress: Optional[Callable[[str], None]] = None,
) -> Tuple[List[CustomCandidate], List[CustomCandidate], CustomCandidate, List[Dict[str, object]]]:
    """Powtórz wyszukiwanie z różnymi ziarnami i połącz stabilne wyniki.

    Pierwszy przebieg używa wybranego poziomu, kolejne poziomu ``quick``, aby
    kontrola zbieżności nie podwajała nadmiernie czasu obliczeń.
    """
    repeats = max(1, min(5, int(repeats)))
    all_standard: List[CustomCandidate] = []
    merged: Dict[Tuple[object, ...], CustomCandidate] = {}
    run_winners: List[Dict[str, object]] = []
    baselines: List[CustomCandidate] = []
    for run in range(repeats):
        run_level = level if run == 0 else "quick"
        seed = int(random_seed) + run * 1009
        if progress:
            progress(f"wyszukiwanie {run + 1}/{repeats}: poziom {run_level}, ziarno {seed}")
        standard, custom, baseline = search_custom_filters(
            ctx,
            l_center=l_center,
            r_center=r_center,
            level=run_level,
            random_seed=seed,
            progress=progress,
        )
        all_standard.extend(standard)
        baselines.append(baseline)
        row: Dict[str, object] = {"run": run + 1, "seed": seed, "level": run_level}
        for profile in PROFILES:
            best = max(custom, key=lambda c: c.score(profile))
            row[profile] = best
        run_winners.append(row)
        for c in custom:
            sig = _candidate_signature(c)
            old = merged.get(sig)
            if old is None or max(c.final_scores.values()) > max(old.final_scores.values()):
                merged[sig] = c
    merged_custom = list(merged.values())
    baseline = max(baselines, key=lambda c: c.nominal_scores["REFERENCE_SAFE"])
    return all_standard, merged_custom, baseline, run_winners


def _write_convergence_report(out_dir: Path, run_winners: Sequence[Dict[str, object]]) -> None:
    lines = [
        "# 06 — Zbieżność wyszukiwania",
        "",
        "Powtórzenia wykorzystują różne ziarna losowe. Pierwszy przebieg ma wybrany poziom dokładności, kolejne są szybką kontrolą, czy algorytm wraca do podobnego obszaru częstotliwości, Q i opóźnień. Tabela pokazuje najlepszy nieograniczony wynik każdego profilu w danym przebiegu; końcowa lista może wybrać nieco słabszy, ale odrębny filtr, aby trzy profile nie były kopią tej samej kaskady.",
        "",
        "| Przebieg | Ziarno | Poziom | Profil | Wynik | XO | Delay L/R | Integracja techniczna | Faza main–sub L/R |",
        "|---:|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in run_winners:
        for profile in PROFILES:
            c = row.get(profile)
            if not isinstance(c, CustomCandidate):
                continue
            lines.append(
                f"| {row['run']} | {row['seed']} | {row['level']} | {PROFILE_TITLES[profile]} | "
                f"{c.score(profile):.1f} | {c.effective_xo_hz:.1f} Hz | {c.l_rel_ms:.2f}/{c.r_rel_ms:.2f} ms | "
                f"{c.technical_score:.1f} | {c.l_main_sub_phase_score:.1f}/{c.r_main_sub_phase_score:.1f} |"
            )
    lines += ["", "## Ocena stabilności", ""]
    for profile in PROFILES:
        cs = [row.get(profile) for row in run_winners if isinstance(row.get(profile), CustomCandidate)]
        if not cs:
            continue
        xo = [c.effective_xo_hz for c in cs]
        ld = [c.l_rel_ms for c in cs]
        rd = [c.r_rel_ms for c in cs]
        score = [c.score(profile) for c in cs]
        lines.append(
            f"- **{PROFILE_TITLES[profile]}:** zakres XO {min(xo):.1f}–{max(xo):.1f} Hz; "
            f"delay L {min(ld):.2f}–{max(ld):.2f} ms; R {min(rd):.2f}–{max(rd):.2f} ms; "
            f"wynik {min(score):.1f}–{max(score):.1f}."
        )
    lines += [
        "",
        "Duży rozrzut nie oznacza automatycznie błędu, ale wskazuje, że wynik zależy od lokalnego minimum i wymaga mocniejszego A/B oraz pomiaru wielopozycyjnego.",
    ]
    (out_dir / "06_ZBIEZNOSC_WYSZUKIWANIA.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

def _ap_candidate_gate(base: CustomCandidate, cand: CustomCandidate, profile: str) -> Tuple[bool, str]:
    # All-pass must not buy a prettier score by sacrificing core safeguards.
    if cand.side_min < base.side_min - 1.5:
        return False, "pogorszenie słabszej strony >1,5 pkt"
    if cand.lr_sum_score < base.lr_sum_score - 1.5:
        return False, "pogorszenie L+P >1,5 pkt"
    if cand.main_sub_phase_min < base.main_sub_phase_min - 1.5:
        return False, "pogorszenie zgodności fazowej main–sub >1,5 pkt"
    if cand.body_score < base.body_score - 2.0:
        return False, "pogorszenie ciągłości korpusu >2 pkt"
    if cand.reference_score < base.reference_score - 2.5:
        return False, "zbyt duże odejście od referencji 90"
    if cand.dirac_burden > base.dirac_burden + 4.0:
        return False, "większy ciężar Diraca >4 pkt"
    if cand.null_risk > base.null_risk + 4.0:
        return False, "większe ryzyko dziury L+P >4 pkt"
    if cand.cut_only_score < base.cut_only_score - 2.0:
        return False, "pogorszenie możliwości korekcji przez tłumienie >2 pkt"
    if cand.boost_need_p95_db > base.boost_need_p95_db + 0.5:
        return False, "większa potrzeba lokalnego podbijania >0,5 dB"
    return True, "przeszedł bramki porównania OFF/ON"


def _allpass_spec_grid() -> List[AllPassSpec]:
    specs = [AllPassSpec()]
    for fc in (55.0, 65.0, 75.0, 85.0, 95.0, 105.0, 120.0, 135.0, 150.0):
        for q in (0.35, 0.45, 0.55, 0.70, 0.85, 1.00, 1.20):
            specs.append(AllPassSpec(True, fc, q))
    return specs


def _allpass_complexity_penalty(candidate: CustomCandidate, fs: float) -> float:
    """Soft preference for gentler phase correction when scores are similar."""
    penalty = 0.0
    enabled = 0
    for spec in (candidate.l_allpass, candidate.r_allpass):
        if not spec.enabled:
            continue
        enabled += 1
        gd = _allpass_peak_group_delay_ms(spec, fs)
        penalty += max(0.0, gd - 4.5) * 0.35
        penalty += max(0.0, spec.q - 0.90) * 1.20
    if enabled > 1:
        penalty += 0.15
    return float(penalty)


def _allpass_objective(candidate: CustomCandidate, profile: str, fs: float, *, robust: bool = False) -> float:
    return float(candidate.score(profile, robust=robust) - _allpass_complexity_penalty(candidate, fs))


def optimize_allpass_ab(
    ctx: EvaluationContext,
    base: CustomCandidate,
    profile: str,
    *,
    random_seed: int,
    progress: Optional[Callable[[str], None]] = None,
) -> AllPassComparison:
    profile = profile.upper()
    specs = _allpass_spec_grid()

    def evaluate(lspec: AllPassSpec, rspec: AllPassSpec, origin: str) -> Optional[CustomCandidate]:
        lsafe, _, _ = _allpass_spec_safe(lspec, ctx.fs)
        rsafe, _, _ = _allpass_spec_safe(rspec, ctx.fs)
        if not lsafe or not rsafe:
            return None
        try:
            c = _evaluate(
                ctx, base.shape, base.l_rel_ms, base.r_rel_ms, origin,
                l_allpass=lspec, r_allpass=rspec,
            )
        except Exception:
            return None
        ok, _ = _ap_candidate_gate(base, c, profile)
        return c if ok else None

    # Rank each side independently first; then combine only promising specs.
    left: List[CustomCandidate] = []
    right: List[CustomCandidate] = []
    for spec in specs:
        c = evaluate(spec, AllPassSpec(), "allpass_left_grid")
        if c is not None:
            left.append(c)
        c = evaluate(AllPassSpec(), spec, "allpass_right_grid")
        if c is not None:
            right.append(c)
    top_l = sorted(left, key=lambda c: _allpass_objective(c, profile, ctx.fs, robust=False), reverse=True)[:8]
    top_r = sorted(right, key=lambda c: _allpass_objective(c, profile, ctx.fs, robust=False), reverse=True)[:8]
    lspecs = [AllPassSpec()] + [c.l_allpass for c in top_l if c.l_allpass.enabled]
    rspecs = [AllPassSpec()] + [c.r_allpass for c in top_r if c.r_allpass.enabled]

    pool: List[CustomCandidate] = []
    seen = set()
    for ls in lspecs:
        for rs in rspecs:
            if not ls.enabled and not rs.enabled:
                continue
            key = (ls.enabled, round(ls.fc_hz, 2), round(ls.q, 3), rs.enabled, round(rs.fc_hz, 2), round(rs.q, 3))
            if key in seen:
                continue
            seen.add(key)
            c = evaluate(ls, rs, "allpass_pair_grid")
            if c is not None:
                pool.append(c)

    if not pool:
        # v20.26: obliczenia A/B są wykonywane zawsze, ale odrzucony AP nie trafia
        # do normalnego folderu ALLPASS_ON. Zachowujemy go wyłącznie jako materiał
        # diagnostyczny, a OFF pozostaje jedynym ustawieniem do wgrania.
        fallback: List[CustomCandidate] = []

        def evaluate_fallback(lspec: AllPassSpec, rspec: AllPassSpec, origin: str) -> Optional[CustomCandidate]:
            lsafe, _, _ = _allpass_spec_safe(lspec, ctx.fs)
            rsafe, _, _ = _allpass_spec_safe(rspec, ctx.fs)
            if not lsafe or not rsafe:
                return None
            if not lspec.enabled and not rspec.enabled:
                return None
            try:
                return _evaluate(
                    ctx, base.shape, base.l_rel_ms, base.r_rel_ms, origin,
                    l_allpass=lspec, r_allpass=rspec,
                )
            except Exception:
                return None

        # Tylko jedna strona naraz: najmniejsze ryzyko i czytelny test odsłuchowy.
        for spec in specs:
            if not spec.enabled:
                continue
            c = evaluate_fallback(spec, AllPassSpec(), "allpass_fallback_left")
            if c is not None:
                fallback.append(c)
            c = evaluate_fallback(AllPassSpec(), spec, "allpass_fallback_right")
            if c is not None:
                fallback.append(c)

        if not fallback:
            # To powinno być osiągalne tylko przy błędzie matematycznym lub danych.
            return AllPassComparison(
                profile, base, base, AllPassSpec(), AllPassSpec(), 0.0, False,
                "Nie udało się zbudować nawet stabilnego testowego all-pass. Sprawdź dane wejściowe.", 0.0, 0.0,
            )

        # Najpierw wybieramy kandydatów najmniej szkodliwych nominalnie, potem
        # sprawdzamy ich odporność. Kara złożoności preferuje niskie Q i mały GD.
        fallback = sorted(
            fallback,
            key=lambda c: _allpass_objective(c, profile, ctx.fs, robust=False),
            reverse=True,
        )[:16]
        _robustness(ctx, base)
        for c in fallback:
            _robustness(ctx, c)
        best_fallback = max(
            fallback,
            key=lambda c: _allpass_objective(c, profile, ctx.fs, robust=True),
        )
        delta = best_fallback.score(profile) - base.score(profile)
        _, _, lgd = _allpass_spec_safe(best_fallback.l_allpass, ctx.fs)
        _, _, rgd = _allpass_spec_safe(best_fallback.r_allpass, ctx.fs)
        reason = (
            "Żaden all-pass nie przeszedł surowych bramek jakościowych. "
            f"Zapisano diagnostycznie najmniej szkodliwy stabilny wariant jednostronny; "
            f"zmiana wyniku odpornego {delta:+.2f} pkt. OFF pozostaje ustawieniem domyślnym."
        )
        if progress:
            progress(
                f"all-pass {profile}: brak kandydata przechodzącego bramki; zapisano tylko diagnostycznie "
                f"L {best_fallback.l_allpass.label()}, R {best_fallback.r_allpass.label()}, Δ {delta:+.2f} pkt"
            )
        return AllPassComparison(
            profile, base, best_fallback, best_fallback.l_allpass, best_fallback.r_allpass,
            float(delta), False, reason, float(lgd), float(rgd),
        )

    # Local deterministic refinement around the best pair. Delays stay unchanged,
    # so OFF/ON remains a true all-pass-only comparison.
    best = max(pool, key=lambda c: _allpass_objective(c, profile, ctx.fs, robust=False))
    rng = np.random.default_rng(int(random_seed) + sum(ord(x) for x in profile))
    refined = list(pool)
    for _ in range(140):
        def mutate(spec: AllPassSpec) -> AllPassSpec:
            if not spec.enabled:
                if rng.random() < 0.18:
                    return AllPassSpec(True, float(rng.uniform(60.0, 140.0)), float(rng.uniform(0.38, 1.05)))
                return spec
            if rng.random() < 0.06:
                return AllPassSpec()
            return AllPassSpec(
                True,
                float(np.clip(spec.fc_hz + rng.normal(0.0, 4.0), 50.0, 150.0)),
                float(np.clip(spec.q * math.exp(rng.normal(0.0, 0.09)), 0.35, 1.25)),
            )
        ls = mutate(best.l_allpass)
        rs = mutate(best.r_allpass)
        if not ls.enabled and not rs.enabled:
            continue
        c = evaluate(ls, rs, "allpass_local_refine")
        if c is not None:
            refined.append(c)
            if _allpass_objective(c, profile, ctx.fs, robust=False) > _allpass_objective(best, profile, ctx.fs, robust=False):
                best = c

    # Robustness includes the same ±0.2 ms / ±0.5 dB perturbations as OFF.
    # Re-evaluate OFF as well; selected candidates outside the original robust
    # pool may otherwise carry only the conservative placeholder score.
    _robustness(ctx, base)
    unique: Dict[Tuple[object, ...], CustomCandidate] = {}
    for c in refined:
        key = (
            c.l_allpass.enabled, round(c.l_allpass.fc_hz, 2), round(c.l_allpass.q, 3),
            c.r_allpass.enabled, round(c.r_allpass.fc_hz, 2), round(c.r_allpass.q, 3),
        )
        old = unique.get(key)
        if old is None or _allpass_objective(c, profile, ctx.fs, robust=False) > _allpass_objective(old, profile, ctx.fs, robust=False):
            unique[key] = c
    robust_pool = sorted(unique.values(), key=lambda c: _allpass_objective(c, profile, ctx.fs, robust=False), reverse=True)[:12]
    for c in robust_pool:
        _robustness(ctx, c)
    best = max(robust_pool, key=lambda c: _allpass_objective(c, profile, ctx.fs, robust=True))
    delta = best.score(profile) - base.score(profile)
    l_ok, l_reason, lgd = _allpass_spec_safe(best.l_allpass, ctx.fs)
    r_ok, r_reason, rgd = _allpass_spec_safe(best.r_allpass, ctx.fs)
    gate, gate_reason = _ap_candidate_gate(base, best, profile)
    recommendation_gd_limit_ms = 6.0
    gd_ok_for_recommendation = max(lgd, rgd) <= recommendation_gd_limit_ms
    recommended = bool(l_ok and r_ok and gate and delta >= 0.75 and gd_ok_for_recommendation)
    soft_penalty = _allpass_complexity_penalty(best, ctx.fs)
    if recommended:
        reason = (
            f"All-pass poprawia wynik odporny o {delta:+.2f} pkt, przechodzi bramki, "
            f"peak group delay nie przekracza {recommendation_gd_limit_ms:.1f} ms "
            f"i ma karę złożoności {soft_penalty:.2f} pkt."
        )
    else:
        if not gd_ok_for_recommendation:
            status_detail = (
                f"peak group delay {max(lgd, rgd):.2f} ms przekracza limit rekomendacji "
                f"{recommendation_gd_limit_ms:.1f} ms"
            )
        else:
            status_detail = "test warunkowy" if gate else gate_reason
        reason = (
            f"Najlepszy stabilny wariant zmienia wynik o {delta:+.2f} pkt (kara złożoności {soft_penalty:.2f}); "
            f"status: {status_detail}. "
            "OFF pozostaje ustawieniem domyślnym."
        )
    if progress:
        progress(
            f"all-pass {profile}: L {best.l_allpass.label()}, R {best.r_allpass.label()}, "
            f"Δ robust {delta:+.2f} pkt, {'ZALECANY' if recommended else 'WARUNKOWY/OFF'}"
        )
    return AllPassComparison(
        profile, base, best, best.l_allpass, best.r_allpass, float(delta), recommended, reason,
        float(lgd), float(rgd),
    )



def _shape_distance(a: FilterShape, b: FilterShape) -> float:
    af = np.asarray(a.main_fc + a.sub_fc, dtype=float)
    bf = np.asarray(b.main_fc + b.sub_fc, dtype=float)
    aq = np.asarray(a.main_q + a.sub_q, dtype=float)
    bq = np.asarray(b.main_q + b.sub_q, dtype=float)
    return float(np.mean(np.abs(af - bf)) / 2.0 + np.mean(np.abs(aq - bq)) * 2.0)


def select_profile_winners(custom: Sequence[CustomCandidate], *, cut_only_enabled: bool = True) -> List[Tuple[str, CustomCandidate]]:
    selected: List[Tuple[str, CustomCandidate]] = []
    used: List[CustomCandidate] = []
    for profile in PROFILES:
        ranked = sorted(
            custom,
            key=lambda c: (c.score(profile), c.technical_score, c.cut_only_score),
            reverse=True,
        )
        choice = None
        for c in ranked:
            if not c.safety.safe:
                continue
            if c.side_min < 10.0 or c.lr_sum_score < 45.0 or c.main_sub_phase_min < 20.0:
                continue
            if all(_shape_distance(c.shape, prev.shape) >= 0.35 or abs(c.l_rel_ms - prev.l_rel_ms) >= 0.08 or abs(c.r_rel_ms - prev.r_rel_ms) >= 0.08 for prev in used):
                choice = c
                break
        if choice is None:
            choice = ranked[0]
        if any(choice is prev for prev in used):
            choice = copy.deepcopy(choice)
        used.append(choice)
        selected.append((profile, choice))
    for i, (profile, c) in enumerate(selected, start=1):
        c.rank = i
        c.code = f"ABD_{i:02d}_{profile}_{int(round(c.effective_xo_hz))}HZ"
        c.decision_class, c.recommendation = _decision_class(
            c.score(profile), c.side_min, c.lr_sum_score, c.body_score,
            c.null_risk, c.dirac_burden, "" if c.safety.safe else c.safety.reason,
        )
        export_status, export_reason = _export_status(profile, c)
        if export_status == "DIAGNOSTYKA_NIE_WGRYWAC":
            c.decision_class = "DIAGNOSTYKA — NIE WGRYWAĆ"
            c.recommendation = "Brak plików do importu. " + export_reason
        elif export_status == "TEST_WARUNKOWY":
            c.decision_class = "TEST WARUNKOWY"
            c.recommendation = "Pliki trafią do oddzielnego folderu TEST_WARUNKOWY. " + export_reason
        else:
            c.decision_class = "KANDYDAT DO KONTROLOWANEGO TESTU"
            c.recommendation = "Pliki trafią do folderu DO_TESTU; nadal wymagany pomiar przed i po Dirac."
    return selected



def _export_status(profile: str, c: CustomCandidate) -> Tuple[str, str]:
    """Klasa zapisu plików do miniDSP niezależna od atrakcyjności rankingu."""
    score = c.score(profile)
    hard = []
    if not c.safety.safe:
        hard.append("filtr nie przeszedł bramek stabilności")
    if not 70.0 <= float(c.effective_xo_hz) <= 105.0:
        hard.append(f"efektywne XO {c.effective_xo_hz:.1f} Hz poza twardym limitem 70–105 Hz")
    if c.safety_reasons and not c.candidate_safe_to_export:
        hard.extend(c.safety_reasons)
    if c.technical_score < 40.0:
        hard.append(f"bardzo niska integracja techniczna {c.technical_score:.1f}/100")
    if c.main_sub_phase_min < 35.0:
        hard.append(f"słaba zgodność fazowa main–sub {c.main_sub_phase_min:.1f}/100")
    if c.lr_sum_score < 45.0:
        hard.append(f"słaba suma L+P {c.lr_sum_score:.1f}/100")
    if c.side_min < 10.0:
        hard.append(f"bardzo słaba jedna strona {c.side_min:.1f}/100")
    if c.spatial_count > 0 and np.isfinite(c.spatial_technical_min) and c.spatial_technical_min < 38.0:
        hard.append(f"słaba odporność przestrzenna {c.spatial_technical_min:.1f}/100")
    if hard:
        return "DIAGNOSTYKA_NIE_WGRYWAC", "; ".join(hard)
    if (not c.cut_only_safe) or score < 65.0 or c.technical_score < 58.0:
        reasons = []
        if not c.cut_only_safe:
            reasons.append("nie spełnia pomocniczej oceny korekcji głównie przez tłumienie")
        if score < 65.0:
            reasons.append(f"wynik łączny {score:.1f}/100 < 65")
        if c.technical_score < 58.0:
            reasons.append(f"integracja techniczna {c.technical_score:.1f}/100 < 58")
        if c.spatial_count > 0 and np.isfinite(c.spatial_technical_min) and c.spatial_technical_min < 52.0:
            reasons.append(f"minimalna integracja przestrzenna {c.spatial_technical_min:.1f}/100 < 52")
        return "TEST_WARUNKOWY", "; ".join(reasons)
    return "DO_TESTU", "kandydat przeszedł bramki eksportu do kontrolowanego testu"

def _write_coefficients(path: Path, sections: Sequence[Biquad], fcs: Sequence[float], qs: Sequence[float], fs: float) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "section", "fs_hz", "fc_hz", "q", "kind", "b0", "b1", "b2",
            "a1_math", "a2_math", "a1_minidsp_shd", "a2_minidsp_shd", "max_pole_radius",
        ])
        for bq, fc, q in zip(sections, fcs, qs):
            w.writerow([
                bq.section, fs, fc, q, bq.kind, bq.b0, bq.b1, bq.b2,
                bq.a1, bq.a2, -bq.a1, -bq.a2, max(biquad_pole_radii(bq)),
            ])


def _write_response_csv(path: Path, ctx: EvaluationContext, c: CustomCandidate) -> None:
    main, sub = _sections(c.shape, ctx.fs)
    hm = freq_response(main, ctx.fs, ctx.freq)
    hs = freq_response(sub, ctx.fs, ctx.freq)
    lm = ctx.lm_raw * hm
    rm = ctx.rm_raw * hm
    lap = _allpass_response(c.l_allpass, ctx.fs, ctx.freq)
    rap = _allpass_response(c.r_allpass, ctx.fs, ctx.freq)
    ls = apply_delay_and_polarity(ctx.ls_raw * hs * lap, ctx.freq, c.l_rel_ms, False)
    rs = apply_delay_and_polarity(ctx.rs_raw * hs * rap, ctx.freq, c.r_rel_ms, False)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "freq_hz", "main_filter_db", "sub_filter_db", "main_filter_phase_deg", "sub_filter_phase_deg",
            "l_allpass_phase_deg", "r_allpass_phase_deg",
            "l_main_filtered_db", "l_sub_filtered_delayed_db", "l_main_sub_phase_diff_deg", "l_sum_gain_over_stronger_db", "l_sum_db",
            "r_main_filtered_db", "r_sub_filtered_delayed_db", "r_main_sub_phase_diff_deg", "r_sum_gain_over_stronger_db", "r_sum_db",
            "lr_total_db", "reference_90_total_db",
        ])
        for i, fr in enumerate(ctx.freq):
            w.writerow([
                fr, db(hm)[i], db(hs)[i], np.rad2deg(np.angle(hm[i])), np.rad2deg(np.angle(hs[i])),
                np.degrees(np.angle(lap[i])), np.degrees(np.angle(rap[i])),
                db(lm)[i], db(ls)[i], np.degrees(np.angle(lm[i] * np.conj(ls[i]))),
                db(lm + ls)[i] - max(db(lm)[i], db(ls)[i]), db(lm + ls)[i],
                db(rm)[i], db(rs)[i], np.degrees(np.angle(rm[i] * np.conj(rs[i]))),
                db(rm + rs)[i] - max(db(rm)[i], db(rs)[i]), db(rm + rs)[i],
                db(lm + ls + rm + rs)[i], db(ctx.ref_sum)[i],
            ])


def _sub_sections_with_allpass(shape: FilterShape, fs: float, spec: AllPassSpec) -> List[Biquad]:
    _, sub = _sections(shape, fs)
    out = list(sub)
    if spec.enabled:
        out.append(make_allpass_section(fs, spec.fc_hz, spec.q, section=len(out) + 1))
    return out


def _write_allpass_comparison_csv(path: Path, ctx: EvaluationContext, comp: AllPassComparison) -> None:
    off = comp.off
    on = comp.on
    main, sub = _sections(off.shape, ctx.fs)
    hm = freq_response(main, ctx.fs, ctx.freq)
    hs = freq_response(sub, ctx.fs, ctx.freq)
    lm = ctx.lm_raw * hm
    rm = ctx.rm_raw * hm
    off_ls = apply_delay_and_polarity(ctx.ls_raw * hs, ctx.freq, off.l_rel_ms, False)
    off_rs = apply_delay_and_polarity(ctx.rs_raw * hs, ctx.freq, off.r_rel_ms, False)
    lap = _allpass_response(comp.l_spec, ctx.fs, ctx.freq)
    rap = _allpass_response(comp.r_spec, ctx.fs, ctx.freq)
    on_ls = apply_delay_and_polarity(ctx.ls_raw * hs * lap, ctx.freq, on.l_rel_ms, False)
    on_rs = apply_delay_and_polarity(ctx.rs_raw * hs * rap, ctx.freq, on.r_rel_ms, False)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "freq_hz", "off_l_sum_db", "on_l_sum_db", "off_r_sum_db", "on_r_sum_db",
            "off_lr_total_db", "on_lr_total_db", "l_ap_phase_deg", "r_ap_phase_deg",
        ])
        for i, fr in enumerate(ctx.freq):
            w.writerow([
                fr, db(lm + off_ls)[i], db(lm + on_ls)[i], db(rm + off_rs)[i], db(rm + on_rs)[i],
                db(lm + off_ls + rm + off_rs)[i], db(lm + on_ls + rm + on_rs)[i],
                np.degrees(np.angle(lap[i])), np.degrees(np.angle(rap[i])),
            ])



def _write_variant(
    out_dir: Path,
    profile: str,
    c: CustomCandidate,
    ctx: EvaluationContext,
    shd_anchor_main: float,
    allpass: Optional[AllPassComparison] = None,
) -> None:
    vdir = out_dir / c.code
    vdir.mkdir(parents=True, exist_ok=True)
    main, sub = _sections(c.shape, ctx.fs)
    _write_coefficients(vdir / "MAIN_C1_CUSTOM_HPF_coefficients.csv", main, c.shape.main_fc, c.shape.main_q, ctx.fs)
    _write_coefficients(vdir / "SUB_REL_CUSTOM_LPF_coefficients.csv", sub, c.shape.sub_fc, c.shape.sub_q, ctx.fs)
    _write_response_csv(vdir / "FILTER_I_SYMULACJA_AKUSTYCZNA.csv", ctx, c)

    export_status, export_reason = _export_status(profile, c)
    export_root = vdir / export_status
    export_root.mkdir(parents=True, exist_ok=True)
    import_allowed = bool(ctx.prepare_import_files and c.candidate_safe_to_export and export_status != "DIAGNOSTYKA_NIE_WGRYWAC")
    if import_allowed:
        write_minidsp_biquads(export_root / "MAIN_C1_CUSTOM_HPF_MINIDSP_BIQUADS.txt", main, c.code, slots=8)
        write_minidsp_biquads(export_root / "SUB_REL_CUSTOM_LPF_MINIDSP_BIQUADS.txt", sub, c.code, slots=8)
        off_dir = export_root / "ALLPASS_OFF"
        off_dir.mkdir(parents=True, exist_ok=True)
        write_minidsp_biquads(off_dir / "MAIN_LR_C1_HPF_ALLPASS_OFF.txt", main, c.code + " AP OFF MAIN", slots=8)
        write_minidsp_biquads(off_dir / "SUB_L_REL_LPF_ALLPASS_OFF.txt", sub, c.code + " AP OFF SUB L", slots=8)
        write_minidsp_biquads(off_dir / "SUB_R_REL_LPF_ALLPASS_OFF.txt", sub, c.code + " AP OFF SUB R", slots=8)
        (export_root / "STATUS_EKSPORTU.md").write_text(
            f"# {export_status.replace('_', ' ')}\n\n{export_reason}\n\n"
            "Pliki służą wyłącznie do kontrolowanego testu z pomiarem REW przed i po Dirac.\n",
            encoding="utf-8",
        )
    else:
        (export_root / "NIE_WGRYWAC.md").write_text(
            "# DIAGNOSTYKA — NIE WGRYWAĆ\n\n" + export_reason +
            "\n\nNie zapisano żadnych plików biquad `.txt`. Dostępne są tylko współczynniki CSV i symulacja do audytu.\n",
            encoding="utf-8",
        )

    if allpass is not None and (allpass.l_spec.enabled or allpass.r_spec.enabled):
        ap_can_export = bool(allpass.recommended and import_allowed)
        on_dir_name = "ALLPASS_ON" if ap_can_export else "ALLPASS_ODRZUCONY_DIAGNOSTYKA_NIE_WGRYWAC"
        on_dir = (export_root if ap_can_export else vdir) / on_dir_name
        on_dir.mkdir(parents=True, exist_ok=True)
        sub_l_on = _sub_sections_with_allpass(c.shape, ctx.fs, allpass.l_spec)
        sub_r_on = _sub_sections_with_allpass(c.shape, ctx.fs, allpass.r_spec)

        # Tylko kandydat przechodzący wszystkie bramki otrzymuje gotowe pliki
        # miniDSP do importu. Odrzucony AP pozostaje materiałem diagnostycznym:
        # parametry + symulacja, bez aktywnych biquadów .txt.
        if ap_can_export:
            write_minidsp_biquads(on_dir / "MAIN_LR_C1_HPF_ALLPASS_ON.txt", main, c.code + " AP ON MAIN", slots=8)
            write_minidsp_biquads(on_dir / "SUB_L_REL_LPF_PLUS_ALLPASS_ON.txt", sub_l_on, c.code + " AP ON SUB L", slots=8)
            write_minidsp_biquads(on_dir / "SUB_R_REL_LPF_PLUS_ALLPASS_ON.txt", sub_r_on, c.code + " AP ON SUB R", slots=8)

        _write_response_csv(on_dir / "FILTER_I_SYMULACJA_AKUSTYCZNA_ALLPASS_ON.csv", ctx, allpass.on)
        _write_allpass_comparison_csv(vdir / "ALLPASS_OFF_VS_ON.csv", ctx, allpass)
        if not ap_can_export:
            (on_dir / "NIE_WGRYWAC.md").write_text(
                "# ALL-PASS ODRZUCONY — NIE WGRYWAĆ\n\n"
                + allpass.reason
                + "\n\nFolder zawiera wyłącznie parametry i symulację diagnostyczną. "
                  "Nie zapisano w nim plików biquad `.txt`. Użyj `ALLPASS_OFF`.\n",
                encoding="utf-8",
            )
        with (vdir / "ALLPASS_PARAMETERS.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["side", "enabled", "fc_hz", "q", "peak_group_delay_ms"])
            w.writerow(["L_SUB", allpass.l_spec.enabled, allpass.l_spec.fc_hz if allpass.l_spec.enabled else "", allpass.l_spec.q if allpass.l_spec.enabled else "", allpass.l_group_delay_peak_ms])
            w.writerow(["R_SUB", allpass.r_spec.enabled, allpass.r_spec.fc_hz if allpass.r_spec.enabled else "", allpass.r_spec.q if allpass.r_spec.enabled else "", allpass.r_group_delay_peak_ms])

    ml, mr, sl, sr = _map_relative_to_shd(c.l_rel_ms, c.r_rel_ms)
    aml, amr, asl, asr, awarn = _map_relative_to_shd_main_anchor(c.l_rel_ms, c.r_rel_ms, shd_anchor_main)
    section_lines = []
    for i, (fc, q) in enumerate(zip(c.shape.main_fc, c.shape.main_q), start=1):
        section_lines.append(f"- HPF main sekcja {i}: **{fc:.2f} Hz, Q {q:.4f}**")
    for i, (fc, q) in enumerate(zip(c.shape.sub_fc, c.shape.sub_q), start=1):
        section_lines.append(f"- LPF sub sekcja {i}: **{fc:.2f} Hz, Q {q:.4f}**")
    tonal_profile_value = {
        "REFERENCE_SAFE": c.tonal_score_reference,
        "KICK_CONTROL": c.tonal_score_kick,
        "CLEAN_BODY": c.tonal_score_clean,
    }[profile]
    if import_allowed:
        install_text = (
            f"1. Pliki do importu znajdują się wyłącznie w folderze `{export_status}`.\n"
            f"2. Test bez filtra all-pass: użyj `{export_status}/ALLPASS_OFF`.\n"
            "3. Wyłącz standardowy crossover z listy i nie kaskaduj go z filtrem customowym.\n"
            "4. Ustaw podane opóźnienia i polaryzację OFF/OFF.\n"
            "5. Wykonaj pomiar REW przed Dirac, następnie nowy Dirac i pomiar po Dirac.\n"
            "6. All-pass ON testuj tylko wtedy, gdy istnieje folder `ALLPASS_ON` i raport oznacza AP jako zalecany."
        )
    else:
        install_text = (
            "**NIE WGRYWAĆ.** Kandydat nie przeszedł bramek eksportu. "
            "Folder `DIAGNOSTYKA_NIE_WGRYWAC` nie zawiera plików biquad `.txt`; "
            "pozostawiono wyłącznie współczynniki CSV i symulację do audytu."
        )
    text = f"""# {c.code} — {PROFILE_TITLES[profile]}

## Co jest niestandardowe

Ten wariant nie jest czystym BW48 ani LR48. Każda sekcja 2. rzędu ma własne `fc` i `Q`. Współczynniki zostały wygenerowane dopiero po przejściu twardych bramek stabilności, monotoniczności i rezonansu.

{chr(10).join(section_lines)}

## Delay miniDSP SHD

Zapis minimal-offset:

- L main: **{ml:.2f} ms**
- R main: **{mr:.2f} ms**
- L sub: **{sl:.2f} ms**
- R sub: **{sr:.2f} ms**

Zapis preferowany z równymi main = {shd_anchor_main:.2f} ms:

- L main: **{aml:.2f} ms**
- R main: **{amr:.2f} ms**
- L sub: **{asl:.2f} ms**
- R sub: **{asr:.2f} ms**
- Ostrzeżenie: **{awarn or 'brak'}**

Polaryzacja: **OFF/OFF**.

## Stan gainu

- Stan projektu: **{c.gain_state_name}**
- Globalny gain L/R: **{c.global_gain_l_db:+.2f}/{c.global_gain_r_db:+.2f} dB**
- Lokalna korekta L/R: **{c.local_gain_l_db:+.2f}/{c.local_gain_r_db:+.2f} dB**
- Końcowy gain L/R: **{c.final_gain_l_db:+.2f}/{c.final_gain_r_db:+.2f} dB**
- Candidate Safety Gate: **{'PASS' if c.candidate_safe_to_export else 'BLOCKED'}**

## All-pass OFF/ON

{("- Tryb all-pass nie był uruchomiony. Folder `ALLPASS_OFF` zawiera wariant bez filtra all-pass." if allpass is None else ("- Nie znaleziono bezpiecznego all-pass przechodzącego bramki. Pozostaw `ALLPASS_OFF`." if not allpass.l_spec.enabled and not allpass.r_spec.enabled else f"- L sub: **{allpass.l_spec.label()}**; R sub: **{allpass.r_spec.label()}**.\n- Zmiana wyniku odpornego: **{allpass.score_delta:+.2f} pkt**.\n- Status: **{'ZALECANY DO TESTU A/B' if allpass.recommended else 'WARUNKOWY/TESTOWY — OFF DOMYŚLNY'}**.\n- Powód: {allpass.reason}\n- {('Włączanie: wklej pełne pliki z `ALLPASS_ON`; wyłączanie: przywróć `ALLPASS_OFF`.' if allpass.recommended else 'Wariant został odrzucony i zapisany tylko w `ALLPASS_ODRZUCONY_DIAGNOSTYKA_NIE_WGRYWAC`; użyj `ALLPASS_OFF`.')} Opóźnienia pozostają identyczne."))}

## Kalibracja realnymi pomiarami

- Status: **{'WŁĄCZONA' if c.empirical_calibration_used else 'NIE ZASTOSOWANO DO TEGO KANDYDATA'}**
- Pewność: **{c.empirical_confidence}**
- Sklejenie L/P — czysta symulacja: **{c.l_side_score_raw:.1f}/{c.r_side_score_raw:.1f}/100**
- Sklejenie L/P — po korekcie empirycznej: **{c.l_side_score:.1f}/{c.r_side_score:.1f}/100**
- Integracja techniczna — czysta symulacja: **{c.technical_score_raw:.1f}/100**
- Integracja techniczna — po korekcie empirycznej: **{c.technical_score:.1f}/100**
- Wynik profilu nominalny — czysta symulacja: **{c.nominal_scores_raw[profile]:.1f}/100**
- Wynik profilu nominalny — po korekcie i karze niepewności: **{c.nominal_scores[profile]:.1f}/100**
- Kara niepewności: **{c.empirical_uncertainty_penalty:.2f} pkt**
- Komentarz: {c.empirical_reason}

## Wyniki

- Profil: **{PROFILE_TITLES[profile]}**
- Status eksportu: **{export_status.replace('_', ' ')}** — {export_reason}
- Integracja techniczna projektanta — SYMULACJA PROJEKTU: **{c.technical_score:.1f}/100**
- Ocena tonalna profilu — SYMULACJA PROJEKTU: **{tonal_profile_value:.1f}/100**
- Zgodność fazowa main–sub — SYMULACJA PROJEKTU, L/R: **{c.l_main_sub_phase_score:.1f}/{c.r_main_sub_phase_score:.1f}/100**
- Mediana różnicy fazy main–sub L/R: **{c.l_main_sub_phase_median_deg:.1f}°/{c.r_main_sub_phase_median_deg:.1f}°**
- Wynik projektanta biquadów — SYMULACJA PROJEKTU, nominalny: **{c.nominal_scores[profile]:.1f}/100**
- Wynik projektanta biquadów — SYMULACJA PROJEKTU, końcowy: **{c.score(profile):.1f}/100**
- Odporność na błąd delayu i poziomu — SYMULACJA PROJEKTU, 20. percentyl: **{c.robust_scores[profile]:.1f}/100**
- Klasa: **{c.decision_class}**
- Efektywne XO: **{c.effective_xo_hz:.1f} Hz**
- Sklejenie stron — SYMULACJA PROJEKTU, L/R: **{c.l_side_score:.1f}/{c.r_side_score:.1f}/100**
- Jakość sumy stereo L+P — SYMULACJA PROJEKTU: **{c.lr_sum_score:.1f}/100**
- Ciągłość korpusu 100–220 Hz — SYMULACJA PROJEKTU: **{c.body_score:.1f}/100**
- Autorytet fundamentu 40–80 Hz — SYMULACJA PROJEKTU: **{c.authority_score:.1f}/100**
- Impuls 60–100 Hz — SYMULACJA PROJEKTU: **{c.kick_score:.1f}/100**
- Podobieństwo do referencji 90: **{c.reference_score:.1f}/100**, RMS **{c.reference_rms_db:.2f} dB**
- Fundament 40–80 względem 90: **{c.bass_delta_vs_ref_db:+.2f} dB**
- Symulowane ryzyko dziury L+P — SYMULACJA PROJEKTU: **{c.null_risk:.1f}/100**
- Symulowane obciążenie korekcji Dirac — SYMULACJA PROJEKTU: **{c.dirac_burden:.1f}/100**

## Korekcja głównie przez tłumienie

- Stan subów podczas pomiaru RAW: **{ctx.raw_sub_trim_db:+.1f} dB**
- Stan projektu **{ctx.gain_state_name}** skaluje zespolone odpowiedzi L/R o **{ctx.global_gain_l_db:+.2f}/{ctx.global_gain_r_db:+.2f} dB**.
- Tryb aktywny: **{'TAK' if ctx.cut_only_enabled else 'NIE'}**
- Możliwość korekcji głównie przez tłumienie — SYMULACJA PROJEKTU: **{c.cut_only_score:.1f}/100**
- Status: **{'SPEŁNIA BRAMKĘ' if c.cut_only_safe else 'WARUNKOWY — przewidywana potrzeba podbijania'}**
- Średnia przewidywana potrzeba podbicia: **{c.boost_need_mean_db:.2f} dB**
- P95 przewidywanej potrzeby podbicia: **{c.boost_need_p95_db:.2f} dB**
- Udział pasma 40–180 Hz z niedoborem >1 dB: **{c.deficit_share_pct:.1f}%**
- Mediana zapasu do cięcia: **{c.cut_headroom_db:.2f} dB**
- Margines fundamentu 40–70 Hz: **{c.foundation_margin_db:+.2f} dB**
- Margines masy 70–120 Hz: **{c.mass_margin_db:+.2f} dB**
- Margines korpusu 125–180 Hz: **{c.body_margin_db:+.2f} dB**
- Komentarz: {c.cut_only_reason}

Ta ocena dotyczy stanu projektowego. Osobna bramka sprawdza identyczny filtr w stanie Dirac i odsłuchowym.

## Audyt filtra

- Status: **{'SAFE' if c.safety.safe else 'NIEBEZPIECZNY'}**
- Peak: **{c.safety.peak_db:.3f} dB**
- Naruszenie monotoniczności: **{c.safety.monotonic_violation_db:.3f} dB**
- Ripple passbandu: **{c.safety.passband_ripple_db:.3f} dB**
- HPF @ 1/2 fc: **{c.safety.hp_stop_db:.1f} dB**
- LPF @ 2× fc: **{c.safety.lp_stop_db:.1f} dB**
- Maksymalny promień bieguna: **{c.safety.max_pole_radius:.8f}**
- Komentarz: {c.safety.reason}

## Wgrywanie

{install_text}

## Ograniczenie

Projektant kontroluje odporność na drobny błąd opóźnienia i poziomu, ale nie zastępuje pomiaru wielopozycyjnego ani odsłuchu. Referencja 90 chroni przede wszystkim średnicę. Eksport wymaga zweryfikowanego Gain Bridge oraz przejścia Candidate Safety Gate w stanie Dirac i odsłuchowym.
"""
    (vdir / "README_WARIANTU.md").write_text(text, encoding="utf-8")


def _write_all_csv(path: Path, candidates: Sequence[CustomCandidate]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        header = [
            "origin", "effective_xo_hz", "l_rel_ms", "r_rel_ms",
            *[f"main_fc_{i}" for i in range(1, 5)], *[f"main_q_{i}" for i in range(1, 5)],
            *[f"sub_fc_{i}" for i in range(1, 5)], *[f"sub_q_{i}" for i in range(1, 5)],
            "side_l_base_simulation_0_100", "side_r_base_simulation_0_100",
            "side_l_empirical_calibrated_0_100", "side_r_empirical_calibrated_0_100",
            "lr_sum_project_0_100", "body_continuity_project_0_100", "authority_project_0_100", "kick_project_0_100", "phase_lr_sum_project_0_100",
            "phase_main_sub_l", "phase_main_sub_r", "phase_main_sub_min",
            "phase_main_sub_l_median_deg", "phase_main_sub_r_median_deg",
            "technical_base_simulation_0_100", "technical_empirical_calibrated_0_100",
            "tonal_reference_project_0_100", "tonal_kick_project_0_100", "tonal_clean_project_0_100",
            "lr_null_risk_project_0_100", "dirac_burden_project_0_100", "reference_similarity_project_0_100",
            "reference_rms_db", "bass_delta_vs_ref_db",
            "empirical_calibration_used", "empirical_confidence", "empirical_uncertainty_penalty", "empirical_reason",
            "cut_only_score", "cut_only_safe", "boost_need_mean_db", "boost_need_p95_db", "deficit_share_pct",
            "cut_headroom_db", "foundation_margin_db", "mass_margin_db", "body_margin_db", "cut_only_reason",
            "safe", "peak_db", "monotonic_violation_db", "passband_ripple_db", "hp_stop_db", "lp_stop_db", "max_pole_radius",
            "reference_safe_nominal_base", "reference_safe_nominal_calibrated", "reference_safe_robust20", "reference_safe_final",
            "kick_control_nominal_base", "kick_control_nominal_calibrated", "kick_control_robust20", "kick_control_final",
            "clean_body_nominal_base", "clean_body_nominal_calibrated", "clean_body_robust20", "clean_body_final",
            "global_gain_l_db", "global_gain_r_db", "local_gain_l_db", "local_gain_r_db",
            "final_gain_l_db", "final_gain_r_db", "gain_state_name",
            "candidate_safe_to_export", "candidate_safety_reasons", "gain_state_metrics_json",
        ]
        w.writerow(header)
        for c in candidates:
            row = [
                c.origin, c.effective_xo_hz, c.l_rel_ms, c.r_rel_ms,
                *c.shape.main_fc, *c.shape.main_q, *c.shape.sub_fc, *c.shape.sub_q,
                c.l_side_score_raw, c.r_side_score_raw, c.l_side_score, c.r_side_score,
                c.lr_sum_score, c.body_score, c.authority_score, c.kick_score, c.phase_energy_score,
                c.l_main_sub_phase_score, c.r_main_sub_phase_score, c.main_sub_phase_min,
                c.l_main_sub_phase_median_deg, c.r_main_sub_phase_median_deg,
                c.technical_score_raw, c.technical_score,
                c.tonal_score_reference, c.tonal_score_kick, c.tonal_score_clean,
                c.null_risk, c.dirac_burden, c.reference_score, c.reference_rms_db, c.bass_delta_vs_ref_db,
                c.empirical_calibration_used, c.empirical_confidence, c.empirical_uncertainty_penalty, c.empirical_reason,
                c.cut_only_score, c.cut_only_safe, c.boost_need_mean_db, c.boost_need_p95_db, c.deficit_share_pct,
                c.cut_headroom_db, c.foundation_margin_db, c.mass_margin_db, c.body_margin_db, c.cut_only_reason,
                c.safety.safe, c.safety.peak_db, c.safety.monotonic_violation_db, c.safety.passband_ripple_db,
                c.safety.hp_stop_db, c.safety.lp_stop_db, c.safety.max_pole_radius,
                c.nominal_scores_raw["REFERENCE_SAFE"], c.nominal_scores["REFERENCE_SAFE"], c.robust_scores.get("REFERENCE_SAFE", float("nan")), c.final_scores.get("REFERENCE_SAFE", float("nan")),
                c.nominal_scores_raw["KICK_CONTROL"], c.nominal_scores["KICK_CONTROL"], c.robust_scores.get("KICK_CONTROL", float("nan")), c.final_scores.get("KICK_CONTROL", float("nan")),
                c.nominal_scores_raw["CLEAN_BODY"], c.nominal_scores["CLEAN_BODY"], c.robust_scores.get("CLEAN_BODY", float("nan")), c.final_scores.get("CLEAN_BODY", float("nan")),
                c.global_gain_l_db, c.global_gain_r_db, c.local_gain_l_db, c.local_gain_r_db,
                c.final_gain_l_db, c.final_gain_r_db, c.gain_state_name,
                c.candidate_safe_to_export, " | ".join(c.safety_reasons),
                json.dumps(c.state_metrics, ensure_ascii=False, sort_keys=True),
            ]
            w.writerow(row)


def _write_polarity_diagnostic(
    out_dir: Path,
    ctx: EvaluationContext,
    selected: Sequence[Tuple[str, CustomCandidate]],
) -> None:
    """Porównaj polaryzacje bez automatycznej zmiany eksportowanych ustawień."""
    lines = [
        "# 08 — Diagnostyka polaryzacji",
        "",
        "Projektant eksportuje domyślnie OFF/OFF. Poniższa tabela sprawdza, czy pojedyncze lub podwójne odwrócenie daje wyraźną poprawę techniczną. Wynik nie zmienia automatycznie plików do miniDSP.",
        "",
        "| Wariant | Polaryzacja L/R sub | Integracja techniczna — SYMULACJA PROJEKTU | Zgodność fazowa main–sub L/R | Sklejenie stron L/R — SYMULACJA PROJEKTU | Jakość sumy L+P — SYMULACJA PROJEKTU | Symulowane ryzyko dziury L+P | Δ techniczna vs OFF/OFF |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for profile, base in selected:
        combos = [
            (False, False, "OFF/OFF"),
            (True, False, "INV/OFF"),
            (False, True, "OFF/INV"),
            (True, True, "INV/INV"),
        ]
        evaluated = []
        for li, ri, label in combos:
            c = _evaluate(
                ctx,
                base.shape,
                base.l_rel_ms,
                base.r_rel_ms,
                f"polarity_{label}",
                l_invert=li,
                r_invert=ri,
            )
            evaluated.append((label, c))
        off_tech = evaluated[0][1].technical_score
        for label, c in evaluated:
            lines.append(
                f"| `{base.code}` | {label} | {c.technical_score:.1f} | "
                f"{c.l_main_sub_phase_score:.1f}/{c.r_main_sub_phase_score:.1f} | "
                f"{c.l_side_score:.1f}/{c.r_side_score:.1f} | {c.lr_sum_score:.1f} | {c.null_risk:.1f} | "
                f"{c.technical_score-off_tech:+.1f} |"
            )
        best_label, best = max(evaluated, key=lambda x: x[1].technical_score)
        if best_label != "OFF/OFF" and best.technical_score >= off_tech + 5.0 and best.lr_sum_score >= evaluated[0][1].lr_sum_score - 1.0:
            lines += [
                "",
                f"**{base.code}:** warto wykonać osobny kontrolny test `{best_label}`; poprawa techniczna {best.technical_score-off_tech:+.1f} pkt. Nie zmieniono eksportu automatycznie.",
                "",
            ]
        else:
            lines += ["", f"**{base.code}:** brak jednoznacznej podstawy do odejścia od OFF/OFF.", ""]
    (out_dir / "08_DIAGNOSTYKA_POLARYZACJI.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

def _load_spatial_context(
    mdat_path: Path,
    *,
    fs: float,
    reference_mdat: Optional[Path],
    l_center: float,
    r_center: float,
    raw_sub_trim_db: float,
    cut_only_enabled: bool,
    empirical_calibration: Optional[EmpiricalCalibration] = None,
) -> EvaluationContext:
    lm, ls, rm, rs, project = load_optimizer_measurements_from_mdat(mdat_path)
    audit = _audit_input_for_acoustic_design(mdat_path, project.role_to_name)
    if _audit_is_diagnostic_only(audit[0], audit[1]):
        raise ValueError(f"pomiar przestrzenny nie przeszedł bramki RAW: {audit[1]}")
    freq = common_grid([lm, rm, ls, rs], points=1500)
    freq = freq[(freq >= 20.0) & (freq <= 500.0)]
    lm_raw = measurement_to_complex(lm, freq)
    rm_raw = measurement_to_complex(rm, freq)
    ls_raw = measurement_to_complex(ls, freq)
    rs_raw = measurement_to_complex(rs, freq)
    ref_sum, ref_source = _load_reference_sum(
        reference_mdat,
        freq,
        raw=(lm_raw, rm_raw, ls_raw, rs_raw),
        fs=fs,
        l_rel_ref=l_center,
        r_rel_ref=r_center,
    )
    return EvaluationContext(
        freq=freq,
        fs=fs,
        lm_raw=lm_raw,
        rm_raw=rm_raw,
        ls_raw=ls_raw,
        rs_raw=rs_raw,
        ref_sum=ref_sum,
        ref_source=ref_source,
        empirical_calibration=empirical_calibration,
        raw_sub_trim_db=float(raw_sub_trim_db),
        cut_only_enabled=bool(cut_only_enabled),
    )


def _spatial_verification(
    selected: Sequence[Tuple[str, CustomCandidate]],
    contexts: Sequence[Tuple[Path, EvaluationContext]],
    out_dir: Path,
) -> None:
    if not contexts:
        return
    rows: List[Tuple[str, str, str, float, float, float, float]] = []
    for profile, c in selected:
        vals: List[float] = []
        for path, ctx in contexts:
            x = _evaluate(ctx, c.shape, c.l_rel_ms, c.r_rel_ms, f"spatial_{path.stem}")
            vals.append(x.technical_score)
            rows.append((c.code, PROFILE_TITLES[profile], path.name, x.technical_score, x.l_main_sub_phase_score, x.r_main_sub_phase_score, x.lr_sum_score))
        c.spatial_count = len(vals)
        c.spatial_technical_min = float(min(vals)) if vals else float("nan")
        c.spatial_technical_mean = float(np.mean(vals)) if vals else float("nan")
    lines = [
        "# 07 — Odporność przestrzenna",
        "",
        "Każdy dodatkowy MDAT musi zawierać osobne RAW L main, R main, L sub i R sub wykonane z identycznymi ustawieniami. Wynik służy jako bramka eksportu, ale nie zastępuje pomiaru po Dirac.",
        "",
        "| Wariant | Profil | Pomiar | Integracja techniczna | Faza main–sub L/R | L+P |",
        "|---|---|---|---:|---:|---:|",
    ]
    for code, title, name, tech, lp, rp, lr in rows:
        lines.append(f"| `{code}` | {title} | `{name}` | {tech:.1f} | {lp:.1f}/{rp:.1f} | {lr:.1f} |")
    lines += ["", "## Podsumowanie", ""]
    for profile, c in selected:
        lines.append(
            f"- **{c.code}:** średnia {c.spatial_technical_mean:.1f}/100, minimum {c.spatial_technical_min:.1f}/100 z {c.spatial_count} dodatkowych pozycji."
        )
    (out_dir / "07_ODPORNOSC_PRZESTRZENNA.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

def write_reports(
    out_dir: Path,
    selected: Sequence[Tuple[str, CustomCandidate]],
    baseline: CustomCandidate,
    standard: Sequence[CustomCandidate],
    custom: Sequence[CustomCandidate],
    *,
    mdat_path: Path,
    input_audit: Tuple[str, str, str],
    ctx: EvaluationContext,
    diagnostic_only: bool,
    shd_anchor_main: float,
    allpass_results: Optional[Dict[str, AllPassComparison]] = None,
    allpass_mode: str = "off",
    run_winners: Optional[Sequence[Dict[str, object]]] = None,
) -> None:
    status, decision, note = input_audit
    lines = [
        "# 01 — Co teraz zrobić: projektant biquadów akustycznych",
        "",
        f"Wersja: **{APP_VERSION}**",
        f"RAW: `{mdat_path.name}`",
        f"Referencja średnicy: **{ctx.ref_source}**",
        f"All-pass: **{allpass_mode.upper()}** (domyślne ustawienie odsłuchowe: OFF)",
        "",
        "## Audyt wejścia",
        "",
        f"- Status: **{status}**",
        f"- Decyzja: **{decision}**",
        f"- Komentarz: {note}",
        "",
    ]
    cut_safe_count = sum(1 for _, c in selected if c.cut_only_safe)
    best_cut = max((c for _, c in selected), key=lambda c: c.cut_only_score) if selected else None
    export_rows = [(profile, c, *_export_status(profile, c)) for profile, c in selected]
    export_counts = {
        key: sum(1 for _, _, status_name, _ in export_rows if status_name == key)
        for key in ("DO_TESTU", "TEST_WARUNKOWY", "DIAGNOSTYKA_NIE_WGRYWAC")
    }
    lines += [
        "## Ocena fundamentu i korekcji przez tłumienie",
        "",
        f"- Stan RAW subów: **{ctx.raw_sub_trim_db:+.1f} dB**; projekt policzony w stanie **{ctx.gain_state_name}**, globalne skalowanie L/R **{ctx.global_gain_l_db:+.2f}/{ctx.global_gain_r_db:+.2f} dB**.",
        f"- Warianty spełniające pomocniczą ocenę korekcji głównie przez tłumienie: **{cut_safe_count}/{len(selected)}**",
        f"- Eksport: DO TESTU **{export_counts['DO_TESTU']}**, TEST WARUNKOWY **{export_counts['TEST_WARUNKOWY']}**, DIAGNOSTYKA BEZ PLIKÓW TXT **{export_counts['DIAGNOSTYKA_NIE_WGRYWAC']}**.",
    ]
    if best_cut is not None and cut_safe_count == 0:
        lines += [
            f"- Najlepszy wynik tej metryki: margines fundamentu **{best_cut.foundation_margin_db:+.2f} dB**, masy **{best_cut.mass_margin_db:+.2f} dB**, korpusu **{best_cut.body_margin_db:+.2f} dB**; potrzeba podbicia P95 **{best_cut.boost_need_p95_db:.2f} dB**.",
            "- To nie jest bezpośrednia rekomendacja podniesienia gainu o tę wartość: zmiana poziomu wpływa również na rzeczywiste przecięcie i sumowanie.",
            "- Schodzenie z RAW do -14 lub -16 dB nie pomoże zachować fundamentu; takie pomiary mogą służyć wyłącznie diagnostyce fazy i odporności.",
            "",
        ]
    elif best_cut is not None:
        lines += [
            "- Co najmniej jeden wariant ma wystarczający zapas energii, aby docelowy kształt uzyskiwać głównie przez tłumienie.",
            "",
        ]

    if diagnostic_only:
        lines += [
            "## Wynik",
            "",
            "**TYLKO DIAGNOSTYKA — NIE WYEKSPORTOWANO BIQUADÓW DO WGRANIA.**",
            "",
            "Wykonaj czysty RAW: Dirac OFF, crossover OFF, PEQ OFF, delay 0, REL120; osobno L main, R main, L sub, R sub.",
            "",
        ]
    else:
        if ctx.cut_only_enabled and cut_safe_count == 0:
            lines += [
                "## Wygenerowane warianty warunkowe",
                "",
                "**Brak wariantu spełniającego zasadę korekcji głównie przez tłumienie.**",
                "",
                "Pliki do importu są rozdzielone według bramek eksportu. Wariant diagnostyczny nie otrzymuje plików `.txt`; wariant warunkowy trafia wyłącznie do folderu `TEST_WARUNKOWY`.",
                "",
            ]
        else:
            lines += [
                "## Wygenerowane warianty",
                "",
                "Do kontrolowanego testu używaj wyłącznie wariantów z folderem `DO_TESTU` albo `TEST_WARUNKOWY`. Wariant `DIAGNOSTYKA_NIE_WGRYWAC` pomiń.",
                "",
            ]
        for profile, c in selected:
            delta = c.score(profile) - baseline.nominal_scores[profile]
            ap = (allpass_results or {}).get(c.code)
            ap_txt = "" if ap is None else f" All-pass ON vs OFF: {ap.score_delta:+.2f} pkt ({'zalecany' if ap.recommended else 'odrzucony — tylko diagnostyka'})."
            export_status, export_reason = _export_status(profile, c)
            lines.append(
                f"{c.rank}. **{c.code}** — {PROFILE_TITLES[profile]}, integracja techniczna **{c.technical_score:.1f}/100**, "
                f"wynik odporny **{c.score(profile):.1f}/100**, status **{export_status.replace('_', ' ')}**, "
                f"Δ vs najlepszy standard {delta:+.1f} pkt. {export_reason}." + ap_txt
            )
        if ctx.cut_only_enabled and cut_safe_count == 0:
            lines += [
                "",
                "## Co zrobić przy braku wariantu spełniającego pomocniczą ocenę tonalną",
                "",
                "1. Nie obniżaj poziomu RAW wyłącznie po to, aby zwiększyć liczbę punktów — ta metryka nie jest główną oceną crossovera.",
                "2. Najpierw sprawdź integrację techniczną, fazę main–sub i status eksportu każdego wariantu.",
                "3. Wariant z folderu `TEST_WARUNKOWY` można sprawdzić tylko kontrolowanym pomiarem REW; wariant diagnostyczny nie ma plików do importu.",
                "4. Ostateczną ilość oraz jakość basu oceniaj po rzeczywistym Dirac, a referencję 90 BW48 traktuj głównie jako wzorzec średnicy.",
                "",
            ]
        else:
            lines += [
                "",
                "## Zalecana kolejność",
                "",
                "1. Zacznij od najwyżej ocenionego wariantu, który ma folder `DO_TESTU` albo `TEST_WARUNKOWY`; nie używaj wariantu diagnostycznego.",
                "2. `KICK_CONTROL` porównaj, gdy potrzebujesz więcej impulsu; `CLEAN_BODY` — gdy 90 BW48 pozostaje zbyt ciężki.",
                "3. Każdy wariant: zastosuj filtr → kontrolny pomiar REW → nowy Dirac → pomiar po Dirac w posL / MLP / posP.",
                "4. Finalnie porównaj z realnym 90 BW48 na wokalach i instrumentach akustycznych.",
                "",
            ]
    (out_dir / "01_CO_TERAZ_ZROBIC_ACOUSTIC_BIQUAD.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    table = [
        "# 02 — Trzy profile niestandardowego crossovera",
        "",
        "| Profil | Kod | Integracja przed kalibracją → po kalibracji | Tonalność profilu | Wynik nominalny przed kalibracją → po kalibracji | Odporność P20 | Wynik końcowy | XO | Faza main–sub L/R | Zgodność L/R przed kalibracją → po kalibracji | Suma L+P | Korpus | Impuls | Ref. 90 | Głównie tłumienie | Obciążenie Dirac |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for profile, c in selected:
        table.append(
            f"| {PROFILE_TITLES[profile]} | `{c.code}` | {c.technical_score_raw:.1f} → {c.technical_score:.1f} | "
            f"{({'REFERENCE_SAFE': c.tonal_score_reference, 'KICK_CONTROL': c.tonal_score_kick, 'CLEAN_BODY': c.tonal_score_clean}[profile]):.1f} | "
            f"{c.nominal_scores_raw[profile]:.1f} → {c.nominal_scores[profile]:.1f} | {c.robust_scores[profile]:.1f} | {c.score(profile):.1f} | {c.effective_xo_hz:.1f} | "
            f"{c.l_main_sub_phase_score:.1f}/{c.r_main_sub_phase_score:.1f} | {c.l_side_score_raw:.1f}/{c.r_side_score_raw:.1f} → {c.l_side_score:.1f}/{c.r_side_score:.1f} | "
            f"{c.lr_sum_score:.1f} | {c.body_score:.1f} | {c.kick_score:.1f} | {c.reference_score:.1f} | "
            f"{c.cut_only_score:.1f} | {c.dirac_burden:.1f} |"
        )
    table += [
        "",
        "## Najlepszy standardowy punkt startowy",
        "",
        f"- Źródło: **{baseline.origin}**",
        f"- Efektywne XO: **{baseline.effective_xo_hz:.1f} Hz**",
        f"- Delaye relacyjne L/R: **{baseline.l_rel_ms:.2f}/{baseline.r_rel_ms:.2f} ms**",
        f"- Wynik standardowego punktu startowego — SYMULACJA PROJEKTU: **{baseline.nominal_scores['REFERENCE_SAFE']:.1f}/100**",
        "",
        "Różnica dodatnia względem standardu oznacza poprawę w modelu projektanta, a nie gwarantowaną poprawę odsłuchową.",
    ]
    (out_dir / "02_WARIANTY_REFERENCE_SAFE_KICK_CLEAN.md").write_text("\n".join(table) + "\n", encoding="utf-8")

    audit = [
        "# 03 — Audyt projektanta biquadów akustycznych",
        "",
        "## Co moduł robi naprawdę",
        "",
        "- optymalizuje osobne `fc` i `Q` czterech sekcji HPF oraz czterech sekcji LPF;",
        "- nie losuje bezpośrednio współczynników b0/b1/b2/a1/a2;",
        "- każdą sekcję tworzy ze stabilnej postaci RBJ;",
        "- odrzuca kaskady z peak >0,35 dB, niemonotonicznością, słabym stopbandem lub biegunem przy granicy stabilności;",
        "- ocenia odporność na ±0,2 ms delayu i ±0,5 dB wspólnego poziomu subów;",
        "- chroni kształt 100–220 Hz względem realnego 90 BW48, ale pozwala odjąć ciężar 40–80 Hz;",
        "- koryguje przewidywane sklejenie na podstawie realnych pomiarów tego systemu;",
        "- po znalezieniu kształtu wykonuje deterministyczne dogęszczenie opóźnień L/R.",
        "",
        "## Ważne ograniczenie Q",
        "",
        "Nie narzucono sztucznego limitu Q=1,2. Standardowy BW48 sam zawiera sekcję o Q około 2,56. Bezpieczeństwo jest oceniane po całej kaskadzie: jej peak, monotoniczność, stabilność i stopband są ważniejsze niż pojedyncza liczba Q.",
        "",
        "## Statystyka wyszukiwania",
        "",
        f"- bezpieczne standardowe warianty: **{len(standard)}**",
        f"- bezpieczne niestandardowe warianty: **{len(custom)}**",
        f"- referencja: **{ctx.ref_source}**",
        "",
    ]
    for profile, c in selected:
        audit += [
            f"## {c.code}",
            "",
            f"- status filtra: **{'SAFE' if c.safety.safe else 'NIEBEZPIECZNY'}**",
            f"- peak: {c.safety.peak_db:.3f} dB",
            f"- monotoniczność: {c.safety.monotonic_violation_db:.3f} dB",
            f"- ripple: {c.safety.passband_ripple_db:.3f} dB",
            f"- HP stop: {c.safety.hp_stop_db:.1f} dB; LP stop: {c.safety.lp_stop_db:.1f} dB",
            f"- max pole radius: {c.safety.max_pole_radius:.8f}",
            f"- integracja techniczna — czysta symulacja / po kalibracji: {c.technical_score_raw:.1f}/{c.technical_score:.1f}/100",
            f"- sklejenie L/P — czysta symulacja: {c.l_side_score_raw:.1f}/{c.r_side_score_raw:.1f}/100",
            f"- sklejenie L/P — po kalibracji: {c.l_side_score:.1f}/{c.r_side_score:.1f}/100",
            f"- pewność kalibracji: {c.empirical_confidence}; kara niepewności {c.empirical_uncertainty_penalty:.2f} pkt",
            f"- zgodność fazowa main–sub L/R: {c.l_main_sub_phase_score:.1f}/{c.r_main_sub_phase_score:.1f}/100 (mediana {c.l_main_sub_phase_median_deg:.1f}°/{c.r_main_sub_phase_median_deg:.1f}°)",
            f"- korekcja przez tłumienie: {c.cut_only_score:.1f}/100; {'SPEŁNIA' if c.cut_only_safe else 'WARUNKOWY'}",
            f"- potrzeba podbicia średnia/P95: {c.boost_need_mean_db:.2f}/{c.boost_need_p95_db:.2f} dB",
            f"- fundament/masa/korpus: {c.foundation_margin_db:+.2f}/{c.mass_margin_db:+.2f}/{c.body_margin_db:+.2f} dB",
            f"- rekomendacja: {c.recommendation}",
            "",
        ]
    (out_dir / "03_AUDYT_I_BEZPIECZENSTWO.md").write_text("\n".join(audit) + "\n", encoding="utf-8")

    cut_lines = [
        "# 04A — Korekcja głównie przez tłumienie",
        "",
        f"- Stan RAW subów: **{ctx.raw_sub_trim_db:+.1f} dB**; ta wartość jest składnikiem jawnego GainState.",
        f"- Tryb oceny: **{'WŁĄCZONY' if ctx.cut_only_enabled else 'WYŁĄCZONY'}**",
        "- Aplikacja symuluje ten sam filtr w stanie Dirac i odsłuchowym; eksport wymaga przejścia obu stanów.",
        "- Cel basowy jest względny wobec własnego poziomu 180–220 Hz kandydata; nie kopiuje bezpośrednio basu referencji 90 BW48 wykonanego w innym stanie gainu/Dirac.",
        "- To pomocnicza ocena tonalna. Nie może przeważyć nad integracją techniczną main–sub.",
        "",
        "| Wariant | Ocena | Status | Podbicie średnie | Podbicie P95 | Pasmo z niedoborem >1 dB | Zapas do cięcia | Fundament | Masa | Korpus |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for profile, c in selected:
        cut_lines.append(
            f"| `{c.code}` | {c.cut_only_score:.1f} | {'SPEŁNIA' if c.cut_only_safe else 'WARUNKOWY'} | "
            f"{c.boost_need_mean_db:.2f} dB | {c.boost_need_p95_db:.2f} dB | {c.deficit_share_pct:.1f}% | "
            f"{c.cut_headroom_db:.2f} dB | {c.foundation_margin_db:+.2f} dB | {c.mass_margin_db:+.2f} dB | {c.body_margin_db:+.2f} dB |"
        )
        cut_lines += ["", f"**{c.code}:** {c.cut_only_reason}", ""]
    cut_lines += [
        "## Interpretacja",
        "",
        "- `SPEŁNIA`: kształt może być w przybliżeniu osiągnięty głównie przez tłumienie.",
        "- `WARUNKOWY`: występuje szeroki lub głęboki niedobór; Dirac prawdopodobnie musiałby podbijać część pasma.",
        "- Dodatni margines oznacza zapas energii ponad cel, który można ciąć. Ujemny oznacza niedobór.",
        "- Ostateczną ilość i jakość basu ocenia się dopiero z rzeczywistego pomiaru po Dirac.",
        "- Pomiar przy -14/-16 dB nie jest sposobem na odzyskanie fundamentu. Niższy poziom może pomóc diagnostycznie, lecz zwiększa ryzyko chudego wyniku i potrzeby podbijania.",
        "- Jeżeli żaden wariant przy -6 dB nie spełnia bramki, aplikacja ma to pokazać wprost zamiast sztucznie podwyższać wynik.",
    ]
    (out_dir / "04A_KOREKCJA_GLOWNIE_PRZEZ_TLUMIENIE.md").write_text("\n".join(cut_lines) + "\n", encoding="utf-8")

    ap_lines = [
        "# 05 — All-pass OFF vs ON",
        "",
        "All-pass jest opcjonalny i domyślnie pozostaje **OFF**. Po wybraniu porównania aplikacja zawsze oblicza OFF/ON. Tylko AP przechodzący bramki oraz limit peak group delay **≤6 ms** trafia do `ALLPASS_ON`; wariant odrzucony jest izolowany w `ALLPASS_ODRZUCONY_DIAGNOSTYKA_NIE_WGRYWAC` bez plików biquad `.txt`. Delaye i podstawowy crossover są identyczne w obu stanach.",
        "",
        "| Profil | L sub AP | R sub AP | Δ wynik odporny ON−OFF | Peak GD L/R | Decyzja |",
        "|---|---|---|---:|---:|---|",
    ]
    if not allpass_results:
        status_text = "pominięte przez bramkę RAW/diagnostykę" if diagnostic_only and allpass_mode == "compare" else "wyszukiwanie all-pass wyłączone"
        ap_lines.append(f"| — | OFF | OFF | 0,00 | 0/0 ms | {status_text} |")
    else:
        for profile, c in selected:
            apc = allpass_results.get(c.code)
            if apc is None:
                continue
            ap_lines.append(
                f"| {PROFILE_TITLES[profile]} | {apc.l_spec.label()} | {apc.r_spec.label()} | "
                f"{apc.score_delta:+.2f} | {apc.l_group_delay_peak_ms:.2f}/{apc.r_group_delay_peak_ms:.2f} ms | "
                f"{'ZALECANY A/B' if apc.recommended else 'ODRZUCONY — NIE WGRYWAĆ'} |"
            )
            ap_lines += ["", f"**{c.code}:** {apc.reason}", ""]
    ap_lines += [
        "## Jak przełączać",
        "",
        "- **OFF:** użyj folderu `DO_TESTU/ALLPASS_OFF` albo `TEST_WARUNKOWY/ALLPASS_OFF` danego wariantu.",
        "- **ON:** użyj `ALLPASS_ON` wyłącznie w tej samej gałęzi eksportu i tylko wtedy, gdy raport oznacza AP jako zalecany.",
        "- Folder `ALLPASS_ODRZUCONY_DIAGNOSTYKA_NIE_WGRYWAC` nie jest ustawieniem odsłuchowym i nie zawiera importowalnych biquadów.",
        "- Nie zmieniaj opóźnień ani poziomu subów między OFF i ON.",
        "- Po każdej zmianie kliknij `Process and Apply`; amplituda pojedynczego suba powinna pozostać praktycznie taka sama.",
    ]
    (out_dir / "05_ALLPASS_OFF_VS_ON.md").write_text("\n".join(ap_lines) + "\n", encoding="utf-8")

    _write_all_csv(out_dir / "99_ALL_CUSTOM_CANDIDATES.csv", custom)
    with (out_dir / "04_POROWNANIE_STANDARD_VS_CUSTOM.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "profile", "standard_score", "custom_code",
            "technical_base_simulation", "technical_empirical_calibrated", "tonal_score",
            "custom_nominal_base", "custom_nominal_calibrated", "custom_robust20", "custom_final", "delta_vs_standard",
            "side_l_base", "side_r_base", "side_l_calibrated", "side_r_calibrated",
            "empirical_confidence", "empirical_uncertainty_penalty",
            "main_sub_phase_l", "main_sub_phase_r", "dirac_burden",
            "global_gain_l_db", "global_gain_r_db", "local_gain_l_db", "local_gain_r_db",
            "final_gain_l_db", "final_gain_r_db", "gain_state_name", "candidate_safe_to_export",
            "gain_state_metrics_json", "export_status", "export_reason",
        ])
        for profile, c in selected:
            export_status, export_reason = _export_status(profile, c)
            tonal_value = {
                "REFERENCE_SAFE": c.tonal_score_reference,
                "KICK_CONTROL": c.tonal_score_kick,
                "CLEAN_BODY": c.tonal_score_clean,
            }[profile]
            w.writerow([
                profile, baseline.nominal_scores[profile], c.code,
                c.technical_score_raw, c.technical_score, tonal_value,
                c.nominal_scores_raw[profile], c.nominal_scores[profile], c.robust_scores[profile], c.score(profile),
                c.score(profile) - baseline.nominal_scores[profile],
                c.l_side_score_raw, c.r_side_score_raw, c.l_side_score, c.r_side_score,
                c.empirical_confidence, c.empirical_uncertainty_penalty,
                c.l_main_sub_phase_score, c.r_main_sub_phase_score, c.dirac_burden,
                c.global_gain_l_db, c.global_gain_r_db, c.local_gain_l_db, c.local_gain_r_db,
                c.final_gain_l_db, c.final_gain_r_db, c.gain_state_name, c.candidate_safe_to_export,
                json.dumps(c.state_metrics, ensure_ascii=False, sort_keys=True), export_status, export_reason,
            ])

    if run_winners:
        _write_convergence_report(out_dir, run_winners)

    if ctx.cut_only_enabled and cut_safe_count == 0 and not diagnostic_only:
        (out_dir / "BRAK_WARIANTU_TYLKO_CIECIE.md").write_text(
            "# Brak wariantu spełniającego zasadę korekcji głównie przez tłumienie\n\n"
            "Żaden z wybranych wariantów nie spełnił pomocniczej oceny korekcji głównie przez tłumienie. Status eksportu każdego filtra jest wyznaczany osobno przez integrację techniczną i bezpieczeństwo; wariant diagnostyczny nie zawiera plików `.txt`.\n",
            encoding="utf-8",
        )

    if diagnostic_only:
        guard = out_dir / "DIAGNOSTYKA_NIE_WGRYWAC"
        guard.mkdir(parents=True, exist_ok=True)
        (guard / "README.md").write_text(
            "# DIAGNOSTYKA — NIE WGRYWAĆ\n\nBiquady zostały zablokowane, ponieważ wejście nie przeszło bramki RAW XOVER_OFF REL120 DELAY0.\n",
            encoding="utf-8",
        )
    else:
        for profile, c in selected:
            if c.candidate_safe_to_export:
                _write_variant(out_dir, profile, c, ctx, shd_anchor_main, (allpass_results or {}).get(c.code))


def design_acoustic_biquads(
    mdat_path: Path,
    out_dir: Path,
    *,
    fs: float = 96000.0,
    user_reference_path: Optional[Path] = None,
    reference_mdat: Optional[Path] = None,
    search_level: str = "normal",
    random_seed: int = 20260710,
    shd_anchor_main: float = 30.0,
    allpass_mode: str = "off",
    raw_sub_trim_db: float = -6.0,
    dirac_sub_trim_db: Optional[float] = -4.0,
    listening_sub_trim_db: Optional[float] = 0.0,
    gain_bridge_path: Optional[Path] = None,
    allow_unverified_gain_override: bool = False,
    effective_xo_min_hz: float = 70.0,
    effective_xo_max_hz: float = 105.0,
    cut_only_mode: str = "on",
    search_repeats: int = 1,
    spatial_mdat_paths: Optional[Sequence[Path]] = None,
    empirical_calibration_path: Optional[Path] = None,
    empirical_calibration_enabled: bool = False,
    delay_refinement_enabled: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    reference_settings: Optional[Dict[str, object]] = None,
    search_frequencies: Optional[List[float]] = None,
    prepare_import_files: bool = True,
) -> List[Tuple[str, CustomCandidate]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_metric_registry(out_dir)
    lm, ls, rm, rs, project = load_optimizer_measurements_from_mdat(mdat_path)
    input_audit = _audit_input_for_acoustic_design(mdat_path, project.role_to_name)
    diagnostic_only = _audit_is_diagnostic_only(input_audit[0], input_audit[1])

    freq = common_grid([lm, rm, ls, rs], points=1500)
    freq = freq[(freq >= 20.0) & (freq <= 500.0)]
    if freq.size < 100:
        raise ValueError("Za mało punktów 20–500 Hz do projektu niestandardowego crossovera.")
    lm_raw = measurement_to_complex(lm, freq)
    rm_raw = measurement_to_complex(rm, freq)
    ls_raw = measurement_to_complex(ls, freq)
    rs_raw = measurement_to_complex(rs, freq)
    ls_raw_measured = ls_raw.copy()
    rs_raw_measured = rs_raw.copy()

    numeric_state_values = [raw_sub_trim_db, effective_xo_min_hz, effective_xo_max_hz]
    numeric_state_values.extend(value for value in (dirac_sub_trim_db, listening_sub_trim_db) if value is not None)
    if not all(np.isfinite(float(value)) for value in numeric_state_values):
        raise InputIntegrityError("GainState lub limity XO zawierają NaN/Inf.")
    if not float(effective_xo_min_hz) < float(effective_xo_max_hz):
        raise InputIntegrityError("effective_xo_min musi być mniejsze od effective_xo_max.")
    if gain_bridge_path is None and not allow_unverified_gain_override:
        (out_dir / "DESIGN_GAIN_STATE.md").write_text(
            "# Stan gainu użyty w projekcie\n\n"
            "**BRAK GAIN BRIDGE — projekt i eksport zatrzymane.**\n\n"
            "Podaj `--gain-bridge PLIK.mdat` albo jawny `--allow-unverified-gain-override` "
            "wyłącznie do diagnostyki bez eksportu miniDSP.\n",
            encoding="utf-8",
        )
        raise InputIntegrityError("Brak Gain Bridge i brak jawnego override diagnostycznego.")

    bridge_audit = None
    if gain_bridge_path is not None:
        full_nominal_delta = (
            float(listening_sub_trim_db) - float(raw_sub_trim_db)
            if listening_sub_trim_db is not None else 0.0
        )
        if abs(full_nominal_delta) < 1e-12:
            raise InputIntegrityError("Stan RAW i odsłuchowy nie mogą być identyczne przy Gain Bridge.")
        bridge_audit = analyze_gain_bridge(
            Path(mdat_path), Path(gain_bridge_path), expected_gain_db=full_nominal_delta,
        )
        write_gain_bridge_report(bridge_audit, out_dir)
        if bridge_audit.status != "VERIFIED":
            raise InputIntegrityError("Gain Bridge zablokowany: " + bridge_audit.reason)
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
    else:
        gain_state = build_gain_state(
            raw_db=float(raw_sub_trim_db),
            dirac_db=dirac_sub_trim_db,
            listening_db=listening_sub_trim_db,
            source="MANUAL_UNVERIFIED_OVERRIDE",
            verified=False,
        )

    project_state = "dirac" if dirac_sub_trim_db is not None else "raw"
    ls_raw, rs_raw = gain_state.scale_complex_subs(ls_raw_measured, rs_raw_measured, project_state)
    project_gain_l = gain_state.side_delta_db(project_state, "L")
    project_gain_r = gain_state.side_delta_db(project_state, "R")
    gain_lines = [
        "# Stan gainu użyty w projekcie",
        "",
        f"- RAW: **{float(raw_sub_trim_db):+.2f} dB**",
        f"- Dirac: **{dirac_sub_trim_db if dirac_sub_trim_db is not None else 'BRAK'} dB**",
        f"- Odsłuch: **{listening_sub_trim_db if listening_sub_trim_db is not None else 'BRAK'} dB**",
        f"- Stan projektu: **{project_state.upper()}**",
        f"- Skalowanie zespolone L/R: **{project_gain_l:+.2f}/{project_gain_r:+.2f} dB**",
        f"- Źródło: **{gain_state.source}**",
        f"- Weryfikacja: **{'VERIFIED' if gain_state.verified else 'NIEZWERYFIKOWANY — TYLKO DIAGNOSTYKA'}**",
        f"- Hash Gain Bridge: `{gain_state.bridge_sha256 or 'BRAK'}`",
    ]
    (out_dir / "DESIGN_GAIN_STATE.md").write_text("\n".join(gain_lines) + "\n", encoding="utf-8")

    ref_rel = _reference_relative_delays(user_reference_path)
    reference_anchor_verified = ref_rel is not None
    l_center, r_center = ref_rel if ref_rel is not None else (-9.6, -10.9)
    (out_dir / "REFERENCE_REGISTRY_STATUS.md").write_text(
        "# Status referencji delayu\n\n"
        + ("- **VERIFIED** — użyto bezkonfliktowej referencji z dowodem pomiarowym.\n"
           if reference_anchor_verified else
           "- **BLOCKED** — brak zweryfikowanej, bezkonfliktowej referencji. Wartości centrum skanu są tylko priorem konfiguracyjnym i nie mogą odblokować eksportu.\n"),
        encoding="utf-8",
    )
    ref_sum, ref_source = _load_reference_sum(
        reference_mdat, freq,
        raw=(lm_raw, rm_raw, ls_raw, rs_raw), fs=fs,
        l_rel_ref=l_center, r_rel_ref=r_center,
        reference_settings=reference_settings,
    )
    empirical = load_empirical_calibration(empirical_calibration_path)
    if not empirical_calibration_enabled:
        empirical = empirical.with_score_calibration_enabled(False)
    ctx = EvaluationContext(
        freq=freq, fs=fs, lm_raw=lm_raw, rm_raw=rm_raw, ls_raw=ls_raw, rs_raw=rs_raw,
        ref_sum=ref_sum, ref_source=ref_source, empirical_calibration=empirical,
        delay_refinement_enabled=bool(delay_refinement_enabled),
        raw_sub_trim_db=float(raw_sub_trim_db),
        gain_state_name=project_state.upper(),
        global_gain_l_db=float(project_gain_l),
        global_gain_r_db=float(project_gain_r),
        cut_only_enabled=str(cut_only_mode).lower() == "on",
        search_frequencies=search_frequencies,
        prepare_import_files=prepare_import_files,
    )
    write_calibration_summary(empirical, out_dir / "07_KALIBRACJA_REALNYMI_POMIARAMI.md")
    if progress:
        progress(f"referencja: {ref_source}")
        progress(f"centrum opóźnień z referencji: L {l_center:.2f} ms / R {r_center:.2f} ms")
        progress(
            f"GainState {ctx.gain_state_name}: RAW {ctx.raw_sub_trim_db:+.1f} dB, "
            f"skalowanie zespolone L/R {ctx.global_gain_l_db:+.2f}/{ctx.global_gain_r_db:+.2f} dB"
        )
        progress(f"korekcja głównie przez tłumienie: {'WŁĄCZONA' if ctx.cut_only_enabled else 'WYŁĄCZONA'}")
        progress(
            f"kalibracja realnymi pomiarami: {'WŁĄCZONA' if empirical.enabled else 'WYŁĄCZONA'}, "
            f"pewność {empirical.confidence_label}, waga {empirical.weight:.2f}"
        )
        progress(f"dokładne strojenie opóźnień: {'WŁĄCZONE' if ctx.delay_refinement_enabled and empirical.delay_refinement_enabled else 'WYŁĄCZONE'}")

    standard, custom, baseline, run_winners = search_custom_filters_repeated(
        ctx,
        l_center=l_center,
        r_center=r_center,
        level=search_level,
        random_seed=random_seed,
        repeats=search_repeats,
        progress=progress,
    )
    selected = select_profile_winners(custom, cut_only_enabled=ctx.cut_only_enabled)
    _write_polarity_diagnostic(out_dir, ctx, selected)

    spatial_contexts: List[Tuple[Path, EvaluationContext]] = []
    for spatial_path in spatial_mdat_paths or []:
        try:
            sctx = _load_spatial_context(
                Path(spatial_path),
                fs=fs,
                reference_mdat=reference_mdat,
                l_center=l_center,
                r_center=r_center,
                raw_sub_trim_db=raw_sub_trim_db,
                cut_only_enabled=ctx.cut_only_enabled,
                empirical_calibration=empirical,
            )
            spatial_contexts.append((Path(spatial_path), sctx))
            if progress:
                progress(f"dodano pomiar przestrzenny: {Path(spatial_path).name}")
        except Exception as exc:
            if progress:
                progress(f"pominięto pomiar przestrzenny {Path(spatial_path).name}: {exc}")
    if spatial_contexts:
        _spatial_verification(selected, spatial_contexts, out_dir)

    input_integrity_ok = bool(not diagnostic_only and project.phase_metrics_allowed)
    safety_results: List[CandidateSafetyResult] = []
    safe_selected: List[Tuple[str, CustomCandidate]] = []
    for profile, candidate in selected:
        candidate.global_gain_l_db = float(project_gain_l)
        candidate.global_gain_r_db = float(project_gain_r)
        candidate.local_gain_l_db = 0.0
        candidate.local_gain_r_db = 0.0
        candidate.final_gain_l_db = float(project_gain_l)
        candidate.final_gain_r_db = float(project_gain_r)
        candidate.gain_state_name = project_state.upper()
        candidate.state_metrics = {}

        required_state_results: Dict[str, CandidateSafetyResult] = {}
        for state_name in ("raw", "dirac", "listening"):
            try:
                state_ls, state_rs = gain_state.scale_complex_subs(
                    ls_raw_measured, rs_raw_measured, state_name,
                )
            except ValueError:
                continue
            state_ctx = copy.copy(ctx)
            state_ctx.ls_raw = state_ls
            state_ctx.rs_raw = state_rs
            state_ctx.gain_state_name = state_name.upper()
            state_ctx.global_gain_l_db = gain_state.side_delta_db(state_name, "L")
            state_ctx.global_gain_r_db = gain_state.side_delta_db(state_name, "R")
            state_candidate = _evaluate(
                state_ctx,
                candidate.shape,
                candidate.l_rel_ms,
                candidate.r_rel_ms,
                f"gain_state_{state_name}",
                l_allpass=candidate.l_allpass,
                r_allpass=candidate.r_allpass,
            )
            try:
                _map_relative_to_shd(state_candidate.l_rel_ms, state_candidate.r_rel_ms)
                shd_ok = True
            except ValueError:
                shd_ok = False
            state_gate = evaluate_candidate_safety(
                state_candidate,
                input_integrity_ok=input_integrity_ok,
                phase_integrity_ok=bool(project.phase_metrics_allowed),
                shd_representable=shd_ok,
                gain_state_verified=bool(gain_state.verified),
                gain_state_complete=bool(gain_state.complete),
                reference_anchor_verified=reference_anchor_verified,
                effective_xo_min_hz=float(effective_xo_min_hz),
                effective_xo_max_hz=float(effective_xo_max_hz),
            )
            global_l = gain_state.side_delta_db(state_name, "L")
            global_r = gain_state.side_delta_db(state_name, "R")
            candidate.state_metrics[state_name] = {
                "global_gain_l_db": global_l,
                "global_gain_r_db": global_r,
                "local_gain_l_db": 0.0,
                "local_gain_r_db": 0.0,
                "final_gain_l_db": global_l,
                "final_gain_r_db": global_r,
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
            shd_representable=True,
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
            safe_selected.append((profile, candidate))

    ap_results: Dict[str, AllPassComparison] = {}
    if str(allpass_mode).lower() == "compare" and not diagnostic_only:
        if progress:
            progress("all-pass A/B: optymalizacja 1 sekcji na L sub i 1 sekcji na R sub; delaye pozostają bez zmian")
        for profile, candidate in safe_selected:
            export_status, _ = _export_status(profile, candidate)
            if export_status == "DIAGNOSTYKA_NIE_WGRYWAC":
                continue
            ap_results[candidate.code] = optimize_allpass_ab(
                ctx, candidate, profile, random_seed=random_seed, progress=progress,
            )
    write_reports(
        out_dir, selected, baseline, standard, custom,
        mdat_path=mdat_path, input_audit=input_audit, ctx=ctx,
        diagnostic_only=diagnostic_only, shd_anchor_main=shd_anchor_main,
        allpass_results=ap_results, allpass_mode=str(allpass_mode).lower(),
        run_winners=run_winners,
    )
    if not input_integrity_ok:
        write_quarantine(out_dir, [candidate for _, candidate in selected], safety_results, mode="CUSTOM_BIQUAD_INPUT_BLOCK")
        raise InputIntegrityError("Input Integrity Gate nie przeszedł; eksport został zablokowany.")
    if not safe_selected:
        write_quarantine(out_dir, [candidate for _, candidate in selected], safety_results, mode="CUSTOM_BIQUAD_NO_SAFE_CANDIDATE")
        raise NoSafeCandidateError("Analiza zakończona: brak kandydata przechodzącego Candidate Safety Gate w stanie Dirac i odsłuchowym.")
    return selected


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=APP_VERSION)
    ap.add_argument("--mdat", required=True, help="Czysty RAW XOVER_OFF REL120 DELAY0 z L/R main i L/R sub")
    ap.add_argument("--out", required=True, help="Folder wyników")
    ap.add_argument("--fs", type=float, default=96000.0, help="Częstotliwość próbkowania SHD")
    ap.add_argument("--user-reference", default=None, help="USER_REFERENCE_90BW48.yaml z realnymi delayami")
    ap.add_argument("--reference-mdat", default=None, help="Realny pomiar 90 BW48 main/sub do ochrony średnicy")
    ap.add_argument("--search-level", choices=sorted(SEARCH_LEVELS), default="normal", help="quick / normal / deep")
    ap.add_argument("--seed", type=int, default=20260710, help="Deterministyczne ziarno wyszukiwania")
    ap.add_argument("--shd-anchor-main", type=float, default=30.0, help="Preferowany równy delay L/R main")
    ap.add_argument("--all-pass-mode", choices=("off", "compare"), default="off", help="off = bez AP; compare = zaprojektuj AP i eksportuj OFF/ON")
    ap.add_argument("--raw-sub-trim-db", type=float, default=-6.0, help="Rzeczywisty stan subów podczas RAW")
    ap.add_argument("--dirac-sub-trim-db", type=float, default=-4.0, help="Stan subów podczas pomiaru Dirac")
    ap.add_argument("--listening-sub-trim-db", type=float, default=0.0, help="Stan subów podczas odsłuchu")
    ap.add_argument("--gain-bridge", default=None, help="MDAT Gain Bridge wymagany do eksportu miniDSP")
    ap.add_argument("--allow-unverified-gain-override", action="store_true", help="Jawny override tylko diagnostyczny; nie odblokowuje eksportu")
    ap.add_argument("--effective-xo-min", type=float, default=70.0, help="Dolny twardy limit efektywnego XO")
    ap.add_argument("--effective-xo-max", type=float, default=105.0, help="Górny twardy limit efektywnego XO")
    ap.add_argument("--cut-only-mode", choices=("on", "off"), default="on", help="on = dodatkowa ocena możliwości korekcji głównie przez tłumienie")
    ap.add_argument("--search-repeats", type=int, default=1, help="Liczba powtórzeń wyszukiwania z różnymi ziarnami (1–5)")
    ap.add_argument("--spatial-mdat", action="append", default=[], help="Dodatkowy czysty RAW z innej pozycji mikrofonu; opcję można powtórzyć")
    ap.add_argument("--empirical-calibration", default=str(Path(__file__).resolve().parent / "USER_EMPIRICAL_CALIBRATION.json"), help="Plik JSON kalibracji na realnych pomiarach")
    ap.add_argument("--enable-empirical-calibration", action="store_true", help="Włącz eksperymentalną korektę punktową; domyślnie OFF")
    ap.add_argument("--no-empirical-calibration", action="store_true", help="Zgodność wsteczna: pozostaw korektę empiryczną wyłączoną")
    ap.add_argument("--no-delay-refinement", action="store_true", help="Wyłącz deterministyczne dogęszczanie opóźnień")
    args = ap.parse_args(argv)

    print("REWDO", APP_VERSION, flush=True)
    print("RAW:", args.mdat, flush=True)
    print("Wyniki:", args.out, flush=True)
    print("Poziom wyszukiwania:", args.search_level, flush=True)
    print("All-pass:", args.all_pass_mode, "(OFF pozostaje domyślny)", flush=True)
    print("GainState RAW/Dirac/odsłuch:", f"{args.raw_sub_trim_db:+.1f}/{args.dirac_sub_trim_db:+.1f}/{args.listening_sub_trim_db:+.1f} dB", flush=True)
    print("Gain Bridge:", args.gain_bridge or "BRAK — wymaga jawnego override diagnostycznego", flush=True)
    print("Korekcja głównie przez tłumienie:", args.cut_only_mode, flush=True)
    print("Powtórzenia wyszukiwania:", args.search_repeats, flush=True)
    print("Dodatkowe pozycje RAW:", len(args.spatial_mdat), flush=True)
    empirical_enabled = bool(args.enable_empirical_calibration and not args.no_empirical_calibration)
    print("Korekta empiryczna wyniku:", args.empirical_calibration if empirical_enabled else "OFF", flush=True)
    print("Dokładne strojenie opóźnień:", "OFF" if args.no_delay_refinement else "ON", flush=True)

    def progress(message: str) -> None:
        print("[postęp]", message, flush=True)

    try:
        selected = design_acoustic_biquads(
            Path(args.mdat), Path(args.out),
            fs=float(args.fs),
            user_reference_path=Path(args.user_reference) if args.user_reference else None,
            reference_mdat=Path(args.reference_mdat) if args.reference_mdat else None,
            search_level=str(args.search_level),
            random_seed=int(args.seed),
            shd_anchor_main=float(args.shd_anchor_main),
            allpass_mode=str(args.all_pass_mode),
            raw_sub_trim_db=float(args.raw_sub_trim_db),
            dirac_sub_trim_db=float(args.dirac_sub_trim_db),
            listening_sub_trim_db=float(args.listening_sub_trim_db),
            gain_bridge_path=Path(args.gain_bridge) if args.gain_bridge else None,
            allow_unverified_gain_override=bool(args.allow_unverified_gain_override),
            effective_xo_min_hz=float(args.effective_xo_min),
            effective_xo_max_hz=float(args.effective_xo_max),
            cut_only_mode=str(args.cut_only_mode),
            search_repeats=int(args.search_repeats),
            spatial_mdat_paths=[Path(x) for x in args.spatial_mdat],
            empirical_calibration_path=Path(args.empirical_calibration) if args.empirical_calibration else None,
            empirical_calibration_enabled=empirical_enabled,
            delay_refinement_enabled=not bool(args.no_delay_refinement),
            progress=progress,
        )
    except NoSafeCandidateError as exc:
        print("", flush=True)
        print("BRAK BEZPIECZNEGO KANDYDATA:", exc, flush=True)
        print("Zapisano QUARANTINE_DIAGNOSTIC.json/csv i NIE_WGRYWAC_DO_MINIDSP.txt.", flush=True)
        return 3
    except InputIntegrityError as exc:
        print("", flush=True)
        print("BŁĄD INTEGRALNOŚCI WEJŚCIA:", exc, flush=True)
        return 2
    except Exception as exc:
        print("", flush=True)
        print("BŁĄD ACOUSTIC BIQUAD DESIGNER:", exc, flush=True)
        print("Wymagany jest czysty RAW z osobnymi L main / R main / L sub / R sub.", flush=True)
        return 2

    print("", flush=True)
    print("[OK] Wybrane warianty:", flush=True)
    for profile, c in selected:
        print(
            f" {c.rank}. {c.code} | {PROFILE_TITLES[profile]} | "
            f"nominal {c.nominal_scores[profile]:.1f} | robust {c.score(profile):.1f}",
            flush=True,
        )
    print("[OK] Główny raport: 01_CO_TERAZ_ZROBIC_ACOUSTIC_BIQUAD.md", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
