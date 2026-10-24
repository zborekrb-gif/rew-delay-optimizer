#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jawny stan gainu używany przez projektanty REWDO v20.31."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class GainState:
    raw_db: float
    dirac_db: float | None
    listening_db: float | None
    source: str
    verified: bool
    bridge_sha256: str | None
    bridge_left_delta_db: float | None = None
    bridge_right_delta_db: float | None = None

    def __post_init__(self) -> None:
        values = [self.raw_db]
        values.extend(value for value in (self.dirac_db, self.listening_db) if value is not None)
        if not all(np.isfinite(float(value)) for value in values):
            raise ValueError("GainState zawiera NaN/Inf.")
        if self.bridge_sha256 is not None:
            token = str(self.bridge_sha256).lower()
            if len(token) != 64 or any(ch not in "0123456789abcdef" for ch in token):
                raise ValueError("bridge_sha256 nie jest prawidłowym SHA-256.")

    @property
    def complete(self) -> bool:
        return self.dirac_db is not None and self.listening_db is not None

    def target_db(self, state: str) -> float:
        key = str(state).strip().lower()
        if key == "raw":
            return float(self.raw_db)
        if key == "dirac" and self.dirac_db is not None:
            return float(self.dirac_db)
        if key in {"listening", "odsluch", "odsłuch"} and self.listening_db is not None:
            return float(self.listening_db)
        raise ValueError(f"GainState nie zawiera stanu {state!r}.")

    def nominal_delta_db(self, state: str) -> float:
        return self.target_db(state) - float(self.raw_db)

    def side_delta_db(self, state: str, side: str) -> float:
        nominal = self.nominal_delta_db(state)
        if not self.verified or self.listening_db is None:
            return nominal
        full_nominal = float(self.listening_db) - float(self.raw_db)
        if abs(full_nominal) < 1e-12:
            return nominal
        measured = self.bridge_left_delta_db if str(side).upper() == "L" else self.bridge_right_delta_db
        if measured is None or not np.isfinite(float(measured)):
            return nominal
        return float(measured) * nominal / full_nominal

    def scale_complex_subs(self, left: np.ndarray, right: np.ndarray, state: str) -> tuple[np.ndarray, np.ndarray]:
        left_db = self.side_delta_db(state, "L")
        right_db = self.side_delta_db(state, "R")
        return (
            np.asarray(left, dtype=complex) * np.power(10.0, left_db / 20.0),
            np.asarray(right, dtype=complex) * np.power(10.0, right_db / 20.0),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_gain_state(
    *,
    raw_db: float,
    dirac_db: Optional[float],
    listening_db: Optional[float],
    source: str,
    verified: bool,
    bridge_path: Optional[str | Path] = None,
    bridge_left_delta_db: Optional[float] = None,
    bridge_right_delta_db: Optional[float] = None,
) -> GainState:
    return GainState(
        raw_db=float(raw_db),
        dirac_db=None if dirac_db is None else float(dirac_db),
        listening_db=None if listening_db is None else float(listening_db),
        source=str(source),
        verified=bool(verified),
        bridge_sha256=sha256_file(bridge_path) if bridge_path is not None else None,
        bridge_left_delta_db=None if bridge_left_delta_db is None else float(bridge_left_delta_db),
        bridge_right_delta_db=None if bridge_right_delta_db is None else float(bridge_right_delta_db),
    )

