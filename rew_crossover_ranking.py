# -*- coding: utf-8 -*-
"""Ranking crossoverów SHD na podstawie jednego pomiaru bazowego RAW.

Tryb jest celowo osobny od głównego GUI: pełny ranking kilkunastu crossoverów
trwa dłużej niż pojedyncza optymalizacja, więc nie powinien spowalniać zwykłego
workflow. Ranking służy do wybrania 2–3 ustawień do realnego pomiaru w REW.

v19.47: ranking RAW pracuje na relatywnym delayu ``sub - main`` w obie strony.
Wynik jest mapowany na cztery wartości możliwe do wpisania w SHD przy wspólnym
delayu obu mainów i limicie każdego wyjścia SHD.

v19.48: finalne dogęszczenie pozostaje na regularnej siatce delayów, a niezależne
częstotliwości crossovera mogą być liczone równolegle.

v19.49: profile pasm oceny RAW zostały rozszerzone do 120 Hz, a raport ostrzega,
gdy zwycięzca wypada na granicy badanego zakresu.

v19.56: ranking muzyczny otrzymuje lokalną bramkę jakości integracji, miękką
strefę ostrożności 94–102 Hz, osobną diagnostykę wysokich XO oraz wynik mieszany
łączący neutralne pasmo globalne, ostrożny profil muzyczny, lokalny PBIS Safe i
spójność L/R przy XO.

v19.57: globalna selekcja delayów nie ogranicza się już do pierwszych punktów
lokalnego PBIS Safe. Wszystkie warianty przechodzą tani prescreen szerokiego
pasma, a dokładny ranking ocenia inteligentną pulę łączącą top globalny, top
lokalny i reprezentatywne obszary mapy delayów. To chroni przed pominięciem
wariantu minimalnie słabszego lokalnie, lecz lepszego muzycznie w całym paśmie.

v19.58: dodano osobny diagnostyczny skan rodzin polaryzacji subów:

v19.59: poprawiono przekazywanie parametru polarity_scan do równoległego/seryjnego rankingu; tryb diagnostyczny POLARYZACJA uruchamia się bez TypeError.
OFF/OFF, INV/OFF, OFF/INV i INV/INV. Domyślny ranking muzyczny pozostaje OFF/OFF,
a tryb polaryzacji trzeba włączyć jawnie.

v19.60: dodano osobny profil odsłuchowy LOWER-MID BODY SUPPORT / ORGANIC jako dane do
rankingu A/B: pasma 40–60, 60–80, 80–100, 100–160, 120–180, 160–220 i
100–220 Hz, ryzyko odchudzenia/pogrubienia oraz dodatkowy kandydat pod blask
średnicy. Główny ranking techniczno-muzyczny pozostaje porównywalny z v19.59.

v19.61: dodano tryb szybkie A/B dla szybkiego wyboru 3–5 kandydatów odsłuchowych
bez kosztownego dogęszczania całej mapy delayów oraz stały blok porównania z
referencją użytkownika, domyślnie 90 Hz BW48.

v19.62: rozdzielono niższo-średnicowe BODY/SUPPORT od prawdziwego blasku
v19.63: wydanie LITE/STABLE — 3 główne tryby i decyzyjna ochrona referencji 90 Hz.

v19.64: dodano RAW Measurement Guard — kontrolę zakresu, nazw, delay0, REL120,
dostępności pasm body/presence/glass i checklistę pomiaru RAW.

v19.65: dodano workflow decyzyjny A/B: confidence score dla rankingu RAW,
SLOT_PLAN_DO_SHD.md, POMIARY_DO_ZROBIENIA.md, mocniejszą ochronę referencji 90 Hz,
uzasadnienia kandydatów oraz ostrzeżenie, gdy zwycięzca RAW ma niski wynik.

v19.66: uporządkowano raportowanie pewności decyzji, split PRE/POST-DIRAC checklisty
oraz profil odsłuchowy.

v19.70: Music Safety Lock — RAW wskazuje sloty testowe, POST-DIRAC rozdziela
wynik techniczny od decyzji muzycznej, dodaje Safety średnicy i blokuje automatyczne
zastąpienie 90 BW48 przy LOW confidence, profilu caution/technical_control albo
ryzyku utraty/pogrubienia średnicy.

v19.70: Zablokowana rzeczywista referencja — Slot A w SLOT_PLAN_DO_SHD pochodzi z realnego
USER_REFERENCE_90BW48.yaml, a nie z najbliższego wiersza RAW. Raport ostrzega,
gdy referencja wygenerowana z RAW różni się od realnego slotu SHD.

v19.76: sąsiedztwa referencji Report Fix — raport wyraźnie pokazuje, czy kandydat
leży blisko realnej referencji SHD, czy jest od niej oddalony.

v19.76: koszyków topologii — inna topologia delayów nie jest już traktowana jako błąd
ani automatycznie blokowana. Raport rozdziela kandydatów na: bezpośrednich następców
90 BW48, alternatywy muzyczne oraz nową topologię wymagającą osobnego testu A/B.

v19.89: EXE-safe serial ranking — w PyInstaller ONEDIR na Windows domyślnie wymusza tryb seryjny dla rankingów, aby uniknąć zawieszenia spawn/multiprocessing przed utworzeniem plików wynikowych; stdout jest flushowany po każdej linii.

v19.76: Dual Reference Listening Model — 90 BW48 jest referencją tekstury/naturalności
średnicy, a 102 BW48 może być referencją całościową/sceniczną. Globalny zwycięzca RAW
nie jest już nazywany rekomendacją, jeśli jest koszykiem C / nową topologią; raport
szuka kompromisu 90↔102 i traktuje 100 BW48 jako naturalny wariant do A/B.

"""
from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from rew_common import CROSSOVER_PRESETS, OptimizerConfig, _interpolate_crossover_preset, filter_profile_defaults
from rew_crossover import is_raw_xover_off_project, simulate_shd_crossover_from_raw
from rew_mdat import (
    load_optimizer_measurements_from_mdat,
    mdat_import_health_checks,
    write_mdat_health_report,
    write_mdat_numeric_import_report,
)
from rew_raw_measurement_check import write_raw_measurement_check_report
from rew_scoring import (
    _make_candidate_from_metrics,
    apply_delay_and_polarity,
    apply_sound_quality_safe_to_candidate,
    common_grid,
    db,
    map_relative_delays_to_shd,
    measurement_to_complex,
    off_off_priority_main_candidate,
    optimize_delays,
    score_candidate,
    wrap_phase_deg,
)
from rew_metric_registry import side_integration_from_construct_dip, metric_label, write_metric_registry


@dataclass
class CrossoverRankingRow:
    crossover_hz: int
    filter_type: str
    slope_db_oct: int
    # Relacja czasowa analizowana przez silnik: delay suba minus wspólny delay mainów.
    l_sub_minus_main_ms: float
    r_sub_minus_main_ms: float
    # Pełen zestaw wartości gotowych do wpisania do miniDSP SHD Studio.
    l_main_shd_ms: float
    r_main_shd_ms: float
    l_sub_shd_ms: float
    r_sub_shd_ms: float
    shd_limit_ms: float
    shd_max_used_ms: float
    pbis: float
    pbis_safe: float
    pbis_label: str
    naturalness_label: str
    detail_level: str
    elapsed_s: float
    l_sub_polarity: str = "OFF"
    r_sub_polarity: str = "OFF"
    polarity_family: str = "OFF/OFF"
    # v19.46: osobna, pomocnicza ocena relacji L/R przy crossoverze.
    # Nie zastępuje PBIS Safe. Ma wskazać kandydatów do odsłuchu sceny.
    scene_score: float = 0.0
    scene_label: str = ""
    lr_sum_margin_xo_db: float = 0.0
    lr_phase_diff_xo_deg: float = 180.0
    lr_balance_xo_db: float = 0.0
    lr_sum_margin_band_db: float = 0.0
    # v20.07: NAJPIERW SKLEJENIE STRON — osobne sklejenie L main+sub i R main+sub.
    # To jest bramka bezpieczeństwa dla układu dwóch subwooferów stereo: słaba jedna
    # strona nie może zostać zamaskowana przez pozornie dobrą sumę L+R.
    l_sub_main_score_0_100: float = 0.0
    r_sub_main_score_0_100: float = 0.0
    side_integration_score_0_100: float = 0.0
    side_integration_min_0_100: float = 0.0
    side_asymmetry_penalty: float = 0.0
    side_integration_gate_pass: bool = False
    side_integration_gate_reasons: str = ""
    side_integration_label: str = ""
    lr_sum_control_score_0_100: float = 0.0
    # v20.07: MODEL ODPORNOŚCI I OBCIĄŻENIA DIRACA.
    # Nazwy angielskie pozostają w CSV jako techniczne identyfikatory, ale raporty
    # opisują je po polsku: odporność przestrzenna, przewidywane obciążenie korekcji Dirac — PREDYKCJA RAW, ryzyko dziury L+P,
    # ciągłość fazy/energii oraz ranking skalibrowany odsłuchem.
    spatial_robustness_score_0_100: float = 0.0
    dirac_correction_burden_risk_0_100: float = 0.0
    post_dirac_lr_null_risk_0_100: float = 0.0
    phase_continuity_score_0_100: float = 0.0
    listening_calibrated_score_0_100: float = 0.0
    robustness_model_score_0_100: float = 0.0
    robustness_model_label: str = ""
    robustness_model_notes: str = ""
    # v19.60: dane do rankingu odsłuchowego "średnica / blask".
    # Nie zmieniają głównego wyniku muzycznego; wskazują dodatkowych kandydatów A/B.
    band_40_60_db: float = 0.0
    band_60_80_db: float = 0.0
    band_80_100_db: float = 0.0
    band_60_100_db: float = 0.0
    band_100_160_db: float = 0.0
    band_120_180_db: float = 0.0
    band_160_220_db: float = 0.0
    band_100_220_db: float = 0.0
    support_100_160_vs_60_100_db: float = 0.0
    support_100_220_vs_60_100_db: float = 0.0
    support_100_160_vs_80_100_db: float = 0.0
    foundation_40_60_vs_60_100_db: float = 0.0
    tonal_glow_index: float = 0.0
    midrange_glow_score: float = 0.0
    midrange_glow_label: str = ""
    thinness_risk: str = ""
    # v19.62: MUSIC-SAFE POLARITY — ostrzeżenia przed rozwiązaniami okrężnymi
    # typu bardzo duży delay jednego suba albo duża różnica delayów L/R.
    polarity_music_safe_penalty: float = 0.0
    polarity_music_safe_warning: str = ""
    # v19.54/v19.56: stałe pasmo globalne umożliwia uczciwsze porównanie różnych XO.
    # ``global_pbis_safe`` pozostaje zgodnościowym aliasem profilu Music Safe.
    # Neutral Safe używa identycznych wag dla BW/LR; Music Safe dopisuje ostrożne
    # preferencje właściwe dla profilu filtra.
    global_pbis: float = 0.0
    global_pbis_safe: float = 0.0
    global_neutral_pbis: float = 0.0
    global_neutral_safe: float = 0.0
    music_score: float = 0.0
    caution_penalty: float = 0.0
    local_gate_pass: bool = False
    local_gate_reasons: str = ""
    recommendable: bool = False
    xo_zone: str = "preferowany"
    global_eval_low_hz: float = 55.0
    global_eval_high_hz: float = 120.0
    music_xo_cap_hz: float = 92.0
    caution_xo_cap_hz: float = 102.0
    above_music_xo_cap: bool = False
    above_caution_xo_cap: bool = False
    # v19.57: diagnostyka inteligentnej puli kandydatów globalnych.
    global_candidates_available: int = 0
    global_candidates_gate_pass: int = 0
    global_candidates_prescreened: int = 0
    global_candidates_evaluated: int = 0
    global_pool_strategy: str = "smart-prescreen"
    # v19.76: odległość od realnej referencji SHD 90 BW48. W trybie
    # sąsiedztwa referencji kandydaci spoza tolerancji kanałowej są diagnostyczni, nie rekomendowalni.
    reference_distance_ms: float = float("nan")
    reference_neighborhood_pass: bool = True
    reference_neighborhood_label: str = ""
    # v19.76: kandydat spoza sąsiedztwa referencji nie jest zły z definicji.
    # Jest klasyfikowany do koszyka decyzyjnego zamiast automatycznie blokowany.
    topology_bucket_code: str = "UNCLASSIFIED"
    topology_bucket_label: str = "nieklasyfikowany"
    topology_bucket_decision: str = "brak klasyfikacji"
    topology_bucket_reason: str = ""


def _bands_for_hz(hz: int):
    label = f"{int(hz)} Hz"
    values = CROSSOVER_PRESETS.get(label)
    if values is None:
        values = _interpolate_crossover_preset(int(hz))
    return values


def _cfg_for_crossover(
    hz: int,
    filter_type: str,
    slope_db_oct: int,
    delay_min_ms: float,
    delay_max_ms: float,
    shd_delay_limit_ms: float,
    *,
    precise: bool,
    polarity_scan: bool = False,
    fast_ab: bool = False,
) -> OptimizerConfig:
    e_low, e_high, b_low, b_high = _bands_for_hz(int(hz))
    # v19.53: ranking RAW korzysta z tych samych profili oceny BW/LR co GUI.
    # Wcześniej transmitancja filtra była symulowana poprawnie, ale ranking RAW
    # pozostawiał część wag scoringu na wartościach domyślnych OptimizerConfig.
    # Przy porównaniu BW48 z LR48 byłoby to niespójne z pojedynczą optymalizacją.
    defaults = filter_profile_defaults(str(filter_type).upper(), int(slope_db_oct))
    common = dict(
        delay_min_ms=float(delay_min_ms),
        delay_max_ms=float(delay_max_ms),
        eval_low_hz=float(e_low), eval_high_hz=float(e_high),
        bulge_low_hz=float(b_low), bulge_high_hz=float(b_high),
        prefer_smoothness=float(defaults["prefer_smoothness"]),
        prefer_lr_sum=float(defaults["prefer_lr_sum"]),
        dip_penalty_weight=float(defaults["dip_penalty_weight"]),
        bulge_penalty_weight=float(defaults["bulge_penalty_weight"]),
        bulge_tolerance_db=float(defaults["bulge_tolerance_db"]),
        bulge_peak_penalty_weight=float(defaults["bulge_peak_penalty_weight"]),
        use_psycho_bulge_model=bool(defaults["use_psycho_bulge_model"]),
        filter_type=str(filter_type).upper(), slope_db_oct=int(slope_db_oct),
        # v19.47: opóźnienia są relacją sub - main, a nie tylko dodatnim delayem suba.
        relative_delay_shd_mode=True,
        shd_delay_limit_ms=float(shd_delay_limit_ms),
        polarity_scan_mode="ALL" if bool(polarity_scan) else "OFF_ONLY",
    )
    if precise:
        # Finalne dogęszczenie topowych crossoverów: ten sam krok 0,1 ms co w GUI.
        return OptimizerConfig(
            **common,
            delay_step_ms=0.1,
            top_n=20,
        )
    if bool(fast_ab):
        # v19.61 szybkie A/B: tryb roboczy do szybkiego wskazania kandydatów do
        # realnego pomiaru. Celowo nie próbuje być finalną mapą 0,1 ms.
        return OptimizerConfig(
            **common,
            delay_step_ms=0.5,
            top_n=8,
            analysis_pool_n=90,
            freq_points=520,
            fast_coarse_step_ms=2.0,
            fast_refine_radius_ms=0.75,
            fast_seed_n=10,
            fast_exact_threshold=500,
            fast_max_coarse_combinations=1_600,
        )
    # Szybki screening całego zakresu crossoverów. Ma wyłonić topowe częstotliwości,
    # które potem są liczone ponownie dokładniej co 0,1 ms.
    return OptimizerConfig(
        **common,
        delay_step_ms=0.3,
        top_n=10,
        analysis_pool_n=300,
        fast_coarse_step_ms=1.0,
        fast_refine_radius_ms=1.0,
        fast_seed_n=24,
        fast_exact_threshold=5_000,
    )


def _cfg_for_global_fixed_band(
    filter_type: str,
    slope_db_oct: int,
    global_eval_low_hz: float,
    global_eval_high_hz: float,
    global_bulge_low_hz: float,
    global_bulge_high_hz: float,
    *,
    neutral: bool = False,
    freq_points: int = 1400,
) -> OptimizerConfig:
    """Stałe pasmo porównawcze dla różnych częstotliwości crossovera.

    ``neutral=False`` zachowuje ostrożny profil właściwy dla BW/LR. Wariant
    ``neutral=True`` używa identycznych wag dla wszystkich filtrów, dzięki czemu
    bezpośrednie porównanie BW48 i LR48 nie jest przechylone przez preferencje
    profilu brzmieniowego. Transmitancja filtra nadal pozostaje rzeczywista —
    neutralizowane są tylko wagi oceny.
    """
    profile_key = "OTHER" if bool(neutral) else str(filter_type).upper()
    defaults = filter_profile_defaults(profile_key, int(slope_db_oct))
    return OptimizerConfig(
        eval_low_hz=float(global_eval_low_hz),
        eval_high_hz=float(global_eval_high_hz),
        bulge_low_hz=float(global_bulge_low_hz),
        bulge_high_hz=float(global_bulge_high_hz),
        prefer_smoothness=float(defaults["prefer_smoothness"]),
        prefer_lr_sum=float(defaults["prefer_lr_sum"]),
        dip_penalty_weight=float(defaults["dip_penalty_weight"]),
        bulge_penalty_weight=float(defaults["bulge_penalty_weight"]),
        bulge_tolerance_db=float(defaults["bulge_tolerance_db"]),
        bulge_peak_penalty_weight=float(defaults["bulge_peak_penalty_weight"]),
        use_psycho_bulge_model=bool(defaults["use_psycho_bulge_model"]),
        filter_type="NEUTRAL" if bool(neutral) else str(filter_type).upper(),
        slope_db_oct=int(slope_db_oct),
        sound_quality_safe=True,
        freq_points=max(160, int(freq_points)),
    )


@dataclass
class _ComplexEvaluationContext:
    """Wspólna siatka i zespolone odpowiedzi używane dla wielu kandydatów.

    v19.57: przed tą zmianą każdy kandydat ponownie interpolował te same cztery
    przebiegi. Przy szerszej puli globalnej byłoby to niepotrzebnie kosztowne.
    """
    freq: np.ndarray
    l_main: np.ndarray
    l_sub: np.ndarray
    r_main: np.ndarray
    r_sub: np.ndarray


def _make_complex_context(simulated, *, freq_points: Optional[int] = None, native_first_grid: bool = False) -> _ComplexEvaluationContext:
    measurements = list(simulated)
    if bool(native_first_grid):
        freq = np.asarray(measurements[0].freq, dtype=float)
    else:
        freq = common_grid(measurements, int(freq_points or 1400))
    return _ComplexEvaluationContext(
        freq=np.asarray(freq, dtype=float),
        l_main=measurement_to_complex(measurements[0], freq),
        l_sub=measurement_to_complex(measurements[1], freq),
        r_main=measurement_to_complex(measurements[2], freq),
        r_sub=measurement_to_complex(measurements[3], freq),
    )


def _candidate_identity(cand) -> tuple[float, float, str, str]:
    return (
        round(float(getattr(cand, "l_delay_ms", 0.0)), 6),
        round(float(getattr(cand, "r_delay_ms", 0.0)), 6),
        str(getattr(cand, "l_polarity", "OFF")).upper(),
        str(getattr(cand, "r_polarity", "OFF")).upper(),
    )


def _local_candidate_sort_key(cand):
    side = _side_integration_metrics_for_candidate(cand)
    return (
        float(side["side_integration_min_0_100"]),
        float(side["side_integration_score_0_100"]),
        float(getattr(cand, "pbis_safe_score", getattr(cand, "pbis_score", 0.0))),
        float(getattr(cand, "pbis_score", 0.0)),
        float(getattr(cand, "score", 0.0)),
    )


def _add_unique_candidate(target: List, seen: set, cand) -> bool:
    key = _candidate_identity(cand)
    if key in seen:
        return False
    seen.add(key)
    target.append(cand)
    return True


def _select_smart_global_shortlist(
    previews: List[Dict[str, object]],
    *,
    max_exact_n: int,
    cell_ms: float = 1.5,
) -> List[Dict[str, object]]:
    """Wybiera reprezentatywną pulę do dokładnej oceny szerokiego pasma.

    Każdy wariant przechodzący lokalną bramkę otrzymuje tani prescreen globalny.
    Gdy punktów jest więcej niż limit dokładnej oceny, zachowujemy równocześnie:
    - najlepsze wyniki prescreenu globalnego,
    - najlepsze wyniki lokalnego PBIS Safe,
    - najlepszy punkt z każdej komórki mapy delayów,
    - punkty rozmieszczone przestrzennie na mapie.

    Dzięki temu wynik nie zależy już od arbitralnego top-20 lokalnego PBIS Safe.
    """
    if not previews:
        return []
    limit = max(1, min(int(max_exact_n), len(previews)))
    if len(previews) <= limit:
        return list(previews)

    by_global = sorted(
        previews,
        key=lambda x: (
            float(x.get("approx_music_score", 0.0)),
            float(x.get("approx_global_neutral_safe", 0.0)),
            float(x.get("approx_global_music_safe", 0.0)),
            _local_candidate_sort_key(x["candidate"]),
        ),
        reverse=True,
    )
    by_local = sorted(previews, key=lambda x: _local_candidate_sort_key(x["candidate"]), reverse=True)
    selected: List[Dict[str, object]] = []
    seen: set = set()

    def add(item: Dict[str, object]) -> None:
        if len(selected) >= limit:
            return
        cand = item["candidate"]
        if _candidate_identity(cand) in seen:
            return
        seen.add(_candidate_identity(cand))
        selected.append(item)

    # Minimum połowa puli pochodzi z szerokiego prescreenu; część pozostaje
    # zarezerwowana dla dobrych lokalnie oraz przestrzennie odmiennych punktów.
    global_take = min(len(by_global), max(96, int(round(limit * 0.55))))
    local_take = min(len(by_local), max(48, int(round(limit * 0.20))))
    for item in by_global[:global_take]:
        add(item)
    for item in by_local[:local_take]:
        add(item)

    # Najlepszy punkt prescreenu w każdej komórce l/r. To chroni odległe wyspy
    # mapy przed zniknięciem, nawet gdy ich lokalny PBIS jest tylko umiarkowany.
    cell = max(float(cell_ms), 0.25)
    best_by_cell: Dict[tuple[int, int], Dict[str, object]] = {}
    for item in by_global:
        cand = item["candidate"]
        key = (
            int(np.floor(float(getattr(cand, "l_delay_ms", 0.0)) / cell)),
            int(np.floor(float(getattr(cand, "r_delay_ms", 0.0)) / cell)),
        )
        if key not in best_by_cell:
            best_by_cell[key] = item
    for item in sorted(best_by_cell.values(), key=lambda x: float(x.get("approx_music_score", 0.0)), reverse=True):
        add(item)

    # Greedy spacing w metryce max(|dL|, |dR|). Najpierw szersze odstępy,
    # potem ciaśniejsze, aby wypełnić pulę bez powrotu do jednego klastra.
    for spacing in (3.0, 2.0, 1.25, 0.75, 0.0):
        for item in by_global:
            if len(selected) >= limit:
                break
            cand = item["candidate"]
            if spacing > 0.0:
                too_close = any(
                    max(
                        abs(float(getattr(prev["candidate"], "l_delay_ms", 0.0)) - float(getattr(cand, "l_delay_ms", 0.0))),
                        abs(float(getattr(prev["candidate"], "r_delay_ms", 0.0)) - float(getattr(cand, "r_delay_ms", 0.0))),
                    ) < spacing - 1e-9
                    for prev in selected
                )
                if too_close:
                    continue
            add(item)
        if len(selected) >= limit:
            break
    return selected


def _global_fixed_band_metrics_for_candidate(
    simulated,
    best,
    cfg: OptimizerConfig,
    *,
    context: Optional[_ComplexEvaluationContext] = None,
) -> Dict[str, float]:
    """Ponownie ocenia wskazany punkt na stałym paśmie globalnym.

    PBIS Safe globalny nie zawiera kary za plateau: pojedynczy punkt kontrolny nie
    ma własnej mapy sąsiedztwa delayów. Pozostałe ryzyka Safe nadal są liczone.
    """
    ctx = context or _make_complex_context(simulated, freq_points=int(cfg.freq_points))
    freq = ctx.freq
    l_main = ctx.l_main
    l_sub = ctx.l_sub
    r_main = ctx.r_main
    r_sub = ctx.r_sub
    l_inv = str(getattr(best, "l_polarity", "OFF")).upper() == "INV"
    r_inv = str(getattr(best, "r_polarity", "OFF")).upper() == "INV"
    l_sub_s = apply_delay_and_polarity(l_sub, freq, float(best.l_delay_ms), l_inv)
    r_sub_s = apply_delay_and_polarity(r_sub, freq, float(best.r_delay_ms), r_inv)
    score, metrics = score_candidate(freq, l_main, l_sub_s, r_main, r_sub_s, cfg)
    cand = _make_candidate_from_metrics(
        float(best.l_delay_ms), l_inv, float(best.r_delay_ms), r_inv, score, metrics
    )
    cand.pbis_plateau = "pominięto — stałe pasmo globalne"
    apply_sound_quality_safe_to_candidate(cand, cfg, include_plateau=False)
    return {
        "global_pbis": float(getattr(cand, "pbis_score", 0.0)),
        "global_pbis_safe": float(getattr(cand, "pbis_safe_score", getattr(cand, "pbis_score", 0.0))),
    }


def _safe_mean(values: np.ndarray, default: float = 0.0) -> float:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return float(default)
    return float(np.mean(values))


def _scene_label(score: float) -> str:
    if score >= 75.0:
        return "bardzo obiecujący"
    if score >= 60.0:
        return "warto sprawdzić"
    if score >= 45.0:
        return "porównaj odsłuchowo"
    return "mniej obiecujący"


def _scene_metrics_for_candidate(
    simulated, hz: int, best, *, context: Optional[_ComplexEvaluationContext] = None
) -> Dict[str, float | str]:
    """Pomocnicza ocena sumowania L+R w okolicy crossovera.

    To nie jest gwarancja jakości sceny stereo. Model wskazuje ustawienia, przy
    których lewa i prawa strona po integracji main+sub mają większą szansę
    sumować się konstruktywnie w rejonie podziału.
    """
    ctx = context or _make_complex_context(simulated, native_first_grid=True)
    freq = ctx.freq
    zl_main = ctx.l_main
    zl_sub = ctx.l_sub
    zr_main = ctx.r_main
    zr_sub = ctx.r_sub
    # W v19.47 best.*_delay_ms jest relacją sub - main. Ujemny wynik oznacza,
    # że w fizycznym SHD zostaną opóźnione oba mainy; relacja fazowa jest ta sama.
    l_inv = str(getattr(best, "l_polarity", "OFF")).upper() == "INV"
    r_inv = str(getattr(best, "r_polarity", "OFF")).upper() == "INV"
    zl = zl_main + apply_delay_and_polarity(zl_sub, freq, float(best.l_delay_ms), l_inv)
    zr = zr_main + apply_delay_and_polarity(zr_sub, freq, float(best.r_delay_ms), r_inv)
    zlr = zl + zr

    hz_f = float(hz)
    # Wąskie pasmo odpowiada temu, co użytkownik zwykle chce ocenić "przy crossie".
    # Szersze pasmo chroni przed wyborem pojedynczego, przypadkowego punktu.
    narrow = (freq >= hz_f - 3.0) & (freq <= hz_f + 3.0)
    broad = (freq >= hz_f - 12.0) & (freq <= hz_f + 12.0)
    if not np.any(narrow):
        narrow = np.abs(freq - hz_f) == np.min(np.abs(freq - hz_f))
    if not np.any(broad):
        broad = narrow.copy()

    l_db = db(zl)
    r_db = db(zr)
    lr_db = db(zlr)
    margin = lr_db - np.maximum(l_db, r_db)
    # Różnica fazy L/R po zsumowaniu main+sub dla obu stron.
    denom = np.where(np.abs(zr) > 1e-30, zr, 1e-30 + 0j)
    phase_abs = np.abs(wrap_phase_deg(np.rad2deg(np.angle(zl / denom))))

    margin_xo = _safe_mean(margin[narrow])
    margin_band = _safe_mean(margin[broad])
    phase_xo = _safe_mean(phase_abs[narrow], 180.0)
    balance_xo = _safe_mean(np.abs(l_db[narrow] - r_db[narrow]))

    # Heurystyka 0–100: premiuje konstruktywne L+R w pobliżu crossovera,
    # małą różnicę fazy i rozsądny balans poziomów. To osobna perspektywa obok PBIS.
    score = 100.0
    score -= 0.48 * min(max(phase_xo, 0.0), 180.0)
    score -= 4.0 * max(0.0, -margin_xo)
    score -= 2.0 * max(0.0, 1.0 - margin_band)
    score -= 2.2 * max(0.0, balance_xo - 3.0)
    score = float(np.clip(score, 0.0, 100.0))
    return {
        "scene_score": score,
        "scene_label": _scene_label(score),
        "lr_sum_margin_xo_db": float(margin_xo),
        "lr_phase_diff_xo_deg": float(phase_xo),
        "lr_balance_xo_db": float(balance_xo),
        "lr_sum_margin_band_db": float(margin_band),
    }



def _band_mean_db(freq: np.ndarray, response: np.ndarray, low_hz: float, high_hz: float) -> float:
    """Średni poziom pasma z odpornością na brak danych w RAW."""
    f = np.asarray(freq, dtype=float)
    z = np.asarray(response)
    mask = (f >= float(low_hz)) & (f <= float(high_hz))
    if not np.any(mask):
        return float("nan")
    values = db(z[mask])
    values = values[np.isfinite(values)]
    if values.size == 0:
        return float("nan")
    return float(np.mean(values))


def _safe_db_delta(a: float, b: float, default: float = 0.0) -> float:
    if np.isfinite(float(a)) and np.isfinite(float(b)):
        return float(a) - float(b)
    return float(default)


def _target_window_score(value: float, target: float, good_width: float, zero_width: float) -> float:
    """100 w pobliżu targetu, liniowy spadek do 0 poza szerokim oknem."""
    if not np.isfinite(float(value)):
        return 50.0
    distance = abs(float(value) - float(target))
    if distance <= float(good_width):
        return 100.0
    if distance >= float(zero_width):
        return 0.0
    return float(100.0 * (1.0 - (distance - float(good_width)) / max(1e-9, float(zero_width) - float(good_width))))


def _midrange_glow_label(score: float, thinness_risk: str) -> str:
    risk = str(thinness_risk or "").lower()
    if "odchudzenia" in risk:
        return "technicznie OK, ale możliwe wycofanie średnicy"
    if "pogrubienia" in risk:
        return "muzykalny kandydat, ale sprawdź bulwę 120–180 Hz"
    if float(score) >= 78.0:
        return "mocny kandydat pod blask/organiczność średnicy"
    if float(score) >= 65.0:
        return "warto sprawdzić pod średnicę"
    if float(score) >= 52.0:
        return "neutralny, kontrolny"
    return "mniej obiecujący pod średnicę"


def _midrange_glow_score(
    *,
    music_score: float,
    tonal_glow_index: float,
    local_pbis_safe: float,
    scene_score: float,
    local_gate_pass: bool,
    xo_zone: str,
) -> float:
    """Osobny wynik A/B pod nasycenie i blask średnicy.

    To nie jest zastępstwo głównego rankingu. Ma wyłapać warianty typu 90 BW48,
    które mogą dawać lepszą nośność 100–220 Hz mimo mniej idealnego wykresu.
    """
    score = (
        0.42 * float(music_score)
        + 0.30 * float(tonal_glow_index)
        + 0.16 * float(local_pbis_safe)
        + 0.12 * float(scene_score)
    )
    if not bool(local_gate_pass):
        score -= 12.0
    if str(xo_zone).lower().startswith("diagnost"):
        score -= 8.0
    return float(np.clip(score, 0.0, 100.0))


def _midrange_glow_metrics_for_candidate(
    simulated,
    best,
    *,
    context: Optional[_ComplexEvaluationContext] = None,
) -> Dict[str, float | str]:
    """Dane pasmowe do dodatkowego rankingu "średnica / blask".

    Uwaga interpretacyjna: mierzymy przejście bas/niższa średnica na podstawie
    pełnej symulowanej sumy main+sub. Nie udajemy oceny właściwej średnicy 700 Hz–3 kHz,
    bo RAW crossoverowy zwykle nie zawiera tego pasma z dobrą rozdzielczością.
    """
    ctx = context or _make_complex_context(simulated, freq_points=1800)
    freq = ctx.freq
    l_inv = str(getattr(best, "l_polarity", "OFF")).upper() == "INV"
    r_inv = str(getattr(best, "r_polarity", "OFF")).upper() == "INV"
    zl = ctx.l_main + apply_delay_and_polarity(ctx.l_sub, freq, float(best.l_delay_ms), l_inv)
    zr = ctx.r_main + apply_delay_and_polarity(ctx.r_sub, freq, float(best.r_delay_ms), r_inv)
    mono = zl + zr

    b40_60 = _band_mean_db(freq, mono, 40.0, 60.0)
    b60_80 = _band_mean_db(freq, mono, 60.0, 80.0)
    b80_100 = _band_mean_db(freq, mono, 80.0, 100.0)
    b60_100 = _band_mean_db(freq, mono, 60.0, 100.0)
    b100_160 = _band_mean_db(freq, mono, 100.0, 160.0)
    b120_180 = _band_mean_db(freq, mono, 120.0, 180.0)
    b160_220 = _band_mean_db(freq, mono, 160.0, 220.0)
    b100_220 = _band_mean_db(freq, mono, 100.0, 220.0)

    support_100_160 = _safe_db_delta(b100_160, b60_100)
    support_100_220 = _safe_db_delta(b100_220, b60_100)
    support_100_160_vs_80_100 = _safe_db_delta(b100_160, b80_100)
    foundation_40_60 = _safe_db_delta(b40_60, b60_100)
    support_120_180 = _safe_db_delta(b120_180, b60_100)

    # Heurystyka tonalna:
    # - najlepiej, gdy 100–160 Hz nie jest wycięte względem 60–100 Hz;
    # - umiarkowana przewaga 100–160 nad 80–100 może dać ciało bez "subowego brzucha";
    # - nadmiar 120–180 Hz jest kontrolowany, bo może pogrubić i zamulić C1.
    s1 = _target_window_score(support_100_160, target=1.0, good_width=1.4, zero_width=5.5)
    s2 = _target_window_score(support_100_220, target=0.0, good_width=1.8, zero_width=6.0)
    s3 = _target_window_score(support_100_160_vs_80_100, target=1.5, good_width=2.0, zero_width=7.0)
    s4 = _target_window_score(support_120_180, target=0.8, good_width=2.2, zero_width=6.5)
    # Delikatnie premiujemy fundament 40–60 Hz, ale tylko do czasu, gdy nie dominuje nad 60–100 Hz.
    s5 = _target_window_score(foundation_40_60, target=0.5, good_width=2.5, zero_width=8.0)
    tonal = float(np.clip(0.30 * s1 + 0.24 * s2 + 0.24 * s3 + 0.14 * s4 + 0.08 * s5, 0.0, 100.0))

    if support_100_160 < -2.0 or support_100_220 < -2.5:
        risk = "ryzyko odchudzenia / wycofania średnicy"
    elif support_100_160 > 4.0 or support_120_180 > 4.5:
        risk = "ryzyko pogrubienia / bulwy 120–180 Hz"
    else:
        risk = "niskie"

    # Label zostanie doprecyzowany po policzeniu końcowego midrange_glow_score.
    return {
        "band_40_60_db": float(b40_60) if np.isfinite(b40_60) else 0.0,
        "band_60_80_db": float(b60_80) if np.isfinite(b60_80) else 0.0,
        "band_80_100_db": float(b80_100) if np.isfinite(b80_100) else 0.0,
        "band_60_100_db": float(b60_100) if np.isfinite(b60_100) else 0.0,
        "band_100_160_db": float(b100_160) if np.isfinite(b100_160) else 0.0,
        "band_120_180_db": float(b120_180) if np.isfinite(b120_180) else 0.0,
        "band_160_220_db": float(b160_220) if np.isfinite(b160_220) else 0.0,
        "band_100_220_db": float(b100_220) if np.isfinite(b100_220) else 0.0,
        "support_100_160_vs_60_100_db": float(support_100_160),
        "support_100_220_vs_60_100_db": float(support_100_220),
        "support_100_160_vs_80_100_db": float(support_100_160_vs_80_100),
        "foundation_40_60_vs_60_100_db": float(foundation_40_60),
        "tonal_glow_index": float(tonal),
        "thinness_risk": str(risk),
    }


def _xo_zone(hz: int, music_xo_cap_hz: float, caution_xo_cap_hz: float) -> str:
    value = float(hz)
    if value <= float(music_xo_cap_hz):
        return "preferowany"
    if value <= float(caution_xo_cap_hz):
        return "ostrożny"
    return "diagnostyczny"



def _linear_score_between(value: float, good: float, bad: float, *, higher_is_better: bool = True) -> float:
    """Prosta mapa 0–100 między progiem dobrym i złym."""
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


def _side_integration_metrics_for_candidate(best) -> Dict[str, float | str | bool]:
    """Wspólna skala sklejenia stron v1.

    Od v20.23 identyczny wzór jest używany w trybach 1/3/5/6 oraz 8/9.
    Różnica między raportami dotyczy etapu dowodowego: tutaj jest to
    PREDYKCJA RAW, a w projektantach — SYMULACJA PROJEKTU.
    """
    values = side_integration_from_construct_dip(
        float(getattr(best, "l_construct_db", 0.0) or 0.0),
        float(getattr(best, "r_construct_db", 0.0) or 0.0),
        float(getattr(best, "l_dip_db", getattr(best, "lr_dip_db", 0.0)) or 0.0),
        float(getattr(best, "r_dip_db", getattr(best, "lr_dip_db", 0.0)) or 0.0),
    )
    return {
        "l_sub_main_score_0_100": float(values["l_score"]),
        "r_sub_main_score_0_100": float(values["r_score"]),
        "side_integration_score_0_100": float(values["side_score"]),
        "side_integration_min_0_100": float(values["side_min"]),
        "side_asymmetry_penalty": float(values["asymmetry_penalty"]),
        "side_integration_gate_pass": bool(values["gate_pass"]),
        "side_integration_gate_reasons": str(values["gate_reasons"]),
        "side_integration_label": str(values["label"]),
    }




def _v20_linear_score(value: float, good: float, bad: float, *, higher_is_better: bool = True) -> float:
    return _linear_score_between(value, good=good, bad=bad, higher_is_better=higher_is_better)


def _v20_raw_robustness_metrics(
    *,
    side: Dict[str, float | str | bool],
    scene: Dict[str, float | str],
    music_metrics: Dict[str, float | str],
    neutral_metrics: Dict[str, float | str],
    local_safe: float,
    glow_metrics: Dict[str, float | str],
) -> Dict[str, float | str]:
    """v20.07 — polski model odporności i obciążenia Diraca dla RAW.

    To nie jest zastępstwo pomiaru wielopunktowego ani POST-DIRAC. To predykcja
    z RAW: czy preset wygląda stabilnie dla stereo REL i czy Dirac nie będzie
    musiał ratować złej topologii. Pełne potwierdzenie wymaga pomiarów PRE/POST-DIRAC.
    """
    side_min = float(side.get("side_integration_min_0_100", 0.0) or 0.0)
    side_score = float(side.get("side_integration_score_0_100", 0.0) or 0.0)
    asym_pen = float(side.get("side_asymmetry_penalty", 0.0) or 0.0)
    scene_score = float(scene.get("scene_score", 0.0) or 0.0)
    lr_margin_xo = float(scene.get("lr_sum_margin_xo_db", -6.0) or -6.0)
    lr_margin_band = float(scene.get("lr_sum_margin_band_db", -6.0) or -6.0)
    phase_xo = abs(float(scene.get("lr_phase_diff_xo_deg", 180.0) or 180.0))
    balance_xo = abs(float(scene.get("lr_balance_xo_db", 6.0) or 6.0))
    global_neutral = float(neutral_metrics.get("global_pbis_safe", 0.0) or 0.0)
    global_music = float(music_metrics.get("global_pbis_safe", 0.0) or 0.0)
    mid_body = float(glow_metrics.get("midrange_glow_score", 0.0) or 0.0)

    asym_score = _v20_linear_score(asym_pen, good=0.0, bad=30.0, higher_is_better=False)
    lr_margin_xo_score = _v20_linear_score(lr_margin_xo, good=1.5, bad=-3.0, higher_is_better=True)
    lr_margin_band_score = _v20_linear_score(lr_margin_band, good=1.0, bad=-3.0, higher_is_better=True)
    phase_score = _v20_linear_score(phase_xo, good=45.0, bad=150.0, higher_is_better=False)
    balance_score = _v20_linear_score(balance_xo, good=1.0, bad=5.0, higher_is_better=False)

    # Przewidywana odporność przestrzenna — PREDYKCJA RAW: przy pojedynczym RAW to tylko proxy. Wyżej = bezpieczniej.
    spatial = float(np.clip(
        0.34 * side_min + 0.22 * side_score + 0.18 * scene_score + 0.10 * asym_score
        + 0.08 * lr_margin_band_score + 0.08 * balance_score,
        0.0, 100.0,
    ))

    # Przewidywane obciążenie korekcji Dirac — PREDYKCJA RAW: wyżej = gorzej, czyli Dirac prawdopodobnie musi mocniej ratować układ.
    base_quality = float(np.clip(
        0.28 * global_neutral + 0.20 * global_music + 0.18 * float(local_safe)
        + 0.18 * side_min + 0.10 * scene_score + 0.06 * lr_margin_band_score,
        0.0, 100.0,
    ))
    dirac_burden = float(np.clip(100.0 - base_quality, 0.0, 100.0))

    # Przewidywane ryzyko dziury L+P po Dirac — PREDYKCJA RAW: wyżej = gorzej.
    margin_risk = 100.0 - lr_margin_xo_score
    phase_risk = 100.0 - phase_score
    scene_risk = 100.0 - scene_score
    asym_risk = 100.0 - asym_score
    post_null = float(np.clip(
        0.35 * margin_risk + 0.25 * phase_risk + 0.20 * scene_risk + 0.12 * asym_risk
        + 0.08 * max(0.0, 100.0 - side_min),
        0.0, 100.0,
    ))

    # Przewidywana ciągłość fazy i energii — PREDYKCJA RAW: czy przejście nie jest tylko punktowo dobre przy XO.
    phase_cont = float(np.clip(
        0.36 * phase_score + 0.22 * lr_margin_band_score + 0.18 * scene_score
        + 0.14 * float(local_safe) + 0.10 * balance_score,
        0.0, 100.0,
    ))

    # Ranking skalibrowany odsłuchem: preferuje naturalne body, brak ryzyka dziury i niski ciężar Diraca.
    listening_cal = float(np.clip(
        0.28 * mid_body + 0.22 * side_min + 0.18 * (100.0 - post_null)
        + 0.16 * (100.0 - dirac_burden) + 0.10 * phase_cont + 0.06 * global_neutral,
        0.0, 100.0,
    ))

    robustness = float(np.clip(
        0.28 * spatial + 0.20 * (100.0 - dirac_burden) + 0.22 * (100.0 - post_null)
        + 0.16 * phase_cont + 0.14 * listening_cal,
        0.0, 100.0,
    ))

    if robustness >= 76 and post_null <= 35 and dirac_burden <= 40:
        label = "odporny kandydat do A/B"
    elif robustness >= 62 and post_null <= 55:
        label = "warunkowo odporny — wymaga POST-DIRAC"
    elif post_null > 65:
        label = "wysokie przewidywane ryzyko dziury L+P po Dirac — PREDYKCJA RAW"
    elif dirac_burden > 65:
        label = "wysokie przewidywane obciążenie korekcji Dirac — PREDYKCJA RAW"
    else:
        label = "diagnostyczny — nie ustawiaj w ciemno"

    notes: List[str] = []
    if post_null > 55:
        notes.append("podwyższone przewidywane ryzyko dziury L+P po Dirac — PREDYKCJA RAW")
    if dirac_burden > 55:
        notes.append("Dirac może musieć mocno ratować RAW")
    if spatial < 55:
        notes.append("niska odporność na asymetrię pokoju/głowy")
    if phase_cont < 55:
        notes.append("ciągłość fazy/energii w okolicy XO jest słaba")
    if mid_body < 45:
        notes.append("ryzyko mniej organicznej średnicy/body")
    if not notes:
        notes.append("brak dużych ostrzeżeń w modelu v20; potwierdź PRE/POST-DIRAC")

    return {
        "spatial_robustness_score_0_100": spatial,
        "dirac_correction_burden_risk_0_100": dirac_burden,
        "post_dirac_lr_null_risk_0_100": post_null,
        "phase_continuity_score_0_100": phase_cont,
        "listening_calibrated_score_0_100": listening_cal,
        "robustness_model_score_0_100": robustness,
        "robustness_model_label": label,
        "robustness_model_notes": "; ".join(notes),
    }

def _local_gate_reasons(
    *,
    pbis_safe: float,
    scene_score: float,
    lr_sum_margin_xo_db: float,
    lr_phase_diff_xo_deg: float,
    min_local_pbis_safe: float,
    min_lr_coherence: float,
    min_lr_sum_margin_xo_db: float,
    max_lr_phase_diff_xo_deg: float,
    side_gate_pass: bool = True,
    side_gate_reasons: str = "OK",
) -> List[str]:
    """Zwraca powody odrzucenia przez lokalną bramkę integracji przy XO.

    v20.07: pierwsza jest bramka sklejenia stron. Dobra suma L+R nie może
    ukryć słabego sklejenia L main+sub albo R main+sub.
    """
    reasons: List[str] = []
    if not bool(side_gate_pass):
        reasons.append(f"Bramka sklejenia stron: {side_gate_reasons}")
    if float(pbis_safe) < float(min_local_pbis_safe):
        reasons.append(f"Lokalna jakość integracji PBIS — PREDYKCJA RAW {float(pbis_safe):.1f} < {float(min_local_pbis_safe):.1f}")
    if float(scene_score) < float(min_lr_coherence):
        reasons.append(f"spójność L/R {float(scene_score):.1f} < {float(min_lr_coherence):.1f}")
    if float(lr_sum_margin_xo_db) < float(min_lr_sum_margin_xo_db):
        reasons.append(f"L+R przy XO {float(lr_sum_margin_xo_db):+.2f} dB < {float(min_lr_sum_margin_xo_db):+.2f} dB")
    if float(lr_phase_diff_xo_deg) > float(max_lr_phase_diff_xo_deg):
        reasons.append(f"faza L/R {float(lr_phase_diff_xo_deg):.1f}° > {float(max_lr_phase_diff_xo_deg):.1f}°")
    return reasons


def _music_score(
    *,
    global_neutral_safe: float,
    global_music_safe: float,
    local_pbis_safe: float,
    side_integration_score: float,
    side_integration_min: float,
    scene_score: float,
    hz: int,
    music_xo_cap_hz: float,
    caution_xo_cap_hz: float,
    caution_penalty_per_hz: float,
) -> tuple[float, float]:
    """Łączy globalną neutralność z zasadą NAJPIERW SKLEJENIE STRON.

    v20.07: osobne sklejenie L main+sub i R main+sub ma większą wagę niż
    sama suma L+R, ale nie zastępuje kontroli stereo. Słabsza strona wpływa
    mocniej niż średnia, żeby L+R nie maskowało asymetrii pokoju/sprzętu.
    """
    cautious_hz = max(0.0, min(float(hz), float(caution_xo_cap_hz)) - float(music_xo_cap_hz))
    diagnostic_hz = max(0.0, float(hz) - float(caution_xo_cap_hz))
    caution_penalty = float(caution_penalty_per_hz) * cautious_hz + 1.0 * diagnostic_hz
    side_component = 0.62 * float(side_integration_score) + 0.38 * float(side_integration_min)
    score = (
        0.30 * float(global_neutral_safe)
        + 0.22 * float(global_music_safe)
        + 0.14 * float(local_pbis_safe)
        + 0.24 * float(side_component)
        + 0.10 * float(scene_score)
        - caution_penalty
    )
    return float(np.clip(score, 0.0, 100.0)), float(caution_penalty)


def _polarity_music_safe_assessment(mapped: Dict[str, object], best) -> tuple[float, str]:
    """Kara/ostrzeżenie dla diagnostyki polaryzacji i podejrzanych delayów.

    Celem nie jest zakazanie INV. INV przy 0 ms może być poprawne. Ryzykowne są
    raczej bardzo duże delaye subów lub duże różnice L/R, które mogą oznaczać
    fazowe obejście działające punktowo, lecz mniej naturalne czasowo.
    """
    l_sub = float(mapped.get("l_sub_ms", 0.0))
    r_sub = float(mapped.get("r_sub_ms", 0.0))
    sub_max = max(l_sub, r_sub)
    sub_diff = abs(l_sub - r_sub)
    rel_diff = abs(float(getattr(best, "l_delay_ms", 0.0)) - float(getattr(best, "r_delay_ms", 0.0)))
    penalty = 0.0
    notes: List[str] = []
    if sub_max > 15.0:
        penalty += min(14.0, (sub_max - 15.0) * 0.9)
        notes.append(f"duży delay suba {sub_max:.2f} ms > 15 ms")
    if sub_diff > 10.0:
        penalty += min(16.0, (sub_diff - 10.0) * 1.1)
        notes.append(f"duża różnica delayów sub L/R {sub_diff:.2f} ms > 10 ms")
    if rel_diff > 12.0:
        penalty += min(10.0, (rel_diff - 12.0) * 0.6)
        notes.append(f"duża różnica relacji sub-main L/R {rel_diff:.2f} ms > 12 ms")
    lpol = str(getattr(best, "l_polarity", "OFF")).upper()
    rpol = str(getattr(best, "r_polarity", "OFF")).upper()
    if (lpol != rpol) and sub_diff > 8.0:
        penalty += 4.0
        notes.append("mieszana polaryzacja + asymetryczny delay — konieczny pomiar L/R/L+R")
    if not notes:
        return 0.0, "OK"
    return float(np.clip(penalty, 0.0, 35.0)), "; ".join(notes)


def _row_from_candidate(
    simulated,
    best,
    *,
    hz: int,
    filter_type: str,
    slope_db_oct: int,
    shd_delay_limit_ms: float,
    global_eval_low_hz: float,
    global_eval_high_hz: float,
    global_bulge_low_hz: float,
    global_bulge_high_hz: float,
    music_xo_cap_hz: float,
    caution_xo_cap_hz: float,
    caution_penalty_per_hz: float,
    min_local_pbis_safe: float,
    min_lr_coherence: float,
    min_lr_sum_margin_xo_db: float,
    max_lr_phase_diff_xo_deg: float,
    polarity_scan: bool,
    precise: bool,
    elapsed_s: float,
    scene_context: Optional[_ComplexEvaluationContext] = None,
    global_context: Optional[_ComplexEvaluationContext] = None,
    music_cfg: Optional[OptimizerConfig] = None,
    neutral_cfg: Optional[OptimizerConfig] = None,
    precomputed_scene: Optional[Dict[str, float | str]] = None,
    global_candidates_available: int = 0,
    global_candidates_gate_pass: int = 0,
    global_candidates_prescreened: int = 0,
    global_candidates_evaluated: int = 0,
    global_pool_strategy: str = "smart-prescreen",
) -> CrossoverRankingRow:
    mapped = map_relative_delays_to_shd(best.l_delay_ms, best.r_delay_ms, shd_delay_limit_ms)
    if not bool(mapped["feasible"]):
        raise RuntimeError(f"Błąd mapowania delayów SHD dla {hz} Hz: wynik przekracza limit.")
    polarity_penalty, polarity_warning = _polarity_music_safe_assessment(mapped, best)
    scene = precomputed_scene or _scene_metrics_for_candidate(simulated, int(hz), best, context=scene_context)
    music_cfg = music_cfg or _cfg_for_global_fixed_band(
        filter_type, int(slope_db_oct),
        float(global_eval_low_hz), float(global_eval_high_hz),
        float(global_bulge_low_hz), float(global_bulge_high_hz),
        neutral=False,
    )
    neutral_cfg = neutral_cfg or _cfg_for_global_fixed_band(
        filter_type, int(slope_db_oct),
        float(global_eval_low_hz), float(global_eval_high_hz),
        float(global_bulge_low_hz), float(global_bulge_high_hz),
        neutral=True,
    )
    global_context = global_context or _make_complex_context(simulated, freq_points=int(music_cfg.freq_points))
    music_metrics = _global_fixed_band_metrics_for_candidate(simulated, best, music_cfg, context=global_context)
    neutral_metrics = _global_fixed_band_metrics_for_candidate(simulated, best, neutral_cfg, context=global_context)
    local_safe = float(getattr(best, "pbis_safe_score", getattr(best, "pbis_score", 0.0)))
    side = _side_integration_metrics_for_candidate(best)
    reasons = _local_gate_reasons(
        pbis_safe=local_safe,
        scene_score=float(scene["scene_score"]),
        lr_sum_margin_xo_db=float(scene["lr_sum_margin_xo_db"]),
        lr_phase_diff_xo_deg=float(scene["lr_phase_diff_xo_deg"]),
        min_local_pbis_safe=float(min_local_pbis_safe),
        min_lr_coherence=float(min_lr_coherence),
        min_lr_sum_margin_xo_db=float(min_lr_sum_margin_xo_db),
        max_lr_phase_diff_xo_deg=float(max_lr_phase_diff_xo_deg),
        side_gate_pass=bool(side["side_integration_gate_pass"]),
        side_gate_reasons=str(side["side_integration_gate_reasons"]),
    )
    zone = _xo_zone(int(hz), float(music_xo_cap_hz), float(caution_xo_cap_hz))
    mixed, caution_pen = _music_score(
        global_neutral_safe=float(neutral_metrics["global_pbis_safe"]),
        global_music_safe=float(music_metrics["global_pbis_safe"]),
        local_pbis_safe=local_safe,
        side_integration_score=float(side["side_integration_score_0_100"]),
        side_integration_min=float(side["side_integration_min_0_100"]),
        scene_score=float(scene["scene_score"]),
        hz=int(hz),
        music_xo_cap_hz=float(music_xo_cap_hz),
        caution_xo_cap_hz=float(caution_xo_cap_hz),
        caution_penalty_per_hz=float(caution_penalty_per_hz),
    )
    if float(polarity_penalty) > 0.0:
        mixed = float(np.clip(float(mixed) - float(polarity_penalty), 0.0, 100.0))
    gate_pass = not reasons
    glow_metrics = _midrange_glow_metrics_for_candidate(simulated, best, context=global_context)
    glow_score = _midrange_glow_score(
        music_score=float(mixed),
        tonal_glow_index=float(glow_metrics["tonal_glow_index"]),
        local_pbis_safe=local_safe,
        scene_score=float(scene["scene_score"]),
        local_gate_pass=bool(gate_pass),
        xo_zone=str(zone),
    )
    glow_metrics["midrange_glow_score"] = float(glow_score)
    glow_metrics["midrange_glow_label"] = _midrange_glow_label(float(glow_score), str(glow_metrics.get("thinness_risk", "")))
    v20_metrics = _v20_raw_robustness_metrics(
        side=side,
        scene=scene,
        music_metrics=music_metrics,
        neutral_metrics=neutral_metrics,
        local_safe=local_safe,
        glow_metrics=glow_metrics,
    )
    return CrossoverRankingRow(
        crossover_hz=int(hz),
        filter_type=str(filter_type).upper(),
        slope_db_oct=int(slope_db_oct),
        l_sub_minus_main_ms=float(best.l_delay_ms),
        r_sub_minus_main_ms=float(best.r_delay_ms),
        l_main_shd_ms=float(mapped["l_main_ms"]),
        r_main_shd_ms=float(mapped["r_main_ms"]),
        l_sub_shd_ms=float(mapped["l_sub_ms"]),
        r_sub_shd_ms=float(mapped["r_sub_ms"]),
        l_sub_polarity=str(getattr(best, "l_polarity", "OFF")).upper(),
        r_sub_polarity=str(getattr(best, "r_polarity", "OFF")).upper(),
        polarity_family=f"{str(getattr(best, 'l_polarity', 'OFF')).upper()}/{str(getattr(best, 'r_polarity', 'OFF')).upper()}",
        shd_limit_ms=float(mapped["shd_limit_ms"]),
        shd_max_used_ms=float(mapped["max_used_ms"]),
        pbis=float(getattr(best, "pbis_score", 0.0)),
        pbis_safe=local_safe,
        pbis_label=str(getattr(best, "pbis_label", "")),
        naturalness_label=str(getattr(best, "naturalness_label", "")),
        detail_level="dokładny 0,1 ms" if precise else "screening",
        elapsed_s=float(elapsed_s),
        global_pbis=float(music_metrics["global_pbis"]),
        global_pbis_safe=float(music_metrics["global_pbis_safe"]),
        global_neutral_pbis=float(neutral_metrics["global_pbis"]),
        global_neutral_safe=float(neutral_metrics["global_pbis_safe"]),
        music_score=float(mixed),
        caution_penalty=float(caution_pen),
        local_gate_pass=bool(gate_pass),
        local_gate_reasons="; ".join(reasons) if reasons else "OK",
        polarity_music_safe_penalty=float(polarity_penalty),
        polarity_music_safe_warning=str(polarity_warning),
        recommendable=bool(gate_pass and zone != "diagnostyczny"),
        xo_zone=str(zone),
        global_eval_low_hz=float(global_eval_low_hz),
        global_eval_high_hz=float(global_eval_high_hz),
        music_xo_cap_hz=float(music_xo_cap_hz),
        caution_xo_cap_hz=float(caution_xo_cap_hz),
        above_music_xo_cap=bool(float(hz) > float(music_xo_cap_hz)),
        above_caution_xo_cap=bool(float(hz) > float(caution_xo_cap_hz)),
        global_candidates_available=int(global_candidates_available),
        global_candidates_gate_pass=int(global_candidates_gate_pass),
        global_candidates_prescreened=int(global_candidates_prescreened),
        global_candidates_evaluated=int(global_candidates_evaluated),
        global_pool_strategy=str(global_pool_strategy),
        lr_sum_control_score_0_100=float(scene["scene_score"]),
        **v20_metrics,
        **glow_metrics,
        **side,
        **scene,
    )


def _candidate_row_key(row: CrossoverRankingRow):
    """Wybór delayu wewnątrz jednego XO z pierwszeństwem lokalnej poprawności."""
    return (
        bool(row.local_gate_pass),
        float(row.music_score),
        -float(row.polarity_music_safe_penalty),
        float(row.global_neutral_safe),
        float(row.global_pbis_safe),
        float(row.pbis_safe),
        float(row.scene_score),
        -float(row.shd_max_used_ms),
    )


def _calculate_one(
    raw_measurements,
    hz: int,
    filter_type: str,
    slope_db_oct: int,
    delay_min_ms: float,
    delay_max_ms: float,
    shd_delay_limit_ms: float,
    global_eval_low_hz: float,
    global_eval_high_hz: float,
    global_bulge_low_hz: float,
    global_bulge_high_hz: float,
    music_xo_cap_hz: float,
    caution_xo_cap_hz: float,
    caution_penalty_per_hz: float,
    global_candidate_pool_n: int,
    min_local_pbis_safe: float,
    min_lr_coherence: float,
    min_lr_sum_margin_xo_db: float,
    max_lr_phase_diff_xo_deg: float,
    polarity_scan: bool,
    *,
    precise: bool,
    fast_ab: bool = False,
    reference_neighborhood: bool = False,
    reference_l_rel_ms: Optional[float] = None,
    reference_r_rel_ms: Optional[float] = None,
    reference_radius_ms: float = 2.0,
) -> CrossoverRankingRow:
    cfg = _cfg_for_crossover(
        int(hz), filter_type, int(slope_db_oct), float(delay_min_ms), float(delay_max_ms),
        float(shd_delay_limit_ms), precise=precise, polarity_scan=bool(polarity_scan), fast_ab=bool(fast_ab)
    )
    simulated = simulate_shd_crossover_from_raw(
        *raw_measurements, float(hz), str(filter_type).upper(), int(slope_db_oct)
    )
    t0 = time.perf_counter()
    _freq, _responses, candidates = optimize_delays(*simulated, cfg)
    elapsed = time.perf_counter() - t0
    if bool(polarity_scan):
        eval_candidates = list(candidates)
        family_label = "ALL_POLARITIES"
    else:
        eval_candidates = [
            c for c in candidates
            if str(getattr(c, "l_polarity", "OFF")).upper() == "OFF"
            and str(getattr(c, "r_polarity", "OFF")).upper() == "OFF"
        ]
        family_label = "OFF/OFF"
    if not eval_candidates:
        raise RuntimeError(f"Brak wariantów {family_label} dla {hz} Hz.")
    eval_candidates.sort(key=_local_candidate_sort_key, reverse=True)

    # v19.76: koszyków topologii.
    # Nie zawężamy już obliczeń wyłącznie do sąsiedztwa referencji. Inna topologia
    # delayów może być lepsza, ale musi być nazwana uczciwie jako osobny test A/B,
    # a nie jako bezpośredni następca 90 BW48.
    neighborhood_note = ""
    if bool(reference_neighborhood) and reference_l_rel_ms is not None and reference_r_rel_ms is not None:
        near_count = sum(
            1 for c in eval_candidates
            if _in_reference_neighborhood(
                float(getattr(c, "l_delay_ms", 0.0)),
                float(getattr(c, "r_delay_ms", 0.0)),
                reference_l_rel_ms, reference_r_rel_ms, float(reference_radius_ms)
            )
        )
        neighborhood_note = (
            f"topology-buckets: klasyfikacja względem ref L {float(reference_l_rel_ms):+.2f} / "
            f"R {float(reference_r_rel_ms):+.2f} ms; {near_count} kandydatów w tolerancji "
            f"±{float(reference_radius_ms):.2f} ms na kanał; obce topologie nie są blokowane"
        )

    # v19.57: lokalną bramkę sprawdzamy dla wszystkich zatrzymanych kandydatów
    # wybranej rodziny/puli polaryzacji. Dopiero potem uruchamiamy tani globalny prescreen. Dzięki temu
    # dokładna pula nie zależy już wyłącznie od pierwszych punktów lokalnego PBIS.
    scene_context = _make_complex_context(simulated, native_first_grid=True)
    exact_points = 520 if bool(fast_ab) else 1400
    exact_music_cfg = _cfg_for_global_fixed_band(
        filter_type, int(slope_db_oct),
        float(global_eval_low_hz), float(global_eval_high_hz),
        float(global_bulge_low_hz), float(global_bulge_high_hz), neutral=False, freq_points=exact_points,
    )
    exact_neutral_cfg = _cfg_for_global_fixed_band(
        filter_type, int(slope_db_oct),
        float(global_eval_low_hz), float(global_eval_high_hz),
        float(global_bulge_low_hz), float(global_bulge_high_hz), neutral=True, freq_points=exact_points,
    )
    # Prescreen jest wielokrotnie tańszy, ale korzysta z tej samej funkcji oceny.
    # Nie ustala finalnych liczb raportu — wyłącznie wybiera reprezentatywną pulę.
    fast_points = 160 if bool(fast_ab) else (360 if precise else 260)
    fast_music_cfg = _cfg_for_global_fixed_band(
        filter_type, int(slope_db_oct),
        float(global_eval_low_hz), float(global_eval_high_hz),
        float(global_bulge_low_hz), float(global_bulge_high_hz), neutral=False, freq_points=fast_points,
    )
    fast_neutral_cfg = _cfg_for_global_fixed_band(
        filter_type, int(slope_db_oct),
        float(global_eval_low_hz), float(global_eval_high_hz),
        float(global_bulge_low_hz), float(global_bulge_high_hz), neutral=True, freq_points=fast_points,
    )
    fast_context = _make_complex_context(simulated, freq_points=fast_points)
    exact_context = _make_complex_context(simulated, freq_points=int(exact_music_cfg.freq_points))

    previews: List[Dict[str, object]] = []
    fallback_previews: List[Dict[str, object]] = []
    for cand in eval_candidates:
        scene = _scene_metrics_for_candidate(simulated, int(hz), cand, context=scene_context)
        side = _side_integration_metrics_for_candidate(cand)
        local_safe = float(getattr(cand, "pbis_safe_score", getattr(cand, "pbis_score", 0.0)))
        reasons = _local_gate_reasons(
            pbis_safe=local_safe,
            scene_score=float(scene["scene_score"]),
            lr_sum_margin_xo_db=float(scene["lr_sum_margin_xo_db"]),
            lr_phase_diff_xo_deg=float(scene["lr_phase_diff_xo_deg"]),
            min_local_pbis_safe=float(min_local_pbis_safe),
            min_lr_coherence=float(min_lr_coherence),
            min_lr_sum_margin_xo_db=float(min_lr_sum_margin_xo_db),
            max_lr_phase_diff_xo_deg=float(max_lr_phase_diff_xo_deg),
            side_gate_pass=bool(side["side_integration_gate_pass"]),
            side_gate_reasons=str(side["side_integration_gate_reasons"]),
        )
        preview: Dict[str, object] = {"candidate": cand, "scene": scene, "side": side, "gate_reasons": reasons}
        fallback_previews.append(preview)
        if not reasons:
            previews.append(preview)

    # Gdy bramki nie przechodzi żaden punkt, pozostawiamy diagnostyczny wiersz
    # zamiast generować pusty raport. Do prescreenu trafiają wtedy najlepsze
    # lokalnie warianty, ale wynik nadal pozostanie oznaczony FAIL.
    gate_pass_count = len(previews)
    if not previews:
        previews = fallback_previews[: max(40, min(120, len(fallback_previews)))]

    for item in previews:
        cand = item["candidate"]
        scene = item["scene"]
        side = item.get("side") or _side_integration_metrics_for_candidate(cand)
        fast_music = _global_fixed_band_metrics_for_candidate(simulated, cand, fast_music_cfg, context=fast_context)
        fast_neutral = _global_fixed_band_metrics_for_candidate(simulated, cand, fast_neutral_cfg, context=fast_context)
        approx_score, _approx_pen = _music_score(
            global_neutral_safe=float(fast_neutral["global_pbis_safe"]),
            global_music_safe=float(fast_music["global_pbis_safe"]),
            local_pbis_safe=float(getattr(cand, "pbis_safe_score", getattr(cand, "pbis_score", 0.0))),
            side_integration_score=float(side["side_integration_score_0_100"]),
            side_integration_min=float(side["side_integration_min_0_100"]),
            scene_score=float(scene["scene_score"]), hz=int(hz),
            music_xo_cap_hz=float(music_xo_cap_hz), caution_xo_cap_hz=float(caution_xo_cap_hz),
            caution_penalty_per_hz=float(caution_penalty_per_hz),
        )
        item["approx_music_score"] = float(approx_score)
        item["approx_global_neutral_safe"] = float(fast_neutral["global_pbis_safe"])
        item["approx_global_music_safe"] = float(fast_music["global_pbis_safe"])

    smart_pool_limit = min(int(global_candidate_pool_n), 60) if bool(fast_ab) else int(global_candidate_pool_n)
    smart_pool = _select_smart_global_shortlist(
        previews, max_exact_n=max(1, int(smart_pool_limit)), cell_ms=1.5,
    )
    rows = [
        _row_from_candidate(
            simulated, item["candidate"],
            hz=int(hz), filter_type=filter_type, slope_db_oct=int(slope_db_oct),
            shd_delay_limit_ms=float(shd_delay_limit_ms),
            global_eval_low_hz=float(global_eval_low_hz), global_eval_high_hz=float(global_eval_high_hz),
            global_bulge_low_hz=float(global_bulge_low_hz), global_bulge_high_hz=float(global_bulge_high_hz),
            music_xo_cap_hz=float(music_xo_cap_hz), caution_xo_cap_hz=float(caution_xo_cap_hz),
            caution_penalty_per_hz=float(caution_penalty_per_hz),
            min_local_pbis_safe=float(min_local_pbis_safe), min_lr_coherence=float(min_lr_coherence),
            min_lr_sum_margin_xo_db=float(min_lr_sum_margin_xo_db),
            max_lr_phase_diff_xo_deg=float(max_lr_phase_diff_xo_deg),
            polarity_scan=bool(polarity_scan),
            precise=bool(precise), elapsed_s=float(elapsed),
            scene_context=scene_context, global_context=exact_context,
            music_cfg=exact_music_cfg, neutral_cfg=exact_neutral_cfg,
            precomputed_scene=item["scene"],
            global_candidates_available=len(eval_candidates),
            global_candidates_gate_pass=int(gate_pass_count),
            global_candidates_prescreened=len(previews),
            global_candidates_evaluated=len(smart_pool),
            global_pool_strategy=("polarity-scan+smart-prescreen+local+cells+spacing" if bool(polarity_scan) else "smart-prescreen+local+cells+spacing"),
        )
        for item in smart_pool
    ]
    if not rows:
        raise RuntimeError(f"Brak wariantów do globalnej oceny dla {hz} Hz.")
    row = max(rows, key=_candidate_row_key)

    if bool(reference_neighborhood) and reference_l_rel_ms is not None and reference_r_rel_ms is not None:
        dist = _row_reference_distance(row, reference_l_rel_ms, reference_r_rel_ms)
        within = _in_reference_neighborhood(
            float(row.l_sub_minus_main_ms), float(row.r_sub_minus_main_ms),
            reference_l_rel_ms, reference_r_rel_ms, float(reference_radius_ms)
        )
        code, bucket_label, bucket_decision, bucket_reason = _topology_bucket_for_row(
            row, reference_l_rel_ms, reference_r_rel_ms, float(reference_radius_ms)
        )
        label = (
            f"REF-NEIGHBOR OK: {dist:.2f} ms od realnej referencji"
            if bool(within)
            else f"POZA BEZPOŚREDNIM SĄSIEDZTWEM REF: {dist:.2f} ms od realnej referencji"
        )
        strategy = str(row.global_pool_strategy or "")
        if neighborhood_note:
            strategy = (strategy + "+" if strategy else "") + neighborhood_note
        row = replace(
            row,
            reference_distance_ms=float(dist),
            reference_neighborhood_pass=bool(within),
            reference_neighborhood_label=label,
            topology_bucket_code=code,
            topology_bucket_label=bucket_label,
            topology_bucket_decision=bucket_decision,
            topology_bucket_reason=bucket_reason,
            global_pool_strategy=strategy,
        )
    return row


def _rank_key(row: CrossoverRankingRow):
    """Ranking muzyczny: najpierw bramka stron, potem suma stereo i wynik globalny."""
    return (
        bool(row.recommendable),
        bool(row.local_gate_pass),
        bool(row.side_integration_gate_pass),
        not bool(row.above_caution_xo_cap),
        float(row.robustness_model_score_0_100),
        float(row.music_score),
        -float(row.post_dirac_lr_null_risk_0_100),
        -float(row.dirac_correction_burden_risk_0_100),
        float(row.side_integration_min_0_100),
        float(row.side_integration_score_0_100),
        -float(row.polarity_music_safe_penalty),
        float(row.global_neutral_safe),
        float(row.global_pbis_safe),
        float(row.pbis_safe),
        float(row.scene_score),
        -abs(float(row.l_sub_minus_main_ms) - float(row.r_sub_minus_main_ms)),
        -float(row.shd_max_used_ms),
    )


def _scene_rank_key(row: CrossoverRankingRow):
    """Pomocniczy ranking sumy stereo, ale tylko po przejściu bramki stron."""
    return (
        bool(row.recommendable),
        bool(row.local_gate_pass),
        bool(row.side_integration_gate_pass),
        not bool(row.above_caution_xo_cap),
        float(row.scene_score),
        float(row.lr_sum_margin_xo_db),
        -float(row.lr_phase_diff_xo_deg),
        float(row.side_integration_min_0_100),
        float(row.music_score),
        float(row.pbis_safe),
    )


def _midrange_glow_rank_key(row: CrossoverRankingRow):
    """Dodatkowy ranking A/B pod ciało, nasycenie i blask średnicy.

    Nie zastępuje głównego _rank_key. Ma wskazać warianty, które warto zmierzyć,
    gdy priorytetem jest organiczna średnica, a nie sama równość basu.
    """
    return (
        bool(row.recommendable),
        bool(row.local_gate_pass),
        not bool(row.above_caution_xo_cap),
        float(row.midrange_glow_score),
        float(row.tonal_glow_index),
        float(row.music_score),
        float(row.scene_score),
    )


def _human_safe(score: float) -> str:
    if score >= 75:
        return "dobry kandydat"
    if score >= 65:
        return "warto sprawdzić"
    if score >= 50:
        return "ostrożnie"
    return "wymaga ostrożności"


def _shd_values_text(row: CrossoverRankingRow) -> str:
    return (
        f"main L {row.l_main_shd_ms:.2f} ms, main R {row.r_main_shd_ms:.2f} ms, "
        f"sub L {row.l_sub_shd_ms:.2f} ms {row.l_sub_polarity}, "
        f"sub R {row.r_sub_shd_ms:.2f} ms {row.r_sub_polarity}"
    )


def _relative_values_text(row: CrossoverRankingRow) -> str:
    return (
        f"rel. sub-main: L {row.l_sub_minus_main_ms:+.2f} ms, "
        f"R {row.r_sub_minus_main_ms:+.2f} ms"
    )


def _row_signature(row: CrossoverRankingRow) -> tuple:
    return (
        int(row.crossover_hz),
        str(row.filter_type).upper(),
        int(row.slope_db_oct),
        round(float(row.l_main_shd_ms), 2),
        round(float(row.r_main_shd_ms), 2),
        round(float(row.l_sub_shd_ms), 2),
        round(float(row.r_sub_shd_ms), 2),
        str(row.l_sub_polarity).upper(),
        str(row.r_sub_polarity).upper(),
    )


def _confidence_from_best(row: Optional[CrossoverRankingRow]) -> tuple[str, str]:
    """v19.65: osobna pewność wyniku RAW — score nie jest automatyczną decyzją."""
    if row is None:
        return "NONE", "Brak wariantu przechodzącego bramkę lokalną."
    score = float(row.music_score)
    if score >= 75.0:
        return "HIGH", "Mocny kandydat RAW; nadal wymaga pomiaru realnego slotu i odsłuchu."
    if score >= 55.0:
        return "MEDIUM", "Sensowny kandydat RAW, ale decyzję powinien potwierdzić pomiar A/B."
    if score >= 40.0:
        return "LOW", "Kandydat diagnostyczny: ranking RAW nie znalazł mocnego zwycięzcy."
    return "VERY LOW", "Brak silnego zwycięzcy RAW; traktuj wynik jako podpowiedź do testu, nie rekomendację finalną."


_CONFIDENCE_ORDER = {"NONE": -1, "VERY LOW": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _confidence_rank(label: object) -> int:
    return _CONFIDENCE_ORDER.get(str(label or "").strip().upper(), 0)


def _fmt_float(value: object, digits: int = 1) -> str:
    try:
        v = float(value)
        if not np.isfinite(v):
            return "—"
        return f"{v:.{int(digits)}f}"
    except Exception:
        return "—"


def _clean_yaml_scalar(value: str) -> str:
    """Bardzo mały parser YAML dla prostych plików konfiguracyjnych bez zależności."""
    text = str(value or "").strip()
    if "#" in text:
        text = text.split("#", 1)[0].strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    return text.strip()


def _to_float(value: object, default: float = float("nan")) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


def _parse_filter_name(value: object, default_type: str = "BW", default_slope: int = 48) -> tuple[str, int]:
    text = str(value or "").strip().upper().replace(" ", "")
    m = re.match(r"^(BW|LR)(24|48)?$", text)
    if m:
        return str(m.group(1)), int(m.group(2) or default_slope)
    return str(default_type).upper(), int(default_slope)


def _read_simple_yaml_top_level(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # obsługujemy tylko klucze najwyższego poziomu; zagnieżdżone notatki ignorujemy
        if raw.startswith((" ", "\t")):
            continue
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        data[key.strip()] = _clean_yaml_scalar(value)
    return data


def _resolve_reference_path(path_like: object = None) -> Optional[Path]:
    """Resolve USER_REFERENCE_90BW48.yaml in source and PyInstaller ONEDIR.

    v19.87 fix:
    - GUI in ONEDIR can pass APP_DIR/USER_REFERENCE_90BW48.yaml, while PyInstaller
      may place bundled data in APP_DIR/_internal.
    - If an absolute path is missing, also search by file name in common runtime
      locations instead of failing immediately.
    """
    name = str(path_like or "USER_REFERENCE_90BW48.yaml").strip()
    if not name:
        return None

    p = Path(name)
    base_name = p.name or "USER_REFERENCE_90BW48.yaml"
    candidates: list[Path] = [p]

    def add(c: object) -> None:
        try:
            cp = Path(c)
        except Exception:
            return
        candidates.append(cp)

    # Normal source-mode locations.
    if not p.is_absolute():
        add(Path.cwd() / name)
        try:
            add(Path(__file__).resolve().parent / name)
        except Exception:
            pass

    # PyInstaller ONEDIR / frozen locations.
    try:
        exe_dir = Path(getattr(sys, "executable", "")).resolve().parent
        add(exe_dir / base_name)
        add(exe_dir / "_internal" / base_name)
    except Exception:
        pass
    try:
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        if str(meipass):
            add(meipass / base_name)
            add(meipass / "_internal" / base_name)
    except Exception:
        pass
    try:
        add(Path(__file__).resolve().parent / base_name)
        add(Path(__file__).resolve().parent / "_internal" / base_name)
    except Exception:
        pass

    seen = set()
    for c in candidates:
        try:
            r = c.resolve()
        except Exception:
            r = c
        if str(r) in seen:
            continue
        seen.add(str(r))
        if r.exists():
            return r
    return candidates[0]


def _default_user_reference_slot(path_like: object = None) -> Dict[str, object]:
    """Built-in emergency reference used only when YAML is missing in EXE builds.

    This prevents sąsiedztwa referencji from crashing after PyInstaller places
    data files in a different folder. It is marked as ESTIMATED, not verified.
    """
    return {
        "path": str(path_like or "BUILTIN_DEFAULT_90BW48"),
        "name": "90_BW48_slot2",
        "description": "Awaryjna wbudowana referencja 90 BW48 — oszacowana, zweryfikuj w miniDSP SHD",
        "crossover_hz": 90,
        "filter_type": "BW",
        "slope_db_oct": 48,
        "filter_label": "BW48",
        "l_main_shd_ms": 30.0,
        "r_main_shd_ms": 30.0,
        "l_sub_shd_ms": 20.4,
        "r_sub_shd_ms": 19.1,
        "l_sub_minus_main_ms": -9.6,
        "r_sub_minus_main_ms": -10.9,
        "l_sub_polarity": "OFF",
        "r_sub_polarity": "OFF",
        "source_variant": "BUILTIN_ESTIMATED_v19.87",
        "estimated": True,
    }


def _describe_user_reference_problem(path_like: object = None) -> str:
    path = _resolve_reference_path(path_like)
    if path is None:
        return "nie podano ścieżki"
    if not path.exists():
        return f"plik nie istnieje: {path}"
    data = _read_simple_yaml_top_level(path)
    if not data:
        return f"nie można odczytać prostego YAML: {path}"
    missing = []
    for key in ("l_main_ms", "r_main_ms", "l_sub_ms", "r_sub_ms"):
        if not np.isfinite(_to_float(data.get(key), float("nan"))):
            missing.append(key)
    parts = []
    if missing:
        parts.append("brak realnych delayów SHD w polach: " + ", ".join(missing))
    name = str(data.get("name", ""))
    role = str(data.get("role", ""))
    if "102" in path.name or "102" in name or role:
        parts.append("wybrany plik wygląda na profil kandydata 102; USER_REFERENCE_102BW48.yaml ma delaye null i nie jest realną referencją 90")
    xo = _to_float(data.get("crossover_hz"), float("nan"))
    filt = str(data.get("filter", "")).strip()
    if np.isfinite(xo) and abs(float(xo) - 90.0) > 0.51:
        parts.append(f"crossover_hz={float(xo):g}, a oczekiwana jest realna referencja 90 BW48")
    if filt and "BW48" not in filt.upper().replace(" ", ""):
        parts.append(f"filter={filt}, a oczekiwana jest referencja BW48")
    return "; ".join(parts) if parts else "YAML nie przeszedł walidacji realnej referencji 90"


def _load_user_reference_slot(path_like: object = None) -> Optional[Dict[str, object]]:
    """Czyta realny slot SHD z USER_REFERENCE_90BW48.yaml.

    v20.07: brak albo niepełny YAML nie przechodzi już po cichu na
    wbudowaną referencję. sąsiedztwa referencji ma pracować na realnym
    pliku użytkownika albo zatrzymać analizę czytelnym błędem.

    v19.70: jeśli istnieją pola l_sub_ms/r_sub_ms, mają pierwszeństwo. Dla zgodności
    z v19.70 akceptujemy też l_sub_ms_candidate_a/r_sub_ms_candidate_a.
    """
    path = _resolve_reference_path(path_like)
    if path is None or not path.exists():
        return None
    data = _read_simple_yaml_top_level(path)
    if not data:
        return None
    filt_type, slope = _parse_filter_name(data.get("filter", "BW48"))
    l_main = _to_float(data.get("l_main_ms"))
    r_main = _to_float(data.get("r_main_ms"))
    # realne wartości mają pierwszeństwo; candidate_a jest tylko fallbackiem
    l_sub = _to_float(data.get("l_sub_ms"), float("nan"))
    r_sub = _to_float(data.get("r_sub_ms"), float("nan"))
    source_variant = "l_sub_ms/r_sub_ms"
    if not np.isfinite(l_sub):
        l_sub = _to_float(data.get("l_sub_ms_candidate_a"), float("nan"))
        source_variant = "candidate_a"
    if not np.isfinite(r_sub):
        r_sub = _to_float(data.get("r_sub_ms_candidate_a"), float("nan"))
        source_variant = "candidate_a"
    xo = int(_to_float(data.get("crossover_hz"), 90))
    if not all(np.isfinite(v) for v in [l_main, r_main, l_sub, r_sub]):
        return None
    return {
        "path": str(path),
        "name": data.get("name", "90_BW48_slot2"),
        "description": data.get("description", "Realna referencja użytkownika z konsoli SHD"),
        "crossover_hz": xo,
        "filter_type": filt_type,
        "slope_db_oct": slope,
        "filter_label": f"{filt_type}{slope}",
        "l_main_shd_ms": float(l_main),
        "r_main_shd_ms": float(r_main),
        "l_sub_shd_ms": float(l_sub),
        "r_sub_shd_ms": float(r_sub),
        "l_sub_minus_main_ms": float(l_sub - l_main),
        "r_sub_minus_main_ms": float(r_sub - r_main),
        "l_sub_polarity": data.get("l_sub_polarity", "OFF").strip().upper() or "OFF",
        "r_sub_polarity": data.get("r_sub_polarity", "OFF").strip().upper() or "OFF",
        "source_variant": source_variant,
    }


def _locked_ref_filter_match(row: CrossoverRankingRow, locked_ref: Optional[Dict[str, object]]) -> bool:
    if not locked_ref:
        return False
    return (
        int(row.crossover_hz) == int(locked_ref.get("crossover_hz", -1))
        and str(row.filter_type).upper() == str(locked_ref.get("filter_type", "")).upper()
        and int(row.slope_db_oct) == int(locked_ref.get("slope_db_oct", -1))
    )


def _locked_reference_distance(row: Optional[CrossoverRankingRow], locked_ref: Optional[Dict[str, object]]) -> float:
    if row is None or not locked_ref:
        return float("nan")
    dl = float(row.l_sub_minus_main_ms) - float(locked_ref["l_sub_minus_main_ms"])
    dr = float(row.r_sub_minus_main_ms) - float(locked_ref["r_sub_minus_main_ms"])
    return float((dl * dl + dr * dr) ** 0.5)


def _locked_reference_warning(ref_row: Optional[CrossoverRankingRow], locked_ref: Optional[Dict[str, object]], threshold_ms: float = 1.0) -> str:
    if not locked_ref:
        return "Brak zablokowanej realnej referencji — Slot A pochodzi z rankingu RAW."
    if ref_row is None:
        return "Realna referencja jest zablokowana z YAML; w rankingu RAW nie znaleziono odpowiadającego wiersza 90 BW48."
    dist = _locked_reference_distance(ref_row, locked_ref)
    if np.isfinite(dist) and dist > float(threshold_ms):
        return (
            "UWAGA: realna referencja SHD różni się od referencji RAW. "
            f"RMS różnicy relacji sub-main: {dist:.2f} ms. "
            "Nie przepisuj Slot A z RAW; Slot A jest zablokowany z USER_REFERENCE_90BW48.yaml."
        )
    return "Realna referencja SHD jest zgodna z najbliższą referencją RAW w granicy tolerancji."


def _candidate_reference_distance(cand, ref_l_rel_ms: Optional[float], ref_r_rel_ms: Optional[float]) -> float:
    """Odległość kandydata od realnej referencji SHD w przestrzeni relacji sub-main."""
    if ref_l_rel_ms is None or ref_r_rel_ms is None:
        return float("nan")
    try:
        dl = float(getattr(cand, "l_delay_ms", 0.0)) - float(ref_l_rel_ms)
        dr = float(getattr(cand, "r_delay_ms", 0.0)) - float(ref_r_rel_ms)
        return float((dl * dl + dr * dr) ** 0.5)
    except Exception:
        return float("nan")


def _row_reference_distance(row: CrossoverRankingRow, ref_l_rel_ms: Optional[float], ref_r_rel_ms: Optional[float]) -> float:
    if ref_l_rel_ms is None or ref_r_rel_ms is None:
        return float("nan")
    try:
        dl = float(row.l_sub_minus_main_ms) - float(ref_l_rel_ms)
        dr = float(row.r_sub_minus_main_ms) - float(ref_r_rel_ms)
        return float((dl * dl + dr * dr) ** 0.5)
    except Exception:
        return float("nan")


def _in_reference_neighborhood(l_rel: float, r_rel: float, ref_l_rel_ms: Optional[float], ref_r_rel_ms: Optional[float], radius_ms: float) -> bool:
    if ref_l_rel_ms is None or ref_r_rel_ms is None:
        return True
    return (
        abs(float(l_rel) - float(ref_l_rel_ms)) <= float(radius_ms)
        and abs(float(r_rel) - float(ref_r_rel_ms)) <= float(radius_ms)
    )


def _topology_bucket_for_values(
    l_rel: float,
    r_rel: float,
    ref_l_rel_ms: Optional[float],
    ref_r_rel_ms: Optional[float],
    radius_ms: float = 2.0,
) -> tuple[str, str, str, str]:
    """v19.76: klasyfikacja topologii delayów względem realnej referencji.

    To nie jest ocena jakości. To etykieta decyzyjna: czy kandydat jest bezpośrednim
    następcą 90 BW48, alternatywą muzyczną, czy nową topologią do osobnego testu.
    """
    if ref_l_rel_ms is None or ref_r_rel_ms is None:
        return (
            "UNCLASSIFIED",
            "brak realnej referencji",
            "SLOT TESTOWY",
            "Brak USER_REFERENCE_90BW48.yaml albo brak relacji L/R; nie klasyfikuję topologii.",
        )
    try:
        dl = abs(float(l_rel) - float(ref_l_rel_ms))
        dr = abs(float(r_rel) - float(ref_r_rel_ms))
        dist = float((dl * dl + dr * dr) ** 0.5)
        radius = max(0.1, float(radius_ms))
    except Exception:
        return (
            "UNCLASSIFIED",
            "brak danych topologii",
            "SLOT TESTOWY",
            "Nie udało się policzyć odległości od referencji.",
        )
    if dl <= radius and dr <= radius:
        return (
            "A_DIRECT_SUCCESSOR",
            "A — bezpośredni następca 90 BW48",
            "BEZPOŚREDNI A/B Z 90",
            f"Relacje L/R mieszczą się w tolerancji ±{radius:.2f} ms na kanał; ref-dist {dist:.2f} ms.",
        )
    alt_channel = max(radius * 2.5, 5.0)
    alt_distance = max(radius * 3.5, 7.0)
    if dl <= alt_channel and dr <= alt_channel and dist <= alt_distance:
        return (
            "B_MUSICAL_ALTERNATIVE",
            "B — alternatywa muzyczna",
            "A/B JAKO ALTERNATYWA, NIE PROSTE ZASTĄPIENIE",
            f"Kandydat jest dalej niż bezpośrednie sąsiedztwo, ale nadal w rozsądnym obszarze testu; ΔL {dl:.2f} ms, ΔR {dr:.2f} ms, ref-dist {dist:.2f} ms.",
        )
    return (
        "C_NEW_TOPOLOGY",
        "C — nowa topologia delayów",
        "OSOBNY TEST A/B; NIE PORÓWNUJ 1:1 Z 90",
        f"Relacje są daleko od referencji: ΔL {dl:.2f} ms, ΔR {dr:.2f} ms, ref-dist {dist:.2f} ms. To może być dobre ustawienie, ale jest inną koncepcją integracji sub-main.",
    )


def _topology_bucket_for_row(
    row: CrossoverRankingRow,
    ref_l_rel_ms: Optional[float],
    ref_r_rel_ms: Optional[float],
    radius_ms: float = 2.0,
) -> tuple[str, str, str, str]:
    return _topology_bucket_for_values(
        float(row.l_sub_minus_main_ms), float(row.r_sub_minus_main_ms),
        ref_l_rel_ms, ref_r_rel_ms, float(radius_ms)
    )


def _raw_data_confidence_from_mdat(mdat_path: str | Path) -> tuple[str, str, float, float]:
    """Zakres RAW decyduje, czy ranking może mówić tylko o XO/delay, czy też o barwie."""
    try:
        _lm, _ls, _rm, _rs, project = load_optimizer_measurements_from_mdat(mdat_path)
        lo = float(project.freq_start_hz)
        hi = float(project.freq_end_hz)
    except Exception as exc:
        return "LOW", f"nie udało się pewnie odczytać zakresu RAW: {exc}", float("nan"), float("nan")
    if hi < 300:
        return "LOW", f"RAW kończy się przy {hi:.0f} Hz — dobry do basu i korpusu dolnej średnicy, ale bez pełnej oceny pełni/obecności/szklistości", lo, hi
    if hi < 1000:
        return "LOW", f"RAW kończy się przy {hi:.0f} Hz — brak niższej/pełnej średnicy 300–1000 Hz oraz blasku 1–4 kHz", lo, hi
    if hi < 4000:
        return "MEDIUM", f"RAW kończy się przy {hi:.0f} Hz — brak pełnej oceny presence 1–4 kHz i glass 4–8 kHz", lo, hi
    if hi < 8000:
        return "MEDIUM", f"RAW kończy się przy {hi:.0f} Hz — presence jest częściowo ocenialne, ale glass 4–8 kHz niepełne", lo, hi
    return "HIGH", "RAW obejmuje bas, body, presence 1–4 kHz i glass 4–8 kHz", lo, hi


def _min_confidence(a: str, b: str) -> str:
    return a if _confidence_rank(a) <= _confidence_rank(b) else b


def _raw_reference_decision(best: Optional[CrossoverRankingRow], ref: Optional[CrossoverRankingRow], data_confidence: str) -> tuple[str, str]:
    """RAW może wskazać slot testowy, ale nie powinien sam zmieniać referencji 90 BW48."""
    if best is None:
        return "BRAK ZMIANY REFERENCJI", "żaden wariant nie przeszedł lokalnej bramki integracji"
    if ref is not None and _row_signature(best) == _row_signature(ref):
        return "ZOSTAW 90 BW48", "najlepszy kandydat pokrywa się z referencją użytkownika"
    reasons: List[str] = []
    if _confidence_rank(data_confidence) <= _confidence_rank("LOW"):
        reasons.append("LOW confidence zakresu RAW — brak pełnego obrazu średnicy/presence/glass")
    cand_conf, cand_reason = _confidence_from_best(best)
    if _confidence_rank(cand_conf) < _confidence_rank("HIGH"):
        reasons.append(f"kandydat RAW ma tylko {cand_conf}: {cand_reason}")
    if best.above_caution_xo_cap:
        reasons.append("XO powyżej strefy ostrożnej — diagnostyka, nie preset muzyczny")
    elif best.above_music_xo_cap:
        reasons.append("XO w strefie ostrożnej — wymaga A/B i kontroli średnicy")
    if ref is not None:
        d160 = float(best.support_100_160_vs_60_100_db) - float(ref.support_100_160_vs_60_100_db)
        d220 = float(best.support_100_220_vs_60_100_db) - float(ref.support_100_220_vs_60_100_db)
        if d160 < -0.8 or d220 < -0.8:
            reasons.append(f"mniej podparcia średnicy niż 90 BW48: 100–160 {d160:+.2f} dB, 100–220 {d220:+.2f} dB")
        if d160 > 2.0 or d220 > 2.0:
            reasons.append(f"więcej body niż 90 BW48: 100–160 {d160:+.2f} dB, 100–220 {d220:+.2f} dB — sprawdź maskowanie blasku")
    if reasons:
        return "SLOT TESTOWY A/B — NIE ZASTĘPUJ 90", "; ".join(reasons)
    return "SLOT TESTOWY A/B — WARUNKOWY RYWAL 90", "RAW wygląda dobrze, ale finalna decyzja wymaga realnego pomiaru PRE/POST-DIRAC i odsłuchu"


def _candidate_reason(row: CrossoverRankingRow, ref: Optional[CrossoverRankingRow] = None) -> str:
    parts: List[str] = []
    if row.recommendable:
        parts.append("przeszedł lokalną bramkę")
    else:
        parts.append("nie przeszedł lokalnej bramki — tylko diagnostycznie")
    if row.crossover_hz >= row.caution_xo_cap_hz:
        parts.append("wysoki XO: sprawdź, czy REL nie maskuje C1")
    elif row.crossover_hz > row.music_xo_cap_hz:
        parts.append("strefa ostrożna XO")
    if row.side_integration_min_0_100 >= 65:
        parts.append(f"obie strony sub/main sklejone bezpiecznie: L {row.l_sub_main_score_0_100:.1f}, R {row.r_sub_main_score_0_100:.1f}")
    elif row.side_integration_min_0_100 < 55:
        parts.append(f"słaba jedna strona sub/main: L {row.l_sub_main_score_0_100:.1f}, R {row.r_sub_main_score_0_100:.1f}")
    if row.midrange_glow_score >= 65:
        parts.append("dobre podparcie 100–220 Hz")
    elif row.midrange_glow_score < 35:
        parts.append("ryzyko chudszej/dalszej średnicy")
    if row.scene_score >= 70:
        parts.append("dobra spójność L/R przy XO")
    if abs(float(row.lr_phase_diff_xo_deg)) > 100:
        parts.append("duża różnica fazy L/R przy XO")
    if ref is not None:
        d160 = float(row.support_100_160_vs_60_100_db) - float(ref.support_100_160_vs_60_100_db)
        if d160 < -1.0:
            parts.append(f"mniej body 100–160 niż ref o {d160:.1f} dB")
        elif d160 > 1.0:
            parts.append(f"więcej body 100–160 niż ref o {d160:+.1f} dB")
        else:
            parts.append("tonalnie blisko ref 90 BW48")
    return "; ".join(parts) + "."


def _anchored_shd_values_for_row(row: CrossoverRankingRow, locked_ref: Optional[Dict[str, object]]) -> tuple[float, float, float, float, str]:
    """v19.76: dla kandydatów blisko referencji wpisuj delaye na tej samej kotwicy mainów co Slot A.

    Relacja sub-main jest tym, co akustycznie istotne; zachowanie mainów 30/30 ms
    minimalizuje ryzyko przypadkowej zmiany globalnej latencji i ułatwia A/B w SHD.
    """
    if not locked_ref:
        return (
            float(row.l_main_shd_ms), float(row.r_main_shd_ms),
            float(row.l_sub_shd_ms), float(row.r_sub_shd_ms),
            "mapowanie automatyczne"
        )
    try:
        lm = float(locked_ref["l_main_shd_ms"])
        rm = float(locked_ref["r_main_shd_ms"])
        ls = lm + float(row.l_sub_minus_main_ms)
        rs = rm + float(row.r_sub_minus_main_ms)
        limit = float(row.shd_limit_ms)
        if min(lm, rm, ls, rs) >= -1e-6 and max(lm, rm, ls, rs) <= limit + 1e-6:
            return (lm, rm, ls, rs, "mapowanie zakotwiczone do mainów realnej referencji")
    except Exception:
        pass
    return (
        float(row.l_main_shd_ms), float(row.r_main_shd_ms),
        float(row.l_sub_shd_ms), float(row.r_sub_shd_ms),
        "mapowanie automatyczne — kotwica referencji poza limitem albo niedostępna"
    )


def _append_slot_block(lines: List[str], label: str, row: CrossoverRankingRow, ref: Optional[CrossoverRankingRow] = None, locked_ref: Optional[Dict[str, object]] = None) -> None:
    lm, rm, ls, rs, mapping_note = _anchored_shd_values_for_row(row, locked_ref)
    lines.extend([
        f"## {label}",
        "",
        f"- XO / filtr: **{row.crossover_hz} Hz {row.filter_type}{row.slope_db_oct}**",
        f"- Polaryzacja: **L sub {row.l_sub_polarity}, R sub {row.r_sub_polarity}**",
        f"- L main: **{lm:.2f} ms**",
        f"- R main: **{rm:.2f} ms**",
        f"- L sub: **{ls:.2f} ms**",
        f"- R sub: **{rs:.2f} ms**",
        f"- Relacja sub-main: L **{row.l_sub_minus_main_ms:+.2f} ms**, R **{row.r_sub_minus_main_ms:+.2f} ms**",
        f"- Odległość od realnej referencji: **{_fmt_float(row.reference_distance_ms, 2)} ms** — {row.reference_neighborhood_label}",
        f"- Koszyk topologii: **{row.topology_bucket_label}** — {row.topology_bucket_decision}",
        f"- Uzasadnienie koszyka: {row.topology_bucket_reason}",
        f"- Sposób mapowania SHD: **{mapping_note}**",
        f"- Model odporności rankingu RAW — PREDYKCJA RAW: **{row.robustness_model_score_0_100:.1f}/100** — {row.robustness_model_label}",
        f"- Przewidywana odporność przestrzenna — PREDYKCJA RAW: **{row.spatial_robustness_score_0_100:.1f}/100**; przewidywane ryzyko dziury L+P po Dirac — PREDYKCJA RAW: **{row.post_dirac_lr_null_risk_0_100:.1f}/100**; przewidywane obciążenie korekcji Dirac — PREDYKCJA RAW: **{row.dirac_correction_burden_risk_0_100:.1f}/100**",
        f"- Wynik muzyczny rankingu RAW — PREDYKCJA RAW rankingu RAW — PREDYKCJA RAW: **{row.music_score:.1f}/100**",
        f"- Sklejenie stron — PREDYKCJA RAW: **L {row.l_sub_main_score_0_100:.1f}/100, R {row.r_sub_main_score_0_100:.1f}/100, min {row.side_integration_min_0_100:.1f}/100** — {row.side_integration_label}",
        f"- Podparcie dolnej średnicy — PREDYKCJA RAW: **{row.midrange_glow_score:.1f}/100** ({row.thinness_risk})",
        f"- Spójność sum L/R przy XO — PREDYKCJA RAW: **{row.scene_score:.1f}/100**",
        f"- Dlaczego ten kandydat: {_candidate_reason(row, ref)}",
        "",
        "Do SHD wpisz dokładnie:",
        "",
        "```text",
        f"Crossover: {row.crossover_hz} Hz",
        f"Filter: {row.filter_type}{row.slope_db_oct}",
        f"L main delay: {lm:.2f} ms",
        f"R main delay: {rm:.2f} ms",
        f"L sub delay:  {ls:.2f} ms, polarity {row.l_sub_polarity}",
        f"R sub delay:  {rs:.2f} ms, polarity {row.r_sub_polarity}",
        "```",
        "",
    ])


def _append_locked_reference_block(lines: List[str], label: str, locked_ref: Dict[str, object], ref_row: Optional[CrossoverRankingRow] = None) -> None:
    """v19.70: Slot A zawsze może pochodzić z realnej konsoli SHD, nie z RAW."""
    warn = _locked_reference_warning(ref_row, locked_ref)
    lines.extend([
        f"## {label}",
        "",
        "**LOCKED REAL REFERENCE — NIE NADPISUJ Z RANKINGU RAW.**",
        "",
        f"- Źródło: `{locked_ref.get('path', 'USER_REFERENCE_90BW48.yaml')}` ({locked_ref.get('source_variant', 'real')})",
        f"- Nazwa: **{locked_ref.get('name', '90_BW48_slot2')}**",
        f"- Opis: {locked_ref.get('description', '')}",
        f"- XO / filtr: **{int(locked_ref['crossover_hz'])} Hz {locked_ref['filter_label']}**",
        f"- Polaryzacja: **L sub {locked_ref['l_sub_polarity']}, R sub {locked_ref['r_sub_polarity']}**",
        f"- L main: **{float(locked_ref['l_main_shd_ms']):.2f} ms**",
        f"- R main: **{float(locked_ref['r_main_shd_ms']):.2f} ms**",
        f"- L sub: **{float(locked_ref['l_sub_shd_ms']):.2f} ms**",
        f"- R sub: **{float(locked_ref['r_sub_shd_ms']):.2f} ms**",
        f"- Relacja sub-main: L **{float(locked_ref['l_sub_minus_main_ms']):+.2f} ms**, R **{float(locked_ref['r_sub_minus_main_ms']):+.2f} ms**",
        f"- Kontrola zgodności z RAW: **{warn}**",
        "",
        "Do SHD wpisz dokładnie:",
        "",
        "```text",
        f"Crossover: {int(locked_ref['crossover_hz'])} Hz",
        f"Filter: {locked_ref['filter_label']}",
        f"L main delay: {float(locked_ref['l_main_shd_ms']):.2f} ms",
        f"R main delay: {float(locked_ref['r_main_shd_ms']):.2f} ms",
        f"L sub delay:  {float(locked_ref['l_sub_shd_ms']):.2f} ms, polarity {locked_ref['l_sub_polarity']}",
        f"R sub delay:  {float(locked_ref['r_sub_shd_ms']):.2f} ms, polarity {locked_ref['r_sub_polarity']}",
        "```",
        "",
    ])


def _slot_label_to_tag(label: str, idx: int) -> str:
    m = re.search(r"SLOT\s+([A-D])", str(label).upper())
    if m:
        return m.group(1)
    return chr(ord("A") + idx - 1)


def _slot_clean_name(label: str, obj: object, idx: int) -> str:
    tag = _slot_label_to_tag(label, idx)
    if isinstance(obj, dict):
        xo = int(obj.get("crossover_hz", 90))
        filt = str(obj.get("filter_label", "BW48"))
    else:
        xo = int(obj.crossover_hz)
        filt = f"{obj.filter_type}{obj.slope_db_oct}"
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{tag}_{xo}{filt}").strip("_")


def _write_slot_plan_and_rew_checklist(
    rows: Sequence[CrossoverRankingRow],
    out: Path,
    *,
    best: Optional[CrossoverRankingRow],
    ref: Optional[CrossoverRankingRow],
    glow_best: Optional[CrossoverRankingRow],
    scene_best: Optional[CrossoverRankingRow],
    locked_ref: Optional[Dict[str, object]] = None,
) -> None:
    """v19.70: gotowy plan A/B do SHD z zablokowaną realną referencją Slot A."""
    chosen: List[tuple[str, object]] = []
    skipped: List[str] = []

    def add_row(label: str, row: Optional[CrossoverRankingRow]) -> None:
        if row is None:
            skipped.append(f"{label}: brak kandydata.")
            return
        sig = _row_signature(row)
        for existing_label, existing in chosen:
            if isinstance(existing, CrossoverRankingRow) and _row_signature(existing) == sig:
                skipped.append(f"{label}: taki sam jak {existing_label}, pominięty żeby nie dublować slotu.")
                return
        chosen.append((label, row))

    if locked_ref is not None:
        chosen.append(("SLOT A — LOCKED REAL REFERENCE 90 BW48", locked_ref))
    else:
        add_row("SLOT A — REFERENCJA UŻYTKOWNIKA 90 BW48 Z RAW", ref)

    add_row("SLOT B — TECH CANDIDATE / SLOT TESTOWY", best)
    add_row("SLOT C — BODY SUPPORT / ORGANIC FOUNDATION", glow_best)
    add_row("SLOT D — L/R COHERENCE CONTROL", scene_best)

    slot_lines: List[str] = [
        "# SLOT PLAN DO SHD — REWDO v20.17 CLEAN LISTENING-SAFE",
        "",
        "Ten plik jest praktycznym planem A/B. Slot A ma pierwszeństwo z realnej konsoli SHD (`USER_REFERENCE_90BW48.yaml`), a nie z najbliższego wiersza RAW.",
        "RAW ranking wybiera sloty testowe B/C/D. Decyzję robi realny pomiar slotów PRE/POST-DIRAC i odsłuch pianina/wokalu.",
        "",
    ]
    if locked_ref is None:
        slot_lines.extend([
            "⚠️ **Brak zablokowanej realnej referencji.** Slot A pochodzi z rankingu RAW. Po sprawdzeniu konsoli SHD uruchom `4_VERIFY_USER_SLOT_SHD.bat` i zapisz referencję do `USER_REFERENCE_90BW48.yaml`.",
            "",
        ])

    if not chosen:
        slot_lines.append("Brak kandydatów do slot planu.")
    for label, obj in chosen:
        if isinstance(obj, dict):
            _append_locked_reference_block(slot_lines, label, obj, ref)
        else:
            _append_slot_block(slot_lines, label, obj, ref, locked_ref)

    if skipped:
        slot_lines.extend(["# Sloty pominięte / zduplikowane", ""])
        slot_lines.extend([f"- {s}" for s in skipped])
        slot_lines.append("")

    slot_lines.extend([
        "# Zasada decyzyjna",
        "",
        "- Slot A jest Twoją muzyczną referencją i nie wolno jej nadpisać samym rankingiem RAW.",
        "- Nie zastępuj 90 samym wykresem, ale nie broń też mgiełki 90: porównuj 90 jako teksturę średnicy z 102 jako sceną/energią/komfortem.",
        "- Kandydat musi poprawić kontrolę bez utraty ciała 120–220 Hz, sceny i blasku średnicy.",
        "- PRE-DIRAC jest kontrolą opcjonalną dla nowych/topologicznie podejrzanych kandydatów. Przy porównaniu gotowych slotów idź od razu do Dirac i POST-DIRAC.",
        "- v20.07: dla finalnych slotów zalecany jest POST-DIRAC L+P w trzech pozycjach mikrofonu: posL, MLP, posP.",
        "",
    ])
    (out / "SLOT_PLAN_DO_SHD.md").write_text("\n".join(slot_lines), encoding="utf-8")

    pre_lines: List[str] = [
        "# POMIARY PRE-DIRAC DO ZROBIENIA W REW — REWDO v20.17",
        "",
        "Cel: kontrola integracji slotu bez wpływu Dirac. Warunki: Dirac OFF, PEQ OFF, mikrofon nieruszany, ten sam volume i poziomy subów.",
        "",
        "Zakres: minimum 20–300 Hz, lepiej 20–500 Hz. Jeśli chcesz potem oceniać korpusu/obecności/blasku jednym plikiem, mierz 20 Hz–20 kHz.",
        "",
    ]
    post_lines: List[str] = [
        "# POMIARY POST-DIRAC DO ZROBIENIA W REW — REWDO v20.17",
        "",
        "Cel: finalna decyzja brzmieniowa po Dirac. To te pomiary podajesz do `2_POST_DIRAC_AB_ANALYZER.bat`.",
        "",
        "v20.07 zaleca pomiar odporności 3-punktowej: pełne stereo L+P/L+R w pozycjach mikrofonu `posL`, `MLP`, `posP`.",
        "",
        "Zakres: minimum 20–250/300 Hz pozwala ocenić bas i body. Do oceny blasku średnicy/presence/glass mierz co najmniej do 8 kHz, najlepiej 20 Hz–20 kHz.",
        "",
    ]
    for idx, (label, obj) in enumerate(chosen, start=1):
        clean = _slot_clean_name(label, obj, idx)
        pre_lines.extend([
            f"## {label}",
            "",
            f"- `{clean}_PRE_L_full` — lewa C1 + lewy REL, Dirac OFF",
            f"- `{clean}_PRE_R_full` — prawa C1 + prawy REL, Dirac OFF",
            f"- `{clean}_PRE_LR_full` — cały system L+R, Dirac OFF",
            "",
        ])
        post_lines.extend([
            f"## {label}",
            "",
            f"- `{clean}_POST_LR_posL` — pełne stereo L+P/L+R, mikrofon 15–25 cm w lewo od MLP, Dirac ON",
            f"- `{clean}_POST_LR_MLP` — pełne stereo L+P/L+R, mikrofon w głównym punkcie odsłuchu, Dirac ON",
            f"- `{clean}_POST_LR_posP` — pełne stereo L+P/L+R, mikrofon 15–25 cm w prawo od MLP, Dirac ON",
            f"- `{clean}_POST_L_full` — opcjonalnie: lewa C1 + lewy REL w MLP, diagnostyka",
            f"- `{clean}_POST_R_full` — opcjonalnie: prawa C1 + prawy REL w MLP, diagnostyka",
            "",
        ])
    pre_lines.extend([
        "## Kolejność",
        "",
        "1. Sprawdź, czy nie ma dużej dziury w okolicy 60–100 Hz.",
        "2. Odrzuć warianty z podejrzaną fazą/polaryzacją albo dużym opóźnieniem tylko jednego suba.",
        "3. Dopiero najlepsze 1–2 warianty przepuść przez Dirac.",
        "",
    ])
    post_lines.extend([
        "## Kolejność odsłuchu",
        "",
        "1. Najpierw w raporcie sprawdź MLP oraz odporność 3-punktową L/MLP/P.",
        "2. Potem porównaj pianino i wokale, nie bas.",
        "3. Zapisz, czy preset ma ciało, blask bez szkła, scenę i naturalne wykończenie nut.",
        "4. Jeśli pomiar kończy się przy 250 Hz, traktuj ocenę presence/glow jako brak danych — decyduje odsłuch albo pomiar do 8–20 kHz.",
        "",
    ])
    (out / "POMIARY_PRE_DIRAC_DO_ZROBIENIA.md").write_text("\n".join(pre_lines), encoding="utf-8")
    (out / "POMIARY_POST_DIRAC_DO_ZROBIENIA.md").write_text("\n".join(post_lines), encoding="utf-8")
    measure_lines = [
        "# POMIARY DO ZROBIENIA — INDEKS REWDO v20.17",
        "",
        "Checklista jest rozdzielona:",
        "",
        "- `POMIARY_PRE_DIRAC_DO_ZROBIENIA.md` — kontrola integracji przed Dirac.",
        "- `POMIARY_POST_DIRAC_DO_ZROBIENIA.md` — finalny A/B po Dirac.",
        "",
    ]
    (out / "POMIARY_DO_ZROBIENIA.md").write_text("\n".join(measure_lines), encoding="utf-8")


    # v20.07 UX CLEANUP: pierwszy plik do otwarcia po RAW — konkretne kroki, bez przeładowania metrykami.
    def _role_from_label(lbl: str) -> str:
        u = str(lbl).upper()
        if "SLOT A" in u:
            return "referencja — zostaw"
        if "TECH" in u or "TEST" in u:
            return "kandydat A/B"
        if "BODY" in u or "ORGANIC" in u:
            return "kandydat body/średnica"
        if "COHERENCE" in u or "SPÓJ" in u:
            return "kontrola spójności"
        return "slot testowy"

    co_lines: List[str] = [
        "# 01 — Co teraz zrobić po RAW",
        "",
        "## Wniosek z RAW",
        "",
        "RAW nie wybiera finalnego zwycięzcy. RAW wybiera **sloty do wpisania w SHD i przepuszczenia przez Dirac**.",
        "",
        "## Sloty do wpisania w SHD",
        "",
        "| Slot | Rola | Co zrobić |",
        "|---|---|---|",
    ]
    if chosen:
        for label, obj in chosen:
            role = _role_from_label(label)
            clean = _slot_clean_name(label, obj, len(co_lines)) if obj is not None else ""
            safe_label = str(label).replace("|", "\\|")
            co_lines.append(f"| {safe_label} | {role} | wpisz do SHD; nazwij pomiary np. `{clean}_POST_LR_MLP` |")
    else:
        co_lines.append("| — | brak | ranking nie wskazał slotów do wpisania |")
    co_lines.extend([
        "",
        "## Czy robić PRE-DIRAC?",
        "",
        "Nie jako obowiązek.",
        "",
        "Zrób PRE-DIRAC tylko wtedy, gdy kandydat jest nową topologią, raport pokazuje ostrzeżenia sklejenia stron albo chcesz szybko odsiać wariant przed stratą czasu na Dirac.",
        "",
        "Dla normalnego porównania 3–4 gotowych slotów przejdź od razu do Diraca i wykonaj POST-DIRAC.",
        "",
        "## Co zmierzyć po Diracu",
        "",
        "Najlepiej pełne stereo L+P/L+R w trzech pozycjach mikrofonu:",
        "",
        "```text",
        "<slot>_POST_LR_posL",
        "<slot>_POST_LR_MLP",
        "<slot>_POST_LR_posP",
        "```",
        "",
        "Opcjonalnie do diagnostyki kanałów w MLP:",
        "",
        "```text",
        "<slot>_POST_L_full",
        "<slot>_POST_R_full",
        "```",
        "",
        "## Co słuchać w A/B",
        "",
        "1. Wokal żeński i męski — czy nie robi się chudszy albo dalszy.",
        "2. Fortepian/gitara/saksofon — czy jest ciało i naturalny atak.",
        "3. Kontrabas/stopa — czy bas jest punktowy, ale nie suchy.",
        "4. Centrum sceny — czy nie robi się rozdmuchane albo niestabilne.",
        "5. Zmęczenie po 30 minutach — czy nie pojawia się szklistość albo natarczywość.",
    ])
    (out / "01_CO_TERAZ_ZROBIC_RAW.md").write_text("\n".join(co_lines), encoding="utf-8")


# v19.48: opcjonalne równoległe liczenie niezależnych częstotliwości crossovera.
# Stan jest inicjalizowany raz na proces roboczy, żeby nie kopiować pomiarów RAW
# przy każdym zadaniu. Na Windows używany jest bezpieczny tryb spawn, a na
# systemach POSIX fork. Gdy środowisko nie pozwala utworzyć procesów, ranking
# automatycznie wraca do trybu seryjnego bez zmiany algorytmu.
_RANK_WORKER_RAW = None
_RANK_WORKER_ARGS = None


def _init_rank_worker(raw_measurements, worker_args) -> None:
    global _RANK_WORKER_RAW, _RANK_WORKER_ARGS
    _RANK_WORKER_RAW = raw_measurements
    _RANK_WORKER_ARGS = worker_args


def _rank_worker(task):
    hz, precise = task
    if _RANK_WORKER_RAW is None or _RANK_WORKER_ARGS is None:
        raise RuntimeError("Proces roboczy rankingu RAW nie został poprawnie zainicjalizowany.")
    a = _RANK_WORKER_ARGS
    return _calculate_one(
        _RANK_WORKER_RAW, int(hz), a["filter_type"], int(a["slope_db_oct"]),
        float(a["delay_min_ms"]), float(a["delay_max_ms"]),
        float(a["shd_delay_limit_ms"]),
        float(a["global_eval_low_hz"]), float(a["global_eval_high_hz"]),
        float(a["global_bulge_low_hz"]), float(a["global_bulge_high_hz"]),
        float(a["music_xo_cap_hz"]), float(a["caution_xo_cap_hz"]),
        float(a["caution_penalty_per_hz"]), int(a["global_candidate_pool_n"]),
        float(a["min_local_pbis_safe"]), float(a["min_lr_coherence"]),
        float(a["min_lr_sum_margin_xo_db"]), float(a["max_lr_phase_diff_xo_deg"]),
        bool(a.get("polarity_scan", False)),
        precise=bool(precise),
        fast_ab=bool(a.get("fast_ab", False)),
        reference_neighborhood=bool(a.get("reference_neighborhood", False)),
        reference_l_rel_ms=a.get("reference_l_rel_ms", None),
        reference_r_rel_ms=a.get("reference_r_rel_ms", None),
        reference_radius_ms=float(a.get("reference_radius_ms", 2.0)),
    )


def _calculate_many_crossovers(
    raw_measurements,
    hz_values: Sequence[int],
    *,
    filter_type: str,
    slope_db_oct: int,
    delay_min_ms: float,
    delay_max_ms: float,
    shd_delay_limit_ms: float,
    global_eval_low_hz: float,
    global_eval_high_hz: float,
    global_bulge_low_hz: float,
    global_bulge_high_hz: float,
    music_xo_cap_hz: float,
    caution_xo_cap_hz: float,
    caution_penalty_per_hz: float,
    global_candidate_pool_n: int,
    min_local_pbis_safe: float,
    min_lr_coherence: float,
    min_lr_sum_margin_xo_db: float,
    max_lr_phase_diff_xo_deg: float,
    polarity_scan: bool = False,
    precise: bool = False,
    fast_ab: bool = False,
    reference_neighborhood: bool = False,
    reference_l_rel_ms: Optional[float] = None,
    reference_r_rel_ms: Optional[float] = None,
    reference_radius_ms: float = 2.0,
    workers: int = 1,
    progress: Optional[Callable[[str], None]] = None,
) -> List[CrossoverRankingRow]:
    values = [int(v) for v in hz_values]
    if not values:
        return []

    def say(text: str) -> None:
        if progress is not None:
            progress(text)

    mode_label = "Dopracowuję" if precise else "Sprawdzam"
    if bool(getattr(sys, "frozen", False)) and int(workers) > 1:
        say("  EXE/ONEDIR: wymuszam workers=1, żeby uniknąć zawieszeń multiprocessing/spawn.")
        workers = 1
    worker_count = max(1, min(int(workers), len(values)))
    if worker_count <= 1:
        rows = []
        for idx, hz in enumerate(values, start=1):
            say(f"  {mode_label} {hz} Hz ({idx}/{len(values)})...")
            rows.append(_calculate_one(
                raw_measurements, hz, filter_type, slope_db_oct, delay_min_ms,
                delay_max_ms, shd_delay_limit_ms,
                global_eval_low_hz, global_eval_high_hz,
                global_bulge_low_hz, global_bulge_high_hz,
                music_xo_cap_hz, caution_xo_cap_hz,
                caution_penalty_per_hz, global_candidate_pool_n,
                min_local_pbis_safe, min_lr_coherence,
                min_lr_sum_margin_xo_db, max_lr_phase_diff_xo_deg,
                bool(polarity_scan),
                precise=precise,
                fast_ab=bool(fast_ab),
                reference_neighborhood=bool(reference_neighborhood),
                reference_l_rel_ms=reference_l_rel_ms,
                reference_r_rel_ms=reference_r_rel_ms,
                reference_radius_ms=float(reference_radius_ms),
            ))
        return rows

    worker_args = {
        "filter_type": str(filter_type),
        "slope_db_oct": int(slope_db_oct),
        "delay_min_ms": float(delay_min_ms),
        "delay_max_ms": float(delay_max_ms),
        "shd_delay_limit_ms": float(shd_delay_limit_ms),
        "global_eval_low_hz": float(global_eval_low_hz),
        "global_eval_high_hz": float(global_eval_high_hz),
        "global_bulge_low_hz": float(global_bulge_low_hz),
        "global_bulge_high_hz": float(global_bulge_high_hz),
        "music_xo_cap_hz": float(music_xo_cap_hz),
        "caution_xo_cap_hz": float(caution_xo_cap_hz),
        "caution_penalty_per_hz": float(caution_penalty_per_hz),
        "global_candidate_pool_n": int(global_candidate_pool_n),
        "min_local_pbis_safe": float(min_local_pbis_safe),
        "min_lr_coherence": float(min_lr_coherence),
        "min_lr_sum_margin_xo_db": float(min_lr_sum_margin_xo_db),
        "max_lr_phase_diff_xo_deg": float(max_lr_phase_diff_xo_deg),
        "polarity_scan": bool(polarity_scan),
        "fast_ab": bool(fast_ab),
        "reference_neighborhood": bool(reference_neighborhood),
        "reference_l_rel_ms": reference_l_rel_ms,
        "reference_r_rel_ms": reference_r_rel_ms,
        "reference_radius_ms": float(reference_radius_ms),
    }
    tasks = [(hz, bool(precise)) for hz in values]
    context_name = "spawn" if os.name == "nt" else "fork"
    say(f"  Liczenie równoległe: {worker_count} procesy robocze.")
    try:
        ctx = mp.get_context(context_name)
        by_hz = {}
        with ctx.Pool(
            processes=worker_count,
            initializer=_init_rank_worker,
            initargs=(raw_measurements, worker_args),
        ) as pool:
            for idx, row in enumerate(pool.imap_unordered(_rank_worker, tasks), start=1):
                by_hz[int(row.crossover_hz)] = row
                say(f"  Gotowe {row.crossover_hz} Hz ({idx}/{len(values)}).")
        return [by_hz[hz] for hz in values]
    except Exception as exc:
        say(f"  Nie udało się użyć procesów równoległych ({exc}). Wracam do trybu seryjnego.")
        rows = []
        for idx, hz in enumerate(values, start=1):
            say(f"  {mode_label} {hz} Hz ({idx}/{len(values)})...")
            rows.append(_calculate_one(
                raw_measurements, hz, filter_type, slope_db_oct, delay_min_ms,
                delay_max_ms, shd_delay_limit_ms,
                global_eval_low_hz, global_eval_high_hz,
                global_bulge_low_hz, global_bulge_high_hz,
                music_xo_cap_hz, caution_xo_cap_hz,
                caution_penalty_per_hz, global_candidate_pool_n,
                min_local_pbis_safe, min_lr_coherence,
                min_lr_sum_margin_xo_db, max_lr_phase_diff_xo_deg,
                bool(polarity_scan),
                precise=precise,
                fast_ab=bool(fast_ab),
                reference_neighborhood=bool(reference_neighborhood),
                reference_l_rel_ms=reference_l_rel_ms,
                reference_r_rel_ms=reference_r_rel_ms,
                reference_radius_ms=float(reference_radius_ms),
            ))
        return rows


def rank_raw_crossovers(
    mdat_path: str | Path,
    out_dir: str | Path,
    *,
    filter_type: str = "BW",
    slope_db_oct: int = 48,
    crossover_min_hz: int = 74,
    crossover_max_hz: int = 102,
    crossover_step_hz: int = 2,
    delay_min_ms: float = -30.0,
    delay_max_ms: float = 30.0,
    shd_delay_limit_ms: float = 30.0,
    refine_top_n: int = 5,
    refine_scene_top_n: int = 3,
    music_xo_cap_hz: float = 92.0,
    caution_xo_cap_hz: float = 102.0,
    caution_penalty_per_hz: float = 0.35,
    global_candidate_pool_n: int = 420,
    min_local_pbis_safe: float = 55.0,
    min_lr_coherence: float = 40.0,
    min_lr_sum_margin_xo_db: float = -1.0,
    max_lr_phase_diff_xo_deg: float = 120.0,
    polarity_scan: bool = False,
    allow_high_xo: bool = False,
    fast_ab: bool = False,
    global_eval_low_hz: float = 55.0,
    global_eval_high_hz: float = 120.0,
    global_bulge_low_hz: float = 120.0,
    global_bulge_high_hz: float = 180.0,
    workers: int = 1,
    user_reference_path: str | Path = "USER_REFERENCE_90BW48.yaml",
    lock_user_reference: bool = True,
    reference_neighborhood: bool = False,
    reference_neighborhood_radius_ms: float = 2.0,
    progress: Optional[Callable[[str], None]] = None,
) -> List[CrossoverRankingRow]:
    """Liczy ranking RAW z lokalną bramką integracji i wynikiem muzycznym.

    Delay jest analizowany jako relacja ``sub - main`` w obu kierunkach. Dla
    każdego XO szerzej oceniana jest pula lokalnie najlepszych kandydatów OFF/OFF,
    dzięki czemu szerokie pasmo globalne może wybrać nieco inny delay niż sam
    lokalny PBIS. Strefa 94–102 Hz pozostaje dopuszczona z miękką karą, a warianty
    powyżej 102 Hz wymagają jawnego trybu diagnostycznego.
    """
    def say(text: str) -> None:
        if progress is not None:
            progress(text)

    if float(delay_min_ms) > float(delay_max_ms):
        raise ValueError("Dolna granica relatywnego delayu nie może być większa od górnej.")
    if float(shd_delay_limit_ms) <= 0.0:
        raise ValueError("Limit delayu SHD musi być większy od 0 ms.")
    if float(global_eval_low_hz) >= float(global_eval_high_hz):
        raise ValueError("Stałe pasmo globalne musi mieć dolną granicę niższą od górnej.")
    if float(global_bulge_low_hz) >= float(global_bulge_high_hz):
        raise ValueError("Globalne pasmo kontroli bulwy musi mieć dolną granicę niższą od górnej.")
    if float(music_xo_cap_hz) > float(caution_xo_cap_hz):
        raise ValueError("Limit preferowanego XO nie może być wyższy od limitu strefy ostrożnej.")
    if float(crossover_max_hz) > float(caution_xo_cap_hz) and not bool(allow_high_xo):
        raise ValueError(
            f"Górna granica XO {crossover_max_hz} Hz przekracza strefę ostrożną {caution_xo_cap_hz:.0f} Hz. "
            "Dla wysokich XO uruchom jawnie z --allow-high-xo."
        )
    if int(global_candidate_pool_n) < 1:
        raise ValueError("Pula globalnie ocenianych kandydatów delayu musi być dodatnia.")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_metric_registry(out)
    say(f"Wczytuję plik MDAT: {Path(mdat_path).name} ...")
    t_load = time.time()
    l_main, l_sub, r_main, r_sub, project = load_optimizer_measurements_from_mdat(mdat_path)
    say(f"MDAT wczytany: {int(project.points)} punktów, zakres {float(project.freq_start_hz):.2f}–{float(project.freq_end_hz):.0f} Hz, czas {time.time() - t_load:.1f} s.")
    say(f"Phase Truth Guard: {project.phase_truth_status} — {project.phase_truth_source}")
    errors, warnings = mdat_import_health_checks(project)
    if errors:
        raise ValueError("Plik RAW nie przeszedł kontroli: " + "; ".join(errors))
    if not is_raw_xover_off_project(project):
        raise ValueError("Ranking crossoverów wymaga pliku RAW z wyłączonym crossoverem SHD.")
    try:
        write_mdat_health_report(project, out)
        write_mdat_numeric_import_report(project, out)
        write_raw_measurement_check_report(project, out)
        say("Phase Truth Guard, import numeryczny i RAW_MEASUREMENT_CHECK zapisane.")
    except Exception as exc:
        warnings.append(f"Nie udało się zapisać RAW_MEASUREMENT_CHECK.md: {exc}")
    raw = (l_main, l_sub, r_main, r_sub)
    locked_ref_for_neighborhood = _load_user_reference_slot(user_reference_path) if bool(lock_user_reference) else None
    if bool(reference_neighborhood) and not locked_ref_for_neighborhood:
        problem = _describe_user_reference_problem(user_reference_path)
        raise ValueError(
            "Tryb sąsiedztwa referencji wymaga czytelnego pliku USER_REFERENCE_90BW48.yaml z realnymi delayami SHD. "
            f"Wybrany plik: {user_reference_path}. Problem: {problem}. "
            "Użyj przycisku 'Domyślna 90' w GUI albo uruchom tryb 4 — Verify User Slot SHD i zapisz realną referencję 90."
        )
    values = list(range(int(crossover_min_hz), int(crossover_max_hz) + 1, int(crossover_step_hz)))
    if not values:
        raise ValueError("Pusta lista crossoverów do sprawdzenia.")

    common_kwargs = dict(
        filter_type=filter_type, slope_db_oct=slope_db_oct,
        delay_min_ms=delay_min_ms, delay_max_ms=delay_max_ms,
        shd_delay_limit_ms=shd_delay_limit_ms,
        global_eval_low_hz=global_eval_low_hz, global_eval_high_hz=global_eval_high_hz,
        global_bulge_low_hz=global_bulge_low_hz, global_bulge_high_hz=global_bulge_high_hz,
        music_xo_cap_hz=music_xo_cap_hz, caution_xo_cap_hz=caution_xo_cap_hz,
        caution_penalty_per_hz=caution_penalty_per_hz,
        global_candidate_pool_n=global_candidate_pool_n,
        min_local_pbis_safe=min_local_pbis_safe, min_lr_coherence=min_lr_coherence,
        min_lr_sum_margin_xo_db=min_lr_sum_margin_xo_db,
        max_lr_phase_diff_xo_deg=max_lr_phase_diff_xo_deg,
        polarity_scan=bool(polarity_scan),
        fast_ab=bool(fast_ab),
        reference_neighborhood=bool(reference_neighborhood),
        reference_l_rel_ms=(float(locked_ref_for_neighborhood["l_sub_minus_main_ms"]) if locked_ref_for_neighborhood else None),
        reference_r_rel_ms=(float(locked_ref_for_neighborhood["r_sub_minus_main_ms"]) if locked_ref_for_neighborhood else None),
        reference_radius_ms=float(reference_neighborhood_radius_ms),
        workers=workers, progress=progress,
    )
    if bool(fast_ab):
        if bool(reference_neighborhood) and locked_ref_for_neighborhood:
            say(
                "szybkie A/B REF-NEIGHBORHOOD: klasyfikuję kandydatów względem realnej referencji "
                f"({float(locked_ref_for_neighborhood['l_sub_minus_main_ms']):+.2f}/"
                f"{float(locked_ref_for_neighborhood['r_sub_minus_main_ms']):+.2f} ms, "
                f"tolerancja ±{float(reference_neighborhood_radius_ms):.2f} ms na kanał)."
            )
        else:
            say("szybkie A/B: szybki screening XO, delayów relatywnych i lokalnej bramki integracji.")
    else:
        say("Etap 1/2: screening XO, delayów relatywnych i lokalnej bramki integracji.")
    rows = _calculate_many_crossovers(raw, values, precise=False, **common_kwargs)

    if bool(fast_ab):
        say("szybkie A/B: pomijam kosztowne dogęszczenie 0,1 ms — wyniki służą do wyboru kandydatów A/B.")
        final_rows = list(rows)
    else:
        top_pbis = sorted(rows, key=_rank_key, reverse=True)[: max(1, min(int(refine_top_n), len(rows)))]
        top_scene = sorted(rows, key=_scene_rank_key, reverse=True)[: max(1, min(int(refine_scene_top_n), len(rows)))]
        high_rows = [r for r in rows if bool(r.above_caution_xo_cap)]
        top_high_diag: List[CrossoverRankingRow] = []
        if bool(allow_high_xo) and high_rows:
            top_high_diag = sorted(high_rows, key=_rank_key, reverse=True)[: min(2, len(high_rows))]
        top_for_refine: List[CrossoverRankingRow] = []
        seen_hz = set()
        for row in [*top_pbis, *top_scene, *top_high_diag]:
            if int(row.crossover_hz) not in seen_hz:
                top_for_refine.append(row)
                seen_hz.add(int(row.crossover_hz))
        say("Etap 2/2: dokładne dogęszczenie finalistów co 0,1 ms.")
        refine_hz = [int(old.crossover_hz) for old in top_for_refine]
        refined_rows = _calculate_many_crossovers(raw, refine_hz, precise=True, **common_kwargs)
        refined_by_hz = {int(row.crossover_hz): row for row in refined_rows}
        final_rows = [refined_by_hz.get(int(row.crossover_hz), row) for row in rows]
    final_rows.sort(key=_rank_key, reverse=True)
    write_crossover_ranking_reports(
        final_rows, out, mdat_path=mdat_path, warnings=warnings,
        crossover_min_hz=crossover_min_hz, crossover_max_hz=crossover_max_hz,
        crossover_step_hz=crossover_step_hz, refine_top_n=refine_top_n,
        refine_scene_top_n=refine_scene_top_n,
        relative_delay_min_ms=delay_min_ms, relative_delay_max_ms=delay_max_ms,
        shd_delay_limit_ms=shd_delay_limit_ms, workers=workers,
        music_xo_cap_hz=music_xo_cap_hz, caution_xo_cap_hz=caution_xo_cap_hz,
        caution_penalty_per_hz=caution_penalty_per_hz,
        global_candidate_pool_n=global_candidate_pool_n,
        min_local_pbis_safe=min_local_pbis_safe, min_lr_coherence=min_lr_coherence,
        min_lr_sum_margin_xo_db=min_lr_sum_margin_xo_db,
        max_lr_phase_diff_xo_deg=max_lr_phase_diff_xo_deg,
        polarity_scan=bool(polarity_scan),
        allow_high_xo=allow_high_xo,
        fast_ab=bool(fast_ab),
        global_eval_low_hz=global_eval_low_hz, global_eval_high_hz=global_eval_high_hz,
        global_bulge_low_hz=global_bulge_low_hz, global_bulge_high_hz=global_bulge_high_hz,
        user_reference_path=user_reference_path,
        lock_user_reference=lock_user_reference,
        reference_neighborhood=bool(reference_neighborhood),
        reference_neighborhood_radius_ms=float(reference_neighborhood_radius_ms),
    )
    return final_rows


def write_crossover_ranking_reports(
    rows: Sequence[CrossoverRankingRow],
    out_dir: str | Path,
    *,
    mdat_path: str | Path,
    warnings: Sequence[str],
    crossover_min_hz: int,
    crossover_max_hz: int,
    crossover_step_hz: int,
    refine_top_n: int,
    refine_scene_top_n: int,
    relative_delay_min_ms: float,
    relative_delay_max_ms: float,
    shd_delay_limit_ms: float,
    workers: int = 1,
    profiles: Optional[Sequence[tuple[str, int]]] = None,
    music_xo_cap_hz: float = 92.0,
    caution_xo_cap_hz: float = 102.0,
    caution_penalty_per_hz: float = 0.35,
    global_candidate_pool_n: int = 420,
    min_local_pbis_safe: float = 55.0,
    min_lr_coherence: float = 40.0,
    min_lr_sum_margin_xo_db: float = -1.0,
    max_lr_phase_diff_xo_deg: float = 120.0,
    polarity_scan: bool = False,
    allow_high_xo: bool = False,
    fast_ab: bool = False,
    reference_xo_hz: int = 90,
    reference_filter_type: str = "BW",
    reference_slope_db_oct: int = 48,
    global_eval_low_hz: float = 55.0,
    global_eval_high_hz: float = 120.0,
    global_bulge_low_hz: float = 120.0,
    global_bulge_high_hz: float = 180.0,
    user_reference_path: str | Path = "USER_REFERENCE_90BW48.yaml",
    lock_user_reference: bool = True,
    reference_neighborhood: bool = False,
    reference_neighborhood_radius_ms: float = 2.0,
) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_metric_registry(out)
    locked_ref = _load_user_reference_slot(user_reference_path) if bool(lock_user_reference) else None
    if profiles is None:
        profile_list = sorted({(str(r.filter_type).upper(), int(r.slope_db_oct)) for r in rows})
    else:
        profile_list = [(str(ft).upper(), int(slope)) for ft, slope in profiles]
    profile_labels = [f"{ft}{int(slope)}" for ft, slope in profile_list]

    csv_path = out / "RANKING_CROSSOVEROW_RAW.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "miejsce", "crossover_hz", "filtr", "polaryzacja", "strefa_xo", "rekomendowalny", "bramka_lokalna", "powody_bramki",
            "wynik_muzyczny_0_100", "confidence_kandydata", "confidence_powod", "kara_strefy_ostroznej",
            "l_main_shd_ms", "r_main_shd_ms", "l_sub_shd_ms", "r_sub_shd_ms",
            "l_sub_minus_main_ms", "r_sub_minus_main_ms", "max_uzyty_delay_shd_ms", "limit_shd_ms",
            "pbis_local", "pbis_safe_local", "ocena_local",
            "pbis_global_music", "pbis_global_music_safe", "pbis_global_neutral", "pbis_global_neutral_safe",
            "global_eval_low_hz", "global_eval_high_hz", "limit_preferowanego_xo_hz", "limit_strefy_ostroznej_hz",
            "kandydaci_offoff_zachowani", "kandydaci_bramka_pass", "kandydaci_prescreen", "kandydaci_ocenieni_dokladnie", "strategia_puli_globalnej",
            "ref_distance_ms", "ref_neighborhood_pass", "ref_neighborhood_label",
            "topology_bucket_code", "topology_bucket_label", "topology_bucket_decision", "topology_bucket_reason",
            "l_sub_main_score_0_100", "r_sub_main_score_0_100", "side_integration_score_0_100", "side_integration_min_0_100",
            "side_asymmetry_penalty", "side_integration_gate", "side_integration_label", "side_integration_reasons",
            "odpornosc_przestrzenna_0_100", "obciazenie_diraca_ryzyko_0_100", "ryzyko_dziury_lp_po_dirac_0_100",
            "ciaglosc_fazy_energii_0_100", "ranking_skalibrowany_odsluchem_0_100", "model_odpornosci_v20_0_100", "etykieta_modelu_v20", "uwagi_modelu_v20",
            "spojnosc_lr_xo_0_100", "ocena_spojnosci_lr", "sumowanie_lr_przy_cross_db",
            "roznica_fazy_lr_przy_cross_deg", "balans_lr_przy_cross_db", "sumowanie_lr_w_okolicy_cross_db",
            "midrange_glow_score_0_100", "tonal_glow_index_0_100", "ocena_body_support", "ryzyko_odchudzenia", "polarity_music_safe_penalty", "polarity_music_safe_warning",
            "band_40_60_db", "band_60_80_db", "band_80_100_db", "band_60_100_db", "band_100_160_db", "band_120_180_db", "band_160_220_db", "band_100_220_db",
            "support_100_160_vs_60_100_db", "support_100_220_vs_60_100_db", "support_100_160_vs_80_100_db", "foundation_40_60_vs_60_100_db",
            "dokladnosc", "czas_s"
        ])
        for idx, r in enumerate(rows, start=1):
            writer.writerow([
                idx, r.crossover_hz, f"{r.filter_type}{r.slope_db_oct}", f"{r.l_sub_polarity}/{r.r_sub_polarity}", r.xo_zone,
                "TAK" if r.recommendable else "NIE", "PASS" if r.local_gate_pass else "FAIL", r.local_gate_reasons,
                f"{r.music_score:.1f}", _confidence_from_best(r)[0], _confidence_from_best(r)[1], f"{r.caution_penalty:.2f}",
                f"{r.l_main_shd_ms:.2f}", f"{r.r_main_shd_ms:.2f}", f"{r.l_sub_shd_ms:.2f}", f"{r.r_sub_shd_ms:.2f}",
                f"{r.l_sub_minus_main_ms:.2f}", f"{r.r_sub_minus_main_ms:.2f}", f"{r.shd_max_used_ms:.2f}", f"{r.shd_limit_ms:.2f}",
                f"{r.pbis:.1f}", f"{r.pbis_safe:.1f}", _human_safe(r.pbis_safe),
                f"{r.global_pbis:.1f}", f"{r.global_pbis_safe:.1f}", f"{r.global_neutral_pbis:.1f}", f"{r.global_neutral_safe:.1f}",
                f"{r.global_eval_low_hz:.1f}", f"{r.global_eval_high_hz:.1f}", f"{r.music_xo_cap_hz:.1f}", f"{r.caution_xo_cap_hz:.1f}",
                r.global_candidates_available, r.global_candidates_gate_pass, r.global_candidates_prescreened, r.global_candidates_evaluated, r.global_pool_strategy,
                f"{r.reference_distance_ms:.2f}" if np.isfinite(float(r.reference_distance_ms)) else "", "TAK" if r.reference_neighborhood_pass else "NIE", r.reference_neighborhood_label,
                r.topology_bucket_code, r.topology_bucket_label, r.topology_bucket_decision, r.topology_bucket_reason,
                f"{r.l_sub_main_score_0_100:.1f}", f"{r.r_sub_main_score_0_100:.1f}", f"{r.side_integration_score_0_100:.1f}", f"{r.side_integration_min_0_100:.1f}",
                f"{r.side_asymmetry_penalty:.1f}", "PASS" if r.side_integration_gate_pass else "FAIL", r.side_integration_label, r.side_integration_gate_reasons,
                f"{r.spatial_robustness_score_0_100:.1f}", f"{r.dirac_correction_burden_risk_0_100:.1f}", f"{r.post_dirac_lr_null_risk_0_100:.1f}",
                f"{r.phase_continuity_score_0_100:.1f}", f"{r.listening_calibrated_score_0_100:.1f}", f"{r.robustness_model_score_0_100:.1f}", r.robustness_model_label, r.robustness_model_notes,
                f"{r.scene_score:.1f}", r.scene_label, f"{r.lr_sum_margin_xo_db:.2f}", f"{r.lr_phase_diff_xo_deg:.1f}",
                f"{r.lr_balance_xo_db:.2f}", f"{r.lr_sum_margin_band_db:.2f}",
                f"{r.midrange_glow_score:.1f}", f"{r.tonal_glow_index:.1f}", r.midrange_glow_label, r.thinness_risk, f"{r.polarity_music_safe_penalty:.1f}", r.polarity_music_safe_warning,
                f"{r.band_40_60_db:.2f}", f"{r.band_60_80_db:.2f}", f"{r.band_80_100_db:.2f}", f"{r.band_60_100_db:.2f}",
                f"{r.band_100_160_db:.2f}", f"{r.band_120_180_db:.2f}", f"{r.band_160_220_db:.2f}", f"{r.band_100_220_db:.2f}",
                f"{r.support_100_160_vs_60_100_db:.2f}", f"{r.support_100_220_vs_60_100_db:.2f}",
                f"{r.support_100_160_vs_80_100_db:.2f}", f"{r.foundation_40_60_vs_60_100_db:.2f}",
                r.detail_level, f"{r.elapsed_s:.2f}"
            ])

    glow_csv_path = out / "RANKING_CROSSOVEROW_BODY_SUPPORT.csv"
    glow_rows = sorted(rows, key=_midrange_glow_rank_key, reverse=True)
    with glow_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "miejsce_glow", "miejsce_glow_w_rankingu_glownym", "crossover_hz", "filtr", "polaryzacja", "strefa_xo",
            "rekomendowalny", "bramka_lokalna", "midrange_glow_score_0_100", "tonal_glow_index_0_100",
            "wynik_muzyczny_0_100", "model_odpornosci_v20_0_100", "ryzyko_dziury_lp_po_dirac_0_100", "obciazenie_diraca_ryzyko_0_100",
            "side_min_0_100", "l_sub_main_score_0_100", "r_sub_main_score_0_100", "spojnosc_lr_xo_0_100", "pbis_safe_local",
            "ocena_body_support", "ryzyko_odchudzenia", "polarity_music_safe_penalty", "polarity_music_safe_warning",
            "support_100_160_vs_60_100_db", "support_100_220_vs_60_100_db",
            "support_100_160_vs_80_100_db", "foundation_40_60_vs_60_100_db",
            "band_40_60_db", "band_60_80_db", "band_80_100_db", "band_60_100_db",
            "band_100_160_db", "band_120_180_db", "band_160_220_db", "band_100_220_db",
            "l_main_shd_ms", "r_main_shd_ms", "l_sub_shd_ms", "r_sub_shd_ms",
            "l_sub_minus_main_ms", "r_sub_minus_main_ms",
        ])
        for idx, r in enumerate(glow_rows, start=1):
            writer.writerow([
                idx, rows.index(r) + 1, r.crossover_hz, f"{r.filter_type}{r.slope_db_oct}", f"{r.l_sub_polarity}/{r.r_sub_polarity}", r.xo_zone,
                "TAK" if r.recommendable else "NIE", "PASS" if r.local_gate_pass else "FAIL",
                f"{r.midrange_glow_score:.1f}", f"{r.tonal_glow_index:.1f}", f"{r.music_score:.1f}",
                f"{r.robustness_model_score_0_100:.1f}", f"{r.post_dirac_lr_null_risk_0_100:.1f}", f"{r.dirac_correction_burden_risk_0_100:.1f}",
                f"{r.side_integration_min_0_100:.1f}", f"{r.l_sub_main_score_0_100:.1f}", f"{r.r_sub_main_score_0_100:.1f}", f"{r.scene_score:.1f}", f"{r.pbis_safe:.1f}",
                r.midrange_glow_label, r.thinness_risk, f"{r.polarity_music_safe_penalty:.1f}", r.polarity_music_safe_warning,
                f"{r.support_100_160_vs_60_100_db:.2f}", f"{r.support_100_220_vs_60_100_db:.2f}",
                f"{r.support_100_160_vs_80_100_db:.2f}", f"{r.foundation_40_60_vs_60_100_db:.2f}",
                f"{r.band_40_60_db:.2f}", f"{r.band_60_80_db:.2f}", f"{r.band_80_100_db:.2f}", f"{r.band_60_100_db:.2f}",
                f"{r.band_100_160_db:.2f}", f"{r.band_120_180_db:.2f}", f"{r.band_160_220_db:.2f}", f"{r.band_100_220_db:.2f}",
                f"{r.l_main_shd_ms:.2f}", f"{r.r_main_shd_ms:.2f}", f"{r.l_sub_shd_ms:.2f}", f"{r.r_sub_shd_ms:.2f}",
                f"{r.l_sub_minus_main_ms:.2f}", f"{r.r_sub_minus_main_ms:.2f}",
            ])

    recommendable = [r for r in rows if bool(r.recommendable)]
    local_pass = [r for r in rows if bool(r.local_gate_pass)]
    best = max(recommendable, key=_rank_key) if recommendable else None
    coherence_pool = recommendable or local_pass or list(rows)
    scene_best = max(coherence_pool, key=_scene_rank_key) if coherence_pool else None
    ref_rows = [
        r for r in rows
        if int(r.crossover_hz) == int(reference_xo_hz)
        and str(r.filter_type).upper() == str(reference_filter_type).upper()
        and int(r.slope_db_oct) == int(reference_slope_db_oct)
    ]
    ref = max(ref_rows, key=_midrange_glow_rank_key) if ref_rows else None
    glow_pool = recommendable or local_pass or list(rows)
    glow_best = max(glow_pool, key=_midrange_glow_rank_key) if glow_pool else None
    candidate_confidence_label, candidate_confidence_reason = _confidence_from_best(best)
    raw_data_confidence, raw_data_reason, raw_freq_low, raw_freq_high = _raw_data_confidence_from_mdat(mdat_path)
    confidence_label = _min_confidence(candidate_confidence_label, raw_data_confidence)
    confidence_reason = f"kandydat: {candidate_confidence_reason}; zakres RAW: {raw_data_reason}"
    raw_decision_label, raw_decision_reason = _raw_reference_decision(best, ref, raw_data_confidence)

    lines = [
        "RANKING ZWROTNIC SHD Z JEDNEGO POMIARU RAW — v20.31 UJEDNOLICONE METRYKI",
        "",
        f"Plik bazowy: {Path(mdat_path).name}",
        f"Analizowane profile filtrów SHD: {', '.join(profile_labels) if profile_labels else 'brak'}",
        f"Tryb polaryzacji: {'DIAGNOSTYCZNY — OFF/OFF + INV/OFF + OFF/INV + INV/INV' if bool(polarity_scan) else 'standardowy — tylko OFF/OFF'}",
        f"Zakres crossoverów: {int(crossover_min_hz)}–{int(crossover_max_hz)} Hz co {int(crossover_step_hz)} Hz dla każdego profilu",
        f"Strefy XO: preferowana <= {float(music_xo_cap_hz):.0f} Hz; ostrożna {float(music_xo_cap_hz)+2:.0f}–{float(caution_xo_cap_hz):.0f} Hz; diagnostyczna >{float(caution_xo_cap_hz):.0f} Hz.",
        f"Stałe pasmo globalnego porównania XO: {float(global_eval_low_hz):.0f}–{float(global_eval_high_hz):.0f} Hz; kontrola bulwy: {float(global_bulge_low_hz):.0f}–{float(global_bulge_high_hz):.0f} Hz.",
        f"Zakres RAW: {_fmt_float(raw_freq_low, 1)}–{_fmt_float(raw_freq_high, 0)} Hz; pewność zakresu RAW: {raw_data_confidence} — {raw_data_reason}",
        f"Zakres analizowanej relacji sub - main: {float(relative_delay_min_ms):+.1f}–{float(relative_delay_max_ms):+.1f} ms",
        ("Tryb sąsiedztwa referencji: **AKTYWNY** — kandydaci są klasyfikowani względem tolerancji kanałowej "
         f"±{float(reference_neighborhood_radius_ms):.2f} ms od realnej referencji SHD; obce topologie są klasyfikowane, nie blokowane."
         if bool(reference_neighborhood) else "Tryb sąsiedztwa referencji: nieaktywny — globalny skan diagnostyczny."),
        f"Limit delayu każdego wyjścia SHD: 0,00–{float(shd_delay_limit_ms):.2f} ms",
        f"Dla każdego XO wszystkie zachowane warianty {'wszystkich rodzin polaryzacji' if bool(polarity_scan) else 'OFF/OFF'} przechodzą tani prescreen globalny; oceniana jest pula do {int(min(global_candidate_pool_n, 60) if bool(fast_ab) else global_candidate_pool_n)} punktów (top globalny + top lokalny + reprezentatywne obszary mapy).",
        f"Tryb obliczeń: {'szybkie A/B — szybki wybór kandydatów, bez dogęszczenia 0,1 ms' if bool(fast_ab) else ('równoległy' if int(workers) > 1 else 'seryjny')} ({max(1, int(workers))} procesy robocze).",
        f"Pewność danych RAW: **{confidence_label}** — {confidence_reason}",
        f"Decyzja ochrony 90 BW48: **{raw_decision_label}** — {raw_decision_reason}",
        f"Zablokowana rzeczywista referencja: **{'AKTYWNA' if locked_ref else 'BRAK'}**" + (f" — {locked_ref.get('name')} z `{locked_ref.get('path')}`; relacje L {float(locked_ref['l_sub_minus_main_ms']):+.2f} ms / R {float(locked_ref['r_sub_minus_main_ms']):+.2f} ms" if locked_ref else " — Slot A pochodzi z rankingu RAW"),
        f"Kontrola Slot A: {_locked_reference_warning(ref, locked_ref)}",
        "",
        "LOKALNA BRAMKA INTEGRACJI — wariant rekomendowany musi spełniać wszystkie warunki:",
        "- NAJPIERW SKLEJENIE STRON: L sub/main >= 55/100 i R sub/main >= 55/100; słaba jedna strona blokuje rekomendację.",
        f"- Lokalna jakość integracji PBIS — PREDYKCJA RAW >= {float(min_local_pbis_safe):.1f}",
        f"- Spójność sum L/R przy XO — PREDYKCJA RAW >= {float(min_lr_coherence):.1f}/100",
        f"- L+R przy XO >= {float(min_lr_sum_margin_xo_db):+.2f} dB względem silniejszego kanału",
        f"- różnica fazy L/R przy XO <= {float(max_lr_phase_diff_xo_deg):.1f}°",
        "",
        "MODEL OCENY RANKINGU RAW — v20.23:",
        "- **Sklejenie stron — PREDYKCJA RAW**: wspólna skala v1, identyczny wzór jak w trybach 8/9; etap pozostaje predykcją z RAW.",
        "- **Kontrola sumy stereo L+P**: sprawdza, czy dobra strona po stronie nie tworzy dziury, buły albo rozjazdu fazy w sumie.",
        "- **Przewidywana odporność przestrzenna — PREDYKCJA RAW**: ocena z jednego RAW; nie jest równoznaczna z pomiarem L/MLP/P.",
        "- **Przewidywane obciążenie korekcji Dirac — PREDYKCJA RAW**: im wyżej, tym większe ryzyko; tej skali nie porównuj liczbowo z trybami 8/9.",
        "- **Przewidywane ryzyko dziury L+P po Dirac — PREDYKCJA RAW**: predyktor, nie pomiar końcowy.",
        "- **Przewidywana ciągłość fazy i energii — PREDYKCJA RAW**: sprawdza, czy przejście nie jest dobre tylko punktowo przy XO.",
        "- **Ranking skalibrowany odsłuchem**: preferuje naturalny korpus dolnej średnicy, niskie przewidywane obciążenie korekcji Dirac i niskie przewidywane ryzyko dziury L+P — zgodnie z Twoim priorytetem organicznej średnicy.",
        "",
        "WYNIK MUZYCZNY RANKINGU RAW — PREDYKCJA RAW, 0–100:",
        "- Służy do ustalenia kolejności wariantów w obrębie tego samego pliku RAW; nie jest absolutną oceną jakości całego systemu.",
        "- 30% PBIS Globalna neutralność — PREDYKCJA RAW — identyczne wagi dla BW i LR; uczciwe porównanie filtrów.",
        "- 22% PBIS Globalne bezpieczeństwo muzyczne — PREDYKCJA RAW — ostrożne preferencje właściwe dla profilu filtra.",
        "- 24% Sklejenie stron — PREDYKCJA RAW; wspólna skala v1 z trybami 8/9, mocniej ważona słabsza strona.",
        "- 14% Lokalna jakość integracji PBIS — PREDYKCJA RAW — jakość integracji przy konkretnym XO.",
        "- 10% Spójność sum L/R przy XO — PREDYKCJA RAW — kontrola końcowej sumy stereo, nie zamiennik sklejenia stron.",
        f"- Strefa 94–{float(caution_xo_cap_hz):.0f} Hz: miękka kara {float(caution_penalty_per_hz):.2f} pkt za każdy Hz powyżej {float(music_xo_cap_hz):.0f} Hz.",
        "",
        "DODATKOWY PROFIL PODPARCIA DOLNEJ ŚREDNICY / ORGANICZNOŚCI:",
        "- To nie zastępuje wyniku muzycznego; wskazuje kandydatów A/B pod ciało, nośność i blask średnicy.",
        "- Dane wejściowe: średnie poziomy pasm 40–60, 60–80, 80–100, 100–160, 120–180, 160–220 i 100–220 Hz po symulacji main+sub.",
        "- Szczególnie ważne są relacje 100–160 vs 60–100 Hz oraz 100–220 vs 60–100 Hz.",
        "- Flaga odchudzenia oznacza, że wariant może być pomiarowo poprawny, ale odsłuchowo wycofany/gładki.",
        "- Osobny plik CSV: RANKING_CROSSOVEROW_BODY_SUPPORT.csv.",
        "",
        "Jak czytać ranking:",
        "- PASS oznacza, że wariant przeszedł lokalną bramkę integracji.",
        "- FAIL pozostaje widoczny diagnostycznie, ale nie może wygrać rekomendacji głównej.",
        "- Strefa ostrożna 94–102 Hz nadal może wygrać, gdy jej przewaga jest realna.",
        "- Warianty powyżej 102 Hz są wyłącznie diagnostyczne, nawet przy wysokiej punktacji.",
        "- Każdy wiersz zawiera pełny zestaw czterech delayów do wpisania w SHD.",
        "- W trybie koszyków topologii kandydat spoza tolerancji kanałowej nie jest kasowany; trafia do koszyka `alternatywa muzyczna` albo `nowa topologia` i wymaga osobnego A/B.",
        "",
        "MODEL ODSŁUCHOWY DWÓCH REFERENCJI — v20.23:",
        "- 90 BW48 = referencja tekstury/naturalności średnicy, ale ma zgłoszone minusy: mgiełka, bardziej bułkowaty bas, mniej energii i mniejsza scena.",
        "- 102 BW48 = referencja/kandydat całościowy: szersza scena, bardziej energetyczny bas, mniej mgiełki i mniejsze zmęczenie.",
        "- Celem rankingu nie jest już tylko `zastąpić 90`, ale znaleźć kompromis: naturalność 90 bez mgiełki + scena/energia/komfort 102.",
        "- 100 BW48 jest traktowane jako naturalny kandydat kompromisowy do A/B, nawet jeśli nie wygrywa globalnego rankingu technicznego.",
        "",
        "RANKING:",
    ]
    for idx, r in enumerate(rows, start=1):
        lines.append(
            f"{idx:>2}. {r.crossover_hz:>3} Hz {r.filter_type}{r.slope_db_oct} {r.l_sub_polarity}/{r.r_sub_polarity} | {r.xo_zone} | "
            f"{'PASS' if r.local_gate_pass else 'FAIL'} | model odporności RAW {r.robustness_model_score_0_100:.1f} | wynik muzyczny rankingu RAW — PREDYKCJA RAW {r.music_score:.1f} | "
            f"Przewidywana odporność przestrzenna — PREDYKCJA RAW {r.spatial_robustness_score_0_100:.1f} | przewidywane ryzyko dziury L+P po Dirac — PREDYKCJA RAW {r.post_dirac_lr_null_risk_0_100:.1f} | przewidywane obciążenie korekcji Dirac — PREDYKCJA RAW {r.dirac_correction_burden_risk_0_100:.1f} | "
            f"Globalna neutralność — PREDYKCJA RAW {r.global_neutral_safe:.1f} | Globalne bezpieczeństwo muzyczne — PREDYKCJA RAW {r.global_pbis_safe:.1f} | "
            f"Sklejenie stron — PREDYKCJA RAW L/R {r.l_sub_main_score_0_100:.1f}/{r.r_sub_main_score_0_100:.1f} min {r.side_integration_min_0_100:.1f} | "
            f"Lokalna jakość integracji PBIS — PREDYKCJA RAW {r.pbis_safe:.1f} | Spójność sum L/R przy XO — PREDYKCJA RAW {r.scene_score:.1f} | "
            f"Podparcie dolnej średnicy — PREDYKCJA RAW {r.midrange_glow_score:.1f} ({r.thinness_risk}) | "
            f"100–160 vs 60–100 {r.support_100_160_vs_60_100_db:+.2f} dB | "
            f"L+R XO {r.lr_sum_margin_xo_db:+.2f} dB | faza L/R {r.lr_phase_diff_xo_deg:.1f}° | "
            f"SHD: {_shd_values_text(r)} | {_relative_values_text(r)} | "
            f"ref-dist {(_fmt_float(r.reference_distance_ms, 2) if np.isfinite(float(r.reference_distance_ms)) else '—')} ms | "
            f"{r.topology_bucket_label} | {r.topology_bucket_decision} | "
            f"{r.reference_neighborhood_label} | {r.detail_level} | "
            f"pula globalna {r.global_candidates_evaluated}/{r.global_candidates_prescreened} z {r.global_candidates_available} OFF/OFF"
        )
        lines.append(f"    Model odporności rankingu RAW — PREDYKCJA RAW: {r.robustness_model_label}; {r.robustness_model_notes}")
        lines.append(f"    Dlaczego: {_candidate_reason(r, ref)}")
        if not r.local_gate_pass:
            lines.append(f"    Odrzucony przez bramkę: {r.local_gate_reasons}")

    if bool(reference_neighborhood) and locked_ref:
        near_reference = sorted(
            list(rows),
            key=lambda rr: float(rr.reference_distance_ms) if np.isfinite(float(rr.reference_distance_ms)) else 9999.0,
        )[: min(8, len(rows))]
        lines.extend([
            "",
            "NAJBLIŻSZE REALNE ALTERNATYWY DLA 90 BW48 — REFERENCE-NEIGHBORHOOD:",
            f"To są kandydaci porównywalni z referencją, bo leżą blisko realnych relacji sub-main L {float(locked_ref['l_sub_minus_main_ms']):+.2f} ms / R {float(locked_ref['r_sub_minus_main_ms']):+.2f} ms.",
            f"Tolerancja: ±{float(reference_neighborhood_radius_ms):.2f} ms na kanał, a `ref-dist` pokazuje odległość geometryczną L/R tylko informacyjnie.",
            "",
            "| # | XO / filtr | Relacja L/R sub-main | ref-dist | W tolerancji | Wynik muzyczny rankingu RAW — PREDYKCJA RAW | Podparcie dolnej średnicy — PREDYKCJA RAW | Decyzja |",
            "|---:|---|---:|---:|---|---:|---:|---|",
        ])
        for idx, rr in enumerate(near_reference, 1):
            lines.append(
                f"| {idx} | {rr.crossover_hz} Hz {rr.filter_type}{rr.slope_db_oct} {rr.l_sub_polarity}/{rr.r_sub_polarity} | "
                f"{rr.l_sub_minus_main_ms:+.2f} / {rr.r_sub_minus_main_ms:+.2f} ms | "
                f"{(_fmt_float(rr.reference_distance_ms, 2) if np.isfinite(float(rr.reference_distance_ms)) else '—')} ms | "
                f"{'TAK' if rr.reference_neighborhood_pass else 'NIE'} | "
                f"{rr.music_score:.1f} | {rr.midrange_glow_score:.1f} | "
                f"{'A/B' if rr.recommendable else 'diagnostyka'} |"
            )

    if bool(reference_neighborhood) and locked_ref:
        lines.extend([
            "",
            "KOSZYKI TOPOLOGII OPÓŹNIEŃ — v20.23:",
            "Aplikacja nie zakłada już, że inna topologia jest zła. Oddziela tylko decyzje: co jest bezpośrednim następcą 90 BW48, co jest alternatywą muzyczną, a co wymaga osobnego testu jako nowa koncepcja integracji sub-main.",
        ])
        bucket_order = [
            ("A_DIRECT_SUCCESSOR", "A. BEZPOŚREDNI NASTĘPCY 90 BW48", "Najuczciwsze A/B z aktualną referencją. Można porównywać 1:1 z 90 BW48."),
            ("B_MUSICAL_ALTERNATIVE", "B. ALTERNATYWY MUZYCZNE", "Dalej od referencji, ale jeszcze w rozsądnym obszarze testu. Nie traktuj jako prostego zamiennika 90."),
            ("C_NEW_TOPOLOGY", "C. NOWA TOPOLOGIA DELAYÓW", "Potencjalnie ciekawa lub nawet lepsza, ale wymaga osobnego pomiaru i odsłuchu. Nie porównuj jej z 90 jako tej samej koncepcji."),
        ]
        for code, title, comment in bucket_order:
            pool = [r for r in rows if str(r.topology_bucket_code) == code]
            lines.extend(["", title + ":", comment])
            if not pool:
                lines.append("- Brak kandydatów w tym koszyku w bieżącym skanie.")
                continue
            pool = sorted(pool, key=_rank_key, reverse=True)[:5]
            lines.extend([
                "",
                "| # | XO / filtr | Relacja L/R sub-main | ref-dist | Wynik muzyczny rankingu RAW — PREDYKCJA RAW | Podparcie dolnej średnicy — PREDYKCJA RAW | Status |",
                "|---:|---|---:|---:|---:|---:|---|",
            ])
            for idx, rr in enumerate(pool, 1):
                lines.append(
                    f"| {idx} | {rr.crossover_hz} Hz {rr.filter_type}{rr.slope_db_oct} {rr.l_sub_polarity}/{rr.r_sub_polarity} | "
                    f"{rr.l_sub_minus_main_ms:+.2f} / {rr.r_sub_minus_main_ms:+.2f} ms | "
                    f"{(_fmt_float(rr.reference_distance_ms, 2) if np.isfinite(float(rr.reference_distance_ms)) else '—')} ms | "
                    f"{rr.music_score:.1f} | {rr.midrange_glow_score:.1f} | "
                    f"{'A/B' if rr.recommendable else 'diagnostyka'} |"
                )

    # v19.76: szybka sekcja decyzyjna 90↔102.
    if rows:
        lines.extend([
            "",
            "MODEL DWÓCH REFERENCJI — CO SZUKAĆ TERAZ:",
            "",
            "- **90 BW48**: punkt odniesienia dla naturalności/tekstury średnicy. Nie jest już nietykalnym presetem głównym, bo użytkownik zgłosił mgiełkę i bułkowaty bas.",
            "- **102 BW48**: kandydat całościowy — scena, energia, mniej mgiełki i mniejsze zmęczenie. Nie musi mieć odrobinę najnaturalniejszej średnicy, żeby być lepszym presetem głównym.",
            "- **100 BW48**: najważniejszy kompromis do sprawdzenia, bo leży między charakterem 90 i 102.",
            "",
            "| Rola | Czego szukamy | Co może wygrać |",
            "|---|---|---|",
            "| tekstura średnicy | wokal/pianino, organiczność | 90 lub kandydat z body blisko 90 |",
            "| preset główny | scena, energia, mniej zmęczenia i mniej mgiełki | 102 albo 100 |",
            "| kompromis | naturalność 90 + czystość/scena 102 | najczęściej 100 BW48 do realnego A/B |",
        ])
        compromise_pool = [r for r in rows if str(r.filter_type).upper() == "BW" and int(r.slope_db_oct) == 48 and int(r.crossover_hz) in {98, 100, 102}]
        if compromise_pool:
            compromise_pool = sorted(compromise_pool, key=_rank_key, reverse=True)[:5]
            lines.extend(["", "KANDYDACI KOMPROMISOWI 98/100/102 BW48:", "", "| # | XO | Koszyk | Wynik | Podparcie dolnej średnicy — PREDYKCJA RAW | Ref-dist | Uwagi |", "|---:|---:|---|---:|---:|---:|---|"])
            for ci, cr in enumerate(compromise_pool, 1):
                lines.append(
                    f"| {ci} | {cr.crossover_hz} | {cr.topology_bucket_label} | {cr.music_score:.1f} | {cr.midrange_glow_score:.1f} | "
                    f"{(_fmt_float(cr.reference_distance_ms, 2) if np.isfinite(float(cr.reference_distance_ms)) else '—')} | "
                    f"{'sprawdź 100 jako złoty środek' if int(cr.crossover_hz)==100 else 'porównaj odsłuchowo z 90 i 102'} |"
                )
            lines.append("")

    if profile_list:
        lines.extend(["", "GLOBALNY KANDYDAT TECHNICZNY DLA KAŻDEGO PROFILU FILTRA — NIE ZASTĘPUJE AUTOMATYCZNIE 90/102:"])
        for ft, slope in profile_list:
            profile_rows = [r for r in recommendable if str(r.filter_type).upper() == str(ft).upper() and int(r.slope_db_oct) == int(slope)]
            if not profile_rows:
                lines.append(f"- {ft}{int(slope)} — brak wariantu przechodzącego lokalną bramkę.")
                continue
            pbest = max(profile_rows, key=_rank_key)
            lines.append(
                f"- {ft}{int(slope)} — {pbest.crossover_hz} Hz {pbest.l_sub_polarity}/{pbest.r_sub_polarity}; wynik muzyczny rankingu RAW — PREDYKCJA RAW {pbest.music_score:.1f}; "
                f"SHD: {_shd_values_text(pbest)}; Globalna neutralność — PREDYKCJA RAW {pbest.global_neutral_safe:.1f}; "
                f"Globalne bezpieczeństwo muzyczne — PREDYKCJA RAW {pbest.global_pbis_safe:.1f}; Lokalna jakość integracji PBIS — PREDYKCJA RAW {pbest.pbis_safe:.1f}; Spójność sum L/R przy XO — PREDYKCJA RAW {pbest.scene_score:.1f}."
            )

    if ref is not None:
        lines.extend([
            "",
            f"REFERENCJA UŻYTKOWNIKA — {int(reference_xo_hz)} Hz {str(reference_filter_type).upper()}{int(reference_slope_db_oct)}:",
            f"Najlepszy znaleziony wariant w tej rodzinie: {ref.crossover_hz} Hz {ref.filter_type}{ref.slope_db_oct} {ref.l_sub_polarity}/{ref.r_sub_polarity}",
            f"SHD: {_shd_values_text(ref)}",
            f"Wynik muzyczny rankingu RAW — PREDYKCJA RAW {ref.music_score:.1f}/100; Podparcie dolnej średnicy — PREDYKCJA RAW {ref.midrange_glow_score:.1f}/100; "
            f"Lokalna jakość integracji PBIS — PREDYKCJA RAW {ref.pbis_safe:.1f}/100; Spójność sum L/R przy XO — PREDYKCJA RAW {ref.scene_score:.1f}/100.",
            f"Podparcie dolnej średnicy — PREDYKCJA RAW: 100–160 vs 60–100 = {ref.support_100_160_vs_60_100_db:+.2f} dB; "
            f"100–220 vs 60–100 = {ref.support_100_220_vs_60_100_db:+.2f} dB; ryzyko: {ref.thinness_risk}.",
            "To jest punkt odniesienia do odsłuchu, nie automatyczny zwycięzca. Ma pomóc sprawdzić, czy ranking znalazł realnego rywala dla Twojego ustawienia 90 BW48.",
        ])
    else:
        lines.extend([
            "",
            f"REFERENCJA UŻYTKOWNIKA — {int(reference_xo_hz)} Hz {str(reference_filter_type).upper()}{int(reference_slope_db_oct)}:",
            "Nie ma jej w bieżącym skanie. Rozszerz zakres XO albo dodaj właściwy profil filtra, aby raport mógł porównać ją z kandydatami.",
        ])

    if bool(polarity_scan):
        lines.extend(["", "NAJLEPSZY WARIANT W KAŻDEJ RODZINIE POLARYZACJI — DIAGNOSTYKA:"])
        for fam in ["OFF/OFF", "INV/OFF", "OFF/INV", "INV/INV"]:
            fam_rows = [r for r in rows if f"{r.l_sub_polarity}/{r.r_sub_polarity}" == fam and bool(r.local_gate_pass)]
            if not fam_rows:
                lines.append(f"- {fam}: brak wariantu przechodzącego lokalną bramkę.")
                continue
            fbest = max(fam_rows, key=_rank_key)
            lines.append(
                f"- {fam}: {fbest.crossover_hz} Hz {fbest.filter_type}{fbest.slope_db_oct}; wynik muzyczny rankingu RAW — PREDYKCJA RAW {fbest.music_score:.1f}; "
                f"SHD: {_shd_values_text(fbest)}; relacja {fbest.l_sub_minus_main_ms:+.2f}/{fbest.r_sub_minus_main_ms:+.2f} ms; "
                f"Lokalna jakość integracji PBIS — PREDYKCJA RAW {fbest.pbis_safe:.1f}; Spójność sum L/R przy XO — PREDYKCJA RAW {fbest.scene_score:.1f}; "
                f"MUSIC-SAFE penalty {fbest.polarity_music_safe_penalty:.1f}; {fbest.polarity_music_safe_warning}."
            )
        lines.extend([
            "",
            "UWAGA: tryb polaryzacji jest diagnostyczny. Wariant INV/MIXED należy zawsze potwierdzić pomiarem L, R i L+R oraz odsłuchem przed zapisaniem jako preset docelowy.",
        ])

    if best is None:
        lines.extend([
            "",
            "BRAK AUTOMATYCZNEJ REKOMENDACJI:",
            "Żaden wariant nie przeszedł lokalnej bramki integracji. Nie wpisuj do SHD pierwszego wiersza na ślepo.",
            "Rozszerz zakres delayów, sprawdź plik RAW albo wykonaj pomiar kontrolny ręcznie wybranego wariantu.",
        ])
    else:
        lines.extend([
            "",
            "KANDYDAT TECHNICZNY NR 1 — SLOT TESTOWY / NOWA TOPOLOGIA, NIE AUTOMATYCZNA REFERENCJA:",
            f"{best.crossover_hz} Hz {best.filter_type}{best.slope_db_oct} ({best.xo_zone})",
            f"L main: {best.l_main_shd_ms:.2f} ms",
            f"R main: {best.r_main_shd_ms:.2f} ms",
            f"L sub:  {best.l_sub_shd_ms:.2f} ms, polaryzacja {best.l_sub_polarity}",
            f"R sub:  {best.r_sub_shd_ms:.2f} ms, polaryzacja {best.r_sub_polarity}",
            f"Relacja kontrolna sub - main: L {best.l_sub_minus_main_ms:+.2f} ms, R {best.r_sub_minus_main_ms:+.2f} ms",
            f"Koszyk topologii: {best.topology_bucket_label} — {best.topology_bucket_decision}",
            f"Uzasadnienie koszyka: {best.topology_bucket_reason}",
            f"Model odporności rankingu RAW — PREDYKCJA RAW: {best.robustness_model_score_0_100:.1f}/100 — {best.robustness_model_label}",
            f"Przewidywana odporność przestrzenna — PREDYKCJA RAW: {best.spatial_robustness_score_0_100:.1f}/100; przewidywane ryzyko dziury L+P po Dirac — PREDYKCJA RAW: {best.post_dirac_lr_null_risk_0_100:.1f}/100; przewidywane obciążenie korekcji Dirac — PREDYKCJA RAW: {best.dirac_correction_burden_risk_0_100:.1f}/100.",
            f"Wynik muzyczny rankingu RAW — PREDYKCJA RAW: {best.music_score:.1f}/100",
            f"PBIS Globalna neutralność — PREDYKCJA RAW: {best.global_neutral_safe:.1f}/100",
            f"PBIS Globalne bezpieczeństwo muzyczne — PREDYKCJA RAW: {best.global_pbis_safe:.1f}/100",
            f"Lokalna jakość integracji PBIS — PREDYKCJA RAW: {best.pbis_safe:.1f}/100",
            f"Spójność sum L/R przy XO — PREDYKCJA RAW: {best.scene_score:.1f}/100; L+R {best.lr_sum_margin_xo_db:+.2f} dB; faza {best.lr_phase_diff_xo_deg:.1f}°.",
            f"MUSIC-SAFE POLARITY: kara {best.polarity_music_safe_penalty:.1f}; {best.polarity_music_safe_warning}.",
            f"Pewność danych RAW: {confidence_label} — {confidence_reason}",
            f"Decyzja ochrony 90 BW48: {raw_decision_label} — {raw_decision_reason}",
            (f"Porównywalność z referencją: {best.reference_neighborhood_label}; {best.topology_bucket_label}." if bool(reference_neighborhood) and locked_ref else "Porównywalność z referencją: tryb koszyków topologii nieaktywny."),
            "",
            "CO ZMIERZYĆ NASTĘPNIE — WARIANTY PORÓWNAWCZE A/B:",
        ])
        comparisons: List[CrossoverRankingRow] = []
        best_profile = (str(best.filter_type).upper(), int(best.slope_db_oct))
        for ft, slope in profile_list:
            profile = (str(ft).upper(), int(slope))
            if profile == best_profile:
                continue
            pool = [r for r in recommendable if (str(r.filter_type).upper(), int(r.slope_db_oct)) == profile]
            if pool:
                comparisons.append(max(pool, key=_rank_key))
        for r in recommendable:
            if len(comparisons) >= 2:
                break
            if r is best or r in comparisons:
                continue
            comparisons.append(r)
        if comparisons:
            for r in comparisons[:2]:
                place = rows.index(r) + 1
                lines.append(f"{place}. {r.crossover_hz} Hz {r.filter_type}{r.slope_db_oct}: wynik muzyczny rankingu RAW — PREDYKCJA RAW {r.music_score:.1f}; SHD: {_shd_values_text(r)}")
        else:
            lines.append("- Brak drugiego wariantu przechodzącego bramkę lokalną.")

    glow_pool = recommendable or local_pass or list(rows)
    glow_best = max(glow_pool, key=_midrange_glow_rank_key) if glow_pool else None
    if glow_best is not None:
        place = rows.index(glow_best) + 1
        lines.extend([
            "",
            "KANDYDAT POD CIAŁO ŚREDNICY / ORGANICZNOŚĆ — DO A/B:",
            f"{place}. {glow_best.crossover_hz} Hz {glow_best.filter_type}{glow_best.slope_db_oct} {glow_best.l_sub_polarity}/{glow_best.r_sub_polarity} ({glow_best.xo_zone})",
            f"SHD: {_shd_values_text(glow_best)}",
            f"Podparcie dolnej średnicy — PREDYKCJA RAW: {glow_best.midrange_glow_score:.1f}/100; tonalny indeks podparcia: {glow_best.tonal_glow_index:.1f}/100.",
            f"Ocena: {glow_best.midrange_glow_label}; ryzyko: {glow_best.thinness_risk}.",
            f"Dane pasmowe: 100–160 vs 60–100 = {glow_best.support_100_160_vs_60_100_db:+.2f} dB; "
            f"100–220 vs 60–100 = {glow_best.support_100_220_vs_60_100_db:+.2f} dB; "
            f"100–160 vs 80–100 = {glow_best.support_100_160_vs_80_100_db:+.2f} dB.",
            "Ten kandydat nie zastępuje zwycięzcy technicznego. Ma wskazać ustawienie do odsłuchu, gdy priorytetem jest nasycona średnica i blask bez szklistości.",
        ])
        top_glow = [r for r in glow_rows[:5] if r is not glow_best]
        if top_glow:
            lines.append("Kolejne warianty z profilu Podparcie dolnej średnicy — PREDYKCJA RAW:")
            for r in top_glow[:3]:
                lines.append(
                    f"- {r.crossover_hz} Hz {r.filter_type}{r.slope_db_oct} {r.l_sub_polarity}/{r.r_sub_polarity}: "
                    f"Glow {r.midrange_glow_score:.1f}; 100–160 vs 60–100 {r.support_100_160_vs_60_100_db:+.2f} dB; "
                    f"SHD: {_shd_values_text(r)}"
                )

    if scene_best is not None:
        lines.extend([
            "",
            "KANDYDAT DO SPRAWDZENIA POD KĄTEM SPÓJNOŚCI L/R PRZY XO:",
            f"{scene_best.crossover_hz} Hz {scene_best.filter_type}{scene_best.slope_db_oct} ({scene_best.xo_zone})",
            f"SHD: {_shd_values_text(scene_best)}",
            f"Relacja kontrolna sub - main: L {scene_best.l_sub_minus_main_ms:+.2f} ms, R {scene_best.r_sub_minus_main_ms:+.2f} ms",
            f"Spójność sum L/R przy XO — PREDYKCJA RAW: {scene_best.scene_score:.1f}/100 — {scene_best.scene_label}",
            f"Przy crossoverze: L+R {scene_best.lr_sum_margin_xo_db:+.2f} dB; różnica fazy L/R {scene_best.lr_phase_diff_xo_deg:.1f}°.",
            "To nie jest pomiar szerokości ani głębokości sceny. To kandydat do realnego pomiaru i odsłuchu.",
        ])

    diagnostic = [r for r in rows if bool(r.above_caution_xo_cap)]
    if bool(allow_high_xo) and diagnostic:
        high_best = max(diagnostic, key=_rank_key)
        lines.extend([
            "",
            "DIAGNOSTYKA XO POWYŻEJ 102 Hz — TYLKO DO TRENDÓW, NIE JAKO AUTOMATYCZNA REKOMENDACJA:",
            f"{high_best.crossover_hz} Hz {high_best.filter_type}{high_best.slope_db_oct}; wynik muzyczny rankingu RAW — PREDYKCJA RAW {high_best.music_score:.1f}; "
            f"SHD: {_shd_values_text(high_best)}",
        ])

    if best is not None and int(best.crossover_hz) in {int(crossover_min_hz), int(crossover_max_hz)}:
        boundary = "dolnej" if int(best.crossover_hz) == int(crossover_min_hz) else "górnej"
        lines.extend(["", "UWAGA — ZWYCIĘZCA NA GRANICY SKANU:"])
        if bool(allow_high_xo) and int(best.crossover_hz) == int(crossover_min_hz) and float(crossover_min_hz) >= float(caution_xo_cap_hz):
            lines.extend([
                f"To jest osobny skan diagnostyczny. Punkt {best.crossover_hz} Hz jest mostem do standardowego rankingu i pozostaje rekomendowalny tylko dlatego, że leży jeszcze na granicy strefy ostrożnej.",
                f"Warianty >{float(caution_xo_cap_hz):.0f} Hz służą do diagnozy trendu; nie powinny automatycznie wypierać rekomendacji muzycznej ze standardowego skanu.",
            ])
        elif int(best.crossover_hz) == int(crossover_max_hz) and float(best.crossover_hz) >= float(caution_xo_cap_hz):
            lines.extend([
                f"Najlepszy rekomendowalny wariant wypadł na górnej granicy strefy ostrożnej ({best.crossover_hz} Hz).",
                f"Uruchom osobny skan diagnostyczny powyżej tej granicy. Wyniki >{float(caution_xo_cap_hz):.0f} Hz pozostają diagnostyczne i nie powinny automatycznie wypierać rekomendacji muzycznej.",
            ])
        else:
            lines.append(
                f"Najlepszy wariant wypadł na {boundary} granicy skanu ({best.crossover_hz} Hz). Rozszerz zakres w tym kierunku przed uznaniem wyniku za finalny."
            )
    _write_slot_plan_and_rew_checklist(rows, out, best=best, ref=ref, glow_best=glow_best, scene_best=scene_best, locked_ref=locked_ref)
    lines.extend(["", "PROCEDURA DECYZJI A/B — v20.23:", "- Gotowe ustawienia do wpisania w SHD zapisano jako `SLOT_PLAN_DO_SHD.md`.", "- RAW ranking wskazuje sloty testowe, nie finalną zmianę referencji 90 BW48.", "- Checklisty pomiarów zapisano jako `POMIARY_PRE_DIRAC_DO_ZROBIENIA.md` oraz `POMIARY_POST_DIRAC_DO_ZROBIENIA.md`. Stary plik `POMIARY_DO_ZROBIENIA.md` jest tylko indeksem.", "", "KONTROLA POMIARU RAW:", "- Dodatkowy raport kontroli pomiaru bazowego zapisano jako `RAW_MEASUREMENT_CHECK.md` w tym folderze wynikowym.", "- Dla nowych pomiarów RAW zalecany jest sweep 20 Hz–20 kHz, minimum 15 Hz–8 kHz, żeby aplikacja mogła później oceniać korpusu/obecności/blasku."])
    if warnings:
        lines.extend(["", "UWAGI DO PLIKU RAW:"])
        lines.extend([f"- {w}" for w in warnings])
    lines.extend([
        "",
        "PRE-DIRAC nie jest obowiązkowy. Użyj go tylko wtedy, gdy kandydat jest nową topologią, ma ostrzeżenia albo chcesz szybko sprawdzić sklejenie L-sub/main i R-sub/main przed stratą czasu na Dirac.",
        "Dla normalnego porównania wybranych slotów przejdź do Diraca, wykonaj POST-DIRAC L+P w MLP albo L/MLP/P i dopiero wtedy podejmuj decyzję.",
        "",
    ])
    text = "\n".join(lines)
    (out / "RANKING_CROSSOVEROW_RAW.txt").write_text(text, encoding="utf-8")
    (out / "RANKING_CROSSOVEROW_RAW.md").write_text("```text\n" + text + "```\n", encoding="utf-8")


def parse_filter_profiles(spec: str) -> List[tuple[str, int]]:
    """Parsuje np. ``BW48,LR48`` i usuwa duplikaty z zachowaniem kolejności."""
    text = str(spec or "").strip()
    if not text:
        raise ValueError("Lista profili filtrów jest pusta.")
    result: List[tuple[str, int]] = []
    seen = set()
    for raw in text.replace(";", ",").split(","):
        token = raw.strip().upper().replace(" ", "")
        if not token:
            continue
        if token not in {"BW24", "BW48", "LR24", "LR48"}:
            raise ValueError(f"Nieobsługiwany profil filtra: {raw!r}. Użyj BW24, BW48, LR24 albo LR48.")
        profile = (token[:2], int(token[2:]))
        if profile not in seen:
            result.append(profile)
            seen.add(profile)
    if not result:
        raise ValueError("Lista profili filtrów jest pusta.")
    return result


def rank_raw_crossover_profiles(
    mdat_path: str | Path,
    out_dir: str | Path,
    *,
    profiles: Sequence[tuple[str, int]] = (("BW", 48), ("LR", 48)),
    crossover_min_hz: int = 74,
    crossover_max_hz: int = 102,
    crossover_step_hz: int = 2,
    delay_min_ms: float = -30.0,
    delay_max_ms: float = 30.0,
    shd_delay_limit_ms: float = 30.0,
    refine_top_n: int = 5,
    refine_scene_top_n: int = 3,
    music_xo_cap_hz: float = 92.0,
    caution_xo_cap_hz: float = 102.0,
    caution_penalty_per_hz: float = 0.35,
    global_candidate_pool_n: int = 420,
    min_local_pbis_safe: float = 55.0,
    min_lr_coherence: float = 40.0,
    min_lr_sum_margin_xo_db: float = -1.0,
    max_lr_phase_diff_xo_deg: float = 120.0,
    polarity_scan: bool = False,
    allow_high_xo: bool = False,
    fast_ab: bool = False,
    reference_xo_hz: int = 90,
    reference_filter_type: str = "BW",
    reference_slope_db_oct: int = 48,
    global_eval_low_hz: float = 55.0,
    global_eval_high_hz: float = 120.0,
    global_bulge_low_hz: float = 120.0,
    global_bulge_high_hz: float = 180.0,
    workers: int = 1,
    user_reference_path: str | Path = "USER_REFERENCE_90BW48.yaml",
    lock_user_reference: bool = True,
    reference_neighborhood: bool = False,
    reference_neighborhood_radius_ms: float = 2.0,
    progress: Optional[Callable[[str], None]] = None,
) -> List[CrossoverRankingRow]:
    """Porównuje częstotliwości i wiele profili filtrów w jednym raporcie.

    Każdy profil jest screenowany i dogęszczany niezależnie, dzięki czemu LR48
    nie może zniknąć tylko dlatego, że topowe miejsca globalne zajęły warianty BW48.
    """
    def say(text: str) -> None:
        if progress is not None:
            progress(text)

    normalized: List[tuple[str, int]] = []
    seen = set()
    for ft, slope in profiles:
        profile = (str(ft).upper(), int(slope))
        if profile[0] not in {"BW", "LR"} or profile[1] not in {24, 48}:
            raise ValueError(f"Nieobsługiwany profil filtra: {profile[0]}{profile[1]}.")
        if profile not in seen:
            normalized.append(profile)
            seen.add(profile)
    if not normalized:
        raise ValueError("Nie podano żadnego profilu filtra do rankingu RAW.")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_metric_registry(out)
    all_rows: List[CrossoverRankingRow] = []
    for idx, (ft, slope) in enumerate(normalized, start=1):
        label = f"{ft}{int(slope)}"
        say("")
        say(f"=== PROFIL {label} ({idx}/{len(normalized)}) ===")
        profile_out = out / f"profil_{label}"
        rows = rank_raw_crossovers(
            mdat_path, profile_out,
            filter_type=ft, slope_db_oct=slope,
            crossover_min_hz=crossover_min_hz, crossover_max_hz=crossover_max_hz,
            crossover_step_hz=crossover_step_hz,
            delay_min_ms=delay_min_ms, delay_max_ms=delay_max_ms,
            shd_delay_limit_ms=shd_delay_limit_ms,
            refine_top_n=refine_top_n, refine_scene_top_n=refine_scene_top_n,
            music_xo_cap_hz=music_xo_cap_hz, caution_xo_cap_hz=caution_xo_cap_hz,
            caution_penalty_per_hz=caution_penalty_per_hz, global_candidate_pool_n=global_candidate_pool_n,
            min_local_pbis_safe=min_local_pbis_safe, min_lr_coherence=min_lr_coherence,
            min_lr_sum_margin_xo_db=min_lr_sum_margin_xo_db, max_lr_phase_diff_xo_deg=max_lr_phase_diff_xo_deg,
            polarity_scan=bool(polarity_scan),
            allow_high_xo=allow_high_xo,
            fast_ab=bool(fast_ab),
            global_eval_low_hz=global_eval_low_hz, global_eval_high_hz=global_eval_high_hz,
            global_bulge_low_hz=global_bulge_low_hz, global_bulge_high_hz=global_bulge_high_hz,
            workers=workers,
            user_reference_path=user_reference_path,
            lock_user_reference=lock_user_reference,
            reference_neighborhood=bool(reference_neighborhood),
            reference_neighborhood_radius_ms=float(reference_neighborhood_radius_ms),
            progress=(lambda msg, profile_label=label: say(f"[{profile_label}] {msg}")),
        )
        all_rows.extend(rows)

    all_rows.sort(key=_rank_key, reverse=True)
    _lm, _ls, _rm, _rs, project = load_optimizer_measurements_from_mdat(mdat_path)
    errors, warnings = mdat_import_health_checks(project)
    if errors:
        raise ValueError("Plik RAW nie przeszedł kontroli: " + "; ".join(errors))
    write_crossover_ranking_reports(
        all_rows, out, mdat_path=mdat_path, warnings=warnings,
        crossover_min_hz=crossover_min_hz, crossover_max_hz=crossover_max_hz,
        crossover_step_hz=crossover_step_hz, refine_top_n=refine_top_n,
        refine_scene_top_n=refine_scene_top_n,
        relative_delay_min_ms=delay_min_ms, relative_delay_max_ms=delay_max_ms,
        shd_delay_limit_ms=shd_delay_limit_ms, workers=workers,
        profiles=normalized,
        music_xo_cap_hz=music_xo_cap_hz, caution_xo_cap_hz=caution_xo_cap_hz,
        caution_penalty_per_hz=caution_penalty_per_hz, global_candidate_pool_n=global_candidate_pool_n,
        min_local_pbis_safe=min_local_pbis_safe, min_lr_coherence=min_lr_coherence,
        min_lr_sum_margin_xo_db=min_lr_sum_margin_xo_db, max_lr_phase_diff_xo_deg=max_lr_phase_diff_xo_deg,
        polarity_scan=bool(polarity_scan),
        allow_high_xo=allow_high_xo,
        fast_ab=bool(fast_ab),
        reference_xo_hz=reference_xo_hz,
        reference_filter_type=reference_filter_type,
        reference_slope_db_oct=reference_slope_db_oct,
        global_eval_low_hz=global_eval_low_hz, global_eval_high_hz=global_eval_high_hz,
        global_bulge_low_hz=global_bulge_low_hz, global_bulge_high_hz=global_bulge_high_hz,
        user_reference_path=user_reference_path,
        lock_user_reference=lock_user_reference,
        reference_neighborhood=bool(reference_neighborhood),
        reference_neighborhood_radius_ms=float(reference_neighborhood_radius_ms),
    )
    return all_rows


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ranking crossoverów SHD z jednego pliku RAW .mdat — v20.31 ujednolicone metryki")
    p.add_argument("--mdat", required=True, help="Bazowy RAW z pliku .mdat albo migawki .rewapi.json, z wyłączonym crossoverem SHD")
    p.add_argument("--out", default="ranking_crossovers_RAW", help="Folder raportu")
    p.add_argument("--profiles", default="BW48,LR48", help="Profile do wspólnego rankingu, np. BW48,LR48 albo BW24,BW48,LR24,LR48")
    p.add_argument("--filter-type", default=None, choices=["BW", "LR"], help="Tryb zgodności wstecznej: pojedynczy typ filtra; użyj razem z --slope")
    p.add_argument("--slope", default=None, type=int, choices=[24, 48], help="Tryb zgodności wstecznej: pojedyncza stromizna; użyj razem z --filter-type")
    p.add_argument("--xo-min", default=74, type=int)
    p.add_argument("--xo-max", default=102, type=int, help="Górna granica XO; domyślnie zakres muzyczny z miękką strefą ostrożną do 102 Hz")
    p.add_argument("--xo-step", default=2, type=int)
    p.add_argument("--delay-min", default=-30.0, type=float, help="Minimalna relacja sub - main [ms]")
    p.add_argument("--delay-max", default=30.0, type=float, help="Maksymalna relacja sub - main [ms]")
    p.add_argument("--shd-limit", default=30.0, type=float, help="Maksymalny delay pojedynczego wyjścia SHD [ms]")
    p.add_argument("--refine-top", default=5, type=int)
    p.add_argument("--refine-scene-top", default=3, type=int, help="Legacy name: liczba kandydatów spójności L/R przy XO do dokładnego dogęszczenia")
    p.add_argument("--music-xo-cap", default=92.0, type=float, help="Górna granica preferowanej strefy XO; powyżej działa miękka kara ostrożności")
    p.add_argument("--caution-xo-cap", default=102.0, type=float, help="Górna granica dopuszczonej strefy ostrożnej; wyższe XO są diagnostyczne")
    p.add_argument("--caution-penalty-per-hz", default=0.35, type=float, help="Miękka kara za każdy Hz powyżej preferowanej strefy XO")
    p.add_argument("--global-candidate-pool", default=420, type=int, help="Maksymalna liczba reprezentatywnych par delayów ocenianych dokładnie globalnie dla każdego XO; wcześniej było tylko top-20 lokalnego PBIS")
    p.add_argument("--min-local-pbis-safe", default=55.0, type=float, help="Minimalny Lokalna jakość integracji PBIS — PREDYKCJA RAW lokalnej bramki integracji")
    p.add_argument("--min-lr-coherence", default=40.0, type=float, help="Minimalna spójność L/R przy XO lokalnej bramki")
    p.add_argument("--min-lr-sum-margin-xo", default=-1.0, type=float, help="Minimalne sumowanie L+R przy XO [dB]")
    p.add_argument("--max-lr-phase-diff-xo", default=120.0, type=float, help="Maksymalna różnica fazy L/R przy XO [stopnie]")
    p.add_argument("--polarity-scan", action="store_true", help="TRYB DIAGNOSTYCZNY: skanuj OFF/OFF, INV/OFF, OFF/INV i INV/INV. Domyślnie ranking muzyczny skanuje tylko OFF/OFF.")
    p.add_argument("--allow-high-xo", action="store_true", help="Jawnie zezwól na diagnostyczny skan XO powyżej strefy ostrożnej")
    p.add_argument("--fast-ab", action="store_true", help="v19.61: szybki tryb wyboru kandydatów A/B; pomija dogęszczenie 0,1 ms i ogranicza ciężkie metryki globalne")
    p.add_argument("--reference-xo", default=90, type=int, help="Crossover referencyjny użytkownika pokazywany osobno w raporcie, domyślnie 90 Hz")
    p.add_argument("--reference-filter-type", default="BW", choices=["BW", "LR"], help="Filtr referencyjny użytkownika, domyślnie BW")
    p.add_argument("--reference-slope", default=48, type=int, choices=[24, 48], help="Stromizna referencyjna użytkownika, domyślnie 48")
    p.add_argument("--user-reference", default="USER_REFERENCE_90BW48.yaml", help="v19.70: plik realnej referencji SHD, domyślnie USER_REFERENCE_90BW48.yaml")
    p.add_argument("--no-lock-user-reference", action="store_true", help="Nie blokuj Slotu A z USER_REFERENCE_90BW48.yaml; użyj referencji wygenerowanej z RAW")
    p.add_argument("--reference-neighborhood", action="store_true", help="v19.76: klasyfikuj kandydatów względem realnej referencji SHD; obce topologie nie są blokowane, tylko trafiają do osobnego koszyka A/B")
    p.add_argument("--reference-neighborhood-radius", default=2.0, type=float, help="Tolerancja kanałowa sąsiedztwa realnej referencji w ms dla relacji L/R sub-main; domyślnie ±2 ms na kanał")
    p.add_argument("--global-eval-low", default=55.0, type=float, help="Dolna granica stałego pasma globalnego porównania XO [Hz]")
    p.add_argument("--global-eval-high", default=120.0, type=float, help="Górna granica stałego pasma globalnego porównania XO [Hz]")
    p.add_argument("--global-bulge-low", default=120.0, type=float, help="Dolna granica stałego pasma kontroli bulwy [Hz]")
    p.add_argument("--global-bulge-high", default=180.0, type=float, help="Górna granica stałego pasma kontroli bulwy [Hz]")
    p.add_argument("--workers", default=4, type=int, help="Liczba procesów roboczych dla niezależnych crossoverów")
    return p


def main(argv=None) -> int:
    try:
        if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True, write_through=True, errors="replace")
        if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(line_buffering=True, write_through=True, errors="replace")
    except Exception:
        pass
    mp.freeze_support()
    args = build_parser().parse_args(argv)
    if args.filter_type is not None or args.slope is not None:
        profiles = [(str(args.filter_type or "BW").upper(), int(args.slope or 48))]
    else:
        profiles = parse_filter_profiles(args.profiles)
    try:
        rows = rank_raw_crossover_profiles(
            args.mdat, args.out,
            profiles=profiles,
            crossover_min_hz=args.xo_min, crossover_max_hz=args.xo_max,
            crossover_step_hz=args.xo_step,
            delay_min_ms=args.delay_min, delay_max_ms=args.delay_max,
            shd_delay_limit_ms=args.shd_limit,
            refine_top_n=args.refine_top,
            refine_scene_top_n=args.refine_scene_top,
            music_xo_cap_hz=args.music_xo_cap, caution_xo_cap_hz=args.caution_xo_cap,
            caution_penalty_per_hz=args.caution_penalty_per_hz, global_candidate_pool_n=args.global_candidate_pool,
            min_local_pbis_safe=args.min_local_pbis_safe, min_lr_coherence=args.min_lr_coherence,
            min_lr_sum_margin_xo_db=args.min_lr_sum_margin_xo, max_lr_phase_diff_xo_deg=args.max_lr_phase_diff_xo,
            polarity_scan=bool(args.polarity_scan),
            allow_high_xo=bool(args.allow_high_xo),
            fast_ab=bool(args.fast_ab),
            reference_xo_hz=args.reference_xo,
            reference_filter_type=args.reference_filter_type,
            reference_slope_db_oct=args.reference_slope,
            global_eval_low_hz=args.global_eval_low, global_eval_high_hz=args.global_eval_high,
            global_bulge_low_hz=args.global_bulge_low, global_bulge_high_hz=args.global_bulge_high,
            workers=args.workers,
            user_reference_path=args.user_reference,
            lock_user_reference=not bool(args.no_lock_user_reference),
            reference_neighborhood=bool(args.reference_neighborhood),
            reference_neighborhood_radius_ms=float(args.reference_neighborhood_radius),
            progress=lambda s: print(s, flush=True),
        )
    except Exception as exc:
        print("")
        print("BŁĄD RANKINGU RAW:", exc)
        print("Najpierw uruchom 0_RAW_MEASUREMENT_CHECK.bat i sprawdź, czy plik jest czystym RAW XOVER_OFF DELAY0.")
        return 2
    print("")
    print("Ranking gotowy.")
    print("Porównane profile filtrów: " + ", ".join(f"{ft}{slope}" for ft, slope in profiles))
    print("Tryb polaryzacji: " + ("diagnostyczny ALL" if bool(args.polarity_scan) else "standardowy OFF/OFF"))
    print("Tryb szybkiego A/B: " + ("TAK — kandydaci do pomiaru, bez dogęszczenia 0,1 ms" if bool(args.fast_ab) else "NIE — pełny ranking z dogęszczeniem finalistów"))
    if rows:
        safe_rows = [r for r in rows if bool(r.recommendable)]
        best = max(safe_rows, key=_rank_key) if safe_rows else None
        if best is None:
            print("Brak automatycznej rekomendacji: żaden wariant nie przeszedł lokalnej bramki integracji.")
            boundary_row = rows[0]
        else:
            print("Najlepszy " + ("wariant diagnostyczny" if bool(args.polarity_scan) else "rekomendowalny wariant ogólny") + ":")
            print(
                f"{best.crossover_hz} Hz {best.filter_type}{best.slope_db_oct}: {_shd_values_text(best)}, "
                f"wynik muzyczny rankingu RAW — PREDYKCJA RAW {best.music_score:.1f}, Globalna neutralność — PREDYKCJA RAW {best.global_neutral_safe:.1f}, "
                f"Globalne bezpieczeństwo muzyczne — PREDYKCJA RAW {best.global_pbis_safe:.1f}, Lokalna jakość integracji PBIS — PREDYKCJA RAW {best.pbis_safe:.1f}"
            )
            boundary_row = best
        scene_pool = safe_rows or [r for r in rows if bool(r.local_gate_pass)] or rows
        scene_best = max(scene_pool, key=_scene_rank_key)
        glow_pool = safe_rows or [r for r in rows if bool(r.local_gate_pass)] or rows
        glow_best = max(glow_pool, key=_midrange_glow_rank_key)
        print("")
        print("Kandydat pod średnicę / blask / organiczność:")
        print(
            f"{glow_best.crossover_hz} Hz {glow_best.filter_type}{glow_best.slope_db_oct}: "
            f"{_shd_values_text(glow_best)}, Podparcie dolnej średnicy — PREDYKCJA RAW {glow_best.midrange_glow_score:.1f}, "
            f"100-160 vs 60-100 {glow_best.support_100_160_vs_60_100_db:+.2f} dB"
        )
        print("")
        print("Kandydat do sprawdzenia spójności L/R przy XO:")
        print(
            f"{scene_best.crossover_hz} Hz {scene_best.filter_type}{scene_best.slope_db_oct}: "
            f"{_shd_values_text(scene_best)}, Spójność sum L/R przy XO — PREDYKCJA RAW {scene_best.scene_score:.1f}"
        )
        if int(boundary_row.crossover_hz) in {int(args.xo_min), int(args.xo_max)}:
            print("")
            if bool(args.allow_high_xo) and int(boundary_row.crossover_hz) == int(args.xo_min) and float(args.xo_min) >= float(args.caution_xo_cap):
                print("UWAGA: to skan diagnostyczny. Punkt graniczny 102 Hz łączy go ze standardowym rankingiem; wyniki >102 Hz nie wypierają automatycznie rekomendacji muzycznej.")
            elif int(boundary_row.crossover_hz) == int(args.xo_max) and float(boundary_row.crossover_hz) >= float(args.caution_xo_cap):
                print("UWAGA: zwycięzca jest na górnej granicy strefy ostrożnej. Uruchom osobny skan diagnostyczny powyżej tej granicy.")
            else:
                print("UWAGA: zwycięzca wypada na granicy zakresu. Rozszerz zakres w tym kierunku i uruchom ranking ponownie.")
    print("Otwórz plik RANKING_CROSSOVEROW_RAW.txt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
