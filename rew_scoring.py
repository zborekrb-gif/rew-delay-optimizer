# -*- coding: utf-8 -*-
"""Matematyka audio, scoring kandydatów, PBIS i PBIS Safe."""
from __future__ import annotations

import heapq
import itertools

from rew_common import *
from rew_mdat import *

def final_delay(base_delay_ms: float, additional_delay_ms: float) -> float:
    return float(base_delay_ms) + float(additional_delay_ms)


def baseline_shd_settings(
    cfg: Optional[OptimizerConfig],
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
) -> Dict[str, float]:
    """Zwraca fizyczne delaye aktywne podczas wykonania pomiarów wejściowych.

    W starszych projektach znane były wyłącznie bazowe delaye subów. v19.51
    dodaje delay obu mainów, bo plik kontrolny MDAT może mieć go już zapisany
    w fazie. Wartości subów z konfiguracji mają pierwszeństwo; gdy ich nie
    ustawiono, zachowujemy kompatybilność z argumentami ``l/r_base_delay_ms``.
    """
    l_main = float(getattr(cfg, "baseline_l_main_ms", 0.0) or 0.0) if cfg is not None else 0.0
    r_main = float(getattr(cfg, "baseline_r_main_ms", 0.0) or 0.0) if cfg is not None else 0.0
    l_sub_cfg = getattr(cfg, "baseline_l_sub_ms", None) if cfg is not None else None
    r_sub_cfg = getattr(cfg, "baseline_r_sub_ms", None) if cfg is not None else None
    l_sub = float(l_base_delay_ms if l_sub_cfg is None else l_sub_cfg)
    r_sub = float(r_base_delay_ms if r_sub_cfg is None else r_sub_cfg)
    return {
        "l_main_ms": l_main, "r_main_ms": r_main,
        "l_sub_ms": l_sub, "r_sub_ms": r_sub,
        "l_sub_minus_main_ms": l_sub - l_main,
        "r_sub_minus_main_ms": r_sub - r_main,
    }


def baseline_relative_delays(
    cfg: Optional[OptimizerConfig],
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
) -> Tuple[float, float]:
    base = baseline_shd_settings(cfg, l_base_delay_ms, r_base_delay_ms)
    return float(base["l_sub_minus_main_ms"]), float(base["r_sub_minus_main_ms"])


def candidate_final_relative_delays(
    candidate: Candidate,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    cfg: Optional[OptimizerConfig] = None,
) -> Tuple[float, float]:
    """Relacja sub-main po uwzględnieniu bazy zapisanej w pomiarze MDAT."""
    base_l_rel, base_r_rel = baseline_relative_delays(cfg, l_base_delay_ms, r_base_delay_ms)
    return (
        base_l_rel + float(getattr(candidate, "l_delay_ms", 0.0)),
        base_r_rel + float(getattr(candidate, "r_delay_ms", 0.0)),
    )


def final_delay_warnings(l_final_ms: float, r_final_ms: float) -> List[str]:
    warnings: List[str] = []
    if l_final_ms < 0:
        warnings.append(f"L FINAL delay wychodzi ujemny ({l_final_ms:.2f} ms). miniDSP zwykle nie przyjmie wartości < 0; podnieś bazowe opóźnienia obu kanałów albo zawęź zakres skanu.")
    if r_final_ms < 0:
        warnings.append(f"R FINAL delay wychodzi ujemny ({r_final_ms:.2f} ms). miniDSP zwykle nie przyjmie wartości < 0; podnieś bazowe opóźnienia obu kanałów albo zawęź zakres skanu.")
    return warnings


def map_relative_delays_to_shd(
    l_sub_minus_main_ms: float,
    r_sub_minus_main_ms: float,
    shd_limit_ms: float = 30.0,
) -> Dict[str, object]:
    """Mapuje relatywne delaye ``sub - main`` na cztery wartości do SHD.

    SHD nie pozwala wpisać ujemnego delayu. Jeżeli relatywnie sub powinien
    przyjść wcześniej niż main, opóźniamy oba kanały main o tę samą minimalną
    wartość. Do wszystkich czterech kanałów dodawany jest ten sam offset, więc
    relacje czasowe i wynik symulacji pozostają bez zmian.

    Wspólny delay L/R main jest celowym ograniczeniem konfiguracji stereo.
    """
    l_rel = float(l_sub_minus_main_ms)
    r_rel = float(r_sub_minus_main_ms)
    limit = float(shd_limit_ms)
    if not np.isfinite(l_rel) or not np.isfinite(r_rel):
        raise ValueError("Relatywny delay sub - main musi być liczbą skończoną.")
    if not np.isfinite(limit) or limit < 0.0:
        raise ValueError("Limit delayu SHD musi być liczbą >= 0.")

    # Najmniejszy wspólny offset, który usuwa wartości ujemne.
    main_common = max(0.0, -l_rel, -r_rel)
    l_main = main_common
    r_main = main_common
    l_sub = main_common + l_rel
    r_sub = main_common + r_rel
    values = [l_main, r_main, l_sub, r_sub]
    eps = 1e-9
    feasible = min(values) >= -eps and max(values) <= limit + eps
    # Wyciszamy wyłącznie śmieci numeryczne typu -0.0000000001.
    values = [0.0 if abs(v) < eps else float(v) for v in values]
    return {
        "feasible": bool(feasible),
        "shd_limit_ms": limit,
        "normalization_offset_ms": float(main_common),
        "l_main_ms": values[0],
        "r_main_ms": values[1],
        "l_sub_ms": values[2],
        "r_sub_ms": values[3],
        "max_used_ms": float(max(values)),
        "l_sub_minus_main_ms": l_rel,
        "r_sub_minus_main_ms": r_rel,
    }


def relative_delay_pair_fits_shd(
    l_sub_minus_main_ms: float,
    r_sub_minus_main_ms: float,
    shd_limit_ms: float = 30.0,
) -> bool:
    """Zwraca True, gdy para relatywnych delayów mieści się w limicie SHD."""
    return bool(map_relative_delays_to_shd(l_sub_minus_main_ms, r_sub_minus_main_ms, shd_limit_ms)["feasible"])


def candidate_shd_settings(candidate: Candidate, l_base_delay_ms: float, r_base_delay_ms: float, cfg: Optional[OptimizerConfig] = None) -> Dict[str, object]:
    """Zwraca fizyczne wartości do wpisania na czterech wyjściach SHD.

    W trybie relatywnym kandydat jest *dodatkową zmianą* względem pomiaru
    wejściowego. Jeśli MDAT wykonano np. przy main L/R=30 ms i subach 0 ms,
    faza tej bazy jest już w pliku. Dlatego przed mapowaniem dodajemy relację
    bazową sub-main, zamiast traktować kandydata jako ustawienie absolutne.
    """
    relative = bool(getattr(cfg, "relative_delay_shd_mode", False)) if cfg is not None else False
    limit = float(getattr(cfg, "shd_delay_limit_ms", 30.0)) if cfg is not None else 30.0
    base = baseline_shd_settings(cfg, l_base_delay_ms, r_base_delay_ms)
    if relative:
        l_rel, r_rel = candidate_final_relative_delays(candidate, l_base_delay_ms, r_base_delay_ms, cfg)
        mapped = map_relative_delays_to_shd(l_rel, r_rel, limit)
        mapped.update({
            "mode": "relative-baseline-aware",
            "baseline_l_main_ms": float(base["l_main_ms"]),
            "baseline_r_main_ms": float(base["r_main_ms"]),
            "baseline_l_sub_ms": float(base["l_sub_ms"]),
            "baseline_r_sub_ms": float(base["r_sub_ms"]),
            "baseline_l_sub_minus_main_ms": float(base["l_sub_minus_main_ms"]),
            "baseline_r_sub_minus_main_ms": float(base["r_sub_minus_main_ms"]),
            "l_additional_vs_file_ms": float(getattr(candidate, "l_delay_ms", 0.0)),
            "r_additional_vs_file_ms": float(getattr(candidate, "r_delay_ms", 0.0)),
        })
        return mapped
    l_sub = final_delay(float(base["l_sub_ms"]), float(getattr(candidate, "l_delay_ms", 0.0)))
    r_sub = final_delay(float(base["r_sub_ms"]), float(getattr(candidate, "r_delay_ms", 0.0)))
    values = [float(base["l_main_ms"]), float(base["r_main_ms"]), l_sub, r_sub]
    return {
        "feasible": min(values) >= -1e-9 and max(values) <= limit + 1e-9,
        "shd_limit_ms": limit, "normalization_offset_ms": 0.0,
        "l_main_ms": values[0], "r_main_ms": values[1], "l_sub_ms": l_sub, "r_sub_ms": r_sub,
        "max_used_ms": max(values), "l_sub_minus_main_ms": l_sub - values[0], "r_sub_minus_main_ms": r_sub - values[1],
        "baseline_l_main_ms": float(base["l_main_ms"]), "baseline_r_main_ms": float(base["r_main_ms"]),
        "baseline_l_sub_ms": float(base["l_sub_ms"]), "baseline_r_sub_ms": float(base["r_sub_ms"]),
        "l_additional_vs_file_ms": float(getattr(candidate, "l_delay_ms", 0.0)),
        "r_additional_vs_file_ms": float(getattr(candidate, "r_delay_ms", 0.0)),
        "mode": "standard-baseline-aware",
    }


def shd_settings_lines(candidate: Candidate, l_base_delay_ms: float, r_base_delay_ms: float, cfg: Optional[OptimizerConfig] = None, indent: str = "  ") -> List[str]:
    """Czytelne, zawsze spójne cztery wartości SHD oraz relacja sub-main."""
    m = candidate_shd_settings(candidate, l_base_delay_ms, r_base_delay_ms, cfg)
    return [
        f"{indent}L main: {float(m['l_main_ms']):.2f} ms",
        f"{indent}R main: {float(m['r_main_ms']):.2f} ms",
        f"{indent}L sub: {float(m['l_sub_ms']):.2f} ms, polarity {getattr(candidate, 'l_polarity', 'OFF')}",
        f"{indent}R sub: {float(m['r_sub_ms']):.2f} ms, polarity {getattr(candidate, 'r_polarity', 'OFF')}",
        f"{indent}Relacja sub-main: L {float(m['l_sub_minus_main_ms']):+.2f} ms, R {float(m['r_sub_minus_main_ms']):+.2f} ms",
    ]


def evaluate_manual_candidate_shd(
    l_main_m: Measurement, l_sub_m: Measurement, r_main_m: Measurement, r_sub_m: Measurement,
    cfg: OptimizerConfig, *, l_main_final_ms: float, r_main_final_ms: float,
    l_sub_final_ms: float, r_sub_final_ms: float, l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0, l_polarity: str = "OFF", r_polarity: str = "OFF",
):
    """PBIS dla pełnego zestawu czterech fizycznych wartości SHD.

    v19.51: gdy wejściowy MDAT został pomierzony z aktywnymi delayami, nie
    wolno ponownie nakładać całej relacji docelowej. Nakładamy tylko różnicę
    pomiędzy relacją docelową i relacją bazową już zapisaną w fazie MDAT.
    """
    if abs(float(l_main_final_ms) - float(r_main_final_ms)) > 0.011:
        raise ValueError("W tym workflow delay L main i R main musi mieć tę samą wartość.")
    vals = [float(l_main_final_ms), float(r_main_final_ms), float(l_sub_final_ms), float(r_sub_final_ms)]
    limit = float(getattr(cfg, "shd_delay_limit_ms", 30.0))
    if min(vals) < -1e-9 or max(vals) > limit + 1e-9:
        raise ValueError(f"Każdy delay SHD musi mieścić się w zakresie 0..{limit:.2f} ms.")
    target_l_rel = float(l_sub_final_ms) - float(l_main_final_ms)
    target_r_rel = float(r_sub_final_ms) - float(r_main_final_ms)
    base_l_rel, base_r_rel = baseline_relative_delays(cfg, l_base_delay_ms, r_base_delay_ms)
    l_additional = target_l_rel - base_l_rel
    r_additional = target_r_rel - base_r_rel
    freq, responses, cand, metrics = evaluate_manual_candidate(
        l_main_m, l_sub_m, r_main_m, r_sub_m, cfg,
        l_final_delay_ms=l_additional, r_final_delay_ms=r_additional,
        l_base_delay_ms=0.0, r_base_delay_ms=0.0,
        l_polarity=l_polarity, r_polarity=r_polarity,
    )
    metrics["manual_target_l_sub_minus_main_ms"] = float(target_l_rel)
    metrics["manual_target_r_sub_minus_main_ms"] = float(target_r_rel)
    metrics["manual_baseline_l_sub_minus_main_ms"] = float(base_l_rel)
    metrics["manual_baseline_r_sub_minus_main_ms"] = float(base_r_rel)
    metrics["manual_applied_l_additional_vs_file_ms"] = float(l_additional)
    metrics["manual_applied_r_additional_vs_file_ms"] = float(r_additional)
    return freq, responses, cand, metrics


def parse_crossover_hz(crossover_label: str, cfg: Optional[OptimizerConfig] = None) -> float:
    """
    Zwraca częstotliwość crossovera używaną do oceny sensowności delayu.
    Najpierw czyta etykietę typu "75 Hz"; dla Custom robi ostrożne oszacowanie.
    """
    m = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*Hz", str(crossover_label), re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "."))
    if cfg is not None:
        # Dla naszych presetów eval_high to zwykle ok. crossover + 45 Hz.
        approx = float(cfg.eval_high_hz) - 45.0
        if 40.0 <= approx <= 140.0:
            return approx
        return max(40.0, min(140.0, float((cfg.eval_low_hz + cfg.eval_high_hz) / 2.0)))
    return 75.0


def delay_period_ms(crossover_hz: float) -> float:
    return 1000.0 / max(1.0, float(crossover_hz))


def delay_safety_thresholds(crossover_hz: float) -> Dict[str, float]:
    period = delay_period_ms(crossover_hz)
    return {
        "period_ms": period,
        "delay_soft_ms": 1.25 * period,
        "delay_hard_ms": 1.80 * period,
        "lr_diff_soft_ms": 0.75 * period,
        "lr_diff_hard_ms": 1.25 * period,
    }


def physical_delay_safety(
    candidate: Candidate,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_hz: float,
    cfg: Optional[OptimizerConfig] = None,
) -> Dict[str, object]:
    """Informacyjna ocena długości relacji fazowo-czasowej przy crossoverze.

    v19.51: w trybie relatywnym kandydat opisuje zmianę względem MDAT. Do
    diagnostyki należy używać końcowej relacji ``sub-main``, a nie samej
    zmiany wobec pliku. Wspólny offset dodany do czterech wyjść SHD nie ma
    wpływu na fazę i nie powinien powodować fałszywych ostrzeżeń.
    """
    relative = bool(getattr(cfg, "relative_delay_shd_mode", False)) if cfg is not None else False
    if relative:
        l_rel, r_rel = candidate_final_relative_delays(candidate, l_base_delay_ms, r_base_delay_ms, cfg)
        l_value = abs(float(l_rel))
        r_value = abs(float(r_rel))
        diff = abs(float(l_rel) - float(r_rel))
        value_label = "|rel. sub-main|"
    else:
        l_rel = final_delay(l_base_delay_ms, candidate.l_delay_ms)
        r_rel = final_delay(r_base_delay_ms, candidate.r_delay_ms)
        l_value = float(l_rel)
        r_value = float(r_rel)
        diff = abs(l_value - r_value)
        value_label = "FINAL delay suba"

    th = delay_safety_thresholds(crossover_hz)
    period = float(th["period_ms"])
    flags: List[str] = []
    severity = 0

    if not relative:
        if l_value < 0:
            severity = max(severity, 3)
            flags.append(f"L FINAL delay ujemny: {l_value:.2f} ms — to jest błąd konfiguracji miniDSP.")
        if r_value < 0:
            severity = max(severity, 3)
            flags.append(f"R FINAL delay ujemny: {r_value:.2f} ms — to jest błąd konfiguracji miniDSP.")

    # Informacje cykliczne. Nie podnosimy ich do czerwonego błędu.
    for side, val in [("L", l_value), ("R", r_value)]:
        cycles = val / period if period > 0 else float("nan")
        if val > th["delay_hard_ms"]:
            severity = max(severity, 1)
            flags.append(
                f"{side} {value_label} {val:.2f} ms = {cycles:.2f} okresu przy {crossover_hz:.0f} Hz. "
                "To może być poprawne zawinięcie fazy; nie odrzucaj wyniku tylko z powodu długości relacji."
            )
        elif val > th["delay_soft_ms"]:
            severity = max(severity, 1)
            flags.append(
                f"{side} {value_label} {val:.2f} ms = {cycles:.2f} okresu przy {crossover_hz:.0f} Hz. "
                "Warto sprawdzić stabilność predykcji i realny pomiar."
            )

    diff_cycles = diff / period if period > 0 else float("nan")
    if diff > th["lr_diff_hard_ms"]:
        severity = max(severity, 1)
        flags.append(
            f"Różnica L/R relacji {diff:.2f} ms = {diff_cycles:.2f} okresu przy {crossover_hz:.0f} Hz. "
            "To jest informacja o asymetrii fazowej, nie automatyczna wada."
        )
    elif diff > th["lr_diff_soft_ms"]:
        severity = max(severity, 1)
        flags.append(
            f"Różnica L/R relacji {diff:.2f} ms = {diff_cycles:.2f} okresu przy {crossover_hz:.0f} Hz. "
            "Porównaj L+R oraz walidację predykcja-vs-pomiar."
        )

    if severity >= 3:
        status = "BŁĄD DELAYU"
    elif severity >= 1:
        status = "PHASE-CYCLE"
    else:
        status = "OK"

    burden = max(l_value, r_value) + 0.50 * diff + 0.15 * (l_value + r_value)
    return {
        "status": status,
        "severity": severity,
        "flags": flags,
        "period_ms": period,
        # Zachowane nazwy dla kompatybilności raportów. W trybie relatywnym
        # są to moduły końcowej relacji sub-main.
        "l_final_ms": l_value,
        "r_final_ms": r_value,
        "l_relative_sub_minus_main_ms": float(l_rel),
        "r_relative_sub_minus_main_ms": float(r_rel),
        "lr_diff_ms": diff,
        "l_cycles_at_xo": l_value / period if period > 0 else float("nan"),
        "r_cycles_at_xo": r_value / period if period > 0 else float("nan"),
        "lr_diff_cycles_at_xo": diff / period if period > 0 else float("nan"),
        "delay_burden": burden,
    }


def select_practical_candidate(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
) -> Tuple[Optional[Candidate], str]:
    """
    v18.4: wybiera neutralną alternatywę A/B z tego samego plateau score,
    ale WYŁĄCZNIE w tej samej rodzinie polaryzacji co best technical.
    MIXED polarity nie może wskoczyć jako "praktyczna" alternatywa dla OFF/OFF.
    """
    if not candidates:
        return None, "Brak kandydatów."

    best, policy, policy_note = off_off_priority_main_candidate(candidates)
    best_family = polarity_family(best)
    xo = parse_crossover_hz(crossover_label, cfg)
    tolerance = effective_score_tolerance(cfg)
    eligible = [
        c for c in candidates
        if polarity_family(c) == best_family and (best.score - c.score) <= tolerance + 1e-9
    ]
    if not eligible:
        eligible = [best]

    def sort_key(c: Candidate):
        s = physical_delay_safety(c, l_base_delay_ms, r_base_delay_ms, xo, cfg)
        return (
            int(s["severity"]),
            float(s["delay_burden"]),
            best.score - c.score,
            max(float(s["l_final_ms"]), float(s["r_final_ms"])),
            abs(float(s["l_final_ms"]) - float(s["r_final_ms"])),
        )

    plateau_alt = min(eligible, key=sort_key)
    loss = best.score - plateau_alt.score
    safety = physical_delay_safety(plateau_alt, l_base_delay_ms, r_base_delay_ms, xo, cfg)
    if same_candidate(plateau_alt, best):
        reason = (
            f"Kandydat operacyjny {best_family} jest też jedyną sensowną alternatywą A/B w tej samej rodzinie polaryzacji "
            f"i zadanej tolerancji. Status delayu: {safety['status']}. {policy_note}"
        )
    else:
        reason = (
            f"Alternatywa A/B z tego samego plateau i tej samej rodziny polaryzacji ({best_family}); "
            f"nie zastępuje kandydata operacyjnego. Strata względem kandydata operacyjnego: {loss:.2f} "
            f"przy tolerancji {tolerance:.2f}. {policy_note}"
        )
    return plateau_alt, reason




def simple_verdict_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
    filter_type: str = "BW",
    slope_db_oct: int = 48,
) -> str:
    """v19.44: prosty werdykt dla użytkownika — tylko główny wariant OFF/OFF."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    if best is None:
        return "BRAK WYNIKU\n\n" + note
    safe = float(getattr(best, "pbis_safe_score", getattr(best, "pbis_score", 0.0)))
    if safe >= 85:
        quality = "bardzo dobra"
    elif safe >= 75:
        quality = "dobra"
    elif safe >= 65:
        quality = "średnia — sprawdź dokładnie pomiarem"
    else:
        quality = "słaba — zachowaj ostrożność"
    return "\n".join([
        "PROSTY WYNIK",
        "",
        *shd_settings_lines(best, l_base_delay_ms, r_base_delay_ms, cfg, indent=""),
        f"Ocena: {quality}",
        "",
        "Następny krok: wpisz wartości do SHD i zrób pomiar kontrolny w REW.",
        "PRE-DIRAC jest opcjonalny; decyzję podejmuj po POST-DIRAC i odsłuchu.",
    ])



def practical_recommendation_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
    filter_type: str = "BW",
    slope_db_oct: int = 48,
) -> str:
    """v19.44: czytelna rekomendacja OFF/OFF, alternatywa plateau i kandydat przestrzenny."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    if best is None:
        return note
    alt, alt_reason = select_practical_candidate(
        candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg
    )
    lines = [
        f"REKOMENDACJA OFF/OFF DO WPISANIA W SHD ({profile_label(crossover_label, filter_type, int(slope_db_oct))}):",
        *shd_settings_lines(best, l_base_delay_ms, r_base_delay_ms, cfg, indent="  "),
        f"  PBIS: {float(getattr(best, 'pbis_score', 0.0)):.1f}/100 — {getattr(best, 'pbis_label', '')}",
        "  Co może wymagać uwagi:",
        pbis_risk_user_block(best, indent="    "),
    ]
    if alt is not None and not same_candidate(alt, best):
        loss = float(best.score) - float(alt.score)
        lines.extend([
            "",
            "ALTERNATYWA A/B — PODOBNE USTAWIENIE DO PORÓWNANIA ODSŁUCHOWEGO:",
            *shd_settings_lines(alt, l_base_delay_ms, r_base_delay_ms, cfg, indent="  "),
            f"  Różnica względem rekomendacji głównej: -{loss:.2f} pkt.",
            "  Po co sprawdzić: to ustawienie leży w podobnym stabilnym obszarze. Może zabrzmieć równie dobrze albo naturalniej mimo nieco niższej oceny.",
        ])
    else:
        lines.extend([
            "",
            "ALTERNATYWA A/B:",
            "  Nie znaleziono innego równie sensownego ustawienia OFF/OFF w pobliżu rekomendacji głównej.",
        ])
    lines.extend(["", spatial_coherence_candidate_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg)])
    return "\n".join(lines)


def do_not_chase_warning_text(cfg: OptimizerConfig) -> str:
    # v18.2: u Roberta często problematyczny jest zakres ok. 135–240 Hz.
    return (
        "Zakres 135–240 Hz traktuj jako strefę ostrzegawczą. "
        "Jeżeli pojawia się tam dołek/kasowanie, aplikacja ma to raportować, "
        "ale nie powinna dobierać delayu subów wyłącznie po to, żeby naprawiać ten obszar."
    )


def db(x: np.ndarray, floor: float = 1e-12) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(x), floor))


def wrap_phase_deg(phase: np.ndarray) -> np.ndarray:
    return (phase + 180.0) % 360.0 - 180.0


def measurement_to_complex(m: Measurement, freq: np.ndarray) -> np.ndarray:
    order = np.argsort(m.freq)
    f = m.freq[order]
    spl = m.spl_db[order]
    phase_rad = np.unwrap(np.deg2rad(m.phase_deg[order]))
    spl_i = np.interp(freq, f, spl)
    phase_i = np.interp(freq, f, phase_rad)
    mag_i = 10.0 ** (spl_i / 20.0)
    return mag_i * np.exp(1j * phase_i)


def common_grid(measurements: List[Measurement], points: int) -> np.ndarray:
    f_min = max(float(np.nanmin(m.freq)) for m in measurements)
    f_max = min(float(np.nanmax(m.freq)) for m in measurements)
    if f_min <= 0 or f_max <= f_min:
        raise ValueError("Nieprawidłowy wspólny zakres częstotliwości.")
    # v19.44: nie nadpróbkowujemy ponad liczbę rzeczywistych punktów wejścia.
    # Interpolowanie np. 629 punktów .mdat do 1400 nie dodaje informacji, a każdą
    # kombinację delayu liczy ponad dwa razy wolniej. Zachowujemy jednak minimum
    # potrzebne do stabilnej analizy pasma.
    available = min(max(2, int(len(m.freq))) for m in measurements)
    use_points = max(32, min(max(32, int(points)), available))
    return np.geomspace(f_min, f_max, use_points)


def apply_delay_and_polarity(
    response: np.ndarray,
    freq: np.ndarray,
    delay_ms: float,
    inverted: bool,
) -> np.ndarray:
    """
    Dodatni delay oznacza późniejsze przyjście suba.
    H_delayed = H * exp(-j*2*pi*f*tau)
    Invert/polarity = mnożenie przez -1.
    """
    tau = delay_ms / 1000.0
    shifted = response * np.exp(-1j * 2.0 * np.pi * freq * tau)
    if inverted:
        shifted = -shifted
    return shifted


_NUM_RE = re.compile(r"[-+]?(?:\d+(?:[\.,]\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def parse_rew_text(path: str | Path, name: Optional[str] = None) -> Measurement:
    """
    Parser eksportu tekstowego REW.
    Pobiera pierwsze trzy kolumny numeryczne: frequency, SPL, phase.
    Ignoruje nagłówki i linie komentarzy.
    """
    path = Path(path)
    freqs: List[float] = []
    spls: List[float] = []
    phases: List[float] = []

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            low = raw.lower()
            if low.startswith("*") or low.startswith("#"):
                continue
            if "freq" in low and ("spl" in low or "phase" in low):
                continue

            nums = _NUM_RE.findall(raw)
            if len(nums) < 3:
                continue
            try:
                fr, sp, ph = [float(x.replace(",", ".")) for x in nums[:3]]
            except ValueError:
                continue

            if not (1.0 <= fr <= 50000.0):
                continue
            if not (-200.0 <= sp <= 200.0):
                continue
            if not (-10000.0 <= ph <= 10000.0):
                continue

            freqs.append(fr)
            spls.append(sp)
            phases.append(ph)

    if len(freqs) < 20:
        raise ValueError(f"Nie udało się odczytać danych REW z pliku: {path}")

    f = np.asarray(freqs, dtype=float)
    spl = np.asarray(spls, dtype=float)
    phase = np.asarray(phases, dtype=float)

    order = np.argsort(f)
    f, spl, phase = f[order], spl[order], phase[order]
    unique_f, unique_idx = np.unique(f, return_index=True)


    return Measurement(name or path.stem, unique_f, spl[unique_idx], phase[unique_idx])

def band_mask(freq: np.ndarray, low: float, high: float) -> np.ndarray:
    return (freq >= low) & (freq <= high)


def mean_constructive_margin(sum_resp: np.ndarray, a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    sum_db = db(sum_resp)[mask]
    max_comp_db = np.maximum(db(a)[mask], db(b)[mask])
    return float(np.nanmean(sum_db - max_comp_db))


def dip_depth(values_db: np.ndarray) -> float:
    """
    Robust dip metric used for scoring.

    v17.2 used median - absolute minimum. That made one very narrow REW point
    dominate the ranking. v17.3 uses the 5th percentile instead, so the score
    still reacts to real broad cancellations but does not chase a single-bin
    measurement artefact. For very short arrays we keep the older conservative
    behaviour.
    """
    values = np.asarray(values_db, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return 0.0
    med = float(np.nanmedian(values))
    if values.size < 20:
        low = float(np.nanmin(values))
    else:
        low = float(np.nanpercentile(values, 5))
    return max(0.0, med - low)


def roughness(values_db: np.ndarray) -> float:
    if values_db.size < 5:
        return float(np.nanstd(values_db))
    x = np.linspace(-1.0, 1.0, values_db.size)
    coeff = np.polyfit(x, values_db, 1)
    trend = np.polyval(coeff, x)
    return float(np.nanstd(values_db - trend))


def normalize_scoring_profile(profile: str) -> str:
    """Normalizuje nazwę profilu scoringu.

    Technical = możliwie blisko starego scoringu.
    Balanced = lekki model słyszalności.
    Psychoacoustic = pełny zestaw v19.17.
    PBIS = v19.18 Perceptual Bass Integration Score 0–100, domyślny tryb roboczy.
    """
    p = str(profile or "PBIS").strip().lower()
    if p in {"technical", "techniczny", "old", "legacy"}:
        return "Technical"
    if p in {"balanced", "balance", "zbalansowany"}:
        return "Balanced"
    if p in {"psychoacoustic", "psycho", "psychoakustyczny"}:
        return "Psychoacoustic"
    if p in {"pbis", "bass", "perceptual", "bass integration"}:
        return "PBIS"
    return "PBIS"


def effective_score_tolerance(cfg: OptimizerConfig) -> float:
    """Tolerancja plateau w jednostkach aktualnego scoringu.

    Starsze score używało małych różnic, PBIS używa punktów 0–100,
    więc domyślne 0.75 byłoby zbyt restrykcyjne.
    """
    base = float(getattr(cfg, "practical_score_tolerance_db", 0.75))
    if normalize_scoring_profile(getattr(cfg, "scoring_profile", "PBIS")) == "PBIS":
        return max(base, 3.0)
    return base


def weighted_percentile(values: np.ndarray, weights: np.ndarray, percentile: float) -> float:
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not np.any(mask):
        return float(np.nanpercentile(values, percentile))
    v = values[mask]
    w = weights[mask]
    order = np.argsort(v)
    v = v[order]
    w = w[order]
    cdf = np.cumsum(w)
    total = float(cdf[-1])
    if total <= 0:
        return float(np.nanpercentile(v, percentile))
    target = total * float(percentile) / 100.0
    return float(np.interp(target, cdf, v))


def weighted_roughness(values_db: np.ndarray, weights: np.ndarray) -> float:
    values = np.asarray(values_db, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if np.count_nonzero(mask) < 5:
        return roughness(values)
    y = values[mask]
    w = weights[mask]
    x = np.linspace(-1.0, 1.0, values.size)[mask]
    try:
        # Zamknięta postać ważonej regresji liniowej jest równoważna polyfit
        # dla prostej, ale znacznie lżejsza w pętli tysięcy kandydatów.
        sw = float(np.sum(w))
        sx = float(np.sum(w * x))
        sy = float(np.sum(w * y))
        sxx = float(np.sum(w * x * x))
        sxy = float(np.sum(w * x * y))
        denom = sw * sxx - sx * sx
        slope = 0.0 if abs(denom) < 1e-12 else (sw * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / max(sw, 1e-12)
        resid = y - (intercept + slope * x)
        return float(np.sqrt(np.sum(w * resid * resid) / max(sw, 1e-12)))
    except Exception:
        return roughness(values)


def psycho_frequency_weights(freq_eval: np.ndarray, cfg: OptimizerConfig) -> np.ndarray:
    """Wagi częstotliwości dla basu/sub-main.

    Model nie udaje pełnej psychoakustyki ISO. To praktyczne ważenie pasma,
    w którym różnice delay/fazy są najczęściej czytelne odsłuchowo:
    50–80 Hz = fundament/kick, 80–120 Hz = integracja i punktowość,
    120–170 Hz = kontrola ryzyka bulwy.
    """
    f = np.asarray(freq_eval, dtype=float)
    if f.size == 0:
        return np.ones_like(f)
    profile = normalize_scoring_profile(getattr(cfg, "scoring_profile", "PBIS"))
    if profile == "Technical" or not bool(getattr(cfg, "psycho_freq_weighting", True)):
        return np.ones_like(f)

    strength = 0.55 if profile == "Balanced" else 1.0
    # Łagodne "garby" wag. Nie ustawiamy ostrych progów, żeby scoring nie skakał.
    w = np.ones_like(f, dtype=float)
    w += strength * 0.30 * np.exp(-0.5 * ((f - 65.0) / 18.0) ** 2)
    w += strength * 0.55 * np.exp(-0.5 * ((f - 92.0) / 24.0) ** 2)
    w += strength * 0.20 * np.exp(-0.5 * ((f - 115.0) / 26.0) ** 2)

    # Bardzo niski bas ma mniejszą wagę dla samej decyzji delay/polaryzacji,
    # bo wąskie mody pomieszczenia łatwo dominują matematykę, a nie integrację.
    w *= np.where(f < 42.0, 0.70, 1.0)
    # Powyżej typowego obszaru integracji waga maleje; bulwa ma osobną karę.
    w *= np.where(f > 145.0, 0.85, 1.0)

    mean = float(np.nanmean(w))
    if not np.isfinite(mean) or mean <= 0:
        return np.ones_like(f)
    return w / mean


def weighted_dip_depth(values_db: np.ndarray, weights: np.ndarray) -> float:
    """Szerzej rozumiana głębokość dołka: mediana ważona - 10 percentyl ważony.

    10 percentyl jest celowo łagodniejszy niż absolutne minimum. Dzięki temu
    bardzo wąski notch nie wywraca rankingu, ale szerokie osłabienie nadal boli.
    """
    values = np.asarray(values_db, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if np.count_nonzero(mask) < 5:
        return dip_depth(values)
    p50 = weighted_percentile(values[mask], weights[mask], 50.0)
    p10 = weighted_percentile(values[mask], weights[mask], 10.0)
    return max(0.0, p50 - p10)


def _odd_window_from_hz(freq_eval: np.ndarray, target_hz: float) -> int:
    f = np.asarray(freq_eval, dtype=float)
    if f.size < 5:
        return 3
    diffs = np.diff(f)
    df = float(np.nanmedian(diffs[np.isfinite(diffs)])) if np.any(np.isfinite(diffs)) else 1.0
    if not np.isfinite(df) or df <= 0:
        df = 1.0
    n = int(round(float(target_hz) / df))
    n = max(3, min(n, max(3, f.size // 2)))
    if n % 2 == 0:
        n += 1
    return n


def moving_average_reflect(values: np.ndarray, window: int) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    if v.size == 0 or window <= 1:
        return v.copy()
    window = int(max(3, window))
    if window % 2 == 0:
        window += 1
    pad = window // 2
    kernel = np.ones(window, dtype=float) / float(window)
    padded = np.pad(v, pad, mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def broad_error_metrics(freq_eval: np.ndarray, values_db: np.ndarray, weights: np.ndarray) -> Tuple[float, float, float]:
    """Zwraca: szeroka dziura, szeroka bulwa, wąski notch.

    - szeroka dziura/bulwa: liczona po wygładzeniu kilku-kilkunastu Hz,
    - wąski notch: różnica między surowym ostrym dołkiem i szerokim dołkiem.
    """
    f = np.asarray(freq_eval, dtype=float)
    v = np.asarray(values_db, dtype=float)
    w = np.asarray(weights, dtype=float)
    mask = np.isfinite(f) & np.isfinite(v) & np.isfinite(w) & (w > 0)
    if np.count_nonzero(mask) < 5:
        return 0.0, 0.0, 0.0
    f = f[mask]
    v = v[mask]
    w = w[mask]
    ref = weighted_percentile(v, w, 50.0)
    neg = np.maximum(0.0, ref - v)
    pos = np.maximum(0.0, v - ref)

    band_width = max(float(np.nanmax(f) - np.nanmin(f)), 1.0)
    # Około 1/6–1/5 oktawy w praktycznym paśmie integracji; minimalnie 5 Hz.
    window_hz = max(5.0, min(14.0, 0.13 * band_width))
    win = _odd_window_from_hz(f, window_hz)
    neg_s = moving_average_reflect(neg, win)
    pos_s = moving_average_reflect(pos, win)

    broad_dip = float(np.sum(neg_s * w) / max(np.sum(w), 1e-12))
    broad_bulge = float(np.sum(pos_s * w) / max(np.sum(w), 1e-12))
    raw_notch = max(0.0, weighted_percentile(neg, w, 95.0) - weighted_percentile(neg_s, w, 80.0))
    return broad_dip, broad_bulge, float(raw_notch)


def weighted_positive_excess(
    freq: np.ndarray,
    values_db: np.ndarray,
    mask: np.ndarray,
    reference_db: float,
    tolerance_db: float,
) -> Tuple[float, float]:
    """
    Psychoakustyczna kara za "bulwę".

    Nie karzemy całego pasma bulwy za to, że istnieje. Karzemy tylko dodatni nadmiar
    energii ponad odniesienie z pasma oceny + tolerancja. Waga ma kształt łagodnego
    dzwonu, więc środek pasma bulwy jest ważniejszy niż skraje.
    """
    if not np.any(mask):
        return 0.0, 0.0

    f = freq[mask]
    v = values_db[mask]

    low = float(np.nanmin(f))
    high = float(np.nanmax(f))
    center = 0.5 * (low + high)
    width = max(high - low, 1.0)
    sigma = max(width / 4.0, 1.0)
    weights = np.exp(-0.5 * ((f - center) / sigma) ** 2)
    weights = weights / max(float(np.nanmax(weights)), 1e-12)

    excess = np.maximum(0.0, v - reference_db - tolerance_db)

    if np.nansum(weights) <= 0:
        mean_excess = float(np.nanmean(excess))
    else:
        mean_excess = float(np.nansum(excess * weights) / np.nansum(weights))

    # Peak penalty: nie używamy brutalnego maksimum, tylko 90 percentyl,
    # żeby pojedynczy pik/artefakt nie dominował całego wyniku.
    peak_excess = float(np.nanpercentile(excess, 90))
    return mean_excess, peak_excess



def _clip_float(value: float, low: float, high: float) -> float:
    try:
        v = float(value)
    except Exception:
        return float(low)
    if not np.isfinite(v):
        return float(low)
    return float(max(low, min(high, v)))


def _risk_from_penalty(penalty: float, low: float, mid: float, high: float) -> str:
    """Zamienia karę punktową PBIS na czytelną flagę ryzyka."""
    p = float(penalty) if np.isfinite(penalty) else 0.0
    if p <= low:
        return "niskie"
    if p <= mid:
        return "średnie"
    if p <= high:
        return "podwyższone"
    return "wysokie"


def pbis_quality_label(score_0_100: float) -> str:
    s = _clip_float(score_0_100, 0.0, 100.0)
    if s >= 90.0:
        return "bardzo dobry"
    if s >= 80.0:
        return "dobry / bezpieczny"
    if s >= 70.0:
        return "użyteczny, sprawdzić A/B"
    if s >= 60.0:
        return "ryzykowny"
    return "raczej odrzucić"


def pbis_penalty_summary(
    hole_risk: str,
    bulge_risk: str,
    phase_risk: str,
    symmetry_risk: str,
    plateau: str = "",
) -> str:
    parts = []
    if hole_risk not in {"", "niskie"}:
        parts.append(f"dziura: {hole_risk}")
    if bulge_risk not in {"", "niskie"}:
        parts.append(f"bulwa: {bulge_risk}")
    if phase_risk not in {"", "niskie"}:
        parts.append(f"faza: {phase_risk}")
    if symmetry_risk not in {"", "niskie"}:
        parts.append(f"L/R: {symmetry_risk}")
    if plateau:
        parts.append(f"plateau: {plateau}")
    return " | ".join(parts) if parts else "ryzyka niskie"




def _risk_level(value: str) -> int:
    """Porządkuje polskie flagi ryzyka od 0 do 3."""
    v = str(value or "").strip().lower()
    if v in {"", "brak", "niski", "niskie", "low"}:
        return 0
    if v in {"średni", "średnie", "sredni", "srednie", "medium"}:
        return 1
    if v in {"podwyższony", "podwyższone", "podwyzszony", "podwyzszone", "raised"}:
        return 2
    if v in {"wysoki", "wysokie", "high"}:
        return 3
    if "brak" in v:
        return 3
    return 1


def _plateau_penalty_label(plateau: str) -> Tuple[float, str]:
    p = str(plateau or "").strip().lower()
    if "wysok" in p:
        return 0.0, "plateau stabilne"
    if "śred" in p or "sred" in p:
        return 0.5, "plateau średnie"
    if "nisk" in p:
        return 2.0, "plateau słabe"
    if not p:
        return 1.0, "plateau nieocenione"
    return 5.0, "wynik punktowy / brak plateau"




def plateau_stability_user_label(plateau: str) -> str:
    """Prosty opis stabilności ustawienia zamiast technicznego słowa plateau."""
    value = str(plateau or "").strip().lower()
    if "wysok" in value:
        return "dobra — podobne ustawienia w pobliżu dają zbliżony wynik"
    if "śred" in value or "sred" in value:
        return "średnia — warto porównać wariant zapasowy A/B"
    if "nisk" in value:
        return "słaba — niewielka zmiana opóźnienia może pogorszyć wynik"
    if "brak" in value or "punkt" in value:
        return "niepewna — dobry wynik występuje tylko w wąskim punkcie"
    if "ręcz" in value or "recz" in value or "nie ocen" in value:
        return "nieoceniona — najpierw wykonaj pełną optymalizację"
    return "nieoceniona"


def _risk_word(value: str) -> str:
    text = str(value or "niskie").strip().lower()
    if text in {"", "brak"}:
        return "niskie"
    return text


def _risk_explanation(value: str, low_text: str, medium_text: str, high_text: str) -> str:
    level = _risk_level(value)
    if level <= 0:
        return low_text
    if level == 1:
        return medium_text
    return high_text


def pbis_risk_user_lines(candidate: Candidate) -> List[str]:
    """Wyjaśnia ryzyka PBIS prostym językiem, bez technicznych skrótów."""
    hole = _risk_word(getattr(candidate, "pbis_hole_risk", ""))
    bulge = _risk_word(getattr(candidate, "pbis_bulge_risk", ""))
    phase = _risk_word(getattr(candidate, "pbis_phase_risk", ""))
    symmetry = _risk_word(getattr(candidate, "pbis_symmetry_risk", ""))
    stability = plateau_stability_user_label(getattr(candidate, "pbis_plateau", ""))
    return [
        f"Szeroka dziura w basie: ryzyko {hole} — " + _risk_explanation(
            hole,
            "nie widać istotnego ubytku energii.",
            "w części pasma może brakować energii; sprawdź wykres i odsłuch.",
            "może być wyraźnie za mało basu w części pasma; koniecznie sprawdź pomiar.",
        ),
        f"Nadmiar basu: ryzyko {bulge} — " + _risk_explanation(
            bulge,
            "nie widać istotnego pogrubienia basu.",
            "bas może być nieco za ciężki; sprawdź kontrabas i stopę perkusji.",
            "bas może dudnić albo pogrubiać wokal; ten wariant wymaga ostrożności.",
        ),
        f"Zgranie fazy subów z kolumnami przy podziale: ryzyko {phase} — " + _risk_explanation(
            phase,
            "wygląda poprawnie, ale potwierdź sumę w REW.",
            "może nie być idealne; sprawdź sumę w REW i odsłuch zwartości basu.",
            "może pogarszać połączenie subów z kolumnami; nie wybieraj bez pomiaru kontrolnego.",
        ),
        f"Równowaga lewej i prawej strony: ryzyko {symmetry} — " + _risk_explanation(
            symmetry,
            "kanały wyglądają wystarczająco równo.",
            "kanały mogą różnić się odczuwalnie; zwróć uwagę na stabilność sceny.",
            "kanały różnią się wyraźnie; scena i bas mogą być mniej stabilne.",
        ),
        f"Stabilność ustawienia: {stability}.",
    ]


def pbis_risk_user_block(candidate: Candidate, indent: str = "  ") -> str:
    return "\n".join(f"{indent}- {line}" for line in pbis_risk_user_lines(candidate))


def select_spatial_coherence_candidate(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
) -> Tuple[Optional[Candidate], str]:
    """
    Wybiera dodatkowy wariant OFF/OFF do sprawdzenia spójności fazowej L/R przy crossoverze.

    To nie jest pomiar szerokości ani głębokości sceny stereo. Wskazuje jedynie kandydat do odsłuchowej kontroli relacji przestrzennych.
    Algorytm wybiera spośród dobrych punktów OFF/OFF wariant z korzystnym ryzykiem
    fazy, równowagą L/R i stabilnością ustawienia w okolicy crossovera.
    """
    best, _policy, note = off_off_priority_main_candidate(candidates)
    if best is None:
        return None, note
    offs = [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"]
    tolerance = max(3.0, effective_score_tolerance(cfg))
    eligible = [c for c in offs if float(best.score) - float(c.score) <= tolerance + 1e-9]
    if not eligible:
        eligible = [best]
    xo = parse_crossover_hz(crossover_label, cfg)

    def sort_key(c: Candidate):
        safety = physical_delay_safety(c, l_base_delay_ms, r_base_delay_ms, xo, cfg)
        plateau_penalty, _label = _plateau_penalty_label(getattr(c, "pbis_plateau", ""))
        safe_score = float(getattr(c, "pbis_safe_score", getattr(c, "pbis_score", c.score)) or 0.0)
        return (
            _risk_level(getattr(c, "pbis_phase_risk", "")),
            _risk_level(getattr(c, "pbis_symmetry_risk", "")),
            float(plateau_penalty),
            int(safety["severity"]),
            -safe_score,
            float(best.score) - float(c.score),
            float(safety["delay_burden"]),
        )

    chosen = min(eligible, key=sort_key)
    if same_candidate(chosen, best):
        reason = "To samo ustawienie co rekomendacja główna. Jest także najbardziej obiecującym kandydatem do kontroli spójności fazowej L/R przy crossoverze."
    else:
        loss = float(best.score) - float(chosen.score)
        reason = (
            f"To wariant OFF/OFF do dodatkowego odsłuchu. Traci {loss:.2f} pkt względem rekomendacji głównej, "
            "ale wśród dobrych ustawień ma korzystniejsze połączenie ryzyka fazowego, równowagi L/R i stabilności."
        )
    return chosen, reason


def spatial_coherence_candidate_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
) -> str:
    chosen, reason = select_spatial_coherence_candidate(
        candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg
    )
    if chosen is None:
        return "WARIANT DO SPRAWDZENIA SPÓJNOŚCI L/R PRZY CROSSOVERZE\n" + reason
    return "\n".join([
        "WARIANT DO SPRAWDZENIA SPÓJNOŚCI L/R PRZY CROSSOVERZE:",
        *shd_settings_lines(chosen, l_base_delay_ms, r_base_delay_ms, cfg, indent="  "),
        f"  PBIS: {float(getattr(chosen, 'pbis_score', 0.0)):.1f}/100 — {getattr(chosen, 'pbis_label', '')}",
        f"  {reason}",
        "  To jest kandydat do pomiaru i odsłuchu relacji przestrzennych, a nie predykcja szerokości lub głębokości sceny.",
    ])


def pbis_safe_label(score_0_100: float) -> str:
    s = _clip_float(score_0_100, 0.0, 100.0)
    if s >= 90.0:
        return "Safe A / bardzo dobry"
    if s >= 82.0:
        return "Safe B / naturalny"
    if s >= 74.0:
        return "Safe C / sprawdzić A/B"
    if s >= 65.0:
        return "Safe D / ostrożnie"
    return "Safe E / odrzucić"


def naturalness_from_safe(score_0_100: float, bulge_risk: str, symmetry_risk: str, phase_risk: str) -> str:
    max_risk = max(_risk_level(bulge_risk), _risk_level(symmetry_risk), _risk_level(phase_risk))
    s = _clip_float(score_0_100, 0.0, 100.0)
    if s >= 88.0 and max_risk <= 1:
        return "wysoka"
    if s >= 80.0 and max_risk <= 2:
        return "dobra"
    if s >= 72.0:
        return "średnia / A-B"
    return "ryzykowna"


def apply_sound_quality_safe_to_candidate(
    c: Candidate,
    cfg: OptimizerConfig,
    *,
    include_plateau: bool = True,
) -> None:
    """v19.22: PBIS Safe — ostrożna ocena brzmieniowa.

    PBIS Raw nadal pozostaje niezmienny i porównywalny z ręcznym kalkulatorem.
    PBIS Safe odejmuje dodatkowe kary za ryzyka, które zwykle przeszkadzają w naturalnym
    brzmieniu: bulwa 110–170 Hz, asymetria L/R, słaba faza i brak stabilnego plateau.

    v19.54: ``include_plateau=False`` służy wyłącznie do porównania crossoverów
    na stałym paśmie globalnym. Pojedynczy kandydat nie ma wtedy własnej mapy
    sąsiedztwa delayów, więc nie wolno karać go za brak informacji o plateau.
    """
    raw = float(getattr(c, "pbis_score_raw", getattr(c, "pbis_score", 0.0)) or 0.0)

    hole_lvl = _risk_level(getattr(c, "pbis_hole_risk", ""))
    bulge_lvl = _risk_level(getattr(c, "pbis_bulge_risk", ""))
    phase_lvl = _risk_level(getattr(c, "pbis_phase_risk", ""))
    sym_lvl = _risk_level(getattr(c, "pbis_symmetry_risk", ""))
    plateau_pen, plateau_label = _plateau_penalty_label(getattr(c, "pbis_plateau", ""))
    if not bool(include_plateau):
        plateau_pen = 0.0
        plateau_label = "plateau pominięte w stałym paśmie globalnym"

    # Heurystyka strojenia brzmieniowego: bulwa i asymetria są karane mocniej niż wąskie problemy,
    # bo częściej psują naturalność niższej średnicy i stabilność fundamentu stereo.
    risk_pen = (
        {0: 0.0, 1: 1.5, 2: 4.0, 3: 8.0}[hole_lvl]
        + {0: 0.0, 1: 2.5, 2: 7.0, 3: 13.0}[bulge_lvl]
        + {0: 0.0, 1: 1.5, 2: 4.0, 3: 7.0}[phase_lvl]
        + {0: 0.0, 1: 2.0, 2: 5.0, 3: 9.0}[sym_lvl]
        + plateau_pen
    )

    # Dodatkowa kara ciągła za realną bulwę/pik, niezależna od samych etykiet ryzyka.
    risk_pen += max(0.0, float(getattr(c, "bulge_db", 0.0)) - 0.8) * 1.5
    risk_pen += max(0.0, float(getattr(c, "bulge_peak_db", 0.0)) - 1.8) * 0.8
    risk_pen += max(0.0, float(getattr(c, "lr_dip_db", 0.0)) - 8.0) * 0.25

    safe = _clip_float(raw - risk_pen, 0.0, 100.0)
    c.pbis_safe_score = safe
    c.pbis_safe_penalty = float(risk_pen)
    c.pbis_safe_label = pbis_safe_label(safe)
    c.naturalness_label = naturalness_from_safe(
        safe,
        getattr(c, "pbis_bulge_risk", ""),
        getattr(c, "pbis_symmetry_risk", ""),
        getattr(c, "pbis_phase_risk", ""),
    )

    flags = []
    if bulge_lvl >= 2:
        flags.append("bulwa")
    if hole_lvl >= 2:
        flags.append("dziura")
    if phase_lvl >= 2:
        flags.append("faza")
    if sym_lvl >= 2:
        flags.append("L/R")
    if bool(include_plateau) and plateau_pen >= 2.0:
        flags.append(plateau_label)
    c.sound_quality_flags = " | ".join(flags) if flags else "bez dużych flag"




def sync_pbis_safe_from_scanned_candidate(dst: Candidate, src: Candidate) -> None:
    """Ujednolica PBIS Safe ręcznego wariantu z pełnym skanem.

    PBIS Safe automatyczny zna stabilność plateau z pełnej mapy kandydatów.
    Ręczne liczenie pojedynczego punktu samo z siebie plateau nie zna, więc dla
    punktu identycznego z kandydatem ze skanu kopiujemy te pola z automatu.
    Dzięki temu ręczny PBIS Safe i automatyczny PBIS Safe są 1:1 zgodne.
    """
    fields = [
        "pbis_plateau",
        "pbis_plateau_bonus",
        "pbis_rank_score",
        "pbis_safe_score",
        "pbis_safe_label",
        "pbis_safe_penalty",
        "naturalness_label",
        "sound_quality_flags",
    ]
    for name in fields:
        if hasattr(src, name):
            setattr(dst, name, getattr(src, name))
    # Surowy PBIS pozostaje liczony ręcznie, ale dla czytelności etykiety/ryzyka też
    # trzymamy pełną zgodność z kandydatem ze skanu, jeżeli punkt jest ten sam.
    for name in [
        "pbis_label",
        "pbis_hole_risk",
        "pbis_bulge_risk",
        "pbis_phase_risk",
        "pbis_symmetry_risk",
        "pbis_risk_summary",
    ]:
        if hasattr(src, name):
            setattr(dst, name, getattr(src, name))


def find_scanned_candidate_by_final_values(
    candidates: List[Candidate],
    l_final_delay_ms: float,
    r_final_delay_ms: float,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    l_polarity: str,
    r_polarity: str,
    tolerance_ms: float = 0.011,
    cfg: Optional[OptimizerConfig] = None,
) -> Optional[Candidate]:
    """Znajduje punkt skanu odpowiadający ręcznie wpisanej relacji sub-main.

    Dla trybu relatywnego ``l/r_final_delay_ms`` są relacją docelową sub-main.
    W trybie standardowym zachowujemy zgodność wsteczną i porównujemy delaye
    finalne subów.
    """
    if not candidates:
        return None
    lp = str(l_polarity or "OFF").strip().upper()
    rp = str(r_polarity or "OFF").strip().upper()
    relative = bool(getattr(cfg, "relative_delay_shd_mode", False)) if cfg is not None else False
    best = None
    best_err = None
    for c in candidates:
        if str(getattr(c, "l_polarity", "OFF")).strip().upper() != lp:
            continue
        if str(getattr(c, "r_polarity", "OFF")).strip().upper() != rp:
            continue
        try:
            if relative:
                lf, rf = candidate_final_relative_delays(c, l_base_delay_ms, r_base_delay_ms, cfg)
            else:
                lf = final_delay(l_base_delay_ms, float(getattr(c, "l_delay_ms", 0.0)))
                rf = final_delay(r_base_delay_ms, float(getattr(c, "r_delay_ms", 0.0)))
            err = max(abs(lf - float(l_final_delay_ms)), abs(rf - float(r_final_delay_ms)))
        except Exception:
            continue
        if err <= tolerance_ms and (best_err is None or err < best_err):
            best = c
            best_err = err
    return best


def annotate_sound_quality_safe(candidates: List[Candidate], cfg: OptimizerConfig) -> None:
    """Dopisuje PBIS Safe i opcjonalnie używa go jako rankingu w trybie PBIS."""
    if not candidates:
        return
    profile = normalize_scoring_profile(getattr(cfg, "scoring_profile", "PBIS"))
    use_safe = bool(getattr(cfg, "sound_quality_safe", True)) and profile == "PBIS"
    for c in candidates:
        apply_sound_quality_safe_to_candidate(c, cfg)
        if use_safe:
            # Ranking w trybie Safe preferuje wariant mniej ryzykowny brzmieniowo.
            # PBIS Raw zostaje nietknięty i nadal jest widoczny w tabeli oraz kalkulatorze ręcznym.
            c.score = float(getattr(c, "pbis_safe_score", getattr(c, "pbis_rank_score", c.score)))


def select_safe_natural_candidate(candidates: List[Candidate]) -> Optional[Candidate]:
    """Wybiera wariant 'Safe Natural' — niekoniecznie najwyższy PBIS Raw, ale najmniej ryzykowny."""
    if not candidates:
        return None
    best_raw = max(float(getattr(c, "pbis_score", 0.0) or 0.0) for c in candidates)

    def eligible(c: Candidate) -> bool:
        raw = float(getattr(c, "pbis_score", 0.0) or 0.0)
        if raw < best_raw - 15.0:
            return False
        if _risk_level(getattr(c, "pbis_bulge_risk", "")) >= 3:
            return False
        if _risk_level(getattr(c, "pbis_symmetry_risk", "")) >= 3:
            return False
        if _risk_level(getattr(c, "pbis_phase_risk", "")) >= 3:
            return False
        return True

    pool = [c for c in candidates if eligible(c)] or list(candidates)
    return max(
        pool,
        key=lambda c: (
            float(getattr(c, "pbis_safe_score", 0.0) or 0.0),
            float(getattr(c, "pbis_score", 0.0) or 0.0),
            -_risk_level(getattr(c, "pbis_bulge_risk", "")),
            -_risk_level(getattr(c, "pbis_symmetry_risk", "")),
        ),
    )


def pbis_from_metrics(
    lr_std_db: float,
    lr_dip_db: float,
    broad_dip_db: float,
    broad_bulge_db: float,
    bulge_db: float,
    bulge_peak_db: float,
    l_construct_db: float,
    r_construct_db: float,
    lr_construct_db: float,
    cancellation_penalty: float,
    symmetry_db: float,
) -> Dict[str, object]:
    """v19.18 PBIS — Perceptual Bass Integration Score 0–100.

    To praktyczny model dla integracji subwooferów z monitorami:
    - 35% równość/suma w paśmie integracji,
    - 20% szerokie dziury,
    - 15% bulwa 110–170 Hz,
    - 15% stabilność fazowa/konstruktywność przy crossoverze,
    - 5% symetria L/R,
    - 10% plateau dopisywane później po analizie rodziny kandydatów.
    """
    integration_pen = _clip_float(3.2 * lr_std_db + 2.0 * lr_dip_db + 2.2 * cancellation_penalty, 0.0, 35.0)
    hole_pen = _clip_float(4.2 * broad_dip_db, 0.0, 20.0)
    bulge_pen = _clip_float(4.0 * bulge_db + 1.8 * bulge_peak_db + 1.5 * broad_bulge_db, 0.0, 15.0)

    # Konstruktywność bliska zera lub ujemna jest sygnałem ryzyka fazy, nawet gdy SPL wygląda nieźle.
    phase_raw = (
        max(0.0, 2.5 - float(l_construct_db)) * 1.3
        + max(0.0, 2.5 - float(r_construct_db)) * 1.3
        + max(0.0, 1.5 - float(lr_construct_db)) * 1.8
        + max(0.0, float(cancellation_penalty)) * 1.8
    )
    phase_pen = _clip_float(phase_raw, 0.0, 15.0)

    symmetry_pen = _clip_float(0.75 * float(symmetry_db), 0.0, 5.0)

    score = 100.0 - integration_pen - hole_pen - bulge_pen - phase_pen - symmetry_pen
    score = _clip_float(score, 0.0, 100.0)

    hole_risk = _risk_from_penalty(hole_pen, 2.5, 6.5, 12.0)
    bulge_risk = _risk_from_penalty(bulge_pen, 2.0, 5.0, 10.0)
    phase_risk = _risk_from_penalty(phase_pen, 2.0, 5.0, 10.0)
    symmetry_risk = _risk_from_penalty(symmetry_pen, 0.8, 2.0, 3.5)

    return {
        "pbis_score": score,
        "pbis_label": pbis_quality_label(score),
        "pbis_integration_penalty": integration_pen,
        "pbis_hole_penalty": hole_pen,
        "pbis_bulge_penalty": bulge_pen,
        "pbis_phase_penalty": phase_pen,
        "pbis_symmetry_penalty": symmetry_pen,
        "pbis_hole_risk": hole_risk,
        "pbis_bulge_risk": bulge_risk,
        "pbis_phase_risk": phase_risk,
        "pbis_symmetry_risk": symmetry_risk,
        "pbis_risk_summary": pbis_penalty_summary(hole_risk, bulge_risk, phase_risk, symmetry_risk),
    }


def annotate_pbis_plateau(candidates: List[Candidate], cfg: OptimizerConfig) -> None:
    """Dopisuje komponent stabilności lokalnego obszaru do rankingu PBIS.

    v19.44: wynik matematyczny jest taki sam jak wcześniej, ale sąsiedztwo liczymy
    wektorowo w obrębie każdej rodziny polaryzacji. Poprzednia lista zagnieżdżona
    robiła O(n²) porównań w Pythonie i powodowała długie zatrzymanie po 100% skanu.
    """
    if not candidates:
        return
    profile = normalize_scoring_profile(getattr(cfg, "scoring_profile", "PBIS"))

    for c in candidates:
        raw = float(getattr(c, "pbis_score_raw", 0.0) or getattr(c, "pbis_score", 0.0) or 0.0)
        c.pbis_score_raw = raw
        c.pbis_score = raw

    grouped: Dict[str, List[Candidate]] = {}
    for c in candidates:
        grouped.setdefault(polarity_family(c), []).append(c)

    for fam_items in grouped.values():
        l_vals = np.asarray([float(x.l_delay_ms) for x in fam_items], dtype=float)
        r_vals = np.asarray([float(x.r_delay_ms) for x in fam_items], dtype=float)
        raw_vals = np.asarray([
            float(getattr(x, "pbis_score_raw", getattr(x, "pbis_score", 0.0)) or 0.0)
            for x in fam_items
        ], dtype=float)
        for idx, c in enumerate(fam_items):
            close_count = int(np.count_nonzero(
                (np.abs(l_vals - l_vals[idx]) <= 0.5 + 1e-12)
                & (np.abs(r_vals - r_vals[idx]) <= 0.5 + 1e-12)
                & (np.abs(raw_vals - raw_vals[idx]) <= 3.0 + 1e-12)
            ))
            if close_count >= 7:
                plateau = "wysokie"
                bonus = 2.0
            elif close_count >= 4:
                plateau = "średnie"
                bonus = 1.0
            elif close_count >= 2:
                plateau = "niskie"
                bonus = 0.0
            else:
                plateau = "brak / punktowy wynik"
                bonus = -2.0

            raw_c = float(raw_vals[idx])
            c.pbis_plateau = plateau
            c.pbis_plateau_bonus = float(bonus)
            c.pbis_rank_score = _clip_float(raw_c + bonus, 0.0, 100.0)
            c.pbis_label = pbis_quality_label(raw_c)
            c.pbis_risk_summary = pbis_penalty_summary(
                getattr(c, "pbis_hole_risk", ""),
                getattr(c, "pbis_bulge_risk", ""),
                getattr(c, "pbis_phase_risk", ""),
                getattr(c, "pbis_symmetry_risk", ""),
                plateau,
            )
            if profile == "PBIS":
                c.score = float(c.pbis_rank_score)


def score_candidate(
    freq: np.ndarray,
    l_main: np.ndarray,
    l_sub_shifted: np.ndarray,
    r_main: np.ndarray,
    r_sub_shifted: np.ndarray,
    cfg: OptimizerConfig,
) -> Tuple[float, Dict[str, float]]:
    l_sum = l_main + l_sub_shifted
    r_sum = r_main + r_sub_shifted
    lr_sum = l_sum + r_sum

    eval_m = band_mask(freq, cfg.eval_low_hz, cfg.eval_high_hz)
    bulge_m = band_mask(freq, cfg.bulge_low_hz, cfg.bulge_high_hz)
    if not np.any(eval_m):
        raise ValueError("Pasmo oceny nie pokrywa się z zakresem danych.")

    l_cons = mean_constructive_margin(l_sum, l_main, l_sub_shifted, eval_m)
    r_cons = mean_constructive_margin(r_sum, r_main, r_sub_shifted, eval_m)
    lr_cons = mean_constructive_margin(lr_sum, l_sum, r_sum, eval_m)

    lr_db = db(lr_sum)[eval_m]
    l_db = db(l_sum)[eval_m]
    r_db = db(r_sum)[eval_m]

    lr_std = roughness(lr_db)
    lr_dip = dip_depth(lr_db)
    l_dip = dip_depth(l_db)
    r_dip = dip_depth(r_db)
    lr_mean = float(np.nanmean(lr_db))

    profile = normalize_scoring_profile(getattr(cfg, "scoring_profile", "PBIS"))
    f_eval = freq[eval_m]
    w_eval = psycho_frequency_weights(f_eval, cfg)

    # v19.17: modele 1–2. Dla Technical zachowujemy możliwie stare metryki.
    if profile == "Technical":
        score_lr_std = lr_std
        score_lr_dip = lr_dip
        score_l_dip = l_dip
        score_r_dip = r_dip
        broad_dip = broad_bulge_eval = narrow_notch = 0.0
    else:
        lr_wstd = weighted_roughness(lr_db, w_eval)
        lr_wdip = weighted_dip_depth(lr_db, w_eval)
        l_wdip = weighted_dip_depth(l_db, w_eval)
        r_wdip = weighted_dip_depth(r_db, w_eval)
        broad_dip, broad_bulge_eval, narrow_notch = broad_error_metrics(f_eval, lr_db, w_eval)

        if profile == "Balanced":
            score_lr_std = 0.65 * lr_std + 0.35 * lr_wstd
            score_lr_dip = 0.65 * lr_dip + 0.35 * lr_wdip
            score_l_dip = 0.75 * l_dip + 0.25 * l_wdip
            score_r_dip = 0.75 * r_dip + 0.25 * r_wdip
        else:
            score_lr_std = 0.45 * lr_std + 0.55 * lr_wstd
            score_lr_dip = 0.45 * lr_dip + 0.55 * lr_wdip
            score_l_dip = 0.65 * l_dip + 0.35 * l_wdip
            score_r_dip = 0.65 * r_dip + 0.35 * r_wdip

    if np.any(bulge_m):
        lr_full_db = db(lr_sum)
        if cfg.use_psycho_bulge_model:
            # Odniesienie z mediany głównego pasma oceny jest stabilniejsze niż średnia,
            # bo jest mniej wrażliwe na pojedyncze dołki/piki pokoju.
            reference_db = float(np.nanmedian(lr_db))
            bulge, bulge_peak = weighted_positive_excess(
                freq,
                lr_full_db,
                bulge_m,
                reference_db,
                cfg.bulge_tolerance_db,
            )
        else:
            bulge_mean = float(np.nanmean(lr_full_db[bulge_m]))
            eval_mean = float(np.nanmean(lr_db))
            bulge = max(0.0, bulge_mean - eval_mean)
            bulge_peak = 0.0
    else:
        bulge = 0.0
        bulge_peak = 0.0

    lr_margin = db(lr_sum)[eval_m] - np.maximum(db(l_sum)[eval_m], db(r_sum)[eval_m])
    if profile == "Technical":
        cancellation_penalty = float(np.nanmean(np.maximum(0.0, 1.0 - lr_margin)))
        symmetry_db = float(np.nanmean(np.abs(l_db - r_db)))
    else:
        cancellation_penalty = float(np.nansum(np.maximum(0.0, 1.0 - lr_margin) * w_eval) / max(np.nansum(w_eval), 1e-12))
        symmetry_db = float(np.nansum(np.abs(l_db - r_db) * w_eval) / max(np.nansum(w_eval), 1e-12))

    broad_weight = 0.0
    broad_bulge_weight = 0.0
    narrow_notch_credit = 0.0
    if bool(getattr(cfg, "psycho_broad_error_model", True)) and profile != "Technical":
        if profile == "Balanced":
            broad_weight = 0.55
            broad_bulge_weight = 0.25
            narrow_notch_credit = 0.10
        else:
            broad_weight = 0.95
            broad_bulge_weight = 0.40
            narrow_notch_credit = 0.18

    legacy_score = (
        1.0 * l_cons
        + 1.0 * r_cons
        + cfg.prefer_lr_sum * lr_cons
        - cfg.prefer_smoothness * score_lr_std
        - cfg.dip_penalty_weight * score_lr_dip
        - 0.8 * score_l_dip
        - 0.8 * score_r_dip
        - 1.2 * cancellation_penalty
        - broad_weight * broad_dip
        - broad_bulge_weight * broad_bulge_eval
        + narrow_notch_credit * narrow_notch
        - cfg.bulge_penalty_weight * bulge
        - cfg.bulge_penalty_weight * cfg.bulge_peak_penalty_weight * bulge_peak
    )

    pbis = pbis_from_metrics(
        lr_std_db=score_lr_std,
        lr_dip_db=score_lr_dip,
        broad_dip_db=broad_dip,
        broad_bulge_db=broad_bulge_eval,
        bulge_db=bulge,
        bulge_peak_db=bulge_peak,
        l_construct_db=l_cons,
        r_construct_db=r_cons,
        lr_construct_db=lr_cons,
        cancellation_penalty=cancellation_penalty,
        symmetry_db=symmetry_db,
    )
    score = float(pbis["pbis_score"]) if profile == "PBIS" else float(legacy_score)

    metrics = {
        "l_construct_db": l_cons,
        "r_construct_db": r_cons,
        "lr_construct_db": lr_cons,
        "lr_std_db": lr_std,
        "lr_dip_db": lr_dip,
        "bulge_db": bulge,
        "bulge_peak_db": bulge_peak,
        "lr_mean_db": lr_mean,
        "score_profile": profile,
        "broad_dip_db": broad_dip,
        "broad_bulge_db": broad_bulge_eval,
        "narrow_notch_credit_db": narrow_notch,
        "symmetry_db": symmetry_db,
        "pbis_score": float(pbis["pbis_score"]),
        "pbis_score_raw": float(pbis["pbis_score"]),
        "pbis_rank_score": float(pbis["pbis_score"]),
        "pbis_label": str(pbis["pbis_label"]),
        "pbis_hole_risk": str(pbis["pbis_hole_risk"]),
        "pbis_bulge_risk": str(pbis["pbis_bulge_risk"]),
        "pbis_phase_risk": str(pbis["pbis_phase_risk"]),
        "pbis_symmetry_risk": str(pbis["pbis_symmetry_risk"]),
        "pbis_risk_summary": str(pbis["pbis_risk_summary"]),
        "pbis_integration_penalty": float(pbis["pbis_integration_penalty"]),
        "pbis_hole_penalty": float(pbis["pbis_hole_penalty"]),
        "pbis_bulge_penalty": float(pbis["pbis_bulge_penalty"]),
        "pbis_phase_penalty": float(pbis["pbis_phase_penalty"]),
        "pbis_symmetry_penalty": float(pbis["pbis_symmetry_penalty"]),
    }
    return float(score), metrics



def evaluate_manual_candidate(
    l_main_m: Measurement,
    l_sub_m: Measurement,
    r_main_m: Measurement,
    r_sub_m: Measurement,
    cfg: OptimizerConfig,
    l_final_delay_ms: float,
    r_final_delay_ms: float,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    l_polarity: str = "OFF",
    r_polarity: str = "OFF",
) -> Tuple[np.ndarray, Dict[str, np.ndarray], Candidate, Dict[str, float]]:
    """Liczy PBIS/metryki dla jednego ręcznie wybranego wariantu miniDSP.

    Użytkownik podaje wartości finalne, czyli takie, które chce wpisać w miniDSP.
    Aplikacja przelicza je na delay dodatkowy względem opóźnień bazowych z pliku.
    """
    measurements = [l_main_m, l_sub_m, r_main_m, r_sub_m]
    freq = common_grid(measurements, cfg.freq_points)
    responses = {
        "L_main": measurement_to_complex(l_main_m, freq),
        "L_sub": measurement_to_complex(l_sub_m, freq),
        "R_main": measurement_to_complex(r_main_m, freq),
        "R_sub": measurement_to_complex(r_sub_m, freq),
    }

    l_add = float(l_final_delay_ms) - float(l_base_delay_ms)
    r_add = float(r_final_delay_ms) - float(r_base_delay_ms)
    l_inv = str(l_polarity).strip().upper() == "INV"
    r_inv = str(r_polarity).strip().upper() == "INV"

    l_sub_s = apply_delay_and_polarity(responses["L_sub"], freq, l_add, l_inv)
    r_sub_s = apply_delay_and_polarity(responses["R_sub"], freq, r_add, r_inv)
    score, metrics = score_candidate(
        freq,
        responses["L_main"],
        l_sub_s,
        responses["R_main"],
        r_sub_s,
        cfg,
    )

    cand = Candidate(
        l_delay_ms=l_add,
        l_polarity="INV" if l_inv else "OFF",
        r_delay_ms=r_add,
        r_polarity="INV" if r_inv else "OFF",
        score=score,
        l_construct_db=metrics["l_construct_db"],
        r_construct_db=metrics["r_construct_db"],
        lr_construct_db=metrics["lr_construct_db"],
        lr_std_db=metrics["lr_std_db"],
        lr_dip_db=metrics["lr_dip_db"],
        bulge_db=metrics["bulge_db"],
        bulge_peak_db=metrics["bulge_peak_db"],
        lr_mean_db=metrics["lr_mean_db"],
        pbis_score=metrics.get("pbis_score", 0.0),
        pbis_score_raw=metrics.get("pbis_score_raw", metrics.get("pbis_score", 0.0)),
        pbis_rank_score=metrics.get("pbis_rank_score", metrics.get("pbis_score", 0.0)),
        pbis_label=metrics.get("pbis_label", ""),
        pbis_hole_risk=metrics.get("pbis_hole_risk", ""),
        pbis_bulge_risk=metrics.get("pbis_bulge_risk", ""),
        pbis_phase_risk=metrics.get("pbis_phase_risk", ""),
        pbis_symmetry_risk=metrics.get("pbis_symmetry_risk", ""),
        pbis_risk_summary=metrics.get("pbis_risk_summary", ""),
    )
    cand.pbis_plateau = "nie oceniano — ręczny punkt"
    cand.pbis_plateau_bonus = 0.0
    cand.pbis_score_raw = float(getattr(cand, "pbis_score", 0.0))
    cand.pbis_rank_score = float(getattr(cand, "pbis_score", 0.0))
    apply_sound_quality_safe_to_candidate(cand, cfg)
    return freq, responses, cand, metrics


def write_manual_pbis_report(
    out_dir: str | Path,
    cand: Candidate,
    metrics: Dict[str, float],
    cfg: OptimizerConfig,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
) -> Tuple[Path, Path]:
    """Zapisuje czytelny raport i CSV dla ręcznie sprawdzanego wariantu PBIS."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "CUSTOM_PBIS_EVALUATION.md"
    csv_path = out / "CUSTOM_PBIS_EVALUATION.csv"

    mapped = candidate_shd_settings(cand, l_base_delay_ms, r_base_delay_ms, cfg)
    pbis = float(getattr(cand, "pbis_score", 0.0))
    rows = [
        ("L main SHD [ms]", f"{float(mapped['l_main_ms']):.2f}"),
        ("R main SHD [ms]", f"{float(mapped['r_main_ms']):.2f}"),
        ("L sub SHD [ms]", f"{float(mapped['l_sub_ms']):.2f}"),
        ("L sub polarity", cand.l_polarity),
        ("R sub SHD [ms]", f"{float(mapped['r_sub_ms']):.2f}"),
        ("R sub polarity", cand.r_polarity),
        ("L rel. sub-main [ms]", f"{float(mapped['l_sub_minus_main_ms']):+.2f}"),
        ("R rel. sub-main [ms]", f"{float(mapped['r_sub_minus_main_ms']):+.2f}"),
        ("L additional vs file [ms]", f"{cand.l_delay_ms:.2f}"),
        ("R additional vs file [ms]", f"{cand.r_delay_ms:.2f}"),
        ("PBIS 0-100", f"{pbis:.1f}"),
        ("PBIS Safe 0-100", f"{float(getattr(cand, 'pbis_safe_score', pbis)):.1f}"),
        ("PBIS Safe penalty", f"{float(getattr(cand, 'pbis_safe_penalty', 0.0)):.2f}"),
        ("PBIS Safe label", str(getattr(cand, "pbis_safe_label", ""))),
        ("PBIS plateau", str(getattr(cand, "pbis_plateau", ""))),
        ("Naturalność", str(getattr(cand, "naturalness_label", ""))),
        ("PBIS label", str(getattr(cand, "pbis_label", ""))),
        ("PBIS risks", str(getattr(cand, "pbis_risk_summary", ""))),
        ("integration penalty", f"{float(metrics.get('pbis_integration_penalty', 0.0)):.2f}"),
        ("hole penalty", f"{float(metrics.get('pbis_hole_penalty', 0.0)):.2f}"),
        ("bulge penalty", f"{float(metrics.get('pbis_bulge_penalty', 0.0)):.2f}"),
        ("phase penalty", f"{float(metrics.get('pbis_phase_penalty', 0.0)):.2f}"),
        ("symmetry penalty", f"{float(metrics.get('pbis_symmetry_penalty', 0.0)):.2f}"),
        ("LR roughness [dB]", f"{float(metrics.get('lr_std_db', 0.0)):.2f}"),
        ("LR dip [dB]", f"{float(metrics.get('lr_dip_db', 0.0)):.2f}"),
        ("broad dip [dB]", f"{float(metrics.get('broad_dip_db', 0.0)):.2f}"),
        ("bulge [dB]", f"{float(metrics.get('bulge_db', 0.0)):.2f}"),
        ("symmetry L/R [dB]", f"{float(metrics.get('symmetry_db', 0.0)):.2f}"),
    ]

    md_lines = [
        "# Ręczna ocena PBIS",
        "",
        f"- Profil: **{crossover_label} / {filter_type}{slope_db_oct} / PBIS**",
        f"- Pasmo oceny: **{cfg.eval_low_hz:g}–{cfg.eval_high_hz:g} Hz**",
        f"- Kontrola bulwy: **{cfg.bulge_low_hz:g}–{cfg.bulge_high_hz:g} Hz**",
        "",
        "## Wynik",
        "",
        "| Parametr | Wartość |",
        "|---|---:|",
    ]
    for k, v in rows:
        md_lines.append(f"| {k} | {v} |")
    md_lines.extend([
        "",
        "Uwaga: surowy PBIS jest liczony bezpośrednio dla punktu i powinien być identyczny jak PBIS w tabeli rekomendacji "
        "dla tych samych finalnych delayów/polaryzacji. Jeżeli ręcznie wpisany punkt istnieje w ostatnim pełnym skanie, "
        "aplikacja przejmuje z automatu także PBIS Safe/plateau, żeby wynik ręczny i automatyczny były zgodne 1:1. "
        "Dla punktu spoza siatki pełnego skanu PBIS Safe jest oceną punktową bez pewności plateau.",
        "",
        "Skala PBIS: 90–100 bardzo dobry, 80–90 dobry, 70–80 użyteczny/sprawdzić A/B, 60–70 ryzykowny, <60 raczej odrzucić.",
        "",
    ])
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for k, v in rows:
            writer.writerow([k, v])
    return md_path, csv_path


def _make_candidate_from_metrics(ld: float, linv: bool, rd: float, rinv: bool, score: float, metrics: Dict[str, float]) -> Candidate:
    """Tworzy Candidate w jednym miejscu, żeby skan zgrubny i dokładny liczyły identycznie."""
    return Candidate(
        l_delay_ms=float(ld),
        l_polarity="INV" if linv else "OFF",
        r_delay_ms=float(rd),
        r_polarity="INV" if rinv else "OFF",
        score=float(score),
        l_construct_db=metrics["l_construct_db"],
        r_construct_db=metrics["r_construct_db"],
        lr_construct_db=metrics["lr_construct_db"],
        lr_std_db=metrics["lr_std_db"],
        lr_dip_db=metrics["lr_dip_db"],
        bulge_db=metrics["bulge_db"],
        bulge_peak_db=metrics["bulge_peak_db"],
        lr_mean_db=metrics["lr_mean_db"],
        pbis_score=metrics.get("pbis_score", 0.0),
        pbis_score_raw=metrics.get("pbis_score_raw", metrics.get("pbis_score", 0.0)),
        pbis_rank_score=metrics.get("pbis_rank_score", metrics.get("pbis_score", 0.0)),
        pbis_label=metrics.get("pbis_label", ""),
        pbis_hole_risk=metrics.get("pbis_hole_risk", ""),
        pbis_bulge_risk=metrics.get("pbis_bulge_risk", ""),
        pbis_phase_risk=metrics.get("pbis_phase_risk", ""),
        pbis_symmetry_risk=metrics.get("pbis_symmetry_risk", ""),
        pbis_risk_summary=metrics.get("pbis_risk_summary", ""),
    )


def _delay_grid(min_ms: float, max_ms: float, step_ms: float) -> np.ndarray:
    """Stabilna siatka delayów z końcem zakresu włącznie."""
    step = max(float(step_ms), 0.001)
    lo = float(min_ms)
    hi = float(max_ms)
    if hi < lo:
        lo, hi = hi, lo
    vals = np.arange(lo, hi + step / 2.0, step)
    vals = np.round(vals, 6)
    vals = vals[(vals >= lo - 1e-9) & (vals <= hi + 1e-9)]
    # Zawsze dopisujemy dokładny koniec zakresu, jeżeli regularny krok go nie trafił.
    # Dzięki temu np. 0..1.04 co 0.1 zawiera także 1.04, a nie kończy się na 1.0.
    if len(vals) == 0 or abs(float(vals[-1]) - hi) > 1e-6:
        vals = np.append(vals, round(hi, 6))
    return np.unique(np.round(vals, 6))


def _scan_mode_label(cfg: OptimizerConfig, exhaustive_total: int) -> str:
    if not bool(getattr(cfg, "fast_two_stage", True)):
        return "EXACT"
    if exhaustive_total <= int(getattr(cfg, "fast_exact_threshold", 30_000)):
        return "EXACT"
    return "FAST_2_STAGE"


def _coarse_total_for_step(cfg: OptimizerConfig, step_ms: float) -> Tuple[int, int]:
    delays = _delay_grid(float(getattr(cfg, "delay_min_ms", 0.0)), float(getattr(cfg, "delay_max_ms", 30.0)), max(float(step_ms), 0.001))
    return len(delays), len(delays) * len(delays)


def _effective_fast_coarse_step(cfg: OptimizerConfig, fine_step: Optional[float] = None) -> float:
    """v19.30: bezpiecznik wydajności dla bardzo dużych zakresów FAST coarse.

    Nie zmienia finalnej dokładności wyniku. Podnosi tylko krok zgrubny, gdy sam etap
    coarse byłby za ciężki. Dogęszczanie i walidator nadal używają delay_step_ms.
    """
    fine = max(float(fine_step if fine_step is not None else getattr(cfg, "delay_step_ms", 0.1)), 0.001)
    step = max(float(getattr(cfg, "fast_coarse_step_ms", 0.5)), fine)
    if not bool(getattr(cfg, "fast_auto_coarse_safety", True)):
        return step
    limit = max(int(getattr(cfg, "fast_max_coarse_combinations", 6_000)), 1_000)
    _n, total = _coarse_total_for_step(cfg, step)
    guard = 0
    while total > limit and guard < 24:
        step *= 1.25
        _n, total = _coarse_total_for_step(cfg, step)
        guard += 1
    return max(step, fine)


def optimizer_workload_info(cfg: OptimizerConfig) -> Dict[str, object]:
    """Szacunek ciężkości pracy bez uruchamiania obliczeń."""
    fine_step = max(float(getattr(cfg, "delay_step_ms", 0.1)), 0.001)
    fine_delays = _delay_grid(float(getattr(cfg, "delay_min_ms", 0.0)), float(getattr(cfg, "delay_max_ms", 30.0)), fine_step)
    fine_n = len(fine_delays)
    exhaustive_total = fine_n * fine_n
    mode = _scan_mode_label(cfg, exhaustive_total)
    coarse_step_requested = max(float(getattr(cfg, "fast_coarse_step_ms", 0.5)), fine_step)
    coarse_step_effective = coarse_step_requested
    coarse_n = fine_n
    coarse_total = exhaustive_total
    refine_estimate = 0
    estimated_total = exhaustive_total
    safety_adjusted = False

    if mode == "FAST_2_STAGE":
        coarse_step_effective = _effective_fast_coarse_step(cfg, fine_step)
        safety_adjusted = abs(coarse_step_effective - coarse_step_requested) > 1e-9
        coarse_n, coarse_total = _coarse_total_for_step(cfg, coarse_step_effective)
        seed_n = max(int(getattr(cfg, "fast_seed_n", 80)), int(getattr(cfg, "top_n", 20)) * 2, 20)
        radius = max(float(getattr(cfg, "fast_refine_radius_ms", 0.6)), coarse_step_effective, fine_step * 3.0)
        approx_points_per_axis = int((2.0 * radius) / fine_step) + 1
        refine_estimate = min(seed_n * (approx_points_per_axis ** 2), exhaustive_total)
        estimated_total = coarse_total + refine_estimate

    if estimated_total < 30_000:
        load_label = "lekki"
    elif estimated_total < 150_000:
        load_label = "średni"
    elif estimated_total < 500_000:
        load_label = "ciężki"
    else:
        load_label = "bardzo ciężki"

    return {
        "mode": mode,
        "fine_step_ms": fine_step,
        "fine_delay_points": fine_n,
        "exhaustive_total": exhaustive_total,
        "coarse_step_requested_ms": coarse_step_requested,
        "coarse_step_effective_ms": coarse_step_effective,
        "coarse_delay_points": coarse_n,
        "coarse_total": coarse_total,
        "refine_estimate": refine_estimate,
        "estimated_total": estimated_total,
        "load_label": load_label,
        "coarse_safety_adjusted": safety_adjusted,
        "fast_auto_coarse_safety": bool(getattr(cfg, "fast_auto_coarse_safety", True)),
        "fast_max_coarse_combinations": int(getattr(cfg, "fast_max_coarse_combinations", 6_000)),
    }



def optimizer_mode_summary(cfg: OptimizerConfig) -> str:
    """v19.44: krótka informacja o sposobie liczenia."""
    info = optimizer_workload_info(cfg)
    if str(info.get("mode", "")).upper() == "FAST_2_STAGE":
        return "Sposób liczenia: szybki."
    return "Sposób liczenia: dokładny."


def _keep_candidate_heap(heap: List[Tuple[float, int, Candidate]], cand: Candidate, keep_n: int, counter: int) -> None:
    """Utrzymuje pulę najlepszych bez sortowania całej listy po każdej kombinacji."""
    item = (float(cand.score), int(counter), cand)
    if len(heap) < keep_n:
        heapq.heappush(heap, item)
    elif item[0] > heap[0][0]:
        heapq.heapreplace(heap, item)


def _candidate_key_tuple(ld: float, linv: bool, rd: float, rinv: bool) -> Tuple[float, bool, float, bool]:
    return (round(float(ld), 6), bool(linv), round(float(rd), 6), bool(rinv))


def _family_key_from_flags(linv: bool, rinv: bool) -> str:
    if (not linv) and (not rinv):
        return "OFF/OFF"
    if linv and rinv:
        return "INV/INV"
    if (not linv) and rinv:
        return "MIXED L_OFF/R_INV"
    return "MIXED L_INV/R_OFF"


def _register_family_best(family_best: Dict[str, Candidate], cand: Candidate) -> None:
    fam = polarity_family(cand)
    old = family_best.get(fam)
    if old is None or float(cand.score) > float(old.score):
        family_best[fam] = cand


def _select_diverse_seed_candidates(
    heap: List[Tuple[float, int, Candidate]],
    limit: int,
    min_spacing_ms: float,
) -> List[Candidate]:
    """Wybiera dobre, ale niepowtarzające się centra dogęszczenia FAST.

    Top rankingu zgrubnego często zawiera dziesiątki niemal identycznych punktów.
    Dogęszczanie każdego z nich marnuje czas i nie poprawia pokrycia mapy. Zachowujemy
    najwyżej ocenione centra rozłożone po obszarze delayów, osobno dla każdej rodziny.
    """
    if not heap or limit <= 0:
        return []
    spacing = max(float(min_spacing_ms), 0.0)
    ordered = [item[2] for item in heapq.nlargest(len(heap), heap, key=lambda x: x[0])]
    selected: List[Candidate] = []
    for cand in ordered:
        fam = polarity_family(cand)
        too_close = any(
            polarity_family(prev) == fam
            and max(abs(float(prev.l_delay_ms) - float(cand.l_delay_ms)), abs(float(prev.r_delay_ms) - float(cand.r_delay_ms))) < spacing - 1e-9
            for prev in selected
        )
        if too_close:
            continue
        selected.append(cand)
        if len(selected) >= int(limit):
            break
    # Jeżeli mapa jest mała, dobierz brakujące najlepsze punkty bez wymogu odstępu.
    if len(selected) < int(limit):
        for cand in ordered:
            if not any(same_candidate(cand, prev) for prev in selected):
                selected.append(cand)
                if len(selected) >= int(limit):
                    break
    return selected


def optimize_delays(
    l_main_m: Measurement,
    l_sub_m: Measurement,
    r_main_m: Measurement,
    r_sub_m: Measurement,
    cfg: OptimizerConfig,
    progress_callback=None,
    stop_event=None,
) -> Tuple[np.ndarray, Dict[str, np.ndarray], List[Candidate]]:
    """
    Optymalizacja delay/polarity.

    v19.44 FAST 2-STAGE OFF/OFF:
    1. szeroki skan zgrubny OFF/OFF,
    2. wybór najlepszych obszarów OFF/OFF,
    3. dogęszczenie tylko wokół najlepszych kandydatów do finalnego delay_step_ms,
    4. utrzymywanie puli najlepszych przez heapq, bez sortowania całej listy po każdej kombinacji.

    Dla małych zadań funkcja automatycznie zostaje w trybie EXACT, żeby wynik self-testu i małe skany
    były w pełni deterministyczne i bez przybliżeń.
    """
    def _check_cancelled():
        if stop_event is not None and stop_event.is_set():
            raise CalculationCancelled("Przeliczanie zatrzymane przez użytkownika.")

    _check_cancelled()

    measurements = [l_main_m, l_sub_m, r_main_m, r_sub_m]
    freq = common_grid(measurements, cfg.freq_points)

    responses = {
        "L_main": measurement_to_complex(l_main_m, freq),
        "L_sub": measurement_to_complex(l_sub_m, freq),
        "R_main": measurement_to_complex(r_main_m, freq),
        "R_sub": measurement_to_complex(r_sub_m, freq),
    }

    l_main = responses["L_main"]
    l_sub = responses["L_sub"]
    r_main = responses["R_main"]
    r_sub = responses["R_sub"]

    fine_step = max(float(cfg.delay_step_ms), 0.001)
    fine_delays = _delay_grid(cfg.delay_min_ms, cfg.delay_max_ms, fine_step)
    relative_shd_mode = bool(getattr(cfg, "relative_delay_shd_mode", False))
    shd_delay_limit_ms = float(getattr(cfg, "shd_delay_limit_ms", 30.0))

    def _pair_allowed_by_shd(ld: float, rd: float) -> bool:
        if not relative_shd_mode:
            return True
        base_l_rel, base_r_rel = baseline_relative_delays(cfg)
        return relative_delay_pair_fits_shd(base_l_rel + float(ld), base_r_rel + float(rd), shd_delay_limit_ms)
    # v19.44: domyślnie automatyczny workflow SHD liczy wyłącznie OFF/OFF.
    # v19.58: osobny moduł diagnostyczny może włączyć pełny skan polaryzacji.
    polarity_scan_mode = str(getattr(cfg, "polarity_scan_mode", "OFF_ONLY") or "OFF_ONLY").strip().upper()
    if polarity_scan_mode in {"ALL", "ALL_FAMILIES", "POLARITY_SCAN", "DIAGNOSTIC"}:
        polarities = [False, True]
    else:
        polarities = [False]
    exhaustive_total = len(fine_delays) * len(fine_delays) * len(polarities) * len(polarities)
    if exhaustive_total > cfg.max_cartesian and not bool(getattr(cfg, "fast_two_stage", True)):
        raise ValueError(
            f"Za dużo kombinacji: {exhaustive_total}. Zwiększ krok lub zmniejsz zakres. Limit: {cfg.max_cartesian}."
        )

    keep_n = max(int(getattr(cfg, "analysis_pool_n", 1000)), cfg.top_n * 20, 300)
    # Dla heapu trzymamy trochę szerszą pulę, bo PBIS plateau/Safe mogą zmienić kolejność po adnotacji.
    heap_keep_n = max(keep_n * 2, 1200)
    heap: List[Tuple[float, int, Candidate]] = []
    # v19.44: osobna pula dla każdej rodziny polaryzacji. To chroni OFF/OFF przed
    # zniknięciem z dalszej analizy, gdy inna rodzina dominuje globalny ranking.
    family_heap_keep_n = max(keep_n, 1200)
    family_heaps: Dict[str, List[Tuple[float, int, Candidate]]] = {}
    family_best: Dict[str, Candidate] = {}
    evaluated: set = set()
    counter = 0
    done = 0

    mode = _scan_mode_label(cfg, exhaustive_total)
    if mode == "EXACT":
        coarse_delays = fine_delays
        refine_jobs: List[Tuple[float, bool, float, bool]] = []
        coarse_total = exhaustive_total
        refine_total = 0
    else:
        coarse_step = _effective_fast_coarse_step(cfg, fine_step)
        coarse_delays = _delay_grid(cfg.delay_min_ms, cfg.delay_max_ms, coarse_step)
        coarse_total = len(coarse_delays) * len(coarse_delays) * len(polarities) * len(polarities)
        seed_n = max(int(getattr(cfg, "fast_seed_n", 80)), cfg.top_n * 2, 20)
        # Orientacyjny total dla paska postępu. Finalna liczba refine może być mniejsza dzięki deduplikacji.
        radius = max(float(getattr(cfg, "fast_refine_radius_ms", 0.6)), coarse_step, fine_step * 3.0)
        approx_points_per_axis = int((2.0 * radius) / fine_step) + 1
        refine_total = min(seed_n * (approx_points_per_axis ** 2), exhaustive_total)

    total_for_progress = max(1, coarse_total + refine_total)

    def _precompute_variants(delays: np.ndarray, sub_response: np.ndarray) -> List[Tuple[float, bool, np.ndarray]]:
        variants: List[Tuple[float, bool, np.ndarray]] = []
        for d in delays:
            _check_cancelled()
            for inv in polarities:
                variants.append((float(d), bool(inv), apply_delay_and_polarity(sub_response, freq, float(d), bool(inv))))
        return variants

    def _score_and_store(ld: float, linv: bool, lsub_s: np.ndarray, rd: float, rinv: bool, rsub_s: np.ndarray) -> Optional[Candidate]:
        nonlocal counter, done
        key = _candidate_key_tuple(ld, linv, rd, rinv)
        if key in evaluated:
            return None
        evaluated.add(key)
        # done oznacza liczbę rozpatrzonych par, także odrzuconych przez limit SHD.
        # Dzięki temu pasek postępu nie zatrzymuje się poniżej 100%.
        done += 1
        if done % 250 == 0:
            _check_cancelled()
        # v19.47: w rankingu RAW ld/rd mogą być ujemną relacją sub - main.
        # Odrzucamy pary, których nie da się wpisać do SHD po dodaniu wspólnego
        # delayu obu mainów i z zachowaniem limitu wszystkich czterech wyjść.
        if not _pair_allowed_by_shd(ld, rd):
            return None
        score, metrics = score_candidate(freq, l_main, lsub_s, r_main, rsub_s, cfg)
        cand = _make_candidate_from_metrics(ld, linv, rd, rinv, score, metrics)
        _register_family_best(family_best, cand)
        fam = polarity_family(cand)
        _keep_candidate_heap(family_heaps.setdefault(fam, []), cand, family_heap_keep_n, counter)
        _keep_candidate_heap(heap, cand, heap_keep_n, counter)
        counter += 1
        return cand

    # ETAP 1: szeroki skan zgrubny albo pełny skan EXACT dla małych zadań.
    l_variants = _precompute_variants(coarse_delays, l_sub)
    r_variants = _precompute_variants(coarse_delays, r_sub)
    for i, (ld, linv, lsub_s) in enumerate(l_variants):
        _check_cancelled()
        for rd, rinv, rsub_s in r_variants:
            _score_and_store(ld, linv, lsub_s, rd, rinv, rsub_s)
        if progress_callback and (i % 10 == 0 or i == len(l_variants) - 1):
            progress_callback(min(done, total_for_progress), total_for_progress)

    # ETAP 2/3: dla dużych zadań dogęszczamy najlepsze okolice.
    if mode == "FAST_2_STAGE":
        requested_seed_n = max(int(getattr(cfg, "fast_seed_n", 80)), cfg.top_n * 2, 20)
        effective_coarse_step = _effective_fast_coarse_step(cfg, fine_step)
        radius = max(float(getattr(cfg, "fast_refine_radius_ms", 0.6)), effective_coarse_step, fine_step * 3.0)
        spacing = max(effective_coarse_step * 1.5, fine_step * 3.0)

        # v19.44: mniej, ale sensowniej rozmieszczonych centrów. OFF/OFF otrzymuje
        # najszersze pokrycie, a inne rodziny pozostają diagnostyczne.
        global_seed_n = max(10, min(18, requested_seed_n // 3))
        coarse_best = _select_diverse_seed_candidates(heap, global_seed_n, spacing)
        for fam, fam_heap in family_heaps.items():
            if fam == "OFF/OFF":
                fam_seed_n = max(20, min(32, requested_seed_n // 2))
            else:
                fam_seed_n = max(4, min(8, requested_seed_n // 10))
            fam_seeds = _select_diverse_seed_candidates(fam_heap, fam_seed_n, spacing)
            for fam_cand in fam_seeds:
                if not any(same_candidate(fam_cand, c) for c in coarse_best):
                    coarse_best.append(fam_cand)
        refine_combo_keys = set()
        for seed in coarse_best:
            l_lo = max(float(cfg.delay_min_ms), float(seed.l_delay_ms) - radius)
            l_hi = min(float(cfg.delay_max_ms), float(seed.l_delay_ms) + radius)
            r_lo = max(float(cfg.delay_min_ms), float(seed.r_delay_ms) - radius)
            r_hi = min(float(cfg.delay_max_ms), float(seed.r_delay_ms) + radius)
            # v19.48: lokalne dogęszczenie wybiera punkty z GLOBALNEJ siatki fine.
            # Wcześniej _delay_grid(l_lo, l_hi, fine_step) kotwiczył siatkę w
            # lokalnym l_lo/r_lo. Gdy krok coarse został automatycznie podniesiony
            # np. przez mnożnik 1.25, raport mógł zwrócić wartości typu
            # 18.157812 ms mimo deklarowanego kroku 0.1 ms. Relacja była poprawna,
            # ale wartości były niepotrzebnie trudne do wpisania i porównania.
            # Teraz każdy finalny kandydat pozostaje na regularnej siatce
            # delay_min_ms + N * fine_step.
            l_grid = fine_delays[(fine_delays >= l_lo - 1e-9) & (fine_delays <= l_hi + 1e-9)]
            r_grid = fine_delays[(fine_delays >= r_lo - 1e-9) & (fine_delays <= r_hi + 1e-9)]
            if len(l_grid) == 0:
                l_grid = np.asarray([fine_delays[int(np.argmin(np.abs(fine_delays - float(seed.l_delay_ms))))]])
            if len(r_grid) == 0:
                r_grid = np.asarray([fine_delays[int(np.argmin(np.abs(fine_delays - float(seed.r_delay_ms))))]])
            # Dogęszczamy głównie tę samą rodzinę polaryzacji co seed; dodajemy też rodziny rodzinne z coarse_best.
            seed_linv = seed.l_polarity == "INV"
            seed_rinv = seed.r_polarity == "INV"
            for ld in l_grid:
                for rd in r_grid:
                    refine_combo_keys.add(_candidate_key_tuple(float(ld), seed_linv, float(rd), seed_rinv))

        # Precompute dla unikalnych delay/polarity użytych w refine.
        l_need = sorted({(ld, linv) for (ld, linv, _rd, _rinv) in refine_combo_keys})
        r_need = sorted({(rd, rinv) for (_ld, _linv, rd, rinv) in refine_combo_keys})
        l_lookup: Dict[Tuple[float, bool], np.ndarray] = {}
        r_lookup: Dict[Tuple[float, bool], np.ndarray] = {}
        for ld, linv in l_need:
            l_lookup[(ld, linv)] = apply_delay_and_polarity(l_sub, freq, float(ld), bool(linv))
        for rd, rinv in r_need:
            r_lookup[(rd, rinv)] = apply_delay_and_polarity(r_sub, freq, float(rd), bool(rinv))

        refine_list = sorted(refine_combo_keys)
        # Korygujemy maksimum paska postępu po deduplikacji, żeby nie wisiał na 70–90%.
        total_for_progress = max(1, done + len([k for k in refine_list if k not in evaluated]))
        for idx, (ld, linv, rd, rinv) in enumerate(refine_list):
            _check_cancelled()
            lsub_s = l_lookup[(ld, linv)]
            rsub_s = r_lookup[(rd, rinv)]
            _score_and_store(ld, linv, lsub_s, rd, rinv, rsub_s)
            if progress_callback and (idx % 250 == 0 or idx == len(refine_list) - 1):
                progress_callback(min(done, total_for_progress), total_for_progress)

    # Koniec: dopiero teraz sortujemy pule, czyli raz, a nie po każdej kombinacji.
    # Łączymy ranking globalny z pulami rodzin. Dzięki temu plateau i PBIS Safe dla
    # OFF/OFF są liczone z reprezentatywnego sąsiedztwa, nawet gdy globalnie wygrywa INV.
    merged: Dict[Tuple[float, str, float, str], Candidate] = {}
    def _merge_cand(c: Candidate) -> None:
        key = (round(float(c.l_delay_ms), 6), str(c.l_polarity), round(float(c.r_delay_ms), 6), str(c.r_polarity))
        old = merged.get(key)
        if old is None or float(c.score) > float(old.score):
            merged[key] = c

    global_take_n = max(cfg.top_n, min(int(getattr(cfg, "analysis_pool_n", 1000)), 300))
    for item in heapq.nlargest(min(len(heap), global_take_n), heap, key=lambda x: x[0]):
        _merge_cand(item[2])
    family_take_n = max(cfg.top_n * 10, min(int(getattr(cfg, "analysis_pool_n", 1000)), 1200))
    for fam_heap in family_heaps.values():
        for item in heapq.nlargest(min(len(fam_heap), family_take_n), fam_heap, key=lambda x: x[0]):
            _merge_cand(item[2])
    for special in family_best.values():
        if special is not None:
            _merge_cand(special)

    result = list(merged.values())
    annotate_pbis_plateau(result, cfg)
    annotate_sound_quality_safe(result, cfg)
    result.sort(key=lambda x: x.score, reverse=True)
    return freq, responses, result


def candidate_to_dict(
    c: Candidate,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    crossover_label: str = "Custom",
    filter_type: str = "BW",
    slope_db_oct: int = 48,
    cfg: Optional[OptimizerConfig] = None,
) -> Dict[str, object]:
    mapped = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
    l_final = float(mapped["l_sub_ms"])
    r_final = float(mapped["r_sub_ms"])
    xo = parse_crossover_hz(crossover_label, cfg)
    safety = physical_delay_safety(c, l_base_delay_ms, r_base_delay_ms, xo, cfg)
    return {
        "crossover": crossover_label,
        "filter_type": filter_type,
        "slope_db_oct": int(slope_db_oct),
        "profile": profile_label(crossover_label, filter_type, int(slope_db_oct)),
        "polarity_family": polarity_family(c),
        "score": round(c.score, 4),
        "PBIS_0_100": round(float(getattr(c, "pbis_score", 0.0)), 2),
        "PBIS_raw_0_100": round(float(getattr(c, "pbis_score_raw", getattr(c, "pbis_score", 0.0))), 2),
        "PBIS_rank_0_100": round(float(getattr(c, "pbis_rank_score", c.score)), 2),
        "PBIS_plateau_bonus": round(float(getattr(c, "pbis_plateau_bonus", 0.0)), 2),
        "PBIS_label": getattr(c, "pbis_label", ""),
        "PBIS_hole_risk": getattr(c, "pbis_hole_risk", ""),
        "PBIS_bulge_risk": getattr(c, "pbis_bulge_risk", ""),
        "PBIS_phase_risk": getattr(c, "pbis_phase_risk", ""),
        "PBIS_plateau": getattr(c, "pbis_plateau", ""),
        "PBIS_symmetry_risk": getattr(c, "pbis_symmetry_risk", ""),
        "PBIS_risk_summary": getattr(c, "pbis_risk_summary", ""),
        "PBIS_safe_0_100": round(float(getattr(c, "pbis_safe_score", 0.0)), 2),
        "PBIS_safe_label": getattr(c, "pbis_safe_label", ""),
        "PBIS_safe_penalty": round(float(getattr(c, "pbis_safe_penalty", 0.0)), 2),
        "Naturalnosc_basu": getattr(c, "naturalness_label", ""),
        "Sound_quality_flags": getattr(c, "sound_quality_flags", ""),

        # To są cztery wartości gotowe do wpisania w miniDSP SHD:
        "L_MAIN_delay_ms_for_miniDSP": round(float(mapped["l_main_ms"]), 4),
        "R_MAIN_delay_ms_for_miniDSP": round(float(mapped["r_main_ms"]), 4),
        "L_SUB_delay_ms_for_miniDSP": round(l_final, 4),
        "L_SUB_polarity_for_miniDSP": c.l_polarity,
        "R_SUB_delay_ms_for_miniDSP": round(r_final, 4),
        "R_SUB_polarity_for_miniDSP": c.r_polarity,
        "L_SUB_minus_MAIN_ms": round(float(mapped["l_sub_minus_main_ms"]), 4),
        "R_SUB_minus_MAIN_ms": round(float(mapped["r_sub_minus_main_ms"]), 4),
        # Nazwy starszych kolumn pozostają dla zgodności z wcześniejszymi skryptami.
        "L_FINAL_delay_ms_for_miniDSP": round(l_final, 4),
        "L_FINAL_polarity_for_miniDSP": c.l_polarity,
        "R_FINAL_delay_ms_for_miniDSP": round(r_final, 4),
        "R_FINAL_polarity_for_miniDSP": c.r_polarity,

        # v18.2: informacja o wariancie fazowo-cyklicznym delayu.
        "period_at_xo_ms": round(float(safety["period_ms"]), 4),
        "L_R_delay_difference_ms": round(float(safety["lr_diff_ms"]), 4),
        "delay_safety_status": safety["status"],
        "delay_safety_severity": int(safety["severity"]),
        "delay_safety_flags": " | ".join(str(x) for x in safety["flags"]),

        # Informacyjnie: fizyczna baza zapisana w pomiarze oraz zmiana wobec pliku.
        "baseline_L_MAIN_ms_in_input": round(float(mapped.get("baseline_l_main_ms", 0.0)), 4),
        "baseline_R_MAIN_ms_in_input": round(float(mapped.get("baseline_r_main_ms", 0.0)), 4),
        "baseline_L_SUB_ms_in_input": round(float(mapped.get("baseline_l_sub_ms", l_base_delay_ms)), 4),
        "baseline_R_SUB_ms_in_input": round(float(mapped.get("baseline_r_sub_ms", r_base_delay_ms)), 4),
        "L_additional_relation_change_ms_vs_input": round(float(c.l_delay_ms), 4),
        "R_additional_relation_change_ms_vs_input": round(float(c.r_delay_ms), 4),
        # Starsze nazwy kolumn zachowane dla kompatybilności.
        "L_base_delay_ms_from_file": round(float(mapped.get("baseline_l_sub_ms", l_base_delay_ms)), 4),
        "L_additional_delay_ms_calculated": round(c.l_delay_ms, 4),
        "R_base_delay_ms_from_file": round(float(mapped.get("baseline_r_sub_ms", r_base_delay_ms)), 4),
        "R_additional_delay_ms_calculated": round(c.r_delay_ms, 4),

        "L_construct_db": round(c.l_construct_db, 3),
        "R_construct_db": round(c.r_construct_db, 3),
        "LR_construct_db": round(c.lr_construct_db, 3),
        "LR_std_db": round(c.lr_std_db, 3),
        "LR_dip_db": round(c.lr_dip_db, 3),
        "Bulge_db": round(c.bulge_db, 3),
        "Bulge_peak_db": round(c.bulge_peak_db, 3),
        "LR_mean_db": round(c.lr_mean_db, 3),
    }


def same_candidate(a: Candidate, b: Candidate) -> bool:
    return (
        abs(a.l_delay_ms - b.l_delay_ms) < 1e-9
        and abs(a.r_delay_ms - b.r_delay_ms) < 1e-9
        and a.l_polarity == b.l_polarity
        and a.r_polarity == b.r_polarity
    )


def polarity_family(c: Candidate) -> str:
    """Rodzina polaryzacji używana w v18.4 do oddzielenia OFF/OFF, INV/INV i wariantów mieszanych."""
    if c.l_polarity == "OFF" and c.r_polarity == "OFF":
        return "OFF/OFF"
    if c.l_polarity == "INV" and c.r_polarity == "INV":
        return "INV/INV"
    if c.l_polarity == "OFF" and c.r_polarity == "INV":
        return "MIXED L_OFF/R_INV"
    if c.l_polarity == "INV" and c.r_polarity == "OFF":
        return "MIXED L_INV/R_OFF"
    return f"{c.l_polarity}/{c.r_polarity}"


def is_mixed_polarity(c: Candidate) -> bool:
    return c.l_polarity != c.r_polarity


def best_by_polarity_family(candidates: List[Candidate], family: str) -> Optional[Candidate]:
    items = [c for c in candidates if polarity_family(c) == family]
    if not items:
        return None
    return max(items, key=lambda c: c.score)


def best_mixed_polarity(candidates: List[Candidate]) -> Optional[Candidate]:
    items = [c for c in candidates if is_mixed_polarity(c)]
    if not items:
        return None
    return max(items, key=lambda c: c.score)


def family_loss_vs_best(c: Optional[Candidate], best: Optional[Candidate]) -> Optional[float]:
    if c is None or best is None:
        return None
    return best.score - c.score


def polarity_family_candidates(candidates: List[Candidate], family: str) -> List[Candidate]:
    return [c for c in candidates if polarity_family(c) == family]



def polarity_family_summary_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
    filter_type: str = "BW",
    slope_db_oct: int = 48,
) -> str:
    """v19.44: prosty opis wariantu OFF/OFF do workflow użytkownika."""
    off = best_both_off(candidates)
    if off is None:
        return "Nie znaleziono wariantu OFF/OFF."
    return "\n".join([
        "WARIANT OFF/OFF",
        *shd_settings_lines(off, l_base_delay_ms, r_base_delay_ms, cfg, indent=""),
        f"Ocena: {float(getattr(off, 'pbis_safe_score', getattr(off, 'pbis_score', 0.0))):.1f}/100",
        "To jest główny wariant do wpisania w SHD i sprawdzenia pomiarem REW.",
    ])


def best_both_off(candidates: List[Candidate]) -> Optional[Candidate]:
    offs = [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"]
    if not offs:
        return None
    return max(offs, key=lambda c: c.score)


def best_both_inv(candidates: List[Candidate]) -> Optional[Candidate]:
    invs = [c for c in candidates if c.l_polarity == "INV" and c.r_polarity == "INV"]
    if not invs:
        return None
    return max(invs, key=lambda c: c.score)



def off_off_priority_main_candidate(candidates: List[Candidate]) -> Tuple[Optional[Candidate], str, str]:
    """v19.44: operacyjnie pokazujemy wyłącznie najlepszy wariant OFF/OFF.

    Obliczenia nadal mogą analizować inne ustawienia, ale nie są one proponowane
    użytkownikowi w prostym workflow SHD.
    """
    if not candidates:
        return None, "NO_CANDIDATES", "Brak wyniku. Uruchom optymalizację."
    off = best_both_off(candidates)
    if off is None:
        return None, "NO_OFF_OFF", (
            "Nie znaleziono poprawnego wariantu OFF/OFF. Sprawdź dane wejściowe i zakres opóźnień."
        )
    return off, "OFF_OFF", "Do pierwszego pomiaru użyj ustawienia OFF/OFF."




def off_off_priority_summary_block(
    candidates: List[Candidate],
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    cfg: Optional[OptimizerConfig] = None,
) -> str:
    """Krótki opis głównego wariantu OFF/OFF z pełnym zestawem czterech delayów SHD."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    if best is None:
        return note
    if cfg is None:
        cfg = OptimizerConfig()
    return "\n".join([
        "GŁÓWNY WARIANT: OFF/OFF",
        *shd_settings_lines(best, l_base_delay_ms, r_base_delay_ms, cfg, indent=""),
        note,
    ])



def off_off_vs_inv_inv_assessment(
    candidates: List[Candidate],
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    crossover_label: str = "Custom",
    filter_type: str = "BW",
    slope_db_oct: int = 48,
    cfg: Optional[OptimizerConfig] = None,
) -> str:
    """Porównuje OFF/OFF z INV/INV, zachowując pełny model czterech delayów SHD."""
    off = best_both_off(candidates)
    inv = best_both_inv(candidates)
    lines: List[str] = []
    lines.append(f"PORÓWNANIE OFF/OFF vs INV/INV ({profile_label(crossover_label, filter_type, int(slope_db_oct))}):")

    if off is None:
        lines.append("Brak kandydata OFF/OFF w wynikach.")
        return "\n".join(lines)
    if inv is None:
        lines.append("Brak kandydata INV/INV w wynikach.")
        lines.append("OFF/OFF można ocenić tylko względem najlepszego ogólnego wyniku.")
        return "\n".join(lines)

    if cfg is None:
        cfg = OptimizerConfig()
    off_m = candidate_shd_settings(off, l_base_delay_ms, r_base_delay_ms, cfg)
    inv_m = candidate_shd_settings(inv, l_base_delay_ms, r_base_delay_ms, cfg)
    gap = inv.score - off.score
    off_lr_adv = off.lr_construct_db - inv.lr_construct_db
    off_dip_adv = inv.lr_dip_db - off.lr_dip_db
    off_bulge_adv = inv.bulge_db - off.bulge_db

    lines.append(
        f"OFF/OFF: main {float(off_m['l_main_ms']):.2f}/{float(off_m['r_main_ms']):.2f} ms | "
        f"sub {float(off_m['l_sub_ms']):.2f}/{float(off_m['r_sub_ms']):.2f} ms OFF/OFF | "
        f"rel. {float(off_m['l_sub_minus_main_ms']):+.2f}/{float(off_m['r_sub_minus_main_ms']):+.2f} ms | score {off.score:.3f}"
    )
    lines.append(
        f"INV/INV: main {float(inv_m['l_main_ms']):.2f}/{float(inv_m['r_main_ms']):.2f} ms | "
        f"sub {float(inv_m['l_sub_ms']):.2f}/{float(inv_m['r_sub_ms']):.2f} ms INV/INV | "
        f"rel. {float(inv_m['l_sub_minus_main_ms']):+.2f}/{float(inv_m['r_sub_minus_main_ms']):+.2f} ms | score {inv.score:.3f}"
    )
    lines.append(f"Różnica score INV/INV - OFF/OFF: {gap:+.3f}")
    lines.append(
        f"Różnice metryk OFF/OFF względem INV/INV: "
        f"L+R construct {off_lr_adv:+.2f} dB, "
        f"dip benefit {off_dip_adv:+.2f} dB, "
        f"bulge benefit {off_bulge_adv:+.2f} dB"
    )

    if gap <= -0.30:
        verdict = "OFF/OFF WYGRYWA z INV/INV — ma pełny sens jako kandydat główny."
    elif abs(gap) < 0.30:
        verdict = "OFF/OFF jest praktycznie remisowy z INV/INV — ma sens, szczególnie jeśli daje naturalniejszą scenę i wokal."
    elif gap < 1.00:
        verdict = "OFF/OFF jest blisko INV/INV — ma sens jako wariant do realnego pomiaru i odsłuchu."
    elif gap < 2.00:
        verdict = "OFF/OFF jest wyraźnie słabszy od INV/INV — mierz go tylko jako kontrolę naturalności/sceny."
    else:
        verdict = "OFF/OFF jest znacznie słabszy od INV/INV — nie traktuj go jako ustawienia głównego bez potwierdzenia pomiarem i odsłuchem."
    lines.append(verdict)
    return "\n".join(lines)


def build_input_warnings(
    paths: Dict[str, str],
    measurements: Dict[str, Measurement],
    cfg: OptimizerConfig,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
) -> List[str]:
    """
    Proste sanity-checki wejścia. Nie blokują pracy, ale ostrzegają przed typowymi
    błędami: wczytaniem sumy zamiast samego suba, pomiarem suba z innym bazowym delayem,
    zbyt małym zakresem częstotliwości albo pasmem oceny poza wspólnym zakresem danych.
    """
    warnings: List[str] = []

    def stem(key: str) -> str:
        return Path(paths.get(key, "")).stem.lower().replace(",", ".")

    # Typowe błędy nazewnictwa / wczytania
    for key in ["L sub", "R sub"]:
        s = stem(key)
        if "sum" in s:
            warnings.append(f"{key}: nazwa pliku zawiera 'sum'. Do pola {key} powinien trafić sam sub, nie main+sub.")
        if "main" in s and "sub" not in s:
            warnings.append(f"{key}: nazwa wygląda jak main, sprawdź czy nie wczytano złego pliku.")

    for key in ["L main", "R main"]:
        s = stem(key)
        if "sub" in s:
            warnings.append(f"{key}: nazwa pliku zawiera 'sub'. Do pola {key} powinien trafić sam main/C1.")
        if "sum" in s:
            warnings.append(f"{key}: nazwa pliku zawiera 'sum'. Do pola {key} powinien trafić sam main/C1, nie suma.")

    # Delay z nazwy vs wpisany bazowy
    inferred_l = infer_base_delay_from_filename(paths.get("L sub", ""))
    inferred_r = infer_base_delay_from_filename(paths.get("R sub", ""))
    if abs(inferred_l - l_base_delay_ms) > 0.051:
        warnings.append(
            f"L sub: z nazwy pliku wynika baza {inferred_l:.2f} ms, a wpisano {l_base_delay_ms:.2f} ms."
        )
    if abs(inferred_r - r_base_delay_ms) > 0.051:
        warnings.append(
            f"R sub: z nazwy pliku wynika baza {inferred_r:.2f} ms, a wpisano {r_base_delay_ms:.2f} ms."
        )

    # Wspólny zakres danych
    f_min = max(float(np.nanmin(m.freq)) for m in measurements.values())
    f_max = min(float(np.nanmax(m.freq)) for m in measurements.values())
    needed_low = min(cfg.eval_low_hz, cfg.bulge_low_hz)
    needed_high = max(cfg.eval_high_hz, cfg.bulge_high_hz)
    if f_min > needed_low or f_max < needed_high:
        warnings.append(
            f"Wspólny zakres danych to {f_min:.1f}–{f_max:.1f} Hz, a ustawienia wymagają ok. {needed_low:.1f}–{needed_high:.1f} Hz."
        )

    # Liczba punktów w paśmie oceny
    for key, m in measurements.items():
        n_eval = int(np.sum((m.freq >= cfg.eval_low_hz) & (m.freq <= cfg.eval_high_hz)))
        if n_eval < 20:
            warnings.append(f"{key}: mało punktów w paśmie oceny ({n_eval}). Wynik może być mniej stabilny.")

    # Skan / wydajność / rozdzielczość
    delays = np.arange(cfg.delay_min_ms, cfg.delay_max_ms + cfg.delay_step_ms / 2.0, cfg.delay_step_ms)
    combos = len(delays) * len(delays)
    if combos > 1_000_000:
        warnings.append(f"Skan ma {combos:,} kombinacji. Może liczyć się długo. Rozważ krok 0,2 ms albo mniejszy zakres.")
    if cfg.delay_step_ms > 0.25:
        warnings.append("Krok delayu jest dość duży. Do finalnego wyboru użyj 0,1 ms lub drobniejszego skanu wokół faworyta.")
    if cfg.delay_step_ms < 0.05:
        warnings.append("Krok delayu jest bardzo mały. Wynik może nie być realnie dokładniejszy od pomiaru, a liczenie będzie wolne.")

    return warnings


def confidence_assessment(candidates: List[Candidate]) -> Tuple[str, List[str]]:
    """
    Szacunkowa pewność wyniku. To nie jest pomiar odsłuchu, tylko kontrola:
    czy najlepszy wynik wyraźnie wygrywa i czy nie ma czerwonych flag.
    """
    if not candidates:
        return "brak", ["Brak kandydatów."]

    best = candidates[0]
    reasons: List[str] = []
    level_score = 2  # 0 niska, 1 średnia, 2 wysoka

    if len(candidates) > 1:
        margin = best.score - candidates[1].score
        if margin < 0.20:
            level_score = min(level_score, 0)
            reasons.append(f"Bardzo mała przewaga najlepszego wyniku nad #2: {margin:.2f}. Zmierz realnie top 3.")
        elif margin < 0.60:
            level_score = min(level_score, 1)
            reasons.append(f"Niewielka przewaga najlepszego wyniku nad #2: {margin:.2f}. Warto porównać top 2.")
        else:
            reasons.append(f"Wyraźna przewaga najlepszego wyniku nad #2: {margin:.2f}.")
    else:
        level_score = min(level_score, 1)
        reasons.append("Tylko jeden kandydat w wynikach.")

    if best.lr_dip_db > 9.0:
        level_score = min(level_score, 0)
        reasons.append(f"Duży dołek L+R w paśmie oceny: {best.lr_dip_db:.1f} dB.")
    elif best.lr_dip_db > 6.0:
        level_score = min(level_score, 1)
        reasons.append(f"Umiarkowany dołek L+R w paśmie oceny: {best.lr_dip_db:.1f} dB.")

    if best.bulge_db > 1.5 or best.bulge_peak_db > 2.5:
        level_score = min(level_score, 1)
        reasons.append(f"Ryzyko bulwy: bulge {best.bulge_db:.1f} dB, peak {best.bulge_peak_db:.1f} dB.")

    off = best_both_off(candidates)
    if off is not None and not same_candidate(off, best):
        diff = best.score - off.score
        if diff < 0.50:
            reasons.append(f"Wariant OFF/OFF jest bardzo blisko najlepszego wyniku (różnica {diff:.2f}). Koniecznie porównaj odsłuchowo/pomiarem.")
        elif diff < 1.20:
            reasons.append(f"Wariant OFF/OFF jest blisko najlepszego wyniku (różnica {diff:.2f}). Warto go zmierzyć.")

    inv = best_both_inv(candidates)
    if off is not None and inv is not None:
        gap = inv.score - off.score  # dodatni = INV/INV wyżej; ujemny = OFF/OFF wyżej
        if gap <= -0.30:
            reasons.append(f"OFF/OFF wygrywa z INV/INV (różnica INV/OFF {gap:+.2f}).")
        elif abs(gap) < 0.30:
            reasons.append(f"OFF/OFF jest praktycznie na poziomie INV/INV (różnica INV/OFF {gap:+.2f}).")
        elif gap < 1.00:
            reasons.append(f"OFF/OFF jest blisko INV/INV (różnica INV/OFF {gap:+.2f}). Warto realnie zmierzyć oba.")
        else:
            reasons.append(f"OFF/OFF jest wyraźnie za INV/INV (różnica INV/OFF {gap:+.2f}).")

    label = ["niska", "średnia", "wysoka"][level_score]
    return label, reasons


def plateau_confidence_assessment(candidates: List[Candidate], cfg: OptimizerConfig) -> Tuple[str, List[str]]:
    """
    v19.17: osobna psychoakustyczna ocena plateau/stabilności.

    Nie tylko liczymy, czy #1 wygrał. Sprawdzamy, czy w okolicy jest sensowna
    rodzina podobnych rozwiązań. Jeżeli najlepszy punkt jest samotną igłą,
    aplikacja traktuje go ostrożniej, nawet jeśli ma formalnie najwyższy score.
    """
    if not candidates:
        return "brak", ["Brak kandydatów."]
    best = candidates[0]
    fam = polarity_family(best)
    tolerance = effective_score_tolerance(cfg)
    profile = normalize_scoring_profile(getattr(cfg, "scoring_profile", "PBIS"))

    same_family = [c for c in candidates if polarity_family(c) == fam and (best.score - c.score) <= tolerance + 1e-9]
    close_all = [c for c in candidates if (best.score - c.score) <= tolerance + 1e-9]
    very_close = [c for c in candidates if (best.score - c.score) <= 0.25 + 1e-9]

    if not same_family:
        return "niska", [f"Brak plateau w rodzinie {fam} w tolerancji {tolerance:.2f}."]

    l_vals = [c.l_delay_ms for c in same_family]
    r_vals = [c.r_delay_ms for c in same_family]
    score_losses = [best.score - c.score for c in same_family]
    max_loss = max(score_losses) if score_losses else 0.0
    l_span = max(l_vals) - min(l_vals) if l_vals else 0.0
    r_span = max(r_vals) - min(r_vals) if r_vals else 0.0

    # Stabilność: liczebność + geometryczny rozrzut delayów.
    level = 0
    if len(same_family) >= 20:
        level += 2
    elif len(same_family) >= 6:
        level += 1
    if max(l_span, r_span) >= max(0.2, 2.0 * cfg.delay_step_ms):
        level += 1
    if len(very_close) >= 4:
        level += 1
    if profile == "Technical":
        # W trybie Technical nie podbijamy wniosku psychoakustycznego.
        level = min(level, 2)

    if level >= 3:
        label = "wysoka"
    elif level >= 1:
        label = "średnia"
    else:
        label = "niska"

    reasons = [
        f"Rodzina best: {fam}. W tolerancji score {tolerance:.2f} znaleziono {len(same_family)} kandydatów z tej samej rodziny.",
        f"Wszystkie rodziny w tej tolerancji: {len(close_all)} kandydatów; bardzo blisko best (≤0.25): {len(very_close)}.",
        f"Rozrzut plateau: L ±{l_span/2:.2f} ms orientacyjnie, R ±{r_span/2:.2f} ms orientacyjnie; maks. strata w plateau {max_loss:.2f}.",
    ]
    if label == "wysoka":
        reasons.append("Wniosek: wynik nie jest samotną igłą; rodzina/plateau jest stabilna i różnice w jej obrębie mogą być mało znaczące.")
    elif label == "średnia":
        reasons.append("Wniosek: jest sensowny obszar rozwiązań, ale warto potwierdzić best i alternatywę A/B realnym pomiarem.")
    else:
        reasons.append("Wniosek: wynik jest bardziej punktowy; realna walidacja jest szczególnie ważna.")
    return label, reasons
