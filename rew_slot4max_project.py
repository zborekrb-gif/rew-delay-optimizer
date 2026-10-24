#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Slot4 Shortlist Validator: trzy jawnie zdefiniowane warianty BW48."""
from __future__ import annotations

import argparse
import csv
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

from rew_filter_generator import (
    freq_response,
    make_sections,
    write_csv as write_biquad_csv,
    write_minidsp_biquads,
)
from rew_gain_bridge import analyze_gain_bridge, write_gain_bridge_report
from rew_candidate_safety import (
    CandidateSafetyResult,
    NoSafeCandidateError,
    combine_required_state_safety,
    evaluate_candidate_safety,
    write_quarantine,
)
from rew_mdat import load_optimizer_measurements_from_mdat
from rew_reference_registry import find_reference_conflicts, load_reference_record
from rew_scoring import db, measurement_to_complex, roughness


@dataclass(frozen=True)
class Variant:
    code: str
    title: str
    main_fc: float
    sub_fc: float
    l_rel_ms: float
    r_rel_ms: float


REFERENCE = Variant("A_SLOT4_REFERENCE", "Slot 4 PRE-DIRAC control", 90.0, 90.0, -9.60, -12.10)
SAFE = Variant("B_REFERENCE_SAFE", "Reference Safe", 94.0, 92.0, -9.80, -12.20)
SEAM = Variant("C_SEAM_PHASE", "Seam/Phase", 96.0, 92.0, -10.10, -12.40)
VARIANTS = [REFERENCE, SAFE, SEAM]


@dataclass
class ShortlistSafetyCandidate:
    code: str
    decision_class: str
    effective_xo_hz: float
    side_min: float
    body_score: float
    lr_sum_score: float
    acoustic_score: float
    global_gain_l_db: float
    global_gain_r_db: float
    local_gain_l_db: float = 0.0
    local_gain_r_db: float = 0.0
    final_gain_l_db: float = 0.0
    final_gain_r_db: float = 0.0
    candidate_safe_to_export: bool = False
    safety_reasons: Tuple[str, ...] = ()


def _clip_score(value: float) -> float:
    return float(max(0.0, min(100.0, value)))


def _band(freq: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return (freq >= lo) & (freq <= hi)


def _mean(values: np.ndarray, mask: np.ndarray) -> float:
    return float(np.mean(np.asarray(values)[mask]))


def _delay(response: np.ndarray, freq: np.ndarray, delay_ms: float) -> np.ndarray:
    return response * np.exp(-1j * 2.0 * np.pi * freq * float(delay_ms) / 1000.0)


def _side_metrics(main: np.ndarray, sub: np.ndarray, freq: np.ndarray) -> Tuple[np.ndarray, float, float, float]:
    summed = main + sub
    ideal = np.abs(main) + np.abs(sub)
    loss_db = 20.0 * np.log10(np.maximum(np.abs(summed), 1e-15) / np.maximum(ideal, 1e-15))
    overlap = _band(freq, 45.0, 155.0) & (np.abs(db(main) - db(sub)) <= 18.0)
    if np.count_nonzero(overlap) < 20:
        overlap = _band(freq, 55.0, 145.0)
    phase = np.abs(np.rad2deg(np.angle(main[overlap] / sub[overlap])))
    return (
        summed,
        float(np.mean(loss_db[overlap])),
        float(np.percentile(loss_db[overlap], 10.0)),
        float(np.median(phase)),
    )


class Slot4ShortlistModel:
    def __init__(self, raw_path: Path, bridge_path: Path, fs: float = 96_000.0):
        lm, ls, rm, rs, project = load_optimizer_measurements_from_mdat(raw_path)
        if not project.phase_metrics_allowed:
            raise ValueError("Phase Truth Guard nie potwierdził RAW.")
        self.phase_truth_status = project.phase_truth_status
        self.phase_truth_diagnostics = project.phase_truth_diagnostics
        measurements = [lm, ls, rm, rs]
        reference_freq = np.asarray(lm.freq, dtype=float)
        same_frequency_axis = all(
            len(m.freq) == len(reference_freq)
            and np.allclose(m.freq, reference_freq, rtol=0.0, atol=1e-9)
            for m in measurements[1:]
        )
        if same_frequency_axis:
            # v20.30: zachowujemy rzeczywiste punkty FFT z MDAT. Arbitraryczna
            # siatka logarytmiczna potrafiła przesunąć raportowane przecięcie o
            # ułamek herca i kilka stopni, mimo niezmienionych danych wejściowych.
            self.freq = np.sort(reference_freq)
        else:
            # Importer normalnie daje wspólną oś. Fallback pozostaje gęsty, ale
            # jest ograniczony do wspólnego zakresu wszystkich czterech śladów.
            f_min = max(float(np.nanmin(m.freq)) for m in measurements)
            f_max = min(float(np.nanmax(m.freq)) for m in measurements)
            point_count = min(max(2048, len(m.freq)) for m in measurements)
            self.freq = np.geomspace(f_min, f_max, point_count)
        self.freq = self.freq[(self.freq >= 20.0) & (self.freq <= 500.0)]
        self.lm = measurement_to_complex(lm, self.freq)
        self.ls = measurement_to_complex(ls, self.freq)
        self.rm = measurement_to_complex(rm, self.freq)
        self.rs = measurement_to_complex(rs, self.freq)
        self.fs = float(fs)
        self.bridge = analyze_gain_bridge(raw_path, bridge_path, expected_gain_db=6.0)
        if self.bridge.status != "VERIFIED":
            raise ValueError("Gain bridge nie został potwierdzony.")

    def filter_response(self, kind: str, fc: float) -> np.ndarray:
        sections = make_sections(kind, self.fs, float(fc), "BW48")
        return freq_response(sections, self.fs, self.freq)

    def simulate(self, variant: Variant, state_delta_db: float, perturb=(0, 0, 0, 0, 0, 0)) -> Dict[str, object]:
        dl, dr, gl, gr, df_main, df_sub = [float(x) for x in perturb]
        fraction = float(state_delta_db) / 6.0
        ls = self.ls * np.power(10.0, (self.bridge.left.gain_db * fraction + gl) / 20.0)
        rs = self.rs * np.power(10.0, (self.bridge.right.gain_db * fraction + gr) / 20.0)
        hp = self.filter_response("HP", variant.main_fc + df_main)
        lp = self.filter_response("LP", variant.sub_fc + df_sub)
        lm = self.lm * hp
        rm = self.rm * hp
        ls = _delay(ls * lp, self.freq, variant.l_rel_ms + dl)
        rs = _delay(rs * lp, self.freq, variant.r_rel_ms + dr)
        l_sum, l_eff, l_p10, l_phase = _side_metrics(lm, ls, self.freq)
        r_sum, r_eff, r_p10, r_phase = _side_metrics(rm, rs, self.freq)
        return {
            "lm": lm, "ls": ls, "rm": rm, "rs": rs,
            "l_sum": l_sum, "r_sum": r_sum, "lr_sum": l_sum + r_sum,
            "l_eff": l_eff, "r_eff": r_eff, "l_p10": l_p10, "r_p10": r_p10,
            "l_phase": l_phase, "r_phase": r_phase,
        }

    def cross_metrics(self, variant: Variant, state_delta_db: float) -> Dict[str, float]:
        sim = self.simulate(variant, state_delta_db)
        result: Dict[str, float] = {}
        for side in ("l", "r"):
            main = sim[f"{side}m"]
            sub = sim[f"{side}s"]
            mask = _band(self.freq, 60.0, 130.0)
            local_indices = np.where(mask)[0]
            index = local_indices[int(np.argmin(np.abs(db(main)[mask] - db(sub)[mask])))]
            phase = abs(float(np.rad2deg(np.angle(main[index] / sub[index]))))
            construct = float(db(main + sub)[index] - max(db(main)[index], db(sub)[index]))
            result[f"{side}_cross_hz"] = float(self.freq[index])
            result[f"{side}_phase_deg"] = phase
            result[f"{side}_construct_db"] = construct
        return result

    def compare(self, variant: Variant, state_delta_db: float, perturb=(0, 0, 0, 0, 0, 0)) -> Dict[str, float]:
        candidate = self.simulate(variant, state_delta_db, perturb)
        reference = self.simulate(REFERENCE, state_delta_db, perturb)
        delta_lr = db(candidate["lr_sum"]) - db(reference["lr_sum"])
        delta_l = db(candidate["l_sum"]) - db(reference["l_sum"])
        delta_r = db(candidate["r_sum"]) - db(reference["r_sum"])
        return {
            "l_eff_delta_db": float(candidate["l_eff"] - reference["l_eff"]),
            "r_eff_delta_db": float(candidate["r_eff"] - reference["r_eff"]),
            "l_60_125_db": _mean(delta_l, _band(self.freq, 60.0, 125.0)),
            "r_60_125_db": _mean(delta_r, _band(self.freq, 60.0, 125.0)),
            "stereo_60_125_db": _mean(delta_lr, _band(self.freq, 60.0, 125.0)),
            "body_110_220_db": _mean(delta_lr, _band(self.freq, 110.0, 220.0)),
            "body_p10_db": float(np.percentile(delta_lr[_band(self.freq, 110.0, 220.0)], 10.0)),
            "bass_40_70_db": _mean(delta_lr, _band(self.freq, 40.0, 70.0)),
            "kick_70_100_db": _mean(delta_lr, _band(self.freq, 70.0, 100.0)),
            "lower_mid_125_160_db": _mean(delta_lr, _band(self.freq, 125.0, 160.0)),
            "mid_160_220_db": _mean(delta_lr, _band(self.freq, 160.0, 220.0)),
            "roughness_delta_db": float(
                roughness(db(candidate["lr_sum"])[_band(self.freq, 55.0, 160.0)])
                - roughness(db(reference["lr_sum"])[_band(self.freq, 55.0, 160.0)])
            ),
        }

    def robustness(self, variant: Variant, state_delta_db: float = 2.0) -> Dict[str, Dict[str, float]]:
        perturbations = itertools.product(
            (-0.25, 0.0, 0.25), (-0.25, 0.0, 0.25),
            (-0.5, 0.0, 0.5), (-0.5, 0.0, 0.5),
            (-1.0, 0.0, 1.0), (-1.0, 0.0, 1.0),
        )
        keys = [
            "l_eff_delta_db", "r_eff_delta_db", "l_60_125_db", "r_60_125_db",
            "stereo_60_125_db", "body_110_220_db", "body_p10_db",
        ]
        rows = np.asarray([[self.compare(variant, state_delta_db, p)[key] for key in keys] for p in perturbations])
        return {
            key: {
                "median": float(np.median(rows[:, index])),
                "p10": float(np.percentile(rows[:, index], 10.0)),
                "worst": float(np.min(rows[:, index])),
            }
            for index, key in enumerate(keys)
        }


# Zgodność importów z v20.30; nazwa raportowa i GUI pozostają uczciwie zmienione.
Slot4MaxModel = Slot4ShortlistModel


def _reference_registry_status(reference_yaml: Path) -> Tuple[bool, str]:
    companion = reference_yaml.with_name("USER_REFERENCE_SLOT4MAX_90BW48.yaml")
    if not reference_yaml.exists():
        return False, f"Brak referencji: {reference_yaml}"
    try:
        selected = load_reference_record(reference_yaml)
        if companion.exists() and companion.resolve() != reference_yaml.resolve():
            conflicts = find_reference_conflicts([reference_yaml, companion])
            if conflicts:
                return False, conflicts[0].describe() + ". Konflikt 17,9/19,1 ms wymaga pomiaru."
        if not selected.anchor_allowed:
            return False, "Wybrana referencja nie ma statusu MEASURED/USER_CONFIRMED, SHA-256 pomiaru i daty weryfikacji."
    except ValueError as exc:
        return False, str(exc)
    return True, f"Zweryfikowana referencja: {reference_yaml.name}"


def _export_variant(out: Path, variant: Variant, fs: float, safety: ShortlistSafetyCandidate) -> None:
    folder = out / variant.code
    folder.mkdir(parents=True, exist_ok=True)
    main_sections = make_sections("HP", fs, variant.main_fc, "BW48")
    sub_sections = make_sections("LP", fs, variant.sub_fc, "BW48")
    write_minidsp_biquads(folder / "MAIN_C1_HPF_MINIDSP_BIQUADS.txt", main_sections, "")
    write_minidsp_biquads(folder / "SUB_REL_LPF_MINIDSP_BIQUADS.txt", sub_sections, "")
    write_biquad_csv(folder / "MAIN_C1_HPF_coefficients.csv", main_sections, fs, variant.main_fc, "BW48")
    write_biquad_csv(folder / "SUB_REL_LPF_coefficients.csv", sub_sections, fs, variant.sub_fc, "BW48")
    (folder / "USTAWIENIA_SHD.md").write_text("\n".join([
        f"# {variant.title}", "",
        f"- C1 L/R HPF: **{variant.main_fc:.0f} Hz BW48**",
        f"- REL L/R LPF: **{variant.sub_fc:.0f} Hz BW48**",
        "- Main L/R delay: **30,00 ms**",
        f"- L REL delay: **{30.0 + variant.l_rel_ms:.2f} ms**",
        f"- R REL delay: **{30.0 + variant.r_rel_ms:.2f} ms**",
        "- Polarity L/R REL: **OFF/OFF**",
        "- PEQ: OFF podczas pomiaru kontrolnego",
        "- Dirac: OFF podczas pierwszej weryfikacji; nowy Dirac dopiero dla zwycięzcy",
        "- Wewnętrzny crossover REL: **120 Hz bez zmiany**",
        f"- Globalny gain stanu Dirac L/R: **{safety.global_gain_l_db:+.2f}/{safety.global_gain_r_db:+.2f} dB**",
        f"- Lokalna korekta gainu L/R: **{safety.local_gain_l_db:+.2f}/{safety.local_gain_r_db:+.2f} dB**",
        f"- Końcowy gain L/R: **{safety.final_gain_l_db:+.2f}/{safety.final_gain_r_db:+.2f} dB**",
        "- Candidate Safety Gate: **PASS**",
    ]) + "\n", encoding="utf-8")


def build_project(raw_path: Path, bridge_path: Path, out: Path, reference_yaml: Path | None = None) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    model = Slot4ShortlistModel(raw_path, bridge_path)
    write_gain_bridge_report(model.bridge, out)
    reference_path = reference_yaml or Path(__file__).resolve().with_name("USER_REFERENCE_90BW48.yaml")
    reference_anchor_verified, reference_status = _reference_registry_status(reference_path)

    states = [(0.0, "RAW -6 dB"), (2.0, "Dirac -4 dB"), (6.0, "Odsłuch 0 dB")]
    state_rows: List[Dict[str, object]] = []
    for state_delta, state_name in states:
        for variant in VARIANTS:
            metrics = model.compare(variant, state_delta)
            state_rows.append({"state": state_name, "variant": variant.code, **metrics})
    with (out / "SLOT4_SHORTLIST_METRICS.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(state_rows[0]))
        writer.writeheader()
        writer.writerows(state_rows)

    cross = {variant.code: model.cross_metrics(variant, 2.0) for variant in VARIANTS}
    robust = {variant.code: model.robustness(variant, 2.0) for variant in (SAFE, SEAM)}
    safety_candidates: List[ShortlistSafetyCandidate] = []
    safety_results: List[CandidateSafetyResult] = []
    safe_codes: set[str] = set()
    state_by_variant: Dict[str, Dict[str, Dict[str, object]]] = {}
    for variant in VARIANTS:
        required_results: Dict[str, CandidateSafetyResult] = {}
        state_by_variant[variant.code] = {}
        representative: ShortlistSafetyCandidate | None = None
        for state_delta, state_key in ((2.0, "dirac"), (6.0, "listening")):
            comparison = model.compare(variant, state_delta)
            crossing = model.cross_metrics(variant, state_delta)
            effective_xo = 0.5 * (crossing["l_cross_hz"] + crossing["r_cross_hz"])
            sim = model.simulate(variant, state_delta)
            side_min = _clip_score(100.0 + 15.0 * min(float(sim["l_eff"]), float(sim["r_eff"])))
            body_score = _clip_score(100.0 - 50.0 * max(0.0, -float(comparison["body_110_220_db"])))
            stereo_delta = float(comparison["stereo_60_125_db"])
            lr_sum_score = _clip_score(100.0 - 30.0 * max(0.0, -stereo_delta) - 10.0 * max(0.0, stereo_delta - 3.0))
            phase_ok = bool(
                max(crossing["l_phase_deg"], crossing["r_phase_deg"]) <= 135.0
                and min(crossing["l_construct_db"], crossing["r_construct_db"]) >= -3.0
            )
            fraction = state_delta / 6.0
            global_l = float(model.bridge.left.gain_db * fraction)
            global_r = float(model.bridge.right.gain_db * fraction)
            candidate = ShortlistSafetyCandidate(
                code=variant.code,
                decision_class="KANDYDAT" if phase_ok else "DIAGNOSTYKA",
                effective_xo_hz=effective_xo,
                side_min=side_min,
                body_score=body_score,
                lr_sum_score=lr_sum_score,
                acoustic_score=float(np.mean([side_min, body_score, lr_sum_score])),
                global_gain_l_db=global_l,
                global_gain_r_db=global_r,
                final_gain_l_db=global_l,
                final_gain_r_db=global_r,
            )
            gate = evaluate_candidate_safety(
                candidate,
                input_integrity_ok=True,
                phase_integrity_ok=phase_ok,
                gain_state_verified=True,
                gain_state_complete=True,
                reference_anchor_verified=reference_anchor_verified,
                effective_xo_min_hz=70.0,
                effective_xo_max_hz=105.0,
            )
            required_results[state_key] = gate
            state_by_variant[variant.code][state_key] = {
                "effective_xo_hz": effective_xo,
                "side_min": side_min,
                "body_score": body_score,
                "lr_sum_score": lr_sum_score,
                "phase_integrity_ok": phase_ok,
                "safe_to_export": gate.candidate_safe_to_export,
                "reasons": list(gate.reasons),
            }
            if state_key == "dirac":
                representative = candidate

        assert representative is not None
        base_gate = evaluate_candidate_safety(
            representative,
            input_integrity_ok=True,
            phase_integrity_ok=True,
            gain_state_verified=True,
            gain_state_complete=True,
            reference_anchor_verified=reference_anchor_verified,
            effective_xo_min_hz=70.0,
            effective_xo_max_hz=105.0,
        )
        combined = combine_required_state_safety(base_gate, required_results)
        combined_ok = combined.candidate_safe_to_export
        combined_reasons = combined.reasons
        representative.candidate_safe_to_export = combined_ok
        representative.safety_reasons = combined_reasons
        safety_candidates.append(representative)
        safety_results.append(combined)
        if combined_ok:
            safe_codes.add(variant.code)

    safety_by_code = {candidate.code: candidate for candidate in safety_candidates}
    for variant in VARIANTS:
        if variant.code in safe_codes:
            _export_variant(out, variant, model.fs, safety_by_code[variant.code])
    left_diag = model.phase_truth_diagnostics["sides"][0]
    right_diag = model.phase_truth_diagnostics["sides"][1]

    def metrics_for(code: str, state: str) -> Dict[str, object]:
        return next(row for row in state_rows if row["variant"] == code and row["state"] == state)

    safe_m4 = metrics_for(SAFE.code, "Dirac -4 dB")
    seam_m4 = metrics_for(SEAM.code, "Dirac -4 dB")
    lines = [
        "# Slot4 Shortlist Validator — wynik bieżącego uruchomienia",
        "",
        "## Zakres rzeczywiście policzony",
        "",
        "Ten tryb nie przeszukuje całej przestrzeni zwrotnic. W bieżącym runie policzono wyłącznie trzy z góry ustalone warianty BW48:",
        "",
        "- `REFERENCE`: 90/90 Hz BW48;",
        "- `SAFE`: 94/92 Hz BW48;",
        "- `SEAM`: 96/92 Hz BW48.",
        "",
        "Nie liczono LR24, LR48, 96/96 ani all-pass. Raport nie wydaje werdyktu o tych rodzinach.",
        "",
        "## Rejestr referencji",
        "",
        f"- Status: **{'VERIFIED' if reference_anchor_verified else 'BLOCKED'}**.",
        f"- {reference_status}",
        "- Przy statusie `BLOCKED` obliczenia shortlisty są diagnostyczne i nie mogą utworzyć plików miniDSP.",
        "",
        "## Decyzja w obrębie shortlisty",
        "",
        f"Do eksportu przeszło **{len(safe_codes)}/{len(VARIANTS)}** wariantów bez all-passu. Istniejący Slot 4 nie jest zmieniany; konflikt referencji pozostaje nierozstrzygnięty do czasu pomiaru.",
        "",
        "| Wariant | HP C1 | LP REL | Main L/R | L REL | R REL | Charakter testu |",
        "|---|---:|---:|---:|---:|---:|---|",
        "| A — Slot 4 PRE-DIRAC control | 90 Hz BW48 | 90 Hz BW48 | 30,00 ms | 20,40 ms | 17,90 ms | kontrolne odtworzenie 90 BW48 w tych samych warunkach |",
        "| B — Reference Safe | 94 Hz BW48 | 92 Hz BW48 | 30,00 ms | 20,20 ms | 17,80 ms | niewielka poprawa zszycia z ochroną barwy |",
        "| C — Seam/Phase | 96 Hz BW48 | 92 Hz BW48 | 30,00 ms | 19,90 ms | 17,60 ms | większa poprawa lewej fazy, wyższe ryzyko odsłuchowe |",
        "",
        "## Candidate Safety Gate — stany Dirac i odsłuchowy",
        "",
        "| Wariant | Dirac | Odsłuch | Eksport | Powód blokady |",
        "|---|---|---|---|---|",
        *[
            f"| `{variant.code}` | {'PASS' if state_by_variant[variant.code]['dirac']['safe_to_export'] else 'BLOCKED'} | "
            f"{'PASS' if state_by_variant[variant.code]['listening']['safe_to_export'] else 'BLOCKED'} | "
            f"{'TAK' if variant.code in safe_codes else 'NIE'} | "
            f"{'—' if variant.code in safe_codes else '; '.join(safety_by_code[variant.code].safety_reasons)} |"
            for variant in VARIANTS
        ],
        "",
        "## Jakość danych",
        "",
        f"- Phase Truth Guard: **{model.phase_truth_status}**.",
        f"- Lewa rekonstrukcja MAIN + SUB ≈ SUM: **{left_diag['spl_rms_db']:.2f} dB / {left_diag['phase_rms_deg']:.2f}° RMS**.",
        f"- Prawa rekonstrukcja MAIN + SUB ≈ SUM: **{right_diag['spl_rms_db']:.2f} dB / {right_diag['phase_rms_deg']:.2f}° RMS**.",
        f"- Gain bridge: **L {model.bridge.left.gain_db:+.2f} dB / R {model.bridge.right.gain_db:+.2f} dB — VERIFIED**.",
        "- `LR_SUM_SUB0` nie jest wymagany; mostek poziomu jest liczony amplitudowo osobno dla L i R.",
        "",
        "## Porównanie przy stanie pomiaru Diraca -4 dB",
        "",
        "| Metryka względem Slotu 4 | Reference Safe | Seam/Phase |",
        "|---|---:|---:|",
        f"| Sprawność fazowa lewej strony | {safe_m4['l_eff_delta_db']:+.2f} dB | {seam_m4['l_eff_delta_db']:+.2f} dB |",
        f"| Sprawność fazowa prawej strony | {safe_m4['r_eff_delta_db']:+.2f} dB | {seam_m4['r_eff_delta_db']:+.2f} dB |",
        f"| Suma L+R 60–125 Hz | {safe_m4['stereo_60_125_db']:+.2f} dB | {seam_m4['stereo_60_125_db']:+.2f} dB |",
        f"| Korpus 110–220 Hz | {safe_m4['body_110_220_db']:+.2f} dB | {seam_m4['body_110_220_db']:+.2f} dB |",
        f"| Zmiana nierówności 55–160 Hz (mniej = lepiej) | {safe_m4['roughness_delta_db']:+.2f} dB | {seam_m4['roughness_delta_db']:+.2f} dB |",
        "",
        "## Zszycie przy akustycznym przecięciu — symulacja przed Dirac",
        "",
        "| Wariant | L: f / faza / sumowanie | R: f / faza / sumowanie |",
        "|---|---|---|",
    ]
    for variant in VARIANTS:
        item = cross[variant.code]
        lines.append(
            f"| {variant.title} | {item['l_cross_hz']:.1f} Hz / {item['l_phase_deg']:.1f}° / {item['l_construct_db']:+.2f} dB | "
            f"{item['r_cross_hz']:.1f} Hz / {item['r_phase_deg']:.1f}° / {item['r_construct_db']:+.2f} dB |"
        )
    lines.extend([
        "",
        "## Odporność — wspólne perturbacje ±0,25 ms, ±0,5 dB i ±1 Hz",
        "",
        "| Wariant | L sprawność P10 / worst | R sprawność P10 / worst | Stereo 60–125 P10 / worst | Korpus 110–220 P10 / worst |",
        "|---|---:|---:|---:|---:|",
    ])
    for variant in (SAFE, SEAM):
        item = robust[variant.code]
        lines.append(
            f"| {variant.title} | {item['l_eff_delta_db']['p10']:+.2f} / {item['l_eff_delta_db']['worst']:+.2f} dB | "
            f"{item['r_eff_delta_db']['p10']:+.2f} / {item['r_eff_delta_db']['worst']:+.2f} dB | "
            f"{item['stereo_60_125_db']['p10']:+.2f} / {item['stereo_60_125_db']['worst']:+.2f} dB | "
            f"{item['body_110_220_db']['p10']:+.2f} / {item['body_110_220_db']['worst']:+.2f} dB |"
        )
    lines.extend([
        "",
        "## Hipoteza odsłuchowa",
        "",
        "- Reference Safe prawdopodobnie da nieco szybsze, czytelniejsze połączenie basu z C1 bez odchudzenia wokalu.",
        "- Seam/Phase może dać wyraźniejszy atak i separację, ale ma większe ryzyko przesunięcia balansu — wymaga krótkiego A/B.",
        "- Są to przewidywania z pomiaru RAW; zwycięstwo brzmieniowe wymaga pomiaru po wgraniu i odsłuchu.",
        "",
        "## Ważne ograniczenie",
        "",
        "Wartości fazy w tej tabeli dotyczą symulacji **przed nowym Diracem** przy -4 dB. Nie należy ich porównywać 1:1 z wcześniejszymi wartościami zmierzonego Slotu 4 po jego starym filtrze Dirac.",
    ])
    report = out / "SLOT4_SHORTLIST_VALIDATOR_REPORT.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    (out / "SLOT4_SHORTLIST_TEST_PLAN.md").write_text("\n".join([
        "# Slot4 Shortlist Validator — plan testu",
        "",
        "1. Nie zmieniaj właściwego Slotu 4 ze starym Diracem. Wgraj kontrolne odtworzenie A, Reference Safe B i Seam/Phase C do innych slotów albo testuj je kolejno.",
        "2. Dla wszystkich A/B/C wyłącz Dirac i PEQ, ustaw gain subów na -4 dB, REL crossover 120 Hz, polarity OFF/OFF.",
        "3. Dla każdego wariantu A/B/C wykonaj pełną serię 13 pomiarów 10–500 Hz, 256k lub 512k, ze wspólną Acoustic Timing Reference:",
        "   `L_MAIN`, `L_SUB`, `L_SUM1..3`, `R_MAIN`, `R_SUB`, `R_SUM1..3`, `LR_SUM1..3`.",
        "4. B i C porównuj z rzeczywiście zmierzonym A PRE-DIRAC, nie bezpośrednio z właściwym Slotem 4 mającym stary filtr Dirac. Odrzuć wariant, jeśli traci ponad 0,3 dB średnio w 110–220 Hz albo pogarsza prawą stronę/sumę stereo.",
        "5. Krótkie A/B/C bez nowego Diraca: A PRE-DIRAC control → Reference Safe → Seam/Phase, z wyrównaniem głośności.",
        "6. Nowy Dirac 231 Hz wykonaj tylko dla zwycięzcy przy gainie subów -4 dB. Po obliczeniu filtra przywróć 0 dB, wykonaj pomiar kontrolny i dopiero wtedy porównaj go z zamrożonym Slotem 4.",
        "",
        "Do bridge nie domierzaj `LR_SUM_SUB0`: nie jest potrzebny i grozi przesterowaniem.",
    ]) + "\n", encoding="utf-8")
    if not safe_codes:
        write_quarantine(out, safety_candidates, safety_results, mode="SLOT4_SHORTLIST_NO_SAFE_CANDIDATE")
        raise NoSafeCandidateError("Żaden wariant shortlisty nie przeszedł stanów Dirac i odsłuchowego.")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="REWDO v20.31 — Slot4 Shortlist Validator (REFERENCE/SAFE/SEAM)")
    parser.add_argument("--raw", required=True)
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reference-yaml", default=None, help="Zweryfikowana referencja 90 BW48; konflikt z plikiem Slot4 blokuje eksport")
    args = parser.parse_args(argv)
    try:
        report = build_project(
            Path(args.raw), Path(args.bridge), Path(args.out),
            Path(args.reference_yaml) if args.reference_yaml else None,
        )
    except NoSafeCandidateError as exc:
        print("BRAK BEZPIECZNEGO KANDYDATA SLOT4 SHORTLIST:", exc)
        print("Zapisano QUARANTINE_DIAGNOSTIC.json/csv i NIE_WGRYWAC_DO_MINIDSP.txt.")
        return 3
    except Exception as exc:
        print("BŁĄD SLOT4 SHORTLIST VALIDATOR:", exc)
        return 2
    print("SLOT4 SHORTLIST VALIDATOR gotowy:", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
