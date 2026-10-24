#!/usr/bin/env python3
"""Generate the deterministic POST-DIRAC API fixture used by v20.31 validation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from rew_api_client import encode_float32_base64


def build_fixture() -> dict[str, object]:
    measurements: list[dict[str, object]] = []
    points = 300
    index = 0
    for xo, slot in ((90, 2), (96, 3)):
        for position, offset in (("posL", -0.2), ("MLP", 0.0), ("posP", 0.25)):
            base = f"{xo}_BW48_SLOT{slot}_{position}"
            for suffix, phase in (("L+R", 0.0), ("L", -4.0), ("R", 4.0)):
                index += 1
                level = 72.0 + offset + (xo - 90) * 0.01
                response = {
                    "smoothing": "None",
                    "startFreq": 20.0,
                    "freqStep": 32.0,
                    "magnitudes": encode_float32_base64(np.linspace(level, level - 2.0, points)),
                    "phases": encode_float32_base64(
                        np.linspace(-100.0 + phase + index * 0.02, 30.0 + phase + index * 0.02, points)
                    ),
                }
                measurements.append({
                    "summary": {"title": f"{base}_{suffix}"},
                    "frequencyResponse": response,
                    "decodedInfo": {"ppoRequested": None},
                })
    return {
        "format": "REWDO_REW_API_SNAPSHOT",
        "formatVersion": 3,
        "acquisition": {
            "nativeAxisRequested": True,
            "ppoRequested": None,
            "noSmoothingRequested": True,
            "smoothingRequested": "None",
        },
        "measurements": measurements,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_fixture(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
