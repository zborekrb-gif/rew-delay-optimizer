# -*- coding: utf-8 -*-
"""Symulacja cyfrowego crossovera SHD na pomiarach bazowych RAW.

Pomiar RAW powinien być wykonany z wyłączonym HPF/LPF w SHD, delay 0 ms,
polaryzacją OFF i stałym ustawieniem filtrów analogowych subwooferów.
Dla Roberta: REL S/510 na LOW LEVEL RCA, pokrętło crossovera REL pozostaje
stałe (np. REL120) i jego rzeczywisty wpływ jest już zawarty w pomiarze RAW.
"""
from __future__ import annotations

from rew_api_client import verify_measurement_state

from pathlib import Path
from typing import Dict, Tuple

import numpy as np

from rew_common import Measurement, MdatImportedProject


def _butterworth_poles(order: int) -> np.ndarray:
    """Znormalizowane bieguny analogowego filtra Butterwortha w lewej półpłaszczyźnie."""
    n = int(order)
    if n <= 0:
        raise ValueError("Rząd filtra musi być dodatni.")
    k = np.arange(n, dtype=float)
    return np.exp(1j * np.pi * (2.0 * k + 1.0 + n) / (2.0 * n))


def butterworth_transfer(
    freq_hz: np.ndarray,
    crossover_hz: float,
    order: int,
    pass_type: str,
) -> np.ndarray:
    """Zespolona transmitancja analogowego Butterwortha LPF albo HPF.

    Dla częstotliwości audio względem częstotliwości próbkowania SHD model
    analogowy jest praktycznym modelem docelowej charakterystyki crossovera:
    uwzględnia zarówno amplitudę, jak i obrót fazy.
    """
    f = np.asarray(freq_hz, dtype=float)
    fc = float(crossover_hz)
    n = int(order)
    if fc <= 0:
        raise ValueError("Częstotliwość crossovera musi być dodatnia.")
    if np.any(f <= 0):
        raise ValueError("Oś częstotliwości musi zawierać wartości dodatnie.")
    poles = _butterworth_poles(n)
    s = 1j * f / fc
    denom = np.prod(s[:, None] - poles[None, :], axis=1)
    kind = str(pass_type).strip().upper()
    if kind == "LP":
        dc_gain = np.prod(-poles)
        return dc_gain / denom
    if kind == "HP":
        return (s ** n) / denom
    raise ValueError("pass_type musi mieć wartość LP albo HP.")


def crossover_transfer(
    freq_hz: np.ndarray,
    crossover_hz: float,
    filter_type: str,
    slope_db_oct: int,
    pass_type: str,
) -> np.ndarray:
    """Transmitancja HPF/LPF dla BW24/BW48 albo LR24/LR48."""
    filt = str(filter_type).strip().upper()
    slope = int(slope_db_oct)
    if slope not in {24, 48}:
        raise ValueError("Symulacja RAW obsługuje obecnie stromiznę 24 albo 48 dB/okt.")
    if filt == "BW":
        order = slope // 6
        return butterworth_transfer(freq_hz, crossover_hz, order, pass_type)
    if filt == "LR":
        # Linkwitz-Riley 24 = kaskada 2 x BW12; LR48 = kaskada 2 x BW24.
        bw_order = slope // 12
        h_bw = butterworth_transfer(freq_hz, crossover_hz, bw_order, pass_type)
        return h_bw * h_bw
    raise ValueError("Symulacja RAW obsługuje obecnie filtry BW albo LR.")


def _apply_transfer_to_measurement(m: Measurement, transfer: np.ndarray, suffix: str) -> Measurement:
    transfer = np.asarray(transfer, dtype=complex)
    if transfer.size != m.freq.size:
        raise ValueError("Transmitancja filtra ma inną długość niż pomiar RAW.")
    magnitude_db = 20.0 * np.log10(np.maximum(np.abs(transfer), 1e-15))
    phase_filter_deg = np.rad2deg(np.unwrap(np.angle(transfer)))
    return Measurement(
        name=f"{m.name}{suffix}",
        freq=np.asarray(m.freq, dtype=float).copy(),
        spl_db=np.asarray(m.spl_db, dtype=float).copy() + magnitude_db,
        phase_deg=np.asarray(m.phase_deg, dtype=float).copy() + phase_filter_deg,
    )


def simulate_shd_crossover_from_raw(
    l_main: Measurement,
    l_sub: Measurement,
    r_main: Measurement,
    r_sub: Measurement,
    crossover_hz: float,
    filter_type: str,
    slope_db_oct: int,
) -> Tuple[Measurement, Measurement, Measurement, Measurement]:
    """Nakłada cyfrowy HPF na mainy i LPF na suby w pomiarze bazowym RAW."""
    freq = np.asarray(l_main.freq, dtype=float)
    for m in [l_sub, r_main, r_sub]:
        if m.freq.size != freq.size or not np.allclose(m.freq, freq, rtol=0.0, atol=1e-6):
            raise ValueError("Pomiary RAW muszą mieć tę samą oś częstotliwości.")
    hp = crossover_transfer(freq, crossover_hz, filter_type, slope_db_oct, "HP")
    lp = crossover_transfer(freq, crossover_hz, filter_type, slope_db_oct, "LP")
    suffix = f"__SIM_{float(crossover_hz):g}Hz_{str(filter_type).upper()}{int(slope_db_oct)}"
    return (
        _apply_transfer_to_measurement(l_main, hp, suffix),
        _apply_transfer_to_measurement(l_sub, lp, suffix),
        _apply_transfer_to_measurement(r_main, hp, suffix),
        _apply_transfer_to_measurement(r_sub, lp, suffix),
    )


def is_raw_xover_off_project(project: MdatImportedProject | None) -> bool:
    """Rozpoznaje plik bazowy RAW z wyłączonym crossoverem SHD.

    Wykrywanie jest ostrożne: wymagamy jawnego RAW oraz XOVER_OFF w nazwie pliku
    albo nazw RAW dla wszystkich czterech ról.
    """
    if project is None:
        return False
    state = (project.scan or {}).get("measurement_state", {}) if isinstance(project.scan, dict) else {}
    verified_api, _missing = verify_measurement_state(state)
    if bool((project.scan or {}).get("is_rew_api_snapshot")) and verified_api:
        return True
    path_text = Path(str(project.path)).stem.lower()
    names: Dict[str, str] = dict(project.role_to_name or {})
    name_texts = [str(v).lower() for v in names.values()]
    explicit_path = "raw" in path_text and any(token in path_text for token in ["xover_off", "crossover_off", "xoveroff"])
    all_roles_raw = len(name_texts) >= 4 and all("raw" in n for n in name_texts)
    return bool(explicit_path or all_roles_raw)


def raw_simulation_description(crossover_hz: float, filter_type: str, slope_db_oct: int) -> str:
    return (
        f"Pomiar bazowy RAW: symuluję w SHD HPF dla kolumn i LPF dla subów: "
        f"{float(crossover_hz):g} Hz {str(filter_type).upper()}{int(slope_db_oct)}. "
        "Stały filtr analogowy REL pozostaje uwzględniony w pomiarze RAW."
    )


def write_raw_simulation_note(
    out_dir: str | Path,
    crossover_hz: float,
    filter_type: str,
    slope_db_oct: int,
) -> Path:
    out = Path(out_dir) / "00_START_TUTAJ"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "RAW_CROSSOVER_SYMULACJA.txt"
    text = "\n".join([
        "SYMULACJA CROSSOVERA SHD Z POMIARU RAW",
        "",
        f"Wybrany crossover SHD: {float(crossover_hz):g} Hz {str(filter_type).upper()}{int(slope_db_oct)}",
        "",
        "Aplikacja nałożyła cyfrowo:",
        "- HPF na kolumny główne,",
        "- LPF na oba subwoofery.",
        "",
        "Stały filtr analogowy REL ustawiony podczas pomiaru bazowego pozostaje już zawarty w danych RAW.",
        "Wynik jest symulacją do wyboru ustawień. Przed Dirac wpisz wartości do SHD i wykonaj pomiar kontrolny w REW.",
        "",
    ])
    path.write_text(text, encoding="utf-8")
    return path
