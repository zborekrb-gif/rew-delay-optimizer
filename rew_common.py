#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REW Delay Optimizer

Aplikacja do automatycznego wyszukiwania delay/polarity subwooferów
na podstawie eksportów tekstowych REW: frequency / SPL / phase.
"""

from __future__ import annotations

import argparse

from rew_app_metadata import APP_TITLE, APP_VERSION
from rew_app_prefs import load_app_preferences, save_app_preferences

# FAST build: SVG export is disabled by default to speed up startup/build and reduce Matplotlib dependencies.
EXPORT_SVG = False

import csv
import fnmatch
import os
import re
import shutil
import struct
import sys
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np


class CalculationCancelled(Exception):
    """Raised when the user asks the GUI to stop a long calculation."""
    pass


@dataclass
class Measurement:
    name: str
    freq: np.ndarray
    spl_db: np.ndarray
    phase_deg: np.ndarray



@dataclass
class MdatMeasurementInfo:
    index: int
    name: str
    role: str
    position: int
    base_delay_ms: Optional[float] = None
    notes: str = ""


@dataclass
class MdatImportedProject:
    path: str
    measurements_by_role: Dict[str, Measurement]
    role_to_name: Dict[str, str]
    scan: Dict[str, object]
    freq_start_hz: float
    freq_end_hz: float
    freq_step_hz: float
    points: int
    # v20.29 MDAT PHASE TRUTH GUARD.  The numeric importer records how the
    # phase array was selected instead of treating array order as a format
    # contract.  Consumers use ``phase_metrics_allowed`` as a hard gate.
    phase_truth_status: str = "NOT_CHECKED"
    phase_truth_source: str = ""
    phase_truth_diagnostics: Dict[str, object] = field(default_factory=dict)
    phase_metrics_allowed: bool = True


@dataclass
class Candidate:
    l_delay_ms: float
    l_polarity: str
    r_delay_ms: float
    r_polarity: str
    score: float
    l_construct_db: float
    r_construct_db: float
    lr_construct_db: float
    lr_std_db: float
    lr_dip_db: float
    bulge_db: float
    bulge_peak_db: float
    lr_mean_db: float
    # v19.18 PBIS — Perceptual Bass Integration Score, 0–100.
    # v19.22: pbis_score jest zawsze surowym PBIS punktu, bez premii/kary plateau.
    #          score może być rankingowym PBIS+plateau w trybie PBIS.
    pbis_score: float = 0.0
    pbis_score_raw: float = 0.0
    pbis_plateau_bonus: float = 0.0
    pbis_rank_score: float = 0.0
    pbis_label: str = ""
    pbis_hole_risk: str = ""
    pbis_bulge_risk: str = ""
    pbis_phase_risk: str = ""
    pbis_plateau: str = ""
    pbis_symmetry_risk: str = ""
    pbis_risk_summary: str = ""
    # v19.22 — Sound Quality Safe Mode.
    # PBIS Safe to osobna, bardziej ostrożna ocena brzmieniowa 0–100:
    # chroni przed bulwą wyższego basu, asymetrią L/R, słabą fazą i wynikiem punktowym bez plateau.
    pbis_safe_score: float = 0.0
    pbis_safe_label: str = ""
    pbis_safe_penalty: float = 0.0
    naturalness_label: str = ""
    sound_quality_flags: str = ""


@dataclass
class OptimizerConfig:
    delay_min_ms: float = 0.0
    delay_max_ms: float = 30.0
    delay_step_ms: float = 0.1
    eval_low_hz: float = 55.0
    eval_high_hz: float = 115.0
    bulge_low_hz: float = 120.0
    bulge_high_hz: float = 160.0
    top_n: int = 20
    freq_points: int = 1400
    max_cartesian: int = 2_000_000
    prefer_smoothness: float = 1.0
    prefer_lr_sum: float = 1.2
    dip_penalty_weight: float = 1.6
    bulge_penalty_weight: float = 0.7
    bulge_tolerance_db: float = 0.5
    bulge_peak_penalty_weight: float = 0.35
    use_psycho_bulge_model: bool = True
    filter_type: str = "BW"
    slope_db_oct: int = 48
    practical_score_tolerance_db: float = 0.75
    analysis_pool_n: int = 1000
    safe_midrange_priority: bool = True
    scoring_profile: str = "PBIS"
    psycho_freq_weighting: bool = True
    psycho_broad_error_model: bool = True
    psycho_plateau_model: bool = True
    sound_quality_safe: bool = True
    # v19.26 FAST 2-STAGE: finalny krok delay_step_ms zostaje dokładnością wyniku,
    # ale duże skany są najpierw robione zgrubnie i dopiero potem dogęszczane.
    fast_two_stage: bool = True
    fast_coarse_step_ms: float = 0.5
    fast_refine_radius_ms: float = 0.6
    fast_seed_n: int = 80
    fast_exact_threshold: int = 30_000
    # v19.28 RESULT VALIDATOR: krótki lokalny EXACT-check wokół zwycięzcy FAST 2-STAGE.
    fast_validation_enabled: bool = True
    fast_validation_radius_ms: float = 0.6
    fast_validation_close_score: float = 1.0
    fast_validation_margin: float = 0.15
    # v19.29 SMART RESCAN: automatyczna dogrywka, gdy walidator FAST wykryje ryzyko.
    # Bez nowych pól w GUI: mechanizm działa w tle, a szczegóły trafiają do logu i raportów.
    fast_smart_rescan_enabled: bool = True
    fast_smart_rescan_radius_ms: float = 1.5
    fast_smart_rescan_seed_n: int = 8
    fast_smart_rescan_max_combinations: int = 80_000
    # v19.30 PERFORMANCE DIAGNOSTICS: bez zmian w GUI. Aplikacja tylko loguje
    # szacowaną ciężkość skanu i zapisuje raport czasu pracy. Opcjonalny bezpiecznik
    # podnosi krok zgrubny, gdy sam etap coarse byłby niepotrzebnie ogromny.
    fast_auto_coarse_safety: bool = True
    fast_max_coarse_combinations: int = 6_000
    # v19.47 RELATIVE SHD DELAYS: opcjonalny tryb używany przez ranking RAW.
    # delay_min_ms..delay_max_ms opisują wtedy relację sub - main, także ujemną.
    # Każda para relatywnych delayów musi dać się zmapować na cztery wartości
    # 0..shd_delay_limit_ms przy wspólnym delayu L main = R main.
    relative_delay_shd_mode: bool = False
    shd_delay_limit_ms: float = 30.0
    # v19.51 MDAT BASELINE AWARE: fizyczne delaye aktywne podczas wykonania
    # pomiarów wejściowych. Są już zapisane w fazie pliku MDAT. Kandydat
    # optymalizatora opisuje dodatkową zmianę relacji względem tego pomiaru.
    baseline_l_main_ms: float = 0.0
    baseline_r_main_ms: float = 0.0
    baseline_l_sub_ms: Optional[float] = None
    baseline_r_sub_ms: Optional[float] = None
    # v19.58 POLARITY DIAGNOSTIC: domyślnie automatyczny workflow liczy tylko OFF/OFF.
    # Osobny moduł diagnostyczny może włączyć pełny skan rodzin polaryzacji
    # OFF/OFF, INV/OFF, OFF/INV i INV/INV.
    polarity_scan_mode: str = "OFF_ONLY"


# Presety pasm oceny dla różnych crossoverów.
# To są ustawienia startowe dla Twojego typu pracy: sub + monitor, ocena szerokiej sumy,
# a nie maksymalizacja jednego punktu przy częstotliwości podziału.
# Kotwice zachowują dokładnie dotychczasowe pasma oceny.
# v19.49: GUI udostępnia presety co 2 Hz w zakresie 60–120 Hz.
# Dodatkowo pozostają wcześniejsze punkty 65/75/85/95 Hz, żeby starsze
# projekty i typowe ustawienia nie zniknęły z listy.
_CROSSOVER_PRESET_ANCHORS = {
    # Format: eval_low, eval_high, bulge_low, bulge_high
    60:  (45.0, 100.0, 105.0, 150.0),
    65:  (50.0, 105.0, 110.0, 155.0),
    70:  (55.0, 115.0, 115.0, 165.0),
    75:  (60.0, 120.0, 120.0, 170.0),
    80:  (65.0, 125.0, 125.0, 175.0),
    85:  (70.0, 130.0, 130.0, 180.0),
    90:  (75.0, 140.0, 135.0, 190.0),
    95:  (80.0, 145.0, 140.0, 195.0),
    100: (85.0, 150.0, 145.0, 200.0),
    105: (90.0, 155.0, 150.0, 205.0),
    110: (95.0, 160.0, 155.0, 210.0),
    115: (100.0, 165.0, 160.0, 215.0),
    120: (105.0, 170.0, 165.0, 220.0),
}


def _interpolate_crossover_preset(hz: int) -> Tuple[float, float, float, float]:
    """Zwraca pasma oceny dla crossovera, interpolując pomiędzy kotwicami.

    Interpolacja zmienia wyłącznie profil pasm oceny. Nie ingeruje w obliczenia
    delayu, fazy, PBIS ani w dokładność kroku delayu.
    """
    hz = int(hz)
    if hz in _CROSSOVER_PRESET_ANCHORS:
        return _CROSSOVER_PRESET_ANCHORS[hz]
    anchor_hz = sorted(_CROSSOVER_PRESET_ANCHORS)
    if hz <= anchor_hz[0]:
        return _CROSSOVER_PRESET_ANCHORS[anchor_hz[0]]
    if hz >= anchor_hz[-1]:
        return _CROSSOVER_PRESET_ANCHORS[anchor_hz[-1]]
    lower = max(x for x in anchor_hz if x < hz)
    upper = min(x for x in anchor_hz if x > hz)
    ratio = (hz - lower) / float(upper - lower)
    lo_values = _CROSSOVER_PRESET_ANCHORS[lower]
    hi_values = _CROSSOVER_PRESET_ANCHORS[upper]
    return tuple(round(a + ratio * (b - a), 1) for a, b in zip(lo_values, hi_values))  # type: ignore[return-value]


def _build_crossover_presets() -> Dict[str, Optional[Tuple[float, float, float, float]]]:
    # Główna siatka: co 2 Hz. Zachowujemy też stare, często używane presety nieparzyste.
    values_hz = sorted(set(range(60, 121, 2)) | {65, 75, 85, 95, 105, 115})
    presets: Dict[str, Optional[Tuple[float, float, float, float]]] = {
        "Auto": None,
        "Custom": None,
    }
    for hz in values_hz:
        presets[f"{hz} Hz"] = _interpolate_crossover_preset(hz)
    return presets


CROSSOVER_PRESETS = _build_crossover_presets()

FILTER_TYPES = ["BW", "LR", "Other"]
SLOPE_OPTIONS = ["24", "48"]
SCORE_PROFILES = ["Technical", "Balanced", "Psychoacoustic", "PBIS"]

MODE_DESCRIPTIONS = """
OPIS TRYBÓW / PRZYCISKÓW

## Najważniejsza zasada

Automatyczna optymalizacja sprawdza wyłącznie wariant OFF/OFF. To jest wariant,
którego używasz w SHD. Dzięki temu wynik jest prostszy, stabilniejszy i liczony szybciej.

Ręczny kalkulator PBIS pozwala pojedynczo sprawdzić inne ustawienie, jeżeli kiedyś
będzie potrzebna kontrola. Nie wpływa to na automatyczną rekomendację.

## Podstawowy workflow

1. Wybierz projekt `.mdat`.
2. Jeżeli wybierasz plik z pomiarami bez zwrotnicy SHD (RAW), ustaw crossover, który chcesz zasymulować.
3. Kliknij `START: import + optymalizacja`.
4. W tabeli odczytaj pierwszy wiersz `Główny OFF/OFF`.
5. Wpisz pełny zestaw czterech delayów do SHD: L main, R main, L sub i R sub.
6. Zrób pomiar kontrolny w REW.
7. Przy zwykłym porównaniu slotów przejdź do Diraca i POST-DIRAC; PRE-DIRAC rób tylko dla podejrzanych topologii.

## Pomiary bez zwrotnicy DSP (RAW)

Dla pliku RAW aplikacja automatycznie nakłada cyfrowo HPF na kolumny i LPF na suby.
Uwzględnia zarówno amplitudę, jak i fazę filtra. Stały filtr analogowy REL ustawiony
podczas pomiarów bez zwrotnicy DSP pozostaje już zawarty w danych.

## Jak czytać cztery wiersze tabeli

- `Główny OFF/OFF` — pierwszy wariant do wpisania w SHD.
- `Zapasowy OFF/OFF` — bliski wariant do pomiaru A/B.
- `Dodatkowy OFF/OFF` — kolejny wariant kontrolny.
- `Spójność L/R przy XO OFF/OFF` — kandydat do kontroli fazy i balansu L/R w rejonie podziału. Nie jest pomiarem szerokości ani głębokości sceny.

## Przyciski

- `START: import + optymalizacja` — wczytuje `.mdat` i liczy wynik.
- `Optymalizacja tylko` — liczy ponownie po zmianie ustawień.
- `Zatrzymaj` — bezpiecznie przerywa obliczenia.
- `Folder wyników` — otwiera folder z raportami.
- `SHD final` — otwiera najważniejszą instrukcję do SHD.
- `Mapa PBIS (0–100)` — pokazuje surowy PBIS dla relacji `sub − main`; wyżej = lepiej.
- `Mapa jakości / strata` — pokazuje stratę punktów względem głównej rekomendacji; mniej = lepiej.
- `Sumy SPL: L, R i L+R` — pokazuje przewidywane sumy dla głównego wariantu.
- `Zgodność fazy L/R` — pokazuje różnicę fazy sum lewego i prawego kanału; głębokie nulle są pomijane.
- `Opis wykresów` — wyjaśnia znaczenie osi, kolorów i ograniczenia predykcji.
- `Waliduj Dirac ON` — sprawdza gotowy pomiar po Dirac.
- `Analizuj kurtynę Dirac` — pomaga ocenić górny zakres korekcji Dirac.

## Co dzieje się automatycznie w tle

Przy dużym zakresie aplikacja robi szybki skan zgrubny, potem dokładnie sprawdza
najlepsze okolice OFF/OFF. Na końcu kontroluje zwycięzcę lokalnym dokładnym skanem.
Jeżeli wynik wymaga dodatkowej kontroli, wykonuje dogrywkę w jego pobliżu.

## Ważne

Aplikacja wskazuje dobry punkt startowy. Ostatecznie ustawienie trzeba potwierdzić
pomiarem REW i odsłuchem. Dirac uruchamiaj dopiero po tej kontroli.
"""


MAIN_CHARTS_HELP = """JAK CZYTAĆ NAJWAŻNIEJSZE WYKRESY

1. Mapa PBIS (0–100)
Pokazuje surową ocenę PBIS dla pary relacji sub − main. Im wyższa wartość, tym lepszy punkt startowy do pomiaru REW. Gwiazdka oznacza główną rekomendację OFF/OFF.

2. Mapa jakości / strata
Pokazuje, ile punktów traci każdy wariant względem głównej rekomendacji OFF/OFF. Im mniejsza strata, tym bliżej najlepszego wyniku. To nie jest osobny PBIS, lecz mapa różnicy wyniku rankingowego.

3. Sumy SPL: L, R i L+R
Pokazuje przewidywane odpowiedzi po zastosowaniu wybranego wariantu: L main + L sub, R main + R sub oraz oba kanały razem. Jest to predykcja z bieżącego MDAT, a nie nowy pomiar REW.

4. Zgodność fazy L/R
Pokazuje różnicę fazy pomiędzy sumą lewego i prawego kanału po zastosowaniu wybranego wariantu. Punkty w głębokich nullach są pomijane, ponieważ faza jest tam mało wiarygodna.

WAŻNE
Współrzędne map oznaczają relację sub − main, a nie bezpośrednio cztery wartości do wpisania w SHD. Pełny zestaw delayów SHD odczytaj z pierwszego wiersza tabeli. Wykresy służą do wyboru punktu startowego i nie zastępują kontrolnego pomiaru REW."""



def filter_profile_defaults(filter_type: str, slope_db_oct: int) -> Dict[str, float | bool]:
    """
    Profil oceny zależny od typu filtra.

    Dla zwykłego pomiaru filtr jest już zawarty w danych REW, a profil wpływa na scoring.
    Dla pomiaru bazowego RAW v19.44 osobny moduł rew_crossover.py najpierw nakłada
    cyfrowo HPF/LPF SHD, a ten profil nadal określa sposób oceny wyniku.
    """
    ft = (filter_type or "Other").upper()
    slope = int(slope_db_oct)

    # Domyślne neutralne ustawienie
    profile = {
        "bulge_tolerance_db": 0.6,
        "bulge_penalty_weight": 0.7,
        "bulge_peak_penalty_weight": 0.35,
        "prefer_smoothness": 1.0,
        "prefer_lr_sum": 1.2,
        "dip_penalty_weight": 1.6,
        "use_psycho_bulge_model": True,
    }

    if ft == "BW" and slope == 48:
        # BW48 potrafi dawać najwięcej energii/masy w przejściu.
        # Dla Twojego celu: power + kontur bez tłuszczu/kuli.
        profile.update({
            "bulge_tolerance_db": 0.5,
            "bulge_penalty_weight": 0.85,
            "bulge_peak_penalty_weight": 0.40,
            "prefer_smoothness": 1.05,
            "prefer_lr_sum": 1.2,
            "dip_penalty_weight": 1.6,
        })
    elif ft == "BW" and slope == 24:
        # Łagodniejszy filtr = większe nakładanie pasm, większe ryzyko pogrubienia.
        profile.update({
            "bulge_tolerance_db": 0.4,
            "bulge_penalty_weight": 1.05,
            "bulge_peak_penalty_weight": 0.50,
            "prefer_smoothness": 1.15,
            "prefer_lr_sum": 1.15,
            "dip_penalty_weight": 1.55,
        })
    elif ft == "LR" and slope == 48:
        # LR48 zwykle jest bardziej przewidywalny i mniej tłusty niż BW.
        profile.update({
            "bulge_tolerance_db": 0.7,
            "bulge_penalty_weight": 0.60,
            "bulge_peak_penalty_weight": 0.30,
            "prefer_smoothness": 1.0,
            "prefer_lr_sum": 1.25,
            "dip_penalty_weight": 1.65,
        })
    elif ft == "LR" and slope == 24:
        # LR24 ma więcej nakładania niż LR48; ostrożniej z bulwą.
        profile.update({
            "bulge_tolerance_db": 0.6,
            "bulge_penalty_weight": 0.75,
            "bulge_peak_penalty_weight": 0.35,
            "prefer_smoothness": 1.05,
            "prefer_lr_sum": 1.2,
            "dip_penalty_weight": 1.6,
        })

    return profile


def profile_label(crossover_label: str, filter_type: str, slope_db_oct: int) -> str:
    return f"{crossover_label} {filter_type}{int(slope_db_oct)}"


BULGE_STRENGTH_MAP = {"off": 0.0, "low": 0.35, "medium": 0.7, "high": 1.1}


def resolve_bulge_penalty_weight(
    defaults: Dict[str, float | bool],
    strength_label: Optional[str] = None,
    explicit_weight: Optional[float] = None,
) -> float:
    """
    v17.3: profil filtra BW/LR jest źródłem prawdy.
    - jeżeli użytkownik poda wagę liczbową, używamy jej,
    - jeżeli poda stary opis low/medium/high, zachowujemy kompatybilność,
    - w przeciwnym razie używamy dokładnej wartości z filter_profile_defaults().
    """
    if explicit_weight is not None:
        return float(explicit_weight)
    if strength_label:
        key = str(strength_label).strip().lower()
        if key in BULGE_STRENGTH_MAP:
            return float(BULGE_STRENGTH_MAP[key])
    return float(defaults["bulge_penalty_weight"])


def infer_base_delay_from_filename(path: str | Path) -> float:
    """
    Próbuje wyciągnąć bazowy delay suba z nazwy pliku, np.:
    70_L_sub_1.0.txt -> 1.0
    70_R_sub_4ms.txt -> 4.0
    L_sub_delay_5.6.txt -> 5.6

    v19.44: liczba musi wystąpić bezpośrednio po słowie ``sub`` albo po
    jawnym znaczniku ``delay``/``dly``. Dzięki temu nazwa ``L_SUB_RAW_REL120``
    nie jest błędnie odczytywana jako bazowy delay 120 ms.

    Jeżeli nie ma jednoznacznej wartości, zwraca 0.0.
    """
    # Nie używamy Path(...).stem bezwarunkowo, bo nazwa pomiaru REW
    # ``L_sub_5.6`` nie jest plikiem, a końcówka ``.6`` zostałaby potraktowana
    # jako rozszerzenie. Usuwamy wyłącznie znane rozszerzenia plików.
    stem = Path(str(path)).name.lower().replace(",", ".")
    for suffix in (".txt", ".csv", ".mdat", ".wav"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    idx = stem.find("sub")
    if idx < 0:
        return 0.0
    tail = stem[idx + 3 :]
    patterns = [
        r"^[-_\s]*([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:ms)?(?:$|[-_\s])",
        r"(?:^|[-_\s])(?:delay|dly)[-_\s]*([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:ms)?(?:$|[-_\s])",
    ]
    for pattern in patterns:
        m = re.search(pattern, tail)
        if not m:
            continue
        try:
            return float(m.group(1))
        except Exception:
            return 0.0
    return 0.0


def infer_main_delay_from_filename(path: str | Path) -> float:
    """Odczytuje wspólny bazowy delay mainów z nazwy projektu lub pliku.

    Przykłady:
    - ``90_BW48_main30.mdat`` -> 30.0
    - ``90_BW48_main_19.2.mdat`` -> 19.2
    - ``RAW_SHD_XOVER_OFF_REL120_DELAY0.mdat`` -> 0.0

    Funkcja jest celowo ostrożna: liczba musi wystąpić bezpośrednio po
    znaczniku ``main``/``mains`` albo po jawnym ``main_delay``.
    """
    stem = Path(str(path)).name.lower().replace(",", ".")
    for suffix in (".txt", ".csv", ".mdat", ".wav"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    patterns = [
        r"(?:^|[-_\s])mains?(?:[-_\s]*(?:delay|dly))?[-_\s]*([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:ms)?(?:$|[-_\s])",
        r"(?:^|[-_\s])(?:l|r)[-_\s]*main(?:[-_\s]*(?:delay|dly))?[-_\s]*([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:ms)?(?:$|[-_\s])",
    ]
    for pattern in patterns:
        m = re.search(pattern, stem)
        if not m:
            continue
        try:
            return float(m.group(1))
        except Exception:
            return 0.0
    return 0.0
