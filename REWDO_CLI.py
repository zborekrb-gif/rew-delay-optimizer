#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REWDO CLI helper for GUI ONEDIR builds.

Ten moduł działa jako wewnętrzny helper dla jednego pliku REWDO_GUI.exe
uruchamianego z parametrem --rewdo-cli. Wymuszamy UTF-8 na stdout/stderr,
żeby polskie znaki poprawnie trafiały do okna logu GUI.
"""
from __future__ import annotations

import sys


SUBCOMMANDS = {
    "raw_check": "RAW Measurement Check",
    "crossover_ranking": "RAW crossover ranking / reference-neighborhood / between",
    "post_dirac": "POST-DIRAC A/B analyzer — wspólne źródło MDAT/API",
    "verify_slot": "Verify User Slot SHD",
    "filter_generator": "Generator filtrów zwrotnicy miniDSP",
    "acoustic_crossover": "Akustyczny selektor standardowych BW48/LR48 z RAW",
    "acoustic_biquad": "Projektant biquadów — kalibracja realnymi pomiarami i końcowe dostrojenie opóźnień",
    "calibrate_empirical": "Kalibrator predykcja–pomiar dla pełnego pomiaru PRE-DIRAC",
    "time_domain": "Analiza czasowa / 90 BW48 Fast — impuls, decay i group delay",
    "gain_bridge": "Amplitudowy Gain Bridge L/R bez LR_SUM_SUB0",
    "slot4max": "Slot 4 Shortlist Validator — tylko REFERENCE/SAFE/SEAM",
}


def _usage() -> str:
    lines = [
        "REWDO_CLI.exe — helper dla REWDO_GUI.exe",
        "",
        "Użycie:",
        "  REWDO_CLI.exe raw_check --mdat PLIK.mdat --out FOLDER",
        "  REWDO_CLI.exe crossover_ranking --mdat PLIK.mdat --out FOLDER ...",
        "  REWDO_CLI.exe post_dirac --source PLIK.mdat --out FOLDER --reference 90_BW48_slot2",
        "  REWDO_CLI.exe verify_slot --l-main 30 --r-main 30 --l-sub 20.4 --r-sub 19.1 --out VERIFY.md",
        "  REWDO_CLI.exe filter_generator --out FOLDER",
        "  REWDO_CLI.exe acoustic_crossover --mdat RAW_MAIN_SUB.mdat --gain-bridge BRIDGE.mdat --out FOLDER [--raw-sub-trim-db -6 --dirac-sub-trim-db -4 --listening-sub-trim-db 0]",
        "  REWDO_CLI.exe acoustic_biquad --mdat RAW_MAIN_SUB.mdat --gain-bridge BRIDGE.mdat --out FOLDER [--search-level quick|normal|deep] [--raw-sub-trim-db -6 --dirac-sub-trim-db -4 --listening-sub-trim-db 0] [--cut-only-mode on|off] [--all-pass-mode off|compare] [--enable-empirical-calibration]",
        "  REWDO_CLI.exe calibrate_empirical --prediction-csv WYNIK.csv --candidate-code KOD --measured-mdat POMIAR.mdat --xo-hz 92 --topology-id ID --state-id ID --calibration-json STARY.json --out-json NOWY.json",
        "  REWDO_CLI.exe time_domain --snapshot POMIARY.rewapi.json --out FOLDER --reference REF_SLOT2_90BW48_G0",
        "  REWDO_CLI.exe gain_bridge --raw RAW.mdat --bridge BRIDGE.mdat --out FOLDER",
        "  REWDO_CLI.exe slot4max --raw RAW.mdat --bridge BRIDGE.mdat --reference-yaml USER_REFERENCE_90BW48.yaml --out FOLDER",
        "",
        "Subkomendy:",
    ]
    for key, desc in SUBCOMMANDS.items():
        lines.append(f"  {key:18s} {desc}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", line_buffering=True, write_through=True, errors="replace")
        if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", line_buffering=True, write_through=True, errors="replace")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(_usage())
        return 0 if argv else 1

    cmd = argv.pop(0).strip().lower()

    if cmd == "raw_check":
        from rew_raw_measurement_check import main as raw_main
        return int(raw_main(argv))

    if cmd == "crossover_ranking":
        from rew_crossover_ranking import main as ranking_main
        return int(ranking_main(argv))

    if cmd == "post_dirac":
        from rew_post_dirac_ab import main as post_main
        return int(post_main(argv))

    if cmd == "verify_slot":
        from rew_verify_user_slot import main as verify_main
        return int(verify_main(argv))

    if cmd == "filter_generator":
        from rew_filter_generator import main as filter_main
        return int(filter_main(argv))

    if cmd == "acoustic_crossover":
        from rew_acoustic_crossover_designer import main as acoustic_main
        return int(acoustic_main(argv))

    if cmd == "acoustic_biquad":
        from rew_acoustic_biquad_designer import main as biquad_main
        return int(biquad_main(argv))

    if cmd == "calibrate_empirical":
        from rew_prediction_measurement_calibrator import main as calibrator_main
        return int(calibrator_main(argv))

    if cmd == "time_domain":
        from rew_time_domain_analyzer import main as time_main
        return int(time_main(argv))

    if cmd == "gain_bridge":
        from rew_gain_bridge import main as gain_bridge_main
        return int(gain_bridge_main(argv))

    if cmd == "slot4max":
        from rew_slot4max_project import main as slot4max_main
        return int(slot4max_main(argv))

    print(f"BŁĄD: nieznana subkomenda: {cmd}")
    print("")
    print(_usage())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
