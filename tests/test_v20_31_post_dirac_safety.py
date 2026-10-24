"""Synthetic sine/linear responses and hand-built boundary cases; no private measurements. The missing reference path is isolated in tmp_path."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from rew_api_client import encode_float32_base64
from rew_common import Measurement
from rew_post_dirac_ab import (
    ImportedAnyMeasurement,
    PresetGroup,
    _aggregate_three_point_rows,
    _common_grid,
    _fmt,
    _group_measurements,
    _has_explicit_lr_sum_name,
    _infer_group_identity_from_lr,
    _is_candidate_measurement_name,
    _is_component_measurement_name,
    _is_generic_sum_name,
    _mic_position_from_name,
    _parse_xo_filter,
    _role_from_name,
    _score_between,
    _short_main_risk,
    _short_slot_decision,
    _short_slot_role,
    _three_point_score_for_rows,
    _v20_data_status,
    _v20_post_dirac_metrics,
    analyze_post_dirac_ab,
    main as post_main,
    write_reports,
)


def _measurement(name: str, *, phase_shift: float = 0.0, level: float = 72.0) -> Measurement:
    freq = np.geomspace(20.0, 10_000.0, 320)
    shape = level + 1.5 * np.sin(np.log(freq) * 1.7)
    phase = -25.0 * np.log2(freq / 90.0) + phase_shift
    return Measurement(name, freq, shape, phase)


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("EXCL: SHD, L+R, volume 1", "other"),
        ("RAW_L_MAIN", "component"),
        ("90_BW48_SLOT2_L+R", "sum"),
        ("90_BW48_SLOT2_L", "left"),
        ("90_BW48_SLOT2_R", "right"),
        ("90_BW48_SLOT2", "sum"),
        ("ordinary measurement", "other"),
    ],
)
def test_post_dirac_name_roles_are_fail_closed(name: str, role: str) -> None:
    assert _role_from_name(name) == role


def test_post_dirac_name_parsing_and_position_variants() -> None:
    assert _parse_xo_filter("96 Hz LR48 slot3") == (96, "LR48")
    assert _parse_xo_filter("no crossover") == (None, "")
    assert _is_component_measurement_name("R-sub raw")
    assert _has_explicit_lr_sum_name("Głośniki L / P stereo")
    assert not _has_explicit_lr_sum_name("LR48")
    assert _is_candidate_measurement_name("90_BW48_SLOT2_L+R")
    assert not _is_candidate_measurement_name("notes.txt")
    assert not _is_candidate_measurement_name("folder/name")
    assert _is_generic_sum_name("Głośniki L+P")
    assert not _is_generic_sum_name("90_BW48_L+R")
    for name, expected in (
        ("slot posL L+R", "L"),
        ("slot L-pos L+R", "L"),
        ("slot posP L+R", "P"),
        ("slot right-pos L+R", "P"),
        ("slot sweetspot L+R", "MLP"),
        ("slot no-position L+R", "MLP"),
    ):
        position, explicit = _mic_position_from_name(name)
        assert position == expected
        assert explicit is ("no-position" not in name)


def test_generic_sum_identity_binding_handles_missing_invalid_conflict_and_valid() -> None:
    generic = PresetGroup(
        key="generic", label="Głośniki L+P", xo_hz=None, filter_name="",
        generic_sum_binding=True, sum_measurement=_measurement("Głośniki L+P"),
    )
    _infer_group_identity_from_lr(generic)
    assert "brak sąsiednich" in generic.binding_warning

    generic.left_measurement = _measurement("left unnamed")
    _infer_group_identity_from_lr(generic)
    assert "nie zawierają" in generic.binding_warning

    generic.left_measurement = _measurement("90_BW48_SLOT2_L")
    generic.right_measurement = _measurement("102_BW48_SLOT3_R")
    _infer_group_identity_from_lr(generic)
    assert "różne XO" in generic.binding_warning

    generic.right_measurement = _measurement("90_BW48_SLOT2_R_INV_DIRAC_ON")
    _infer_group_identity_from_lr(generic)
    assert generic.xo_hz == 90
    assert generic.filter_name == "BW48"
    assert generic.label_source == "inferred_from_adjacent_LR"
    assert "INV" in generic.label and "Dirac" in generic.label


def test_grouping_binds_sum_then_lr_and_marks_duplicate_generic_sums() -> None:
    items = [
        ImportedAnyMeasurement("Głośniki L+P", 1, _measurement("Głośniki L+P")),
        ImportedAnyMeasurement("90_BW48_SLOT2_L", 2, _measurement("90_BW48_SLOT2_L")),
        ImportedAnyMeasurement("90_BW48_SLOT2_R", 3, _measurement("90_BW48_SLOT2_R", phase_shift=5)),
        ImportedAnyMeasurement("Głośniki L+P", 4, _measurement("Głośniki L+P")),
    ]
    groups = _group_measurements(items)
    assert groups[0].label_source == "inferred_from_adjacent_LR"
    assert groups[0].left_measurement is not None and groups[0].right_measurement is not None
    assert "DUPLIKAT NAZWY" in groups[1].binding_warning


def test_post_dirac_numeric_helpers_cover_missing_and_edge_data() -> None:
    a = _measurement("a")
    b = Measurement("b", np.geomspace(30.0, 200.0, 32), np.zeros(32), np.zeros(32))
    grid = _common_grid([a, b], low=35.0, high=180.0, points=50)
    assert grid[0] == pytest.approx(35.0)
    with pytest.raises(ValueError, match="Brak wspólnego zakresu"):
        _common_grid([a, b], low=500.0, high=600.0)
    assert _fmt(None) == "—"
    assert _fmt(float("nan")) == "—"
    assert _fmt("tekst") == "tekst"
    assert _score_between("bad", 1.0, 0.0) == 50.0
    assert _score_between(float("nan"), 1.0, 0.0) == 50.0
    assert _score_between(2.0, 1.0, 0.0) == 100.0
    assert _score_between(-1.0, 1.0, 0.0) == 0.0
    assert _score_between(-1.0, 0.0, 1.0, higher_is_better=False) == 100.0
    assert _score_between(2.0, 0.0, 1.0, higher_is_better=False) == 0.0


def _three_row(position: str, shift: float, *, explicit: str = "TAK") -> dict[str, object]:
    return {
        "label": "96 Hz BW48 slot3",
        "source_names": f"96_BW48_SLOT3_pos{position}_L+R",
        "pozycja_mikrofonu": position,
        "pozycja_mikrofonu_jawna": explicit,
        "sum_margin_mean_75_125_db": 1.0 + shift,
        "sum_margin_worst_75_125_db": -0.5 - abs(shift),
        "dip_88_104_vs_shoulders_db": -0.2 - abs(shift),
        "body_125_220_vs_75_125_db": 1.2 + shift,
        "gd_excess_75_125_ms": 20.0 + abs(shift) * 4,
        "spl_span_75_125_db": 3.0 + abs(shift),
    }


def test_three_point_aggregation_reports_complete_partial_and_duplicates() -> None:
    rows = [_three_row("L", -0.2), _three_row("MLP", 0.0), _three_row("P", 0.3), _three_row("P", 0.4)]
    score, values, note = _three_point_score_for_rows(rows[:3])
    assert 0.0 <= score <= 100.0
    assert np.isfinite(values["rozrzut_sumowania_3pkt_db"])
    assert note
    aggregated = _aggregate_three_point_rows(rows)
    assert len(aggregated) == 1
    assert aggregated[0]["pomiar_3pkt_status"] == "PEŁNY L/MLP/P"
    assert "duplikaty pozycji: P" in aggregated[0]["uwagi_3pkt"]

    partial = _aggregate_three_point_rows([_three_row("L", 0.0), _three_row("MLP", 0.0)])
    assert str(partial[0]["pomiar_3pkt_status"]).startswith("CZĘŚCIOWY")
    single = _aggregate_three_point_rows([_three_row("MLP", 0.0, explicit="NIE")])
    assert str(single[0]["pomiar_3pkt_status"]).startswith("BRAK")


def test_short_decision_helpers_expose_risks_and_data_status() -> None:
    ref = {"label": "90 ref"}
    candidate = {
        "label": "102 candidate", "decision_class": "NIE ZASTĘPUJ",
        "pomiar_3pkt_status": "CZĘŚCIOWY", "mid_texture_delta_vs_reference_db": -2.0,
        "ryzyko_dziury_lp_po_dirac_0_100": 70.0, "odpornosc_3pkt_l_mlp_p_0_100": 40.0,
        "listening_comfort_0_100": 30.0,
    }
    assert _short_slot_role(ref, ref) == "referencja tekstury"
    assert _short_slot_role(candidate, ref) == "kandydat warunkowy"
    assert _short_slot_decision(candidate, ref) == "A/B tylko ostrożnie"
    assert "chudsza" in _short_main_risk(candidate)
    assert _v20_data_status(candidate) == "POMIAR CZĘŚCIOWY"
    assert _v20_data_status({"model_odpornosci_v20_0_100": 50.0}).startswith("POJEDYNCZY")
    assert _v20_data_status({}) == "BRAK DANYCH"


def test_v20_metric_model_covers_good_and_bad_labels() -> None:
    good = {
        "has_lr": True, "spl_span_75_125_db": 2.0, "spl_std_75_125_db": 0.7,
        "gd_excess_75_125_ms": 12.0, "gd_spread_75_125_ms": 5.0,
        "phase_resid_rms_75_125_deg": 12.0, "phase_resid_p2p_75_125_deg": 40.0,
        "sum_margin_mean_75_125_db": 2.0, "sum_margin_worst_75_125_db": -0.2,
        "sum_margin_at_xo_db": 2.0, "sum_margin_at_95_db": 2.0, "sum_margin_at_100_db": 2.0,
        "lr_phase_at_xo_deg": 20.0, "lr_phase_mean_75_125_deg": 25.0,
        "lr_balance_mean_75_125_db": 0.5, "lr_balance_max_75_125_db": 1.0,
        "fog_bloom_risk_0_100": 20.0, "listening_comfort_0_100": 90.0,
        "midrange_safety_score_0_100": 90.0, "odpornosc_3pkt_l_mlp_p_0_100": 90.0,
        "ryzyko_dziury_3pkt_0_100": 10.0,
    }
    good_metrics = _v20_post_dirac_metrics(good)
    assert good_metrics["etykieta_modelu_v20"] == "odporny preset po Diracu"
    bad = dict(good)
    bad.update({
        "has_lr": False, "sum_margin_at_xo_db": -8.0, "sum_margin_worst_75_125_db": -8.0,
        "sum_margin_at_95_db": -8.0, "sum_margin_at_100_db": -8.0,
        "gd_excess_75_125_ms": 100.0, "odpornosc_3pkt_l_mlp_p_0_100": float("nan"),
        "ryzyko_dziury_3pkt_0_100": float("nan"),
    })
    bad_metrics = _v20_post_dirac_metrics(bad)
    assert bad_metrics["etykieta_modelu_v20"] in {"wysokie ryzyko dziury L+P", "duże obciążenie korekcji"}
    assert "brak osobnych L/R" in bad_metrics["uwagi_modelu_v20"]


def _api_response(level: float, phase_offset: float, *, points: int = 300) -> dict[str, object]:
    return {
        "smoothing": "None", "startFreq": 20.0, "freqStep": 32.0,
        "magnitudes": encode_float32_base64(np.linspace(level, level - 2.0, points)),
        "phases": encode_float32_base64(np.linspace(-100.0 + phase_offset, 30.0 + phase_offset, points)),
    }


def _three_position_snapshot(path: Path) -> None:
    measurements = []
    index = 0
    for xo, slot in ((90, 2), (82, 3), (102, 4)):
        for position, offset in (("posL", -0.2), ("MLP", 0.0), ("posP", 0.25)):
            base = f"{xo}_BW48_SLOT{slot}_{position}"
            # Suma najpierw, potem L/R: adapter wiąże kanały z właściwym presetem.
            for suffix, phase in (("L+R", 0.0), ("L", -4.0), ("R", 4.0)):
                index += 1
                measurements.append({
                    "summary": {"title": f"{base}_{suffix}"},
                    "frequencyResponse": _api_response(72.0 + offset + (xo - 90) * 0.01, phase + index * 0.02),
                    "decodedInfo": {"ppoRequested": None},
                })
    payload = {
        "format": "REWDO_REW_API_SNAPSHOT", "formatVersion": 3,
        "acquisition": {
            "nativeAxisRequested": True, "ppoRequested": None,
            "noSmoothingRequested": True, "smoothingRequested": "None",
        },
        "measurements": measurements,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_post_dirac_three_position_api_e2e_and_cli(tmp_path: Path) -> None:
    source = tmp_path / "three.rewapi.json"
    _three_position_snapshot(source)
    out = tmp_path / "out"
    rows = analyze_post_dirac_ab(
        source, out, reference="90_BW48_SLOT2",
        texture_reference_yaml=tmp_path / "SYNTHETIC_REFERENCE_MISSING.yaml",
    )
    assert len(rows) == 3
    assert all(row["pomiar_3pkt_status"] == "PEŁNY L/MLP/P" for row in rows)
    assert any("82" in str(row["label"]) for row in rows)
    assert (out / "03_DIAGNOSTYKA_3PUNKTOWA.md").exists()

    cli_out = tmp_path / "cli"
    assert post_main(["--source", str(source), "--out", str(cli_out), "--reference", "90_BW48_SLOT2"]) == 0
    assert (cli_out / "FINAL_DECISION_REPORT.md").exists()
    assert post_main(["--source", str(tmp_path / "missing.mdat"), "--out", str(tmp_path / "bad")]) == 2

    # Brak referencji musi pozostać diagnostyką, również przy ponownym renderze raportu.
    no_ref_out = tmp_path / "no_ref"
    candidates_only = [row for row in rows if "90" not in str(row.get("label", ""))]
    write_reports(candidates_only, no_ref_out, mdat=str(source), reference="nieistniejąca")
    assert "Brak 90 BW48" in (no_ref_out / "FINAL_DECISION_REPORT.md").read_text(encoding="utf-8")
