# -*- coding: utf-8 -*-
"""Kalibracja predykcji projektanta na rzeczywistych pomiarach REW.

Kalibracja wyniku i dokładne strojenie opóźnień są rozdzielone. Wyłączenie
korekty empirycznej nie wyłącza automatycznie dogęszczania opóźnień.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional
import json
import math


@dataclass(frozen=True)
class EmpiricalCalibration:
    enabled: bool = False
    schema_version: int = 2
    system_name: str = ""
    weight: float = 0.0
    custom_side_bias_l: float = 0.0
    custom_side_bias_r: float = 0.0
    custom_side_sigma_l: float = 0.0
    custom_side_sigma_r: float = 0.0
    custom_technical_bias: float = 0.0
    custom_technical_sigma: float = 0.0
    custom_validation_state_count: int = 0
    custom_independent_topology_count: int = 0
    standard_warning_only: bool = True
    standard_bw48_side_bias_l: float = 0.0
    standard_bw48_side_bias_r: float = 0.0
    standard_validation_state_count: int = 0
    standard_independent_topology_count: int = 0
    delay_refinement_enabled: bool = True
    delay_coarse_radius_ms: float = 0.60
    delay_coarse_step_ms: float = 0.10
    delay_fine_radius_ms: float = 0.15
    delay_fine_step_ms: float = 0.05
    delay_max_iterations: int = 6
    delay_improvement_epsilon: float = 0.02
    source_path: str = ""
    notes: str = ""

    @property
    def custom_sample_count(self) -> int:
        """Zgodność wsteczna: liczba stanów walidacyjnych, nie topologii."""
        return int(self.custom_validation_state_count)

    @property
    def standard_sample_count(self) -> int:
        return int(self.standard_validation_state_count)

    @property
    def confidence_label(self) -> str:
        if not self.enabled:
            return "WYŁĄCZONA"
        n = int(self.custom_independent_topology_count)
        if n >= 6:
            return "WYSOKA"
        if n >= 3:
            return "UMIARKOWANA"
        return "NISKA"

    @property
    def uncertainty_penalty(self) -> float:
        """Konserwatywna kara zależna od liczby niezależnych kształtów."""
        if not self.enabled:
            return 0.0
        sigma = max(0.0, float(self.custom_technical_sigma))
        n = int(self.custom_independent_topology_count)
        if n <= 1:
            sample_penalty = 1.8
        elif n == 2:
            sample_penalty = 1.1
        elif n < 6:
            sample_penalty = 0.55
        else:
            sample_penalty = 0.25
        return float(self.weight * sigma + sample_penalty)

    def with_score_calibration_enabled(self, enabled: bool) -> "EmpiricalCalibration":
        """Włącz/wyłącz tylko korektę punktową, zachowując ustawienia delayu."""
        return replace(self, enabled=bool(enabled))


def _f(data: Dict[str, Any], key: str, default: float) -> float:
    try:
        value = float(data.get(key, default))
    except Exception:
        return float(default)
    return value if math.isfinite(value) else float(default)


def _i(data: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(data.get(key, default))
    except Exception:
        return int(default)


def load_empirical_calibration(path: Optional[str | Path]) -> EmpiricalCalibration:
    if not path:
        return EmpiricalCalibration()
    p = Path(path)
    if not p.exists():
        return EmpiricalCalibration(source_path=str(p), notes="Brak pliku kalibracji — użyto czystej symulacji.")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return EmpiricalCalibration(source_path=str(p), notes=f"Nie udało się odczytać kalibracji: {exc}")

    custom = data.get("custom_biquad", {}) if isinstance(data.get("custom_biquad", {}), dict) else {}
    standard = data.get("standard_bw48", {}) if isinstance(data.get("standard_bw48", {}), dict) else {}
    delay = data.get("delay_refinement", {}) if isinstance(data.get("delay_refinement", {}), dict) else {}
    enabled = bool(data.get("enabled", True))
    weight = max(0.0, min(1.0, _f(data, "calibration_weight", 0.50)))

    custom_states = _i(custom, "validation_state_count", _i(custom, "sample_count", 0))
    custom_topologies = _i(custom, "independent_topology_count", 1 if custom_states > 0 else 0)
    standard_states = _i(standard, "validation_state_count", _i(standard, "sample_count", 0))
    standard_topologies = _i(standard, "independent_topology_count", 1 if standard_states > 0 else 0)

    return EmpiricalCalibration(
        enabled=enabled,
        schema_version=_i(data, "schema_version", 1),
        system_name=str(data.get("system_name", "")),
        weight=weight,
        custom_side_bias_l=_f(custom, "side_bias_l", 0.0),
        custom_side_bias_r=_f(custom, "side_bias_r", 0.0),
        custom_side_sigma_l=_f(custom, "side_sigma_l", 0.0),
        custom_side_sigma_r=_f(custom, "side_sigma_r", 0.0),
        custom_technical_bias=_f(custom, "technical_bias", 0.0),
        custom_technical_sigma=_f(custom, "technical_sigma", 0.0),
        custom_validation_state_count=max(0, custom_states),
        custom_independent_topology_count=max(0, custom_topologies),
        standard_warning_only=bool(standard.get("warning_only", True)),
        standard_bw48_side_bias_l=_f(standard, "side_bias_l", 0.0),
        standard_bw48_side_bias_r=_f(standard, "side_bias_r", 0.0),
        standard_validation_state_count=max(0, standard_states),
        standard_independent_topology_count=max(0, standard_topologies),
        delay_refinement_enabled=bool(delay.get("enabled", True)),
        delay_coarse_radius_ms=max(0.0, _f(delay, "coarse_radius_ms", 0.60)),
        delay_coarse_step_ms=max(0.01, _f(delay, "coarse_step_ms", 0.10)),
        delay_fine_radius_ms=max(0.0, _f(delay, "fine_radius_ms", 0.15)),
        delay_fine_step_ms=max(0.01, _f(delay, "fine_step_ms", 0.05)),
        delay_max_iterations=max(1, min(20, _i(delay, "max_iterations", 6))),
        delay_improvement_epsilon=max(0.0, _f(delay, "improvement_epsilon", 0.02)),
        source_path=str(p),
        notes=str(data.get("notes", "")),
    )


def write_calibration_summary(cal: EmpiricalCalibration, out_path: str | Path) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Kalibracja projektanta na realnych pomiarach",
        "",
        f"- Korekta wyniku: **{'WŁĄCZONA' if cal.enabled else 'WYŁĄCZONA'}**",
        f"- Pewność korekty: **{cal.confidence_label}**",
        f"- Waga korekty empirycznej: **{cal.weight:.2f}**",
        f"- Niezależne kształty filtru customowego: **{cal.custom_independent_topology_count}**",
        f"- Stany walidacyjne customowego filtru: **{cal.custom_validation_state_count}**",
        f"- Korekta sklejenia L/P: **{cal.custom_side_bias_l:+.2f}/{cal.custom_side_bias_r:+.2f} pkt**",
        f"- Rozrzut korekty L/P: **{cal.custom_side_sigma_l:.2f}/{cal.custom_side_sigma_r:.2f} pkt**",
        f"- Korekta integracji technicznej: **{cal.custom_technical_bias:+.2f} pkt**",
        f"- Kara niepewności: **{cal.uncertainty_penalty:.2f} pkt**",
        "",
        "## Dokładne strojenie opóźnień — osobna funkcja",
        "",
        f"- Status: **{'WŁĄCZONE' if cal.delay_refinement_enabled else 'WYŁĄCZONE'}**",
        f"- Skan zgrubny: ±{cal.delay_coarse_radius_ms:.2f} ms, krok {cal.delay_coarse_step_ms:.2f} ms",
        f"- Skan dokładny: ±{cal.delay_fine_radius_ms:.2f} ms, krok {cal.delay_fine_step_ms:.2f} ms",
        f"- Maksymalna liczba iteracji L/P: {cal.delay_max_iterations}",
        f"- Minimalna poprawa celu: {cal.delay_improvement_epsilon:.3f} pkt",
        "",
        "Wyłączenie korekty empirycznej nie wyłącza dogęszczania opóźnień. Są to dwie niezależne funkcje.",
        "",
        "## Znaczenie pewności",
        "",
        "Liczba powtórzeń tego samego filtru z innym delayem nie jest liczona jako kilka niezależnych topologii. Przy jednym niezależnym kształcie pewność pozostaje NISKA.",
        "",
        "## Standardowy BW48",
        "",
        f"- Niezależne topologie: **{cal.standard_independent_topology_count}**",
        f"- Stany walidacyjne: **{cal.standard_validation_state_count}**",
        f"- Zaobserwowana różnica predykcja–pomiar L/P: **{cal.standard_bw48_side_bias_l:+.2f}/{cal.standard_bw48_side_bias_r:+.2f} pkt**",
        f"- Tryb: **{'TYLKO OSTRZEŻENIE' if cal.standard_warning_only else 'KOREKTA PUNKTOWA'}**",
        "",
        "Jedna topologia standardowego BW48 nie wystarcza do przeskalowania całej rodziny BW/LR.",
    ]
    if cal.notes:
        lines += ["", "## Uwagi zapisane w pliku", "", cal.notes]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
