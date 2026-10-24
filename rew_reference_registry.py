#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimalny rejestr i kontrola konfliktów referencji REWDO v20.31."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np


def _scalar(value: str) -> object:
    raw = str(value).split(" #", 1)[0].strip().strip('"').strip("'")
    low = raw.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if low in {"", "none", "null", "~"}:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return raw


def read_reference_fields(path: str | Path) -> dict[str, object]:
    fields: dict[str, object] = {}
    for raw_line in Path(path).read_text(encoding="utf-8", errors="strict").splitlines():
        if not raw_line or raw_line[0].isspace():
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = _scalar(value)
    return fields


@dataclass(frozen=True)
class ReferenceRecord:
    path: Path
    name: str
    crossover_hz: float
    filter_name: str
    l_main_ms: float
    r_main_ms: float
    l_sub_ms: float
    r_sub_ms: float
    verified: bool
    verification_status: str
    verified_by_measurement_sha256: Optional[str]
    verified_at: Optional[str]
    configuration_id: Optional[str] = None
    configuration_version: int = 1
    verification_scope: str = ""
    configuration_digest: str = ""

    @property
    def anchor_allowed(self) -> bool:
        status = self.verification_status.upper()
        return bool(
            self.verified
            and status == "MEASURED"
            and self.verified_by_measurement_sha256
            and self.verified_at
            and self.configuration_id
            and self.verification_scope == "CONSOLE_AND_RELATIVE_SUM"
            and self.configuration_digest == self.settings_digest
        )

    @property
    def settings_digest(self) -> str:
        from rew_project import digest
        return digest([self.configuration_id, self.configuration_version, self.crossover_hz, self.filter_name,
                       self.l_main_ms, self.r_main_ms, self.l_sub_ms, self.r_sub_ms])

    @property
    def relative_delays_ms(self) -> tuple[float, float]:
        return self.l_sub_ms - self.l_main_ms, self.r_sub_ms - self.r_main_ms


def load_reference_record(path: str | Path) -> ReferenceRecord:
    source = Path(path)
    fields = read_reference_fields(source)
    required_numbers = ("crossover_hz", "l_main_ms", "r_main_ms", "l_sub_ms", "r_sub_ms")
    missing = [name for name in required_numbers if fields.get(name) is None]
    if missing:
        raise ValueError(f"Referencja {source.name}: brak pól {', '.join(missing)}.")
    numbers = [float(fields[name]) for name in required_numbers]
    if not all(np.isfinite(value) for value in numbers):
        raise ValueError(f"Referencja {source.name}: NaN/Inf w ustawieniach.")
    sha = fields.get("verified_by_measurement_sha256")
    sha_text = str(sha).strip().lower() if sha is not None else None
    if sha_text is not None and (len(sha_text) != 64 or any(ch not in "0123456789abcdef" for ch in sha_text)):
        raise ValueError(f"Referencja {source.name}: błędny verified_by_measurement_sha256.")
    verified_at = fields.get("verified_at")
    return ReferenceRecord(
        path=source,
        name=str(fields.get("name") or source.stem),
        crossover_hz=float(fields["crossover_hz"]),
        filter_name=str(fields.get("filter") or "").upper().replace(" ", ""),
        l_main_ms=float(fields["l_main_ms"]),
        r_main_ms=float(fields["r_main_ms"]),
        l_sub_ms=float(fields["l_sub_ms"]),
        r_sub_ms=float(fields["r_sub_ms"]),
        verified=bool(fields.get("verified") is True),
        verification_status=str(fields.get("verification_status") or "UNVERIFIED"),
        verified_by_measurement_sha256=sha_text,
        verified_at=str(verified_at) if verified_at is not None else None,
        configuration_id=str(fields["configuration_id"]) if fields.get("configuration_id") else None,
        configuration_version=int(fields.get("configuration_version") or 1),
        verification_scope=str(fields.get("verification_scope") or ""),
        configuration_digest=str(fields.get("configuration_digest") or ""),
    )


@dataclass(frozen=True)
class ReferenceConflict:
    first: ReferenceRecord
    second: ReferenceRecord
    differing_fields: tuple[str, ...]

    def describe(self) -> str:
        return (
            f"Konflikt referencji {self.first.path.name} ↔ {self.second.path.name}: "
            + ", ".join(self.differing_fields)
        )


def find_reference_conflicts(paths: Iterable[str | Path], *, tolerance_ms: float = 0.05) -> list[ReferenceConflict]:
    records = [load_reference_record(path) for path in paths]
    conflicts: list[ReferenceConflict] = []
    fields = ("l_main_ms", "r_main_ms", "l_sub_ms", "r_sub_ms")
    for index, first in enumerate(records):
        for second in records[index + 1:]:
            if first.configuration_id and second.configuration_id:
                if (first.configuration_id, first.configuration_version) != (second.configuration_id, second.configuration_version):
                    continue
            if abs(first.crossover_hz - second.crossover_hz) > 0.51 or first.filter_name != second.filter_name:
                continue
            differing = tuple(
                field for field in fields
                if abs(float(getattr(first, field)) - float(getattr(second, field))) > float(tolerance_ms)
            )
            if differing:
                conflicts.append(ReferenceConflict(first, second, differing))
    return conflicts


def reference_relative_delays(
    path: str | Path,
    *,
    companion_paths: Iterable[str | Path] = (),
) -> Optional[tuple[float, float]]:
    record = load_reference_record(path)
    conflicts = find_reference_conflicts([path, *companion_paths])
    if conflicts or not record.anchor_allowed:
        return None
    return record.relative_delays_ms
