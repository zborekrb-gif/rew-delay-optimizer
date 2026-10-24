#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ocena przydatności kandydata do korekcji Dirac opartej głównie na tłumieniu.

Moduł nie symuluje końcowego stanu Dirac ON ani ustawienia wyjść subwooferów 0 dB.
Ocena dotyczy wyłącznie surowego stanu projektowego (domyślnie -6 dB): sprawdza,
czy kandydat ma wystarczającą energię i nie tworzy szerokich niedoborów, które
prawdopodobnie wymagałyby podbijania krzywej docelowej.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class CutOnlyMetrics:
    score: float
    safe: bool
    boost_mean_db: float
    boost_p95_db: float
    deficit_share_pct: float
    cut_headroom_db: float
    foundation_margin_db: float
    mass_margin_db: float
    body_margin_db: float
    reason: str


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, value)))


def _safe_mean(values: np.ndarray) -> float:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    return float(np.mean(arr)) if arr.size else float("nan")


def _band(freq: np.ndarray, lo: float, hi: float) -> np.ndarray:
    f = np.asarray(freq, dtype=float)
    return (f >= float(lo)) & (f <= float(hi))


def _target_offset_db(freq: np.ndarray) -> np.ndarray:
    """Docelowy kształt względem 90 BW48, ale bez kopiowania ciężkiego basu.

    90 BW48 chroni przede wszystkim dolną średnicę i korpus. W basie cel jest
    celowo niższy: zachować fundament 40-70 Hz, mocniej odciążyć 70-110 Hz i
    łagodnie wrócić do referencji w 180-220 Hz.
    """
    knots_hz = np.asarray([20, 30, 35, 40, 70, 100, 125, 180, 220, 500], dtype=float)
    offsets = np.asarray([-8.0, -7.0, -5.0, -1.5, -1.5, -2.5, -1.5, -0.5, 0.0, 0.0], dtype=float)
    return np.interp(np.asarray(freq, dtype=float), knots_hz, offsets)



def _smooth_relative(values: np.ndarray, width: int = 51) -> np.ndarray:
    """Łagodne wygładzenie około 1/6 oktawy dla typowej siatki projektanta."""
    arr = np.asarray(values, dtype=float)
    if arr.size < 15:
        return arr.copy()
    n = min(int(width), arr.size // 3)
    if n < 5:
        return arr.copy()
    if n % 2 == 0:
        n += 1
    pad = n // 2
    padded = np.pad(arr, (pad, pad), mode="edge")
    return np.convolve(padded, np.ones(n, dtype=float) / float(n), mode="valid")

def evaluate_cut_only(
    candidate_db: np.ndarray,
    reference_db: np.ndarray,
    freq: np.ndarray,
) -> CutOnlyMetrics:
    """Oceń, czy wynik można kształtować głównie przez tłumienie.

    Kandydat jest normalizowany w 180-220 Hz. Cel ma charakter względny i nie
    kopiuje bezpośrednio pomiaru 90 BW48 wykonanego w innym stanie gainu/Dirac.
    Parametr ``reference_db`` pozostaje dla zgodności interfejsu, lecz nie jest
    używany do wyznaczania kształtu celu basowego.
    """
    f = np.asarray(freq, dtype=float)
    c = np.asarray(candidate_db, dtype=float)
    r = np.asarray(reference_db, dtype=float)
    anchor = _band(f, 180.0, 220.0)
    if np.count_nonzero(anchor) < 6:
        anchor = _band(f, 160.0, 220.0)
    if np.count_nonzero(anchor) < 6:
        return CutOnlyMetrics(0.0, False, 9.0, 12.0, 100.0, 0.0, -9.0, -9.0, -9.0, "za mało danych 160-220 Hz")

    c0 = c - _safe_mean(c[anchor])
    # Wymaganie podbijania ma dotyczyć szerokiego niedoboru, nie wąskich nulli
    # pomieszczenia, których Dirac nie powinien agresywnie wypełniać.
    c_eval = _smooth_relative(c0, width=51)
    # Cel względny względem własnego poziomu 180-220 Hz. Referencja 90 BW48
    # służy osobno do ochrony korpusu/średnicy, a nie do narzucania basu RAW.
    target = _target_offset_db(f)

    assess = _band(f, 40.0, 180.0)
    if np.count_nonzero(assess) < 20:
        return CutOnlyMetrics(0.0, False, 9.0, 12.0, 100.0, 0.0, -9.0, -9.0, -9.0, "za mało danych 40-180 Hz")

    deficit = np.maximum(target[assess] - c_eval[assess], 0.0)
    surplus = np.maximum(c_eval[assess] - target[assess], 0.0)
    boost_mean = _safe_mean(deficit)
    boost_p95 = float(np.nanpercentile(deficit, 95))
    deficit_share = float(100.0 * np.mean(deficit > 1.0))
    cut_headroom = float(np.nanmedian(surplus))

    def margin(lo: float, hi: float) -> float:
        m = _band(f, lo, hi)
        if np.count_nonzero(m) < 4:
            return float("nan")
        return _safe_mean(c_eval[m] - target[m])

    foundation = margin(40.0, 70.0)
    mass = margin(70.0, 120.0)
    body = margin(125.0, 180.0)

    # Skala ma rozróżniać warianty także wtedy, gdy żaden nie spełnia bramki.
    # Sama bramka `safe` pozostaje surowa, ale wynik nie spada automatycznie do zera.
    score = 100.0
    score -= 8.0 * boost_mean
    score -= 4.0 * boost_p95
    score -= 0.12 * deficit_share
    if np.isfinite(foundation) and foundation < -1.0:
        score -= 3.0 * (-1.0 - foundation)
    if np.isfinite(mass) and mass < -1.5:
        score -= 2.5 * (-1.5 - mass)
    if np.isfinite(body) and body < -1.0:
        score -= 3.0 * (-1.0 - body)
    # Niewielki zapas do cięcia jest pożądany, ale nie premiujemy dużego nadmiaru.
    score += min(max(cut_headroom, 0.0), 2.5) * 1.5
    score = _clip(score)

    safe = bool(
        boost_mean <= 0.75
        and boost_p95 <= 5.00
        and deficit_share <= 20.0
        and (not np.isfinite(foundation) or foundation >= -1.0)
        and (not np.isfinite(mass) or mass >= -1.5)
        and (not np.isfinite(body) or body >= -1.0)
    )
    issues = []
    if boost_mean > 0.75:
        issues.append(f"średnia potrzeba podbicia {boost_mean:.2f} dB")
    if boost_p95 > 5.00:
        issues.append(f"P95 szerokiego niedoboru {boost_p95:.2f} dB")
    if deficit_share > 20.0:
        issues.append(f"{deficit_share:.0f}% pasma wymaga >1 dB podbicia")
    if np.isfinite(foundation) and foundation < -1.0:
        issues.append(f"niedobór fundamentu {foundation:.2f} dB")
    if np.isfinite(mass) and mass < -1.5:
        issues.append(f"niedobór masy {mass:.2f} dB")
    if np.isfinite(body) and body < -1.0:
        issues.append(f"niedobór korpusu {body:.2f} dB")
    reason = "możliwa korekcja głównie przez tłumienie" if safe else "; ".join(issues)

    return CutOnlyMetrics(
        score=score,
        safe=safe,
        boost_mean_db=float(boost_mean),
        boost_p95_db=float(boost_p95),
        deficit_share_pct=float(deficit_share),
        cut_headroom_db=float(cut_headroom),
        foundation_margin_db=float(foundation),
        mass_margin_db=float(mass),
        body_margin_db=float(body),
        reason=reason,
    )
