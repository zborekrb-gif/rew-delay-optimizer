#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Centralna, fail-closed walidacja danych pomiarowych REWDO v20.31."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np

from rew_common import Measurement


@dataclass(frozen=True)
class MeasurementValidationIssue:
    code: str
    message: str
    measurement: str = ""

    def describe(self) -> str:
        prefix = f"{self.measurement}: " if self.measurement else ""
        return prefix + self.message


@dataclass(frozen=True)
class MeasurementValidationResult:
    ok: bool
    issues: tuple[MeasurementValidationIssue, ...]

    @property
    def messages(self) -> list[str]:
        return [issue.describe() for issue in self.issues]


class MeasurementValidationError(ValueError):
    """Dane nie mogą wejść do obliczeń ani otrzymać statusu integralności."""

    def __init__(self, result: MeasurementValidationResult):
        self.result = result
        super().__init__("; ".join(result.messages[:8]) or "Walidacja pomiaru nie przeszła.")


def _array(value: Sequence[float] | np.ndarray) -> np.ndarray:
    return np.asarray(value, dtype=float)


def validate_numeric_response_arrays(
    freq: Sequence[float] | np.ndarray,
    spl_db: Sequence[float] | np.ndarray,
    phase_deg: Sequence[float] | np.ndarray,
    *,
    name: str = "",
    minimum_points: int = 16,
    required_range_hz: Optional[tuple[float, float]] = None,
) -> MeasurementValidationResult:
    """Sprawdź jeden ślad przed i po interpolacji/unwrap.

    Funkcja nie naprawia ani nie sortuje osi. Każda taka operacja ukrywałaby
    uszkodzenie źródła i mogłaby przypisać próbki do niewłaściwych częstotliwości.
    """
    issues: list[MeasurementValidationIssue] = []
    try:
        f = _array(freq)
        spl = _array(spl_db)
        phase = _array(phase_deg)
    except (TypeError, ValueError) as exc:
        issue = MeasurementValidationIssue("ARRAY_DECODE", f"nie można utworzyć tablic numerycznych: {exc}", name)
        return MeasurementValidationResult(False, (issue,))

    arrays = (("częstotliwość", f), ("SPL", spl), ("faza", phase))
    for label, values in arrays:
        if values.ndim != 1:
            issues.append(MeasurementValidationIssue("ARRAY_DIMENSION", f"tablica {label} nie jest jednowymiarowa", name))
    if any(values.ndim != 1 for _label, values in arrays):
        return MeasurementValidationResult(False, tuple(issues))

    lengths = {int(values.size) for _label, values in arrays}
    if len(lengths) != 1:
        issues.append(MeasurementValidationIssue(
            "LENGTH_MISMATCH",
            f"niespójne długości freq/SPL/phase: {f.size}/{spl.size}/{phase.size}",
            name,
        ))
    if min(lengths or {0}) < int(minimum_points):
        issues.append(MeasurementValidationIssue(
            "TOO_FEW_POINTS",
            f"za mało punktów: {min(lengths or {0})}, wymagane co najmniej {int(minimum_points)}",
            name,
        ))

    for label, values in arrays:
        if not np.all(np.isfinite(values)):
            issues.append(MeasurementValidationIssue("NON_FINITE", f"NaN/Inf w tablicy {label}", name))

    if f.size:
        if np.any(f <= 0.0):
            issues.append(MeasurementValidationIssue("NON_POSITIVE_FREQUENCY", "częstotliwości muszą być > 0 Hz", name))
        if f.size > 1 and np.all(np.isfinite(f)):
            delta = np.diff(f)
            if np.any(delta == 0.0):
                issues.append(MeasurementValidationIssue("DUPLICATE_FREQUENCY", "oś zawiera duplikaty częstotliwości", name))
            if np.any(delta < 0.0):
                issues.append(MeasurementValidationIssue("NON_MONOTONIC_AXIS", "oś częstotliwości nie jest ściśle rosnąca", name))

    if required_range_hz is not None and f.size and np.all(np.isfinite(f)):
        low, high = (float(required_range_hz[0]), float(required_range_hz[1]))
        if not low < high:
            issues.append(MeasurementValidationIssue("INVALID_REQUIRED_RANGE", "błędny wymagany zakres częstotliwości", name))
        elif float(f[0]) > low or float(f[-1]) < high:
            issues.append(MeasurementValidationIssue(
                "MISSING_REQUIRED_RANGE",
                f"zakres {float(f[0]):.3f}–{float(f[-1]):.3f} Hz nie pokrywa wymaganego {low:.3f}–{high:.3f} Hz",
                name,
            ))

    if phase.size and np.all(np.isfinite(phase)):
        unwrapped = np.unwrap(np.deg2rad(phase))
        if not np.all(np.isfinite(unwrapped)):
            issues.append(MeasurementValidationIssue("UNWRAP_NON_FINITE", "NaN/Inf po unwrap fazy", name))

    return MeasurementValidationResult(not issues, tuple(issues))


def validate_measurement_project(
    project_or_measurements: object,
    *,
    minimum_points: int = 16,
    required_range_hz: Optional[tuple[float, float]] = None,
    raise_on_error: bool = True,
) -> MeasurementValidationResult:
    """Jeden walidator dla projektów pochodzących z MDAT i REW API."""
    if hasattr(project_or_measurements, "measurements_by_role"):
        measurements_obj = getattr(project_or_measurements, "measurements_by_role")
    else:
        measurements_obj = project_or_measurements

    if isinstance(measurements_obj, Mapping):
        items: Iterable[tuple[str, object]] = ((str(key), value) for key, value in measurements_obj.items())
    else:
        try:
            items = ((str(getattr(item, "name", "pomiar")), item) for item in measurements_obj)  # type: ignore[union-attr]
        except TypeError:
            items = []

    issues: list[MeasurementValidationIssue] = []
    count = 0
    for fallback_name, item in items:
        count += 1
        measurement = getattr(item, "measurement", item)
        if not isinstance(measurement, Measurement) and not all(
            hasattr(measurement, field) for field in ("freq", "spl_db", "phase_deg")
        ):
            issues.append(MeasurementValidationIssue("INVALID_MEASUREMENT", "brak freq/SPL/phase", fallback_name))
            continue
        name = str(getattr(measurement, "name", fallback_name) or fallback_name)
        result = validate_numeric_response_arrays(
            getattr(measurement, "freq"),
            getattr(measurement, "spl_db"),
            getattr(measurement, "phase_deg"),
            name=name,
            minimum_points=minimum_points,
            required_range_hz=required_range_hz,
        )
        issues.extend(result.issues)

    if count == 0:
        issues.append(MeasurementValidationIssue("EMPTY_PROJECT", "projekt nie zawiera pomiarów"))
    result = MeasurementValidationResult(not issues, tuple(issues))
    if raise_on_error and not result.ok:
        raise MeasurementValidationError(result)
    return result

