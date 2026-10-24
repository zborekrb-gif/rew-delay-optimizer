"""Independent synthetic repeatability cases; no values copied from reports.

Three identical analytic references establish zero spread. One candidate has
an eight millisecond anchor mismatch; another only reduces bass level while
keeping every time metric unchanged. Neither condition justifies promotion.
"""
from __future__ import annotations

import numpy as np

from rew_time_domain_analyzer import TimeMeasurement, analyse_variants


def _synthetic_item(name, role, *, bass_db=0.0, anchor_ms=8.0):
    freq = np.arange(20.0, 251.0, 1.0)
    spl = np.full(freq.size, 75.0)
    spl[(freq >= 40.0) & (freq <= 90.0)] += bass_db
    item = TimeMeasurement(name=name, role=role, freq=freq,
                           spl_db=spl, phase_deg=np.zeros(freq.size))
    item.composite_late100_db = -24.0
    item.composite_t20_ms = 150.0
    item.gd_p90_45_110_ms = 12.0
    item.gd_anchor_ms = anchor_ms
    item.gd_source = "SYNTHETIC"
    return item


def test_synthetic_gain_reduction_and_timing_mismatch_are_not_promoted():
    roles = [("L_STRONA", "L Dirac ON"), ("R_STRONA", "R Dirac ON"),
             ("L+R", "L+R Dirac ON")]
    items = []
    for tag in ("SLOT2", "REPEAT2", "REPEAT3"):
        for suffix, role in roles:
            items.append(_synthetic_item(f"REF_{tag}_80LR24_G0_DIRAC_ON_{suffix}", role))
    for suffix, role in roles:
        items.append(_synthetic_item(f"SYNTH_A_80LR24_G-2_DIRAC_ON_{suffix}", role,
                                    bass_db=-2.0, anchor_ms=16.0 if suffix == "L_STRONA" else 8.0))
        items.append(_synthetic_item(f"SYNTH_B_80LR24_G-3_DIRAC_ON_{suffix}", role,
                                    bass_db=-3.0))
    profiles, variants, family, warnings, refs = analyse_variants(items, "REF_SLOT2_80LR24_G0")
    assert family == "80lr24 g0 dirac on"
    assert len(refs) == 9
    assert all(profile.reliable for profile in profiles.values())
    by_name = {variant.variant_id: variant for variant in variants}
    assert by_name["SYNTH_A_80LR24_G-2_DIRAC_ON"].verdict == "DANE CZASOWE NIESPÓJNE"
    assert by_name["SYNTH_B_80LR24_G-3_DIRAC_ON"].verdict != "POTENCJALNIE LEPSZY"
    assert all("REF_REPEAT" not in variant.variant_id for variant in variants)
