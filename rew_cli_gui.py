# -*- coding: utf-8 -*-
"""Punkt wejścia CLI i GUI Tkinter."""
from __future__ import annotations

import time

from rew_common import *
from rew_mdat import *
from rew_crossover import *
from rew_scoring import *
from rew_reports import *
from rew_app_prefs import load_app_preferences, save_app_preferences


def refresh_candidate_outputs(
    candidates: List[Candidate],
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    out_dir: str | Path,
    cfg: OptimizerConfig,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    input_warnings: Optional[List[str]] = None,
    log_callback=None,
    full_outputs: bool = True,
) -> None:
    """Wspólny zapis wyników.

    v19.44: OFF/OFF recommendation consistency; heatmap cleanup retained; ciężkie pliki końcowe zapisujemy tylko raz, po walidacji i ewentualnym
    SMART RESCAN. Dzięki temu końcówka nie robi podwójnie wykresów, heatmap i
    diagnostyki czasu, co było widoczne jako spowolnienie pod koniec pracy.
    """
    out_dir = Path(out_dir)
    save_candidates_csv(candidates, out_dir / "ranking.csv", l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg)
    write_report(candidates, out_dir / "raport.md", cfg, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, input_warnings or [])
    if not candidates:
        return
    if not full_outputs:
        return
    # v19.44: wszystkie główne wykresy muszą opisywać dokładnie ten sam
    # wariant OFF/OFF, który jest pokazywany w tabeli i w komunikacie końcowym.
    operational, _policy, _note = off_off_priority_main_candidate(candidates)
    if operational is None:
        return
    plot_candidate(
        freq, responses, operational, out_dir / "best", cfg,
        l_base_delay_ms, r_base_delay_ms, plot_label="Główna rekomendacja OFF/OFF"
    )
    for msg in create_followup_outputs(
        freq,
        responses,
        candidates,
        out_dir,
        cfg,
        l_base_delay_ms,
        r_base_delay_ms,
        crossover_label,
        filter_type,
        slope_db_oct,
    ):
        if log_callback is not None:
            log_callback(msg)

    # v19.44: nie kopiujemy raportów z podfolderów z powrotem do katalogu głównego.
    # find_result_file() wyszukuje je w uporządkowanej strukturze wyników.

def run_cli(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    mdat_scan: Optional[Dict[str, object]] = None
    mdat_project: Optional[MdatImportedProject] = None
    use_direct_mdat = bool(getattr(args, "mdat", None))
    input_mode_label = "MDAT direct" if use_direct_mdat else "TXT exports"

    # v19.3: wybrany .mdat jest domyślnym źródłem danych liczbowych do optymalizacji.
    if getattr(args, "mdat", None):
        try:
            md_path, csv_path, mdat_scan = write_mdat_scan_report(args.mdat, out_dir)
            print(mdat_scan_summary_text(mdat_scan))
            if bool(getattr(args, "mdat_only", False)) or bool(getattr(args, "mdat_check", False)):
                # Dla --mdat-check wykonamy jeszcze import numeryczny i health-check poniżej.
                pass
        except Exception as e:
            print("BŁĄD skanowania MDAT:", e, file=sys.stderr)
            if bool(getattr(args, "mdat_only", False)) or use_direct_mdat:
                return 2

    crossover_label = args.crossover
    filter_type = args.filter_type
    slope_db_oct = int(args.slope)

    auto_warnings: List[str] = []
    if args.auto_detect_profile or crossover_label == "Auto":
        if use_direct_mdat and mdat_scan is not None:
            profile_paths = {}
            for m in list(mdat_scan.get("measurements", [])):  # type: ignore[arg-type]
                if isinstance(m, MdatMeasurementInfo):
                    profile_paths[m.role] = m.name
        else:
            profile_paths = {
                "L main": args.l_main or "",
                "L sub": args.l_sub or "",
                "R main": args.r_main or "",
                "R sub": args.r_sub or "",
            }
        detected_cross, detected_filter, detected_slope, auto_warnings = infer_profile_from_paths(profile_paths)
        if detected_cross:
            crossover_label = detected_cross
        elif crossover_label == "Auto":
            crossover_label = "Custom"
        if detected_filter:
            filter_type = detected_filter
        if detected_slope:
            slope_db_oct = int(detected_slope)

    if crossover_label in CROSSOVER_PRESETS and CROSSOVER_PRESETS[crossover_label] is not None:
        e_low, e_high, b_low, b_high = CROSSOVER_PRESETS[crossover_label]
    else:
        e_low, e_high, b_low, b_high = args.eval_low, args.eval_high, args.bulge_low, args.bulge_high
    filter_defaults = filter_profile_defaults(filter_type, slope_db_oct)
    explicit_bulge_weight = getattr(args, "bulge_penalty_weight", None)
    bulge_weight = resolve_bulge_penalty_weight(filter_defaults, getattr(args, "bulge_strength", None), explicit_bulge_weight)
    explicit_peak_weight = getattr(args, "bulge_peak_penalty_weight", None)
    bulge_peak_weight = (
        (float(explicit_peak_weight) if explicit_peak_weight is not None else float(filter_defaults["bulge_peak_penalty_weight"]))
        if bool(getattr(args, "bulge_peak", True))
        else 0.0
    )
    bulge_tolerance = (
        float(args.bulge_tolerance)
        if getattr(args, "bulge_tolerance", None) is not None
        else float(filter_defaults["bulge_tolerance_db"])
    )

    cfg = OptimizerConfig(
        delay_min_ms=args.delay_min,
        delay_max_ms=args.delay_max,
        delay_step_ms=args.delay_step,
        eval_low_hz=e_low,
        eval_high_hz=e_high,
        bulge_low_hz=b_low,
        bulge_high_hz=b_high,
        top_n=args.top,
        practical_score_tolerance_db=float(getattr(args, "practical_score_tolerance", 0.75)),
        freq_points=args.freq_points,
        max_cartesian=args.max_cartesian,
        prefer_smoothness=float(filter_defaults["prefer_smoothness"]),
        prefer_lr_sum=float(filter_defaults["prefer_lr_sum"]),
        dip_penalty_weight=float(filter_defaults["dip_penalty_weight"]),
        bulge_penalty_weight=bulge_weight,
        bulge_tolerance_db=bulge_tolerance,
        bulge_peak_penalty_weight=bulge_peak_weight,
        use_psycho_bulge_model=bool(filter_defaults["use_psycho_bulge_model"]) if not args.simple_bulge_model else False,
        filter_type=filter_type,
        slope_db_oct=slope_db_oct,
        safe_midrange_priority=not bool(getattr(args, "no_safe_midrange", False)),
        scoring_profile=getattr(args, "score_profile", "PBIS"),
        sound_quality_safe=not bool(getattr(args, "no_sound_quality_safe", False)),
        fast_two_stage=not bool(getattr(args, "exact_scan", False)),
        fast_coarse_step_ms=float(getattr(args, "fast_coarse_step", 0.5)),
        fast_refine_radius_ms=float(getattr(args, "fast_refine_radius", 0.6)),
        fast_seed_n=int(getattr(args, "fast_seed_n", 80)),
        fast_exact_threshold=int(getattr(args, "fast_exact_threshold", 30000)),
        fast_validation_enabled=not bool(getattr(args, "no_fast_validation", False)),
        fast_validation_radius_ms=float(getattr(args, "fast_validation_radius", 0.6)),
        fast_validation_close_score=float(getattr(args, "fast_validation_close_score", 1.0)),
        fast_smart_rescan_enabled=not bool(getattr(args, "no_smart_rescan", False)),
        fast_smart_rescan_radius_ms=float(getattr(args, "smart_rescan_radius", 1.5)),
        fast_smart_rescan_seed_n=int(getattr(args, "smart_rescan_seed_n", 8)),
        fast_smart_rescan_max_combinations=int(getattr(args, "smart_rescan_max_combinations", 80000)),
        fast_auto_coarse_safety=not bool(getattr(args, "no_fast_coarse_safety", False)),
        fast_max_coarse_combinations=int(getattr(args, "fast_max_coarse_combinations", 6000)),
        relative_delay_shd_mode=bool(getattr(args, "relative_shd", False)),
        shd_delay_limit_ms=float(getattr(args, "shd_limit", 30.0)),
        baseline_l_main_ms=float(getattr(args, "l_main_base_delay", 0.0)),
        baseline_r_main_ms=float(getattr(args, "r_main_base_delay", 0.0)),
        baseline_l_sub_ms=float(getattr(args, "l_base_delay", 0.0)),
        baseline_r_sub_ms=float(getattr(args, "r_base_delay", 0.0)),
    )

    l_base_delay_ms = args.l_base_delay
    r_base_delay_ms = args.r_base_delay
    if use_direct_mdat:
        # v19.51: nazwa projektu może jawnie opisywać delay mainów, np. main30.
        # Nazwy pomiarów wewnątrz MDAT analogicznie opisują bazę subów.
        inferred_main = float(infer_main_delay_from_filename(args.mdat))
        cfg.baseline_l_main_ms = inferred_main
        cfg.baseline_r_main_ms = inferred_main
        if isinstance(mdat_scan, dict):
            l_base_delay_ms = 0.0
            r_base_delay_ms = 0.0
            for m in list(mdat_scan.get("measurements", [])):  # type: ignore[arg-type]
                if isinstance(m, MdatMeasurementInfo) and m.base_delay_ms is not None:
                    if m.role == "L sub":
                        l_base_delay_ms = float(m.base_delay_ms)
                    elif m.role == "R sub":
                        r_base_delay_ms = float(m.base_delay_ms)
        cfg.baseline_l_sub_ms = float(l_base_delay_ms)
        cfg.baseline_r_sub_ms = float(r_base_delay_ms)
        if any(abs(v) > 1e-9 for v in (inferred_main, l_base_delay_ms, r_base_delay_ms)):
            cfg.relative_delay_shd_mode = True
            if abs(float(args.delay_min) - 0.0) < 1e-12 and abs(float(args.delay_max) - 30.0) < 1e-12:
                cfg.delay_min_ms = -30.0
                cfg.delay_max_ms = 30.0

    print("Wczytuję dane REW...")
    if use_direct_mdat:
        base = baseline_shd_settings(cfg, l_base_delay_ms, r_base_delay_ms)
        print(
            "Baza fizyczna MDAT: "
            f"main L/R {float(base['l_main_ms']):.2f}/{float(base['r_main_ms']):.2f} ms | "
            f"sub L/R {float(base['l_sub_ms']):.2f}/{float(base['r_sub_ms']):.2f} ms | "
            f"rel. sub-main {float(base['l_sub_minus_main_ms']):+.2f}/{float(base['r_sub_minus_main_ms']):+.2f} ms"
        )
    if use_direct_mdat:
        print("Tryb wejścia: bezpośredni import .mdat bez ręcznych eksportów TXT.")
        try:
            l_main, l_sub, r_main, r_sub, mdat_project = load_optimizer_measurements_from_mdat(args.mdat)
            md_num, csv_num = write_mdat_numeric_import_report(mdat_project, out_dir)
            health_report = write_mdat_health_report(mdat_project, out_dir)
            errors, warnings_mdat = mdat_import_health_checks(mdat_project)
            print(f"Zaimportowano .mdat: {mdat_project.points} punktów, {mdat_project.freq_start_hz:.2f}–{mdat_project.freq_end_hz:.2f} Hz.")
            for w in warnings_mdat:
                print("OSTRZEŻENIE MDAT:", w)
            if errors:
                for e in errors:
                    print("BŁĄD MDAT:", e, file=sys.stderr)
                return 2
            if bool(getattr(args, "mdat_check", False)) or bool(getattr(args, "mdat_only", False)):
                RESULTS_MANAGER.write_project_status(
                    out_dir,
                    input_mode=input_mode_label,
                    mdat_path=getattr(args, "mdat", None),
                    mdat_project=mdat_project,
                    candidates=None,
                    stage="mdat-check",
                )
                return 0
            if is_raw_xover_off_project(mdat_project):
                if crossover_label in {"Auto", "Custom"}:
                    raise ValueError("Dla pomiaru RAW wybierz konkretny preset crossovera, np. 90 Hz.")
                cfg.relative_delay_shd_mode = True
                cfg.shd_delay_limit_ms = float(getattr(args, "shd_limit", 30.0))
                if abs(float(args.delay_min) - 0.0) < 1e-12 and abs(float(args.delay_max) - 30.0) < 1e-12:
                    cfg.delay_min_ms = -30.0
                    cfg.delay_max_ms = 30.0
                print(f"Tryb RAW / relatywny SHD: ON | relacja sub-main {cfg.delay_min_ms:+.2f}..{cfg.delay_max_ms:+.2f} ms | limit 0..{cfg.shd_delay_limit_ms:.2f} ms")
                xo_hz = parse_crossover_hz(crossover_label, cfg)
                l_main, l_sub, r_main, r_sub = simulate_shd_crossover_from_raw(
                    l_main, l_sub, r_main, r_sub, xo_hz, filter_type, slope_db_oct
                )
                setattr(cfg, "raw_crossover_simulation", True)
                setattr(cfg, "raw_crossover_hz", float(xo_hz))
                write_raw_simulation_note(out_dir, xo_hz, filter_type, slope_db_oct)
                print(raw_simulation_description(xo_hz, filter_type, slope_db_oct))
        except Exception as e:
            print("BŁĄD importu numerycznego MDAT:", e, file=sys.stderr)
            return 2
        input_paths = mdat_measurement_path_labels(mdat_project)
    else:
        required = [args.l_main, args.l_sub, args.r_main, args.r_sub]
        if any(x is None for x in required):
            print("BŁĄD: podaj --mdat albo komplet --l-main --l-sub --r-main --r-sub.", file=sys.stderr)
            return 2
        l_main = parse_rew_text(args.l_main, "L_main")
        l_sub = parse_rew_text(args.l_sub, "L_sub")
        r_main = parse_rew_text(args.r_main, "R_main")
        r_sub = parse_rew_text(args.r_sub, "R_sub")
        input_paths = {"L main": args.l_main, "L sub": args.l_sub, "R main": args.r_main, "R sub": args.r_sub}

    input_warnings = build_input_warnings(
        input_paths,
        {"L_main": l_main, "L_sub": l_sub, "R_main": r_main, "R_sub": r_sub},
        cfg,
        l_base_delay_ms,
        r_base_delay_ms,
    )
    if auto_warnings:
        print("\nAUTODETEKCJA PROFILU:")
        print(f"- Używany profil: {profile_label(crossover_label, filter_type, slope_db_oct)}")
        for w in auto_warnings:
            print("- " + w)
        print("")
    if input_warnings:
        print("\nOSTRZEŻENIA WEJŚCIA:")
        for w in input_warnings:
            print("- " + w)
        print("")

    def prog(done, total):
        pct = 100.0 * done / max(1, total)
        print(f"\rSkanuję kombinacje: {pct:5.1f}%", end="", flush=True)

    timings: Dict[str, float] = {}
    validation_confidence = ""
    smart_status = ""
    print(optimizer_mode_summary(cfg))
    t0 = time.perf_counter()
    freq, responses, candidates = optimize_delays(l_main, l_sub, r_main, r_sub, cfg, progress_callback=prog)
    timings["optimization"] = time.perf_counter() - t0
    print("\nGotowe.")

    if candidates:
        t0 = time.perf_counter()
        validation_confidence, validation_path, validation_notes = write_fast_result_validation(
            candidates, freq, responses, out_dir, cfg,
            l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct,
        )
        timings["fast_validation"] = time.perf_counter() - t0
        print(f"Kontrola wyniku: {simple_validation_label(validation_confidence)}")
        t0 = time.perf_counter()
        candidates_after_smart, smart_status, smart_path, smart_notes = write_smart_rescan_if_needed(
            candidates, freq, responses, out_dir, cfg,
            l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct,
            validation_confidence=validation_confidence,
            validation_notes=validation_notes,
        )
        timings["smart_rescan"] = time.perf_counter() - t0
        if smart_status not in {"SKIPPED", "DISABLED"}:
            candidates = candidates_after_smart
            t0 = time.perf_counter()
            validation_confidence, validation_path, validation_notes = write_fast_result_validation(
                candidates, freq, responses, out_dir, cfg,
                l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct,
            )
            timings["fast_validation_after_smart"] = time.perf_counter() - t0
        print(f"Dodatkowe sprawdzenie: {simple_rescan_label(smart_status)}")

    t0 = time.perf_counter()
    refresh_candidate_outputs(
        candidates, freq, responses, out_dir, cfg,
        l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, input_warnings
    )
    timings["write_outputs_final_once"] = time.perf_counter() - t0

    perf_path, perf_notes = write_performance_diagnostics(
        out_dir, cfg, timings,
        validation_confidence=validation_confidence,
        smart_status=smart_status,
        final_candidates_count=len(candidates or []),
    )

    RESULTS_MANAGER.write_project_status(
        out_dir,
        input_mode=input_mode_label,
        mdat_path=getattr(args, "mdat", None),
        mdat_project=mdat_project,
        candidates=candidates,
        stage="optimization-complete" if candidates else "optimization-no-candidates",
    )
    # v19.44: uporządkuj również raporty zapisane po głównych wykresach.
    # W katalogu głównym zostają tylko dwa krótkie pliki nawigacyjne.
    organize_results_folder(out_dir)

    if candidates:
        print("\n" + simple_off_off_summary_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg))

    print("\nNajlepsze warianty OFF/OFF:")
    for i, c in enumerate(off_off_candidates(candidates, limit=3), start=1):
        m = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
        print(
            f"{i}. main L/R {float(m['l_main_ms']):.2f}/{float(m['r_main_ms']):.2f} ms | "
            f"sub L/R {float(m['l_sub_ms']):.2f}/{float(m['r_sub_ms']):.2f} ms OFF/OFF | "
            f"rel. {float(m['l_sub_minus_main_ms']):+.2f}/{float(m['r_sub_minus_main_ms']):+.2f} ms | "
            f"ocena {simple_quality_label(c)}"
        )
    return 0



def _write_synthetic_rew_txt(path: str | Path, freq: np.ndarray, spl: np.ndarray, phase: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("Freq(Hz) SPL(dB) Phase(deg)\n")
        for fr, sp, ph in zip(freq, spl, phase):
            f.write(f"{float(fr):.6f} {float(sp):.6f} {float(ph):.6f}\n")


def create_self_test_exports(folder: str | Path) -> Dict[str, str]:
    """
    Tworzy mały, syntetyczny zestaw TXT do testu folderów i ścieżek wynikowych.
    Nie służy do oceny audio — tylko do sprawdzenia działania aplikacji.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    freq = np.linspace(20.0, 180.0, 260)

    # Gładkie, sztuczne krzywe: main opada niżej, sub dominuje w dole.
    l_main_spl = 72.0 - 12.0 / (1.0 + np.exp((freq - 75.0) / 8.0))
    r_main_spl = 71.5 - 11.0 / (1.0 + np.exp((freq - 78.0) / 8.0))
    l_sub_spl = 74.0 - 16.0 / (1.0 + np.exp((75.0 - freq) / 9.0))
    r_sub_spl = 73.5 - 15.0 / (1.0 + np.exp((78.0 - freq) / 9.0))

    l_main_phase = -freq * 0.9
    r_main_phase = -freq * 0.92
    l_sub_phase = -freq * 1.05 + 20.0
    r_sub_phase = -freq * 1.04 + 10.0

    paths = {
        "l_main": folder / "75_BW48_L_main.txt",
        "l_sub": folder / "75_BW48_L_sub_0.0.txt",
        "r_main": folder / "75_BW48_R_main.txt",
        "r_sub": folder / "75_BW48_R_sub_0.0.txt",
    }
    _write_synthetic_rew_txt(paths["l_main"], freq, l_main_spl, l_main_phase)
    _write_synthetic_rew_txt(paths["l_sub"], freq, l_sub_spl, l_sub_phase)
    _write_synthetic_rew_txt(paths["r_main"], freq, r_main_spl, r_main_phase)
    _write_synthetic_rew_txt(paths["r_sub"], freq, r_sub_spl, r_sub_phase)
    return {k: str(v) for k, v in paths.items()}


def run_self_test(args: Optional[argparse.Namespace] = None) -> int:
    """
    v19.6: szybki test struktury wyników i centralnej polityki folderów.

    Test sprawdza:
    - czy główna optymalizacja tworzy zorganizowany folder,
    - czy małe moduły są płaskie i czyszczą stare foldery organizera,
    - czy działa wyszukiwanie best_predicted_curves.csv,
    - czy powstaje PROJECT_STATUS.md.
    """
    base_out = Path(getattr(args, "out", "rew_delay_self_test") if args is not None else "rew_delay_self_test")
    if base_out.exists():
        shutil.rmtree(base_out)
    base_out.mkdir(parents=True, exist_ok=True)

    input_dir = base_out / "_synthetic_exports"
    paths = create_self_test_exports(input_dir)
    main_out = base_out / "rew_delay_results"

    parser = build_arg_parser()
    cli_args = parser.parse_args([
        "--l-main", paths["l_main"],
        "--l-sub", paths["l_sub"],
        "--r-main", paths["r_main"],
        "--r-sub", paths["r_sub"],
        "--out", str(main_out),
        "--crossover", "75 Hz",
        "--filter-type", "BW",
        "--slope", "48",
        "--delay-min", "0",
        "--delay-max", "1",
        "--delay-step", "1",
        "--freq-points", "220",
        "--top", "5",
    ])
    rc = run_cli(cli_args)

    errors: List[str] = []
    if rc != 0:
        errors.append(f"run_cli zwrócił kod {rc}.")

    # v19.44: test parsera nazw RAW. REL120 opisuje stały crossover REL-a,
    # a nie bazowy delay subwoofera.
    if abs(infer_base_delay_from_filename("L_SUB_RAW_REL120") - 0.0) > 1e-12:
        errors.append("Parser bazowego delayu błędnie odczytuje REL120 jako delay.")
    if abs(infer_base_delay_from_filename("90_BW48_L_sub_5.6") - 5.6) > 1e-12:
        errors.append("Parser bazowego delayu nie rozpoznaje jawnego L_sub_5.6.")

    # v19.44: test matematyczny transmitancji crossovera RAW.
    test_fc = 90.0
    test_freq = np.geomspace(5.0, 2000.0, 1200)
    for filt, slope, expected_fc_db in [("BW", 24, -3.0103), ("BW", 48, -3.0103), ("LR", 24, -6.0206), ("LR", 48, -6.0206)]:
        for kind in ["LP", "HP"]:
            h_fc = crossover_transfer(np.array([test_fc]), test_fc, filt, slope, kind)
            mag_fc = float(20.0 * np.log10(np.maximum(np.abs(h_fc[0]), 1e-15)))
            if abs(mag_fc - expected_fc_db) > 0.01:
                errors.append(f"{filt}{slope} {kind}: błędne tłumienie przy crossoverze ({mag_fc:.3f} dB).")
        if filt == "LR":
            h_sum = crossover_transfer(test_freq, test_fc, filt, slope, "LP") + crossover_transfer(test_freq, test_fc, filt, slope, "HP")
            max_dev = float(np.nanmax(np.abs(20.0 * np.log10(np.maximum(np.abs(h_sum), 1e-15)))))
            if max_dev > 1e-6:
                errors.append(f"{filt}{slope}: suma HPF+LPF nie jest płaska ({max_dev:.6f} dB).")

    # v19.47: mapowanie relatywnych delayów RAW na cztery legalne wartości SHD.
    mapped = map_relative_delays_to_shd(-5.0, 10.0, 30.0)
    expected = {"l_main_ms": 5.0, "r_main_ms": 5.0, "l_sub_ms": 0.0, "r_sub_ms": 15.0}
    if not bool(mapped.get("feasible")):
        errors.append("Mapowanie relatywnych delayów SHD odrzuciło poprawny wariant -5/+10 ms.")
    for key, value in expected.items():
        if abs(float(mapped.get(key, 9999.0)) - value) > 1e-9:
            errors.append(f"Mapowanie SHD: błędna wartość {key} dla wariantu -5/+10 ms.")
    if relative_delay_pair_fits_shd(-30.0, 30.0, 30.0):
        errors.append("Walidator SHD zaakceptował niemożliwy wariant -30/+30 ms wymagający 60 ms.")

    # v19.51: pomiar kontrolny może mieć bazowy delay mainów zapisany już w fazie MDAT.
    if abs(infer_main_delay_from_filename("90_BW48_main30.mdat") - 30.0) > 1e-12:
        errors.append("Parser bazy mainów nie rozpoznaje nazwy 90_BW48_main30.mdat.")
    baseline_cfg = OptimizerConfig(
        relative_delay_shd_mode=True, shd_delay_limit_ms=30.0,
        baseline_l_main_ms=30.0, baseline_r_main_ms=30.0,
        baseline_l_sub_ms=0.0, baseline_r_sub_ms=0.0,
    )
    baseline_candidate = Candidate(
        l_delay_ms=19.8, l_polarity="OFF", r_delay_ms=18.6, r_polarity="OFF",
        score=0.0, l_construct_db=0.0, r_construct_db=0.0, lr_construct_db=0.0,
        lr_std_db=0.0, lr_dip_db=0.0, bulge_db=0.0, bulge_peak_db=0.0, lr_mean_db=0.0,
    )
    baseline_mapped = candidate_shd_settings(baseline_candidate, 0.0, 0.0, baseline_cfg)
    baseline_expected = {
        "l_main_ms": 11.4, "r_main_ms": 11.4, "l_sub_ms": 1.2, "r_sub_ms": 0.0,
        "l_sub_minus_main_ms": -10.2, "r_sub_minus_main_ms": -11.4,
    }
    for key, value in baseline_expected.items():
        if abs(float(baseline_mapped.get(key, 9999.0)) - value) > 1e-9:
            errors.append(f"Mapowanie bazy MDAT main30: błędna wartość {key}.")


    # v19.59: regresja skanu polaryzacji. W v19.58 launcher przekazywał
    # polarity_scan do _calculate_many_crossovers(), ale funkcja nie miała
    # tego parametru w sygnaturze, co kończyło się TypeError przed raportem.
    try:
        from rew_crossover_ranking import _calculate_many_crossovers
        test_freq_rank = np.geomspace(20.0, 250.0, 180)
        rank_lm = Measurement("L_MAIN_RAW", test_freq_rank, np.zeros_like(test_freq_rank), np.zeros_like(test_freq_rank))
        rank_rm = Measurement("R_MAIN_RAW", test_freq_rank, np.zeros_like(test_freq_rank), np.zeros_like(test_freq_rank))
        rank_ls = Measurement("L_SUB_RAW_REL120", test_freq_rank, np.zeros_like(test_freq_rank), np.ones_like(test_freq_rank) * 180.0)
        rank_rs = Measurement("R_SUB_RAW_REL120", test_freq_rank, np.zeros_like(test_freq_rank), np.ones_like(test_freq_rank) * 180.0)
        pol_rows = _calculate_many_crossovers(
            (rank_lm, rank_ls, rank_rm, rank_rs), [82],
            filter_type="BW", slope_db_oct=48, delay_min_ms=-2.0, delay_max_ms=2.0,
            shd_delay_limit_ms=30.0, global_eval_low_hz=55.0, global_eval_high_hz=120.0,
            global_bulge_low_hz=120.0, global_bulge_high_hz=180.0,
            music_xo_cap_hz=92.0, caution_xo_cap_hz=102.0, caution_penalty_per_hz=0.35,
            global_candidate_pool_n=40, min_local_pbis_safe=0.0, min_lr_coherence=0.0,
            min_lr_sum_margin_xo_db=-10.0, max_lr_phase_diff_xo_deg=180.0,
            polarity_scan=True, precise=False, workers=1, progress=None,
        )
        if not pol_rows:
            errors.append("Skan polaryzacji nie zwrócił żadnego wiersza testowego.")
        elif {pol_rows[0].l_sub_polarity, pol_rows[0].r_sub_polarity} != {"INV"}:
            errors.append("Skan polaryzacji nie potwierdził wariantu INV/INV w teście syntetycznym.")
    except Exception as exc:
        errors.append(f"Regresja skanu polaryzacji v19.59 zakończyła się błędem: {exc}")

    # Zasiej stare, błędne podfoldery organizera w modułach i sprawdź, czy manager je usuwa.
    for module_name in sorted(FLAT_WORKFLOW_DIRS):
        module_dir = main_out / module_name
        (module_dir / "00_START_TUTAJ").mkdir(parents=True, exist_ok=True)
        (module_dir / "01_RAPORTY_MD").mkdir(parents=True, exist_ok=True)
        (module_dir / "stary_plik.tmp").write_text("stale", encoding="utf-8")
        RESULTS_MANAGER.prepare_module_folder(module_dir, stale_patterns=["*.tmp"])
        for forbidden in ORGANIZED_RESULT_DIRS:
            if (module_dir / forbidden).exists():
                errors.append(f"{module_name}: nie usunięto {forbidden}.")
        if any(module_dir.glob("*.tmp")):
            errors.append(f"{module_name}: nie usunięto pliku tymczasowego.")

    best_csv = find_result_file(main_out, "best_predicted_curves.csv")
    if best_csv is None:
        errors.append("Nie znaleziono best_predicted_curves.csv przez find_result_file().")
    if not (main_out / "00_START_TUTAJ").exists():
        errors.append("Główna optymalizacja nie utworzyła 00_START_TUTAJ.")
    if (main_out / "PROJECT_STATUS.md").exists():
        errors.append("Zbędna kopia PROJECT_STATUS.md w katalogu głównym.")
    if not (main_out / "00_START_TUTAJ" / "PROJECT_STATUS.md").exists():
        errors.append("Brak PROJECT_STATUS.md w 00_START_TUTAJ.")
    if find_result_file(main_out, "FAST_2_STAGE_VALIDATION.md") is None:
        errors.append("Brak raportu FAST_2_STAGE_VALIDATION.md.")
    if find_result_file(main_out, "PERFORMANCE_DIAGNOSTICS.md") is None:
        errors.append("Brak raportu PERFORMANCE_DIAGNOSTICS.md.")
    if find_result_file(main_out, "JAK_CZYTAC_NAJWAZNIEJSZE_WYKRESY.md") is None:
        errors.append("Brak przewodnika JAK_CZYTAC_NAJWAZNIEJSZE_WYKRESY.md.")
    for workflow_name in [
        "MY_SYSTEM_WORKFLOW.md",
        "01_WPISZ_DO_MINIDSP.txt",
        "02_ZMIERZ_W_REW.txt",
        "03_CZY_MOZNA_ROBIC_DIRAC.txt",
        "04_ODSLUCH_AB.txt",
        "05_POLITYKA_POLARYZACJI.txt",
    ]:
        if find_result_file(main_out, workflow_name) is None:
            errors.append(f"Brak pliku workflow v19.59: {workflow_name}.")
        if (main_out / workflow_name).exists():
            errors.append(f"Zbędna kopia workflow w katalogu głównym: {workflow_name}.")

    errors.extend(RESULTS_MANAGER.assert_folder_policy(main_out))

    legacy_workflow = find_result_file(main_out, "WORKFLOW_ASSISTANT.md")
    if legacy_workflow is not None:
        legacy_text = legacy_workflow.read_text(encoding="utf-8", errors="ignore")
        for required_text in ["L main:", "R main:", "L sub:", "R sub:", "Relacja sub-main:"]:
            if required_text not in legacy_text:
                errors.append(f"WORKFLOW_ASSISTANT.md nie zawiera pełnego opisu SHD: {required_text}")

    report_lines = [
        "# SELF TEST v19.60\n\n",
        f"- Wersja: {APP_VERSION}\n",
        f"- Folder: `{base_out}`\n",
        f"- Wynik: **{'PASS' if not errors else 'FAIL'}**\n\n",
        "## Sprawdzone elementy\n\n",
        "- Główna optymalizacja i pełna struktura wyników.\n",
        "- Płaskie foldery modułów walidacyjnych.\n",
        "- Wyszukiwanie `best_predicted_curves.csv` po organizacji wyników.\n",
        "- Jeden `PROJECT_STATUS.md` wyłącznie w `00_START_TUTAJ`.\n",
        "- Raport walidatora `FAST_2_STAGE_VALIDATION.md`.\n",
        "- Raport wydajności `PERFORMANCE_DIAGNOSTICS.md`.\n",
        "- SMART RESCAN działa w tle i zapisuje `SMART_RESCAN.md` wtedy, gdy walidator wykryje ryzyko.\n",
        "- Parser RAW nie myli opisu `REL120` z bazowym delayem 120 ms.\n",
        "- Matematyka filtrów RAW: BW24/BW48 oraz LR24/LR48, w tym płaska suma Linkwitz-Riley.\n",
        "- Mapowanie relatywnego delayu sub - main na cztery wartości SHD z limitem 30 ms i wspólnym delayem mainów.\n",
        "- Obsługa fizycznej bazy MDAT, w tym parser nazwy `main30` i równoważne mapowanie main30/sub0 → main11.4/sub1.2/0.\n",
        "- Pliki workflow v19.60: `01_WPISZ_DO_MINIDSP.txt`, `02_ZMIERZ_W_REW.txt`, `03_CZY_MOZNA_ROBIC_DIRAC.txt`, `04_ODSLUCH_AB.txt`.\n\n",
    ]
    if errors:
        report_lines.append("## Błędy\n\n")
        for e in errors:
            report_lines.append(f"- {e}\n")
    else:
        report_lines.append("## Błędy\n\nBrak.\n")

    report_path = base_out / "SELF_TEST_REPORT.md"
    report_path.write_text("".join(report_lines), encoding="utf-8")
    print("\nSELF TEST:", "PASS" if not errors else "FAIL")
    pass
    return 0 if not errors else 3

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="REW Delay Optimizer — skan delay/polarity dla stereo subów")
    p.add_argument("--l-main", required=False, help="Eksport REW TXT: L_main")
    p.add_argument("--l-sub", required=False, help="Eksport REW TXT: L_sub")
    p.add_argument("--r-main", required=False, help="Eksport REW TXT: R_main")
    p.add_argument("--r-sub", required=False, help="Eksport REW TXT: R_sub")
    p.add_argument("--mdat", required=False, help="Projekt REW .mdat: bezpośredni import SPL/fazy i optymalizacja bez eksportów TXT")
    p.add_argument("--mdat-only", action="store_true", help="Sprawdź .mdat i zapisz raporty mapowania + importu numerycznego + health-check, bez optymalizacji")
    p.add_argument("--mdat-check", action="store_true", help="Sprawdź .mdat: role, SPL/faza, oś częstotliwości; zapisz raport, bez optymalizacji")
    p.add_argument("--out", default="rew_delay_results", help="Folder wyników")
    p.add_argument("--crossover", default="Custom", choices=list(CROSSOVER_PRESETS.keys()), help="Preset crossovera/pasma oceny")
    p.add_argument("--filter-type", default="BW", choices=FILTER_TYPES, help="Typ filtra: BW, LR lub Other")
    p.add_argument("--auto-detect-profile", action="store_true", help="Autowykryj crossover/filtr/slope z nazw plików")
    p.add_argument("--slope", default="48", choices=SLOPE_OPTIONS, help="Stromość filtra: 24 albo 48 dB/oct")
    p.add_argument("--score-profile", default="PBIS", choices=SCORE_PROFILES, help="Profil oceny: Technical, Balanced, Psychoacoustic albo PBIS")
    p.add_argument("--l-main-base-delay", type=float, default=0.0, help="Bazowy fizyczny delay L_main aktywny podczas pomiaru wejściowego")
    p.add_argument("--r-main-base-delay", type=float, default=0.0, help="Bazowy fizyczny delay R_main aktywny podczas pomiaru wejściowego")
    p.add_argument("--l-base-delay", type=float, default=0.0, help="Bazowy fizyczny delay L_sub aktywny podczas pomiaru wejściowego, np. 1.0 dla L_sub_1.0.txt")
    p.add_argument("--r-base-delay", type=float, default=0.0, help="Bazowy fizyczny delay R_sub aktywny podczas pomiaru wejściowego, np. 4.0 dla R_sub_4.0.txt")
    p.add_argument("--delay-min", type=float, default=0.0)
    p.add_argument("--delay-max", type=float, default=30.0)
    p.add_argument("--delay-step", type=float, default=0.1)
    p.add_argument("--relative-shd", action="store_true", help="Analizuj relację sub-main w obie strony i mapuj wynik na cztery wartości SHD")
    p.add_argument("--shd-limit", type=float, default=30.0, help="Maksymalny delay pojedynczego wyjścia SHD w ms")
    p.add_argument("--eval-low", type=float, default=55.0)
    p.add_argument("--eval-high", type=float, default=115.0)
    p.add_argument("--bulge-low", type=float, default=120.0)
    p.add_argument("--bulge-high", type=float, default=160.0)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--practical-score-tolerance", type=float, default=0.75, help="v18.4: tolerancja score dla alternatywy plateau A/B")
    p.add_argument("--bulge-tolerance", type=float, default=None, help="Tolerancja nadmiaru bulwy w dB. Domyślnie dokładna wartość z profilu filtra.")
    p.add_argument("--bulge-strength", choices=["off", "low", "medium", "high"], default=None, help="Zgodność wsteczna: opisowa siła kary. Nadpisuje profil, jeżeli nie podano --bulge-penalty-weight.")
    p.add_argument("--bulge-penalty-weight", type=float, default=None, help="Dokładna liczbowa siła kary za bulwę. Domyślnie z profilu BW/LR.")
    p.add_argument("--bulge-peak-penalty-weight", type=float, default=None, help="Dokładna liczbowa waga piku bulwy. Domyślnie z profilu BW/LR.")
    p.add_argument("--bulge-peak", action=argparse.BooleanOptionalAction, default=True, help="Włącz/wyłącz karę za lokalny pik w paśmie bulwy. Dostępne też: --no-bulge-peak.")
    p.add_argument("--simple-bulge-model", action="store_true", help="Użyj starego prostego modelu średniej zamiast psychoakustycznego")
    p.add_argument("--freq-points", type=int, default=1400)
    p.add_argument("--max-cartesian", type=int, default=2_000_000)
    p.add_argument("--dirac-curtain-l", required=False, help="v18: pomiar L Dirac ON do analizy kurtyny")
    p.add_argument("--dirac-curtain-r", required=False, help="v18: pomiar R Dirac ON do analizy kurtyny")
    p.add_argument("--dirac-curtain-lr", required=False, help="v18: pomiar L+R Dirac ON do analizy kurtyny")
    p.add_argument("--dirac-curtain-high", type=float, default=250.0, help="v18: górna kurtyna Dirac w Hz, np. 200/250/320")
    p.add_argument("--dirac-ignore-bands", default="135-155,160-240", help="v18.4: pasma ignorowane / nie goń delayem, np. 135-155,160-240")
    p.add_argument("--no-safe-midrange", action="store_true", help="v18.7: wyłącz tryb bezpieczny dla średnicy w komunikatach Dirac/workflow")
    p.add_argument("--no-sound-quality-safe", action="store_true", help="v19.30: wyłącz PBIS Safe jako ranking brzmieniowy; PBIS Raw pozostaje bez zmian")
    p.add_argument("--exact-scan", action="store_true", help="v19.30: wyłącz FAST 2-STAGE i licz pełny brute-force; używać tylko do walidacji albo małych zakresów")
    p.add_argument("--fast-coarse-step", type=float, default=0.5, help="v19.30: krok skanu zgrubnego w FAST 2-STAGE, domyślnie 0.5 ms")
    p.add_argument("--fast-refine-radius", type=float, default=0.6, help="v19.30: promień dogęszczenia wokół najlepszych obszarów, domyślnie 0.6 ms")
    p.add_argument("--fast-seed-n", type=int, default=80, help="v19.30: liczba najlepszych punktów zgrubnych użytych jako centra dogęszczenia")
    p.add_argument("--fast-exact-threshold", type=int, default=30000, help="v19.30: do tej liczby kombinacji aplikacja liczy pełny EXACT zamiast FAST 2-STAGE")
    p.add_argument("--no-fast-validation", action="store_true", help="v19.30: wyłącz raport FAST_2_STAGE_VALIDATION i lokalny EXACT-check")
    p.add_argument("--fast-validation-radius", type=float, default=0.6, help="v19.30: promień lokalnego EXACT-check wokół zwycięzcy, domyślnie 0.6 ms")
    p.add_argument("--fast-validation-close-score", type=float, default=1.0, help="v19.30: próg podobnych kandydatów w punktach score, domyślnie 1.0")
    p.add_argument("--no-smart-rescan", action="store_true", help="v19.30: wyłącz automatyczny SMART RESCAN po ryzykownej walidacji FAST")
    p.add_argument("--smart-rescan-radius", type=float, default=1.5, help="v19.30: promień dogrywki SMART RESCAN, domyślnie 1.5 ms")
    p.add_argument("--smart-rescan-seed-n", type=int, default=8, help="v19.30: liczba najlepszych centrów użytych do SMART RESCAN")
    p.add_argument("--smart-rescan-max-combinations", type=int, default=80000, help="v19.30: bezpiecznik liczby kombinacji SMART RESCAN")
    p.add_argument("--no-fast-coarse-safety", action="store_true", help="v19.30: wyłącz automatyczny bezpiecznik kroku zgrubnego FAST")
    p.add_argument("--fast-max-coarse-combinations", type=int, default=6000, help="v19.30: limit kombinacji etapu FAST coarse przed podniesieniem kroku zgrubnego")
    p.add_argument("--compare-dirac-curtains-folder", required=False, help="v18: folder z zestawami L/R/LR do porównania kurtyn")
    p.add_argument("--gui", action="store_true", help="Uruchom GUI")
    p.add_argument("--self-test", action="store_true", help="v19.30: uruchom szybki test struktury wyników i PROJECT_STATUS")
    return p


def build_cli_config(args: argparse.Namespace) -> Tuple[OptimizerConfig, str, str, int]:
    """
    Wspólna konfiguracja CLI dla optymalizacji i narzędzi v18.
    W trybie optymalizacji run_cli dodatkowo robi autodetekcję z nazw plików.
    """
    crossover_label = args.crossover
    filter_type = args.filter_type
    slope_db_oct = int(args.slope)

    if crossover_label in CROSSOVER_PRESETS and CROSSOVER_PRESETS[crossover_label] is not None:
        e_low, e_high, b_low, b_high = CROSSOVER_PRESETS[crossover_label]
    else:
        e_low, e_high, b_low, b_high = args.eval_low, args.eval_high, args.bulge_low, args.bulge_high

    filter_defaults = filter_profile_defaults(filter_type, slope_db_oct)
    bulge_weight = resolve_bulge_penalty_weight(
        filter_defaults,
        getattr(args, "bulge_strength", None),
        getattr(args, "bulge_penalty_weight", None),
    )
    explicit_peak_weight = getattr(args, "bulge_peak_penalty_weight", None)
    bulge_peak_weight = (
        (float(explicit_peak_weight) if explicit_peak_weight is not None else float(filter_defaults["bulge_peak_penalty_weight"]))
        if bool(getattr(args, "bulge_peak", True))
        else 0.0
    )
    bulge_tolerance = (
        float(args.bulge_tolerance)
        if getattr(args, "bulge_tolerance", None) is not None
        else float(filter_defaults["bulge_tolerance_db"])
    )

    cfg = OptimizerConfig(
        delay_min_ms=args.delay_min,
        delay_max_ms=args.delay_max,
        delay_step_ms=args.delay_step,
        eval_low_hz=e_low,
        eval_high_hz=e_high,
        bulge_low_hz=b_low,
        bulge_high_hz=b_high,
        top_n=args.top,
        practical_score_tolerance_db=float(getattr(args, "practical_score_tolerance", 0.75)),
        freq_points=args.freq_points,
        max_cartesian=args.max_cartesian,
        prefer_smoothness=float(filter_defaults["prefer_smoothness"]),
        prefer_lr_sum=float(filter_defaults["prefer_lr_sum"]),
        dip_penalty_weight=float(filter_defaults["dip_penalty_weight"]),
        bulge_penalty_weight=bulge_weight,
        bulge_tolerance_db=bulge_tolerance,
        bulge_peak_penalty_weight=bulge_peak_weight,
        use_psycho_bulge_model=bool(filter_defaults["use_psycho_bulge_model"]) if not args.simple_bulge_model else False,
        filter_type=filter_type,
        slope_db_oct=slope_db_oct,
        safe_midrange_priority=not bool(getattr(args, "no_safe_midrange", False)),
        scoring_profile=getattr(args, "score_profile", "PBIS"),
        sound_quality_safe=not bool(getattr(args, "no_sound_quality_safe", False)),
        fast_two_stage=not bool(getattr(args, "exact_scan", False)),
        fast_coarse_step_ms=float(getattr(args, "fast_coarse_step", 0.5)),
        fast_refine_radius_ms=float(getattr(args, "fast_refine_radius", 0.6)),
        fast_seed_n=int(getattr(args, "fast_seed_n", 80)),
        fast_exact_threshold=int(getattr(args, "fast_exact_threshold", 30000)),
        fast_validation_enabled=not bool(getattr(args, "no_fast_validation", False)),
        fast_validation_radius_ms=float(getattr(args, "fast_validation_radius", 0.6)),
        fast_validation_close_score=float(getattr(args, "fast_validation_close_score", 1.0)),
        fast_smart_rescan_enabled=not bool(getattr(args, "no_smart_rescan", False)),
        fast_smart_rescan_radius_ms=float(getattr(args, "smart_rescan_radius", 1.5)),
        fast_smart_rescan_seed_n=int(getattr(args, "smart_rescan_seed_n", 8)),
        fast_smart_rescan_max_combinations=int(getattr(args, "smart_rescan_max_combinations", 80000)),
        fast_auto_coarse_safety=not bool(getattr(args, "no_fast_coarse_safety", False)),
        fast_max_coarse_combinations=int(getattr(args, "fast_max_coarse_combinations", 6000)),
        relative_delay_shd_mode=bool(getattr(args, "relative_shd", False)),
        shd_delay_limit_ms=float(getattr(args, "shd_limit", 30.0)),
        baseline_l_main_ms=float(getattr(args, "l_main_base_delay", 0.0)),
        baseline_r_main_ms=float(getattr(args, "r_main_base_delay", 0.0)),
        baseline_l_sub_ms=float(getattr(args, "l_base_delay", 0.0)),
        baseline_r_sub_ms=float(getattr(args, "r_base_delay", 0.0)),
    )
    return cfg, crossover_label, filter_type, slope_db_oct


def run_cli_dirac_curtain(args: argparse.Namespace) -> int:
    cfg, crossover_label, filter_type, slope_db_oct = build_cli_config(args)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ignore_bands = parse_frequency_bands(getattr(args, "dirac_ignore_bands", ""))

    if getattr(args, "compare_dirac_curtains_folder", None):
        summary = compare_dirac_curtains_folder(
            args.compare_dirac_curtains_folder,
            out_dir / "dirac_curtain_comparison",
            cfg,
            default_curtain_high_hz=float(args.dirac_curtain_high),
            ignore_bands=ignore_bands,
        )
        if not summary:
            print("Nie znaleziono kompletnych zestawów L/R/L+R.")
            return 1
        best = summary[0]
        print(f"Najbezpieczniejsza kurtyna: {best['group']} ({best['verdict']}), score={float(best['curtain_safety_score']):.2f}")
        pass
        return 0

    required = [args.dirac_curtain_l, args.dirac_curtain_r, args.dirac_curtain_lr]
    if any(x is None for x in required):
        raise ValueError("Do analizy kurtyny podaj --dirac-curtain-l --dirac-curtain-r --dirac-curtain-lr albo --compare-dirac-curtains-folder.")

    verdict, metrics = analyze_dirac_curtain_measurements(
        args.dirac_curtain_l,
        args.dirac_curtain_r,
        args.dirac_curtain_lr,
        out_dir / "dirac_curtain_analysis",
        cfg,
        curtain_high_hz=float(args.dirac_curtain_high),
        ignore_bands=ignore_bands,
        label=f"{profile_label(crossover_label, filter_type, slope_db_oct)} / kurtyna {float(args.dirac_curtain_high):.0f} Hz",
    )
    print(f"Analiza kurtyny Dirac: {verdict}")
    print(f"Score bezpieczeństwa: {metrics['curtain_safety_score']:.2f}")
    pass
    return 0


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    # GUI message normalization: fix literal "\n" / "\t" sequences in all dialogs.
    # Some code paths build messages with "\\n"; Tkinter shows those literally unless cleaned.
    def _clean_gui_message(value) -> str:
        if value is None:
            return ""
        return (
            str(value)
            .replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )


    def _simple_user_message(value) -> str:
        """v19.44: usuwa techniczny szum, ścieżki i komunikaty o zapisie plików."""
        raw = _clean_gui_message(value)
        out: List[str] = []
        for original in raw.splitlines():
            line = original.strip()
            lower = line.lower()
            if not line:
                if out and out[-1] != "":
                    out.append("")
                continue
            if "mixed" in lower or "smart rescan" in lower or "fast 2-stage" in lower:
                # Statusy tych mechanizmów są pokazywane osobno prostym językiem.
                if not (lower.startswith("walidacja fast 2-stage:") or lower.startswith("walidacja po smart rescan:") or lower.startswith("smart rescan:")):
                    continue
            if lower.startswith("zapisano") or lower.startswith("zapisuję") or lower.startswith("zapis końcowy"):
                continue
            if lower.startswith("raport importu") or lower.startswith("health-check") or lower.startswith("diagnostyka wydajności"):
                continue
            if " z folderu:" in lower or lower.startswith("folder wyników"):
                continue
            if any(ext in lower for ext in (".md", ".csv", ".png", ".txt")):
                continue
            if lower.startswith("pliki uporządkowano"):
                continue
            if " — " in line and ("validation" in lower or "rescan" in lower or "walidacja fast" in lower):
                line = line.split(" — ", 1)[0]
            if lower.startswith("walidacja fast 2-stage:"):
                status = line.split(":", 1)[1].strip().upper()
                mapping = {"HIGH": "dobra", "MEDIUM": "średnia — sprawdź pomiarem", "LOW": "niska — potrzebne dodatkowe sprawdzenie", "DISABLED": "pominięta"}
                line = "Kontrola wyniku: " + mapping.get(status, status.lower())
            elif lower.startswith("walidacja po smart rescan:"):
                status = line.split(":", 1)[1].strip().upper()
                mapping = {"HIGH": "dobra", "MEDIUM": "średnia — sprawdź pomiarem", "LOW": "niska — sprawdź dokładnie pomiarem"}
                line = "Kontrola po dodatkowym sprawdzeniu: " + mapping.get(status, status.lower())
            elif lower.startswith("smart rescan:"):
                status = line.split(":", 1)[1].strip().upper()
                mapping = {"SKIPPED": "nie było potrzebne", "DISABLED": "pominięte", "CONFIRMED": "wynik potwierdzony", "REFINED": "wynik doprecyzowany", "IMPROVED": "wynik poprawiony", "ERROR": "nie udało się wykonać"}
                line = "Dodatkowe sprawdzenie: " + mapping.get(status, status.lower())
            elif lower.startswith("tryb skanu: fast"):
                line = "Sposób liczenia: szybki."
            elif lower.startswith("tryb skanu: exact"):
                line = "Sposób liczenia: dokładny."
            elif lower.startswith("skanuję delay/polarity"):
                line = "Liczenie ustawień..."
            elif lower.startswith("zaimportowano .mdat"):
                line = "Dane pomiarowe zostały wczytane."
            elif lower.startswith("start v19."):
                line = "Uruchamiam obliczenia."
            elif lower.startswith("czas optymalizacji"):
                line = "Obliczenia zakończone."
            # Nie pokazujemy użytkownikowi ścieżek dostępu.
            if ":\\" in line or line.startswith("/") or "\\\\" in line:
                continue
            out.append(line)
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)

    _orig_showinfo = messagebox.showinfo
    _orig_showwarning = messagebox.showwarning
    _orig_showerror = messagebox.showerror

    def _showinfo_clean(title=None, message=None, *args, **kwargs):
        return _orig_showinfo(_clean_gui_message(title), _simple_user_message(message), *args, **kwargs)

    def _showwarning_clean(title=None, message=None, *args, **kwargs):
        return _orig_showwarning(_clean_gui_message(title), _simple_user_message(message), *args, **kwargs)

    def _showerror_clean(title=None, message=None, *args, **kwargs):
        return _orig_showerror(_clean_gui_message(title), _simple_user_message(message), *args, **kwargs)

    messagebox.showinfo = _showinfo_clean
    messagebox.showwarning = _showwarning_clean
    messagebox.showerror = _showerror_clean


    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1320x860")
    root.minsize(1060, 680)

    # v18.3: odświeżone GUI. Górna część ma własne przewijanie, a dół
    # zawsze pokazuje przyciski oraz czytelny panel "Log / wyniki".
    try:
        style = ttk.Style(root)
        # Nie wymuszamy agresywnego motywu; na Windows "vista" wygląda naturalnie.
        if sys.platform.startswith("win") and "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background="#F6F7F9")
        style.configure("TLabelframe", background="#F6F7F9")
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background="#F6F7F9", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 9), padding=(6, 3))
        style.configure("Primary.TButton", font=("Segoe UI", 9, "bold"), padding=(8, 4))
        style.configure("TEntry", padding=(3, 2))
        style.configure("TCheckbutton", background="#F6F7F9", font=("Segoe UI", 9))
        style.configure("TProgressbar", thickness=14)
    except Exception:
        pass

    vars_paths = {
        "L main": tk.StringVar(),
        "L sub": tk.StringVar(),
        "R main": tk.StringVar(),
        "R sub": tk.StringVar(),
    }
    mdat_path_var = tk.StringVar()

    cfg_vars = {
        "crossover": tk.StringVar(value="70 Hz"),
        "filter_type": tk.StringVar(value="BW"),
        "slope": tk.StringVar(value="48"),
        "score_profile": tk.StringVar(value="PBIS"),
        "sound_quality_safe": tk.BooleanVar(value=True),
        "l_main_base_delay": tk.StringVar(value="0.0"),
        "r_main_base_delay": tk.StringVar(value="0.0"),
        "l_base_delay": tk.StringVar(value="0.0"),
        "r_base_delay": tk.StringVar(value="0.0"),
        "delay_min": tk.StringVar(value="0"),
        "delay_max": tk.StringVar(value="30"),
        "delay_step": tk.StringVar(value="0.1"),
        "relative_delay_shd_mode": tk.BooleanVar(value=False),
        "shd_delay_limit": tk.StringVar(value="30.0"),
        "eval_low": tk.StringVar(value="55"),
        "eval_high": tk.StringVar(value="115"),
        "bulge_low": tk.StringVar(value="120"),
        "bulge_high": tk.StringVar(value="160"),
        "top_n": tk.StringVar(value="20"),
        "bulge_tolerance": tk.StringVar(value="0.5"),
        "bulge_penalty_weight": tk.StringVar(value="0.85"),
        "bulge_peak": tk.BooleanVar(value=True),
        "bulge_peak_penalty_weight": tk.StringVar(value="0.40"),
        "psycho_bulge": tk.BooleanVar(value=True),
        "auto_detect_profile": tk.BooleanVar(value=True),
        "dirac_curtain_high": tk.StringVar(value="250"),
        "dirac_ignore_bands": tk.StringVar(value="135-155,160-240"),
        "safe_midrange": tk.BooleanVar(value=True),
        "out_dir": tk.StringVar(value=str(Path.cwd() / "rew_delay_results")),
        "manual_l_main_final": tk.StringVar(value="0.00"),
        "manual_r_main_final": tk.StringVar(value="0.00"),
        "manual_l_final": tk.StringVar(value="19.50"),
        "manual_r_final": tk.StringVar(value="27.00"),
        "manual_l_polarity": tk.StringVar(value="OFF"),
        "manual_r_polarity": tk.StringVar(value="OFF"),
    }

    gui_preferences = load_app_preferences()
    try:
        saved_txt_paths = gui_preferences.get("txt_paths", {}) if isinstance(gui_preferences, dict) else {}
        for key, value in saved_txt_paths.items():
            if key in vars_paths and value:
                vars_paths[key].set(str(value))
        saved_mdat_path = gui_preferences.get("mdat_path") if isinstance(gui_preferences, dict) else None
        if saved_mdat_path:
            mdat_path_var.set(str(saved_mdat_path))
        saved_cfg = gui_preferences.get("cfg_vars", {}) if isinstance(gui_preferences, dict) else {}
        for key, value in saved_cfg.items():
            if key in cfg_vars:
                cfg_vars[key].set(value)
    except Exception:
        # Uszkodzony plik preferencji nie może blokować startu GUI.
        pass

    def save_current_gui_preferences() -> None:
        data = {
            "app_version": APP_VERSION,
            "mdat_path": mdat_path_var.get().strip(),
            "txt_paths": {k: v.get().strip() for k, v in vars_paths.items()},
            "cfg_vars": {k: v.get() for k, v in cfg_vars.items()},
        }
        save_app_preferences(data)

    preset_state = {"applying": False}
    preset_status_var = tk.StringVar(
        value="Preset automatyczny — pasma i scoring wynikają z crossovera/filtra/slope."
    )
    profile_summary_var = tk.StringVar(
        value="Profil analizy zostanie ustawiony automatycznie."
    )

    outer = ttk.Frame(root, padding=(10, 8, 10, 8))
    outer.pack(fill="both", expand=True)

    main_pane = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
    main_pane.pack(fill="both", expand=True)

    top_outer = ttk.Frame(main_pane)
    right_shell = ttk.Frame(main_pane)
    main_pane.add(top_outer, weight=2)
    main_pane.add(right_shell, weight=3)

    # v19.28: prawy panel ma część stałą oraz przewijaną część roboczą.
    # Dzięki temu główne przyciski, status i log nie znikają przy niższej
    # rozdzielczości albo skalowaniu Windows.
    right_fixed = ttk.Frame(right_shell)
    right_fixed.pack(fill="x", side="top")
    right_scroll_shell = ttk.Frame(right_shell)
    right_scroll_shell.pack(fill="both", expand=True, side="top")
    right_canvas = tk.Canvas(right_scroll_shell, highlightthickness=0, background="#F6F7F9")
    right_scroll_y = ttk.Scrollbar(right_scroll_shell, orient="vertical", command=right_canvas.yview)
    right_canvas.configure(yscrollcommand=right_scroll_y.set)
    right_canvas.pack(side="left", fill="both", expand=True)
    right_scroll_y.pack(side="right", fill="y")
    bottom_outer = ttk.Frame(right_canvas)
    right_canvas_window = right_canvas.create_window((0, 0), window=bottom_outer, anchor="nw")

    def _update_right_scroll_region(event=None):
        try:
            right_canvas.configure(scrollregion=right_canvas.bbox("all"))
        except Exception:
            pass

    def _sync_right_canvas_width(event=None):
        try:
            right_canvas.itemconfigure(right_canvas_window, width=right_canvas.winfo_width())
            _update_right_scroll_region()
        except Exception:
            pass

    bottom_outer.bind("<Configure>", _update_right_scroll_region)
    right_canvas.bind("<Configure>", _sync_right_canvas_width)

    def _right_mousewheel(event):
        try:
            if getattr(event, "num", None) == 4:
                right_canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                right_canvas.yview_scroll(3, "units")
            else:
                delta = int(-1 * (event.delta / 120)) if getattr(event, "delta", 0) else 0
                if delta:
                    right_canvas.yview_scroll(delta * 3, "units")
        except Exception:
            pass
        return "break"

    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        right_canvas.bind(seq, _right_mousewheel)
        bottom_outer.bind(seq, _right_mousewheel)

    # v19.13: lewy panel nie używa już osobnego Canvas/Scrollbar.
    # Poprzednio tworzyły się przewijalne puste przestrzenie i przyciski
    # potrafiły znikać przy skalowaniu. Obecna zawartość mieści się w stałym
    # panelu, a TXT jest tylko składanym trybem awaryjnym.
    frm = ttk.Frame(top_outer, padding=(2, 2, 8, 2))
    frm.pack(fill="both", expand=True)

    def _update_top_scroll_region(event=None):
        return None

    # v19.13: TXT to tylko tryb awaryjny, więc nie zajmuje miejsca na starcie.
    file_box = ttk.LabelFrame(frm, text="Pliki REW TXT — tryb awaryjny")
    # Nie pakujemy od razu; pokazuje go przycisk "Pokaż TXT awaryjnie".

    def choose_file(key: str):
        path = filedialog.askopenfilename(
            title=f"Wybierz {key}",
            filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")]
        )
        if path:
            vars_paths[key].set(path)
            save_current_gui_preferences()
            if key == "L sub":
                cfg_vars["l_base_delay"].set(f"{infer_base_delay_from_filename(path):.2f}")
            elif key == "R sub":
                cfg_vars["r_base_delay"].set(f"{infer_base_delay_from_filename(path):.2f}")
            if bool(cfg_vars["auto_detect_profile"].get()):
                try:
                    autodetect_profile_from_loaded_files(log_result=False)
                except Exception:
                    pass

    for row, key in enumerate(vars_paths.keys()):
        ttk.Label(file_box, text=key).grid(row=row, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(file_box, textvariable=vars_paths[key], width=96).grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(file_box, text="Wybierz", command=lambda k=key: choose_file(k)).grid(row=row, column=2, padx=6, pady=4)
    file_box.columnconfigure(1, weight=1)

    mdat_box = ttk.LabelFrame(frm, text="Projekt REW .mdat — główne wejście")
    mdat_box.pack(fill="x", pady=(8, 0))
    ttk.Label(
        mdat_box,
        text="Wybierz projekt .mdat. To główne wejście aplikacji; TXT jest tylko trybem awaryjnym.",
        foreground="#555",
        wraplength=620,
    ).grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
    ttk.Entry(mdat_box, textvariable=mdat_path_var, width=72).grid(row=1, column=0, sticky="ew", padx=6, pady=4)

    def choose_mdat_file():
        path = filedialog.askopenfilename(
            title="Wybierz projekt REW .mdat",
            filetypes=[("REW MDAT", "*.mdat"), ("All files", "*.*")]
        )
        if path:
            mdat_path_var.set(path)
            save_current_gui_preferences()

    def apply_mdat_baseline_from_path(path_value: str, *, scan=None, log_result: bool = False) -> bool:
        """Ustawia fizyczną bazę delayów zapisaną już w fazie projektu MDAT.

        v19.51: plik kontrolny może być wykonany np. przy main L/R=30 ms oraz
        subach 0/0 ms. Optymalizator ma wtedy skanować *zmianę relacji* wobec
        pomiaru wejściowego, a nie nakładać pełny delay drugi raz.
        """
        path_text = str(path_value or "").strip()
        if not path_text:
            return False
        raw_name = Path(path_text).name.upper()
        raw_mode = ("RAW" in raw_name) or ("XOVER_OFF" in raw_name) or ("CROSSOVER_OFF" in raw_name)
        main_base = float(infer_main_delay_from_filename(path_text))
        cfg_vars["l_main_base_delay"].set(f"{main_base:g}")
        cfg_vars["r_main_base_delay"].set(f"{main_base:g}")
        # Nie przenoś bazy subów z poprzednio otwartego projektu.
        cfg_vars["l_base_delay"].set("0")
        cfg_vars["r_base_delay"].set("0")

        if scan is None:
            try:
                scan = scan_mdat_project(path_text)
            except Exception:
                scan = None
        if isinstance(scan, dict):
            for m in list(scan.get("measurements", [])):  # type: ignore[arg-type]
                if isinstance(m, MdatMeasurementInfo) and m.base_delay_ms is not None:
                    if m.role == "L sub":
                        cfg_vars["l_base_delay"].set(f"{float(m.base_delay_ms):g}")
                    elif m.role == "R sub":
                        cfg_vars["r_base_delay"].set(f"{float(m.base_delay_ms):g}")

        l_sub_base = float(cfg_vars["l_base_delay"].get().replace(",", "."))
        r_sub_base = float(cfg_vars["r_base_delay"].get().replace(",", "."))
        baseline_detected = raw_mode or any(abs(v) > 1e-9 for v in (main_base, l_sub_base, r_sub_base))
        if baseline_detected:
            cfg_vars["relative_delay_shd_mode"].set(True)
            cfg_vars["delay_min"].set("-30")
            cfg_vars["delay_max"].set("30")
            cfg_vars["shd_delay_limit"].set("30")
        if log_result:
            log(
                "Baza fizyczna zapisana w MDAT: "
                f"main L/R {main_base:.2f}/{main_base:.2f} ms | "
                f"sub L/R {l_sub_base:.2f}/{r_sub_base:.2f} ms | "
                f"rel. sub-main {l_sub_base-main_base:+.2f}/{r_sub_base-main_base:+.2f} ms."
            )
            if raw_mode:
                log("Wykryto pomiar RAW / crossover OFF: włączam relatywny tryb SHD sub − main (-30..+30 ms).")
            elif baseline_detected:
                log("Wykryto aktywne delaye podczas pomiaru MDAT: skan ocenia zmianę relacji względem zapisanej bazy.")
        return bool(baseline_detected)

    def apply_raw_mode_from_mdat_path(path_value: str, *, log_result: bool = False) -> bool:
        """Zgodność wsteczna: stosuje także bazę MDAT i zwraca status RAW."""
        apply_mdat_baseline_from_path(path_value, log_result=log_result)
        raw_name = Path(str(path_value or "")).name.upper()
        return bool(("RAW" in raw_name) or ("XOVER_OFF" in raw_name) or ("CROSSOVER_OFF" in raw_name))

    def gui_scan_mdat(show_dialog: bool = True) -> bool:
        path = mdat_path_var.get().strip()
        if not path:
            choose_mdat_file()
            path = mdat_path_var.get().strip()
        if not path:
            return False
        try:
            out_dir = Path(cfg_vars["out_dir"].get().strip())
            md_path, csv_path, scan = write_mdat_scan_report(path, out_dir)
            project = import_mdat_measurements(path)
            md_num, csv_num = write_mdat_numeric_import_report(project, out_dir)
            health_report = write_mdat_health_report(project, out_dir)
            health_errors, health_warnings = mdat_import_health_checks(project)
            log("")
            log(mdat_scan_summary_text(scan))
            log(f"Zaimportowano dane liczbowe: {project.points} punktów, {project.freq_start_hz:.2f}–{project.freq_end_hz:.2f} Hz.")
            if health_warnings:
                log("Ostrzeżenia MDAT:")
                for w in health_warnings:
                    log("- " + w)
            if health_errors:
                log("Błędy MDAT:")
                for e in health_errors:
                    log("- " + e)
                raise ValueError("MDAT health-check nie przeszedł. Szczegóły w MDAT_HEALTH_CHECK.md.")
            for role in ["L main", "L sub", "R main", "R sub"]:
                log(f"{role}: {project.role_to_name.get(role, '')}")

            # Jeżeli profil jest jednoznaczny, ustaw GUI tak samo jak po autowykryciu z nazw TXT.
            changed = []
            if scan.get("profile_cross"):
                cfg_vars["crossover"].set(str(scan["profile_cross"]))
                changed.append(str(scan["profile_cross"]))
            if scan.get("profile_filter"):
                cfg_vars["filter_type"].set(str(scan["profile_filter"]))
                changed.append(str(scan["profile_filter"]))
            if scan.get("profile_slope"):
                cfg_vars["slope"].set(str(int(scan["profile_slope"])))
                changed.append(str(int(scan["profile_slope"])))
            # v19.51: ustaw pełną fizyczną bazę aktywną podczas pomiaru MDAT.
            apply_mdat_baseline_from_path(path, scan=scan, log_result=True)
            if changed:
                apply_crossover_preset(force=True)
                log("Profil z MDAT ustawiony w GUI: " + profile_label(cfg_vars["crossover"].get(), cfg_vars["filter_type"].get(), int(cfg_vars["slope"].get())))

            try:
                update_workflow_status()
            except Exception:
                pass
            if show_dialog:
                messagebox.showinfo(
                    "Direct MDAT Import",
                    "Import .mdat zakończony.\n\n"
                    f"Rozpoznane pomiary: {len(scan.get('measurements', []))}\n"
                    f"Dane liczbowe: {project.points} punktów, {project.freq_start_hz:.1f}–{project.freq_end_hz:.1f} Hz\n"
                    f"Raport: {md_num}\n\n"
                    "Możesz uruchomić optymalizację bez eksportów TXT z REW."
                )
            return True
        except Exception as e:
            log("BŁĄD importu MDAT: " + str(e))
            try:
                update_workflow_status()
            except Exception:
                pass
            if show_dialog:
                messagebox.showerror("Błąd MDAT", str(e))
            return False

    mdat_button_row = ttk.Frame(mdat_box)
    mdat_button_row.grid(row=2, column=0, sticky="e", padx=6, pady=(0, 4))
    ttk.Button(mdat_button_row, text="Wybierz .mdat", command=choose_mdat_file).pack(side="left", padx=(0, 6))
    ttk.Button(mdat_button_row, text="Importuj .mdat", command=lambda: gui_scan_mdat(show_dialog=True)).pack(side="left")
    mdat_box.columnconfigure(0, weight=1)

    txt_fallback_state = {"visible": False, "button": None}

    def toggle_txt_fallback():
        txt_fallback_state["visible"] = not txt_fallback_state["visible"]
        try:
            if txt_fallback_state["visible"]:
                file_box.pack(fill="x", pady=(4, 8), after=txt_toggle_bar)
                if txt_fallback_state["button"] is not None:
                    txt_fallback_state["button"].configure(text="Ukryj TXT awaryjnie")
            else:
                file_box.pack_forget()
                if txt_fallback_state["button"] is not None:
                    txt_fallback_state["button"].configure(text="Pokaż TXT awaryjnie")
            _update_top_scroll_region()
        except Exception:
            pass

    txt_toggle_bar = ttk.Frame(frm)
    txt_toggle_bar.pack(fill="x", pady=(4, 2))
    txt_toggle_bar.columnconfigure(0, weight=1)
    ttk.Label(
        txt_toggle_bar,
        text="Normalny tryb pracy: .mdat. TXT ukryte.",
        foreground="#555",
        wraplength=520,
    ).grid(row=0, column=0, sticky="w", padx=6)
    txt_fallback_state["button"] = ttk.Button(txt_toggle_bar, text="Pokaż TXT awaryjnie", command=toggle_txt_fallback)
    txt_fallback_state["button"].grid(row=0, column=1, sticky="e", padx=6)


    cfg_box = ttk.LabelFrame(frm, text="Ustawienia skanowania")
    cfg_box.pack(fill="x", pady=10)

    def apply_crossover_preset(event=None, force: bool = False) -> bool:
        """
        v17.5: locked auto mode.
        The GUI no longer exposes manual eval/bulge/scoring controls, so the
        currently selected crossover/filter/slope is always the source of truth.
        """
        label = cfg_vars["crossover"].get()
        preset = CROSSOVER_PRESETS.get(label)
        if preset is None:
            # GUI should normally not offer Auto/Custom anymore, but keep a safe fallback.
            label = "70 Hz"
            cfg_vars["crossover"].set(label)
            preset = CROSSOVER_PRESETS[label]

        preset_state["applying"] = True
        try:
            e_low, e_high, b_low, b_high = preset
            cfg_vars["eval_low"].set(f"{e_low:g}")
            cfg_vars["eval_high"].set(f"{e_high:g}")
            cfg_vars["bulge_low"].set(f"{b_low:g}")
            cfg_vars["bulge_high"].set(f"{b_high:g}")

            ft = cfg_vars["filter_type"].get()
            slope = int(cfg_vars["slope"].get())
            defaults = filter_profile_defaults(ft, slope)
            cfg_vars["bulge_tolerance"].set(f"{float(defaults['bulge_tolerance_db']):g}")
            cfg_vars["bulge_penalty_weight"].set(f"{float(defaults['bulge_penalty_weight']):g}")
            cfg_vars["bulge_peak_penalty_weight"].set(f"{float(defaults['bulge_peak_penalty_weight']):g}")
            cfg_vars["bulge_peak"].set(float(defaults["bulge_peak_penalty_weight"]) > 0)
            cfg_vars["psycho_bulge"].set(bool(defaults["use_psycho_bulge_model"]))

            label_text = profile_label(label, ft, slope)
            preset_status_var.set(
                f"Preset aktywny: {label_text}. Pasma i scoring są ustawiane automatycznie."
            )
            profile_summary_var.set(
                f"Ocena: {e_low:g}–{e_high:g} Hz | kontrola bulwy: {b_low:g}–{b_high:g} Hz | "
                f"tryb oceny: {cfg_vars['score_profile'].get()} | Safe: {'ON' if bool(cfg_vars['sound_quality_safe'].get()) else 'OFF'} | "
                f"tolerancja bulwy: {float(defaults['bulge_tolerance_db']):g} dB | "
                f"kara bulwy: {float(defaults['bulge_penalty_weight']):g} | "
                f"pik bulwy: {float(defaults['bulge_peak_penalty_weight']):g}"
            )
            return True
        finally:
            preset_state["applying"] = False

    def on_profile_control_changed(event=None):
        apply_crossover_preset(event=event, force=True)

    def autodetect_profile_from_loaded_files(log_result: bool = True):
        detected_cross, detected_filter, detected_slope, warnings = infer_profile_from_paths(
            {k: v.get().strip() for k, v in vars_paths.items()}
        )
        changed = []
        if detected_cross:
            cfg_vars["crossover"].set(detected_cross)
            changed.append(detected_cross)
        if detected_filter:
            cfg_vars["filter_type"].set(detected_filter)
            changed.append(detected_filter)
        if detected_slope:
            cfg_vars["slope"].set(str(int(detected_slope)))
            changed.append(str(int(detected_slope)))
        apply_crossover_preset(force=True)
        if log_result:
            if changed:
                log("Autowykryto profil z nazw plików: " + profile_label(cfg_vars["crossover"].get(), cfg_vars["filter_type"].get(), int(cfg_vars["slope"].get())))
                log("Preset crossovera zastosowany automatycznie i zablokowany w GUI.")
            else:
                log("Nie udało się jednoznacznie wykryć profilu z nazw plików. Używam aktualnie wybranego presetu.")
            for w in warnings:
                log("- " + w)
        return warnings

    # v19.13: prostszy, dwukolumnowy układ. Nie upychamy wszystkiego w jeden
    # długi rząd, bo przy skalowaniu okna znikały przyciski i pole Top N.
    for col in range(6):
        cfg_box.columnconfigure(col, weight=0)
    cfg_box.columnconfigure(1, weight=1)
    cfg_box.columnconfigure(4, weight=1)

    ttk.Label(cfg_box, text="Crossover preset").grid(row=0, column=0, sticky="w", padx=6, pady=4)
    cross_combo = ttk.Combobox(
        cfg_box,
        textvariable=cfg_vars["crossover"],
        values=[k for k, v in CROSSOVER_PRESETS.items() if v is not None],
        width=12,
        state="readonly",
    )
    cross_combo.grid(row=0, column=1, sticky="w", padx=6, pady=4)
    cross_combo.bind("<<ComboboxSelected>>", on_profile_control_changed)

    ttk.Label(cfg_box, text="Filtr").grid(row=0, column=2, sticky="w", padx=(14, 6), pady=4)
    filter_combo = ttk.Combobox(
        cfg_box,
        textvariable=cfg_vars["filter_type"],
        values=FILTER_TYPES,
        width=8,
        state="readonly",
    )
    filter_combo.grid(row=0, column=3, sticky="w", padx=6, pady=4)
    filter_combo.bind("<<ComboboxSelected>>", on_profile_control_changed)

    ttk.Label(cfg_box, text="Slope").grid(row=0, column=4, sticky="w", padx=(14, 6), pady=4)
    slope_combo = ttk.Combobox(
        cfg_box,
        textvariable=cfg_vars["slope"],
        values=SLOPE_OPTIONS,
        width=8,
        state="readonly",
    )
    slope_combo.grid(row=0, column=5, sticky="w", padx=6, pady=4)
    slope_combo.bind("<<ComboboxSelected>>", on_profile_control_changed)

    ttk.Label(cfg_box, text="Tryb oceny").grid(row=1, column=0, sticky="w", padx=6, pady=4)
    score_profile_combo = ttk.Combobox(
        cfg_box,
        textvariable=cfg_vars["score_profile"],
        values=SCORE_PROFILES,
        width=16,
        state="readonly",
    )
    score_profile_combo.grid(row=1, column=1, sticky="w", padx=6, pady=4)
    score_profile_combo.bind("<<ComboboxSelected>>", lambda event: apply_crossover_preset(force=True))
    ttk.Label(
        cfg_box,
        text="PBIS = główny model integracji basu; Psychoacoustic = kontrola porównawcza.",
        foreground="#555",
        wraplength=300,
    ).grid(row=1, column=2, columnspan=2, sticky="w", padx=6, pady=4)

    ttk.Checkbutton(
        cfg_box,
        text="Sound Quality Safe",
        variable=cfg_vars["sound_quality_safe"],
    ).grid(row=1, column=4, columnspan=2, sticky="w", padx=6, pady=4)

    ttk.Checkbutton(
        cfg_box,
        text="Autowykryj profil z nazw plików",
        variable=cfg_vars["auto_detect_profile"],
    ).grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=4)
    ttk.Button(
        cfg_box,
        text="Autowykryj teraz",
        command=lambda: autodetect_profile_from_loaded_files(log_result=True),
    ).grid(row=2, column=3, columnspan=2, sticky="w", padx=6, pady=4)

    ttk.Label(cfg_box, text="Bazowy L_main w MDAT [ms]").grid(row=3, column=0, sticky="w", padx=6, pady=4)
    ttk.Entry(cfg_box, textvariable=cfg_vars["l_main_base_delay"], width=10).grid(row=3, column=1, sticky="w", padx=6, pady=4)
    ttk.Label(cfg_box, text="Bazowy R_main w MDAT [ms]").grid(row=3, column=2, sticky="w", padx=(14, 6), pady=4)
    ttk.Entry(cfg_box, textvariable=cfg_vars["r_main_base_delay"], width=10).grid(row=3, column=3, sticky="w", padx=6, pady=4)

    ttk.Label(cfg_box, text="Bazowy L_sub w MDAT [ms]").grid(row=4, column=0, sticky="w", padx=6, pady=4)
    ttk.Entry(cfg_box, textvariable=cfg_vars["l_base_delay"], width=10).grid(row=4, column=1, sticky="w", padx=6, pady=4)
    ttk.Label(cfg_box, text="Bazowy R_sub w MDAT [ms]").grid(row=4, column=2, sticky="w", padx=(14, 6), pady=4)
    ttk.Entry(cfg_box, textvariable=cfg_vars["r_base_delay"], width=10).grid(row=4, column=3, sticky="w", padx=6, pady=4)

    compact_fields = [
        (5, 0, "Zmiana relacji min [ms]", "delay_min"),
        (5, 2, "Zmiana relacji max [ms]", "delay_max"),
        (6, 0, "Krok [ms]", "delay_step"),
        (6, 2, "Top N", "top_n"),
    ]
    for r, c, label, key in compact_fields:
        ttk.Label(cfg_box, text=label).grid(row=r, column=c, sticky="w", padx=(6 if c == 0 else 14, 6), pady=4)
        ttk.Entry(cfg_box, textvariable=cfg_vars[key], width=10).grid(row=r, column=c + 1, sticky="w", padx=6, pady=4)

    ttk.Checkbutton(
        cfg_box,
        text="Tryb relatywny SHD: uwzględnij bazę MDAT i analizuj sub − main w obie strony",
        variable=cfg_vars["relative_delay_shd_mode"],
    ).grid(row=7, column=0, columnspan=4, sticky="w", padx=6, pady=4)
    ttk.Label(cfg_box, text="Limit SHD [ms]").grid(row=7, column=4, sticky="w", padx=(14, 6), pady=4)
    ttk.Entry(cfg_box, textvariable=cfg_vars["shd_delay_limit"], width=8).grid(row=7, column=5, sticky="w", padx=6, pady=4)

    ttk.Label(
        cfg_box,
        textvariable=preset_status_var,
        foreground="#555",
        wraplength=620,
    ).grid(row=8, column=0, columnspan=6, sticky="w", padx=6, pady=(2, 6))

    auto_profile_box = ttk.LabelFrame(frm, text="Automatyczny profil analizy")
    auto_profile_box.pack(fill="x", pady=4)
    ttk.Label(
        auto_profile_box,
        textvariable=profile_summary_var,
        foreground="#555",
        wraplength=620,
    ).pack(anchor="w", padx=8, pady=6)

    dirac_box = ttk.LabelFrame(frm, text="Dirac / kurtyna v18")
    dirac_box.pack(fill="x", pady=4)
    ttk.Label(dirac_box, text="Górna kurtyna Dirac [Hz]").grid(row=0, column=0, sticky="w", padx=6, pady=4)
    ttk.Entry(dirac_box, textvariable=cfg_vars["dirac_curtain_high"], width=10).grid(row=0, column=1, sticky="w", padx=6, pady=4)
    ttk.Label(dirac_box, text="Pasma ignorowane / nie goń delayem").grid(row=0, column=2, sticky="w", padx=6, pady=4)
    ttk.Entry(dirac_box, textvariable=cfg_vars["dirac_ignore_bands"], width=24).grid(row=0, column=3, sticky="w", padx=6, pady=4)
    ttk.Checkbutton(
        dirac_box,
        text="Priorytet: naturalna średnica / bez agresywnej korekcji",
        variable=cfg_vars["safe_midrange"],
    ).grid(row=0, column=4, columnspan=3, sticky="w", padx=6, pady=4)
    ttk.Label(
        dirac_box,
        text="Analiza kurtyny ocenia pomiary Dirac ON od końca integracji do górnej kurtyny. Nie zmienia scoringu delayu.",
        foreground="#555",
        wraplength=620,
    ).grid(row=1, column=0, columnspan=8, sticky="w", padx=6, pady=(0, 6))

    out_box = ttk.Frame(frm)
    out_box.pack(fill="x", pady=4)
    ttk.Label(out_box, text="Folder wyników").pack(side="left")
    ttk.Entry(out_box, textvariable=cfg_vars["out_dir"], width=90).pack(side="left", padx=6, fill="x", expand=True)

    def choose_out():
        path = filedialog.askdirectory(title="Wybierz folder wyników")
        if path:
            cfg_vars["out_dir"].set(path)
            save_current_gui_preferences()

    ttk.Button(out_box, text="Wybierz", command=choose_out).pack(side="left")

    # v19.13: rekomendacja miniDSP nie jest już zdublowana w lewym panelu.
    # Lewa kolumna zostaje odchudzona do wejść/ustawień, a rekomendacje
    # pojawiają się w jednej tabeli w prawej kolumnie.
    reco_var = tk.StringVar(value="Gotowe: rekomendacja pojawi się w tabeli SHD po optymalizacji.")
    minidsp_tree_ref = {"tree": None}
    minidsp_rows_cache = {"rows": []}
    last_scan_cache = {"candidates": [], "l_base_delay_ms": 0.0, "r_base_delay_ms": 0.0}

    def ui_call(fn, *args, **kwargs):
        """Bezpieczne wywołanie aktualizacji Tkinter z wątku roboczego."""
        try:
            if threading.current_thread() is threading.main_thread():
                fn(*args, **kwargs)
            else:
                root.after(0, lambda: fn(*args, **kwargs))
        except Exception:
            pass

    def set_reco(value: str):
        ui_call(reco_var.set, _clean_gui_message(value))

    def set_progress(value=None, maximum=None):
        def _apply():
            try:
                if maximum is not None:
                    progress["maximum"] = maximum
                if value is not None:
                    progress["value"] = value
            except Exception:
                pass
        ui_call(_apply)

    def update_minidsp_table(rows):
        """Aktualizuje czytelną tabelę rekomendacji miniDSP w prawej kolumnie."""
        minidsp_rows_cache["rows"] = list(rows or [])

        def _apply():
            tree = minidsp_tree_ref.get("tree")
            if tree is None:
                return
            try:
                for item in tree.get_children():
                    tree.delete(item)
                for row in minidsp_rows_cache["rows"]:
                    tree.insert(
                        "",
                        "end",
                        values=(
                            row.get("variant", ""),
                            row.get("l_main", ""),
                            row.get("r_main", ""),
                            row.get("l_sub", ""),
                            row.get("l_pol", ""),
                            row.get("r_sub", ""),
                            row.get("r_pol", ""),
                            row.get("relative", ""),
                            row.get("pbis", ""),
                            row.get("pbis_safe", ""),
                            row.get("natural", ""),
                            row.get("rating", ""),
                            row.get("loss", ""),
                            row.get("note", ""),
                        ),
                    )
            except Exception:
                pass
        ui_call(_apply)

    def append_minidsp_table_row(row: dict, replace_same_variant: bool = True):
        """Dopisuje pojedynczy wariant do tabeli miniDSP bez kasowania rekomendacji.

        v19.22: ręczny PBIS ma pojawiać się jako dodatkowy wiersz pod
        rekomendacjami z optymalizacji, a nie zastępować Głównej / Plateau / OFF-OFF.
        Jeśli tabela zawiera tylko placeholder "Po obliczeniu", placeholder jest usuwany.
        """
        current = list(minidsp_rows_cache.get("rows") or [])
        cleaned = []
        for existing in current:
            variant = str(existing.get("variant", ""))
            if variant == "Po obliczeniu":
                continue
            if replace_same_variant and variant == str(row.get("variant", "")):
                continue
            cleaned.append(existing)
        cleaned.append(row)
        update_minidsp_table(cleaned)

    def best_pbis_from_minidsp_table(default=None):
        """Zwraca najwyższy PBIS z aktualnej tabeli rekomendacji, jeśli jest dostępny."""
        best = None
        for existing in list(minidsp_rows_cache.get("rows") or []):
            raw = str(existing.get("pbis", "")).replace(",", ".").strip()
            try:
                value = float(raw)
            except Exception:
                continue
            if value > 0 and (best is None or value > best):
                best = value
        return best if best is not None else default

    def clear_minidsp_table():
        update_minidsp_table([])

    def score_loss_info(cand, best_score=None) -> tuple:
        """Przyjazna skala różnicy. Dla PBIS używa surowego PBIS, nie score z plateau."""
        if cand is None or best_score is None:
            return "—", "—", "brak punktu odniesienia"
        try:
            is_pbis = float(getattr(cand, "pbis_score", 0.0) or 0.0) > 0.0 and float(best_score) > 30.0
            if is_pbis:
                loss = max(0.0, float(best_score) - float(getattr(cand, "pbis_score", 0.0)))
            else:
                loss = max(0.0, float(best_score) - float(cand.score))
        except Exception:
            return "—", "—", "brak punktu odniesienia"

        if is_pbis:
            if loss <= 0.5:
                rating = "A / najlepsza"
                meaning = "punkt odniesienia"
            elif loss <= 2.0:
                rating = "A / remis"
                meaning = "różnica PBIS praktycznie pomijalna"
            elif loss <= 5.0:
                rating = "B / blisko"
                meaning = "mała różnica PBIS"
            elif loss <= 10.0:
                rating = "C / słabsza"
                meaning = "różnica PBIS możliwie słyszalna"
            else:
                rating = "D / strata"
                meaning = "różnica PBIS prawdopodobnie istotna"
            return rating, f"{loss:.1f} pkt", meaning

        if loss <= 0.05:
            rating = "A / najlepsza"
            meaning = "punkt odniesienia"
        elif loss <= 0.25:
            rating = "A / remis"
            meaning = "znaczenie odsłuchowe: bardzo małe"
        elif loss <= 0.75:
            rating = "B / blisko"
            meaning = "znaczenie odsłuchowe: małe"
        elif loss <= 1.50:
            rating = "C / słabsza"
            meaning = "znaczenie odsłuchowe: możliwe"
        elif loss <= 3.00:
            rating = "D / wyraźna strata"
            meaning = "znaczenie odsłuchowe: prawdopodobne"
        else:
            rating = "E / kontrolna"
            meaning = "znaczenie odsłuchowe: duże / wariant kontrolny"
        return rating, f"{loss:.2f}", meaning


    def candidate_to_minidsp_row(variant: str, cand, l_base_ms: float, r_base_ms: float, note: str = "", best_score=None, cfg=None) -> dict:
        rating, loss, meaning = score_loss_info(cand, best_score)
        note_parts = []
        if meaning and meaning != "punkt odniesienia":
            note_parts.append(meaning)
        if note:
            note_parts.append(note)
        flags = str(getattr(cand, "sound_quality_flags", "") or "")
        if flags and flags != "bez dużych flag":
            note_parts.append(flags)
        mapped = candidate_shd_settings(cand, l_base_ms, r_base_ms, cfg)
        return {
            "variant": variant,
            "l_main": f"{float(mapped['l_main_ms']):.2f} ms",
            "r_main": f"{float(mapped['r_main_ms']):.2f} ms",
            "l_sub": f"{float(mapped['l_sub_ms']):.2f} ms",
            "l_pol": str(cand.l_polarity),
            "r_sub": f"{float(mapped['r_sub_ms']):.2f} ms",
            "r_pol": str(cand.r_polarity),
            "relative": f"{float(mapped['l_sub_minus_main_ms']):+.2f} / {float(mapped['r_sub_minus_main_ms']):+.2f}",
            "pbis": f"{float(getattr(cand, 'pbis_score', 0.0)):.1f}",
            "pbis_safe": f"{float(getattr(cand, 'pbis_safe_score', getattr(cand, 'pbis_score', 0.0))):.1f}",
            "natural": str(getattr(cand, "naturalness_label", "")),
            "rating": rating,
            "loss": loss,
            "note": " | ".join(note_parts) if note_parts else meaning,
        }

    progress = ttk.Progressbar(frm, mode="determinate")
    progress.pack(fill="x", pady=8)

    stop_event = threading.Event()
    action_buttons = []
    cancel_button_ref = {"button": None}
    busy_state = {"busy": False}

    manual_box = ttk.LabelFrame(bottom_outer, text="Ręczny PBIS — pełny zestaw czterech delayów SHD")
    manual_box.pack(fill="x", pady=(0, 6))
    for col in range(10):
        manual_box.columnconfigure(col, weight=0)
    ttk.Label(manual_box, text="L main [ms]").grid(row=0, column=0, sticky="w", padx=6, pady=4)
    ttk.Entry(manual_box, textvariable=cfg_vars["manual_l_main_final"], width=7).grid(row=0, column=1, sticky="w", padx=4, pady=4)
    ttk.Label(manual_box, text="R main [ms]").grid(row=0, column=2, sticky="w", padx=(8, 4), pady=4)
    ttk.Entry(manual_box, textvariable=cfg_vars["manual_r_main_final"], width=7).grid(row=0, column=3, sticky="w", padx=4, pady=4)
    ttk.Label(manual_box, text="L sub [ms]").grid(row=0, column=4, sticky="w", padx=(8, 4), pady=4)
    ttk.Entry(manual_box, textvariable=cfg_vars["manual_l_final"], width=7).grid(row=0, column=5, sticky="w", padx=4, pady=4)
    ttk.Label(manual_box, text="R sub [ms]").grid(row=0, column=6, sticky="w", padx=(8, 4), pady=4)
    ttk.Entry(manual_box, textvariable=cfg_vars["manual_r_final"], width=7).grid(row=0, column=7, sticky="w", padx=4, pady=4)
    manual_pol_row = ttk.Frame(manual_box)
    manual_pol_row.grid(row=1, column=0, columnspan=5, sticky="w", padx=6, pady=4)
    ttk.Label(manual_pol_row, text="Polaryzacja sub L/R:").pack(side="left", padx=(0, 4))
    ttk.Combobox(manual_pol_row, textvariable=cfg_vars["manual_l_polarity"], values=["OFF", "INV"], width=5, state="readonly").pack(side="left", padx=(0, 4))
    ttk.Label(manual_pol_row, text="/").pack(side="left", padx=(0, 4))
    ttk.Combobox(manual_pol_row, textvariable=cfg_vars["manual_r_polarity"], values=["OFF", "INV"], width=5, state="readonly").pack(side="left", padx=(0, 8))
    manual_button = ttk.Button(manual_box, text="Oblicz PBIS", command=lambda: gui_evaluate_manual_pbis())
    manual_button.grid(row=1, column=7, columnspan=2, sticky="ew", padx=6, pady=4)
    action_buttons.append(manual_button)
    ttk.Label(
        manual_box,
        text="Wpisz dokładnie cztery wartości z SHD. Aplikacja odejmuje bazę fizyczną zapisaną już w MDAT i nakłada tylko zmianę relacji sub − main. W stereo L main i R main muszą być identyczne.",
        foreground="#555",
        wraplength=640,
    ).grid(row=2, column=0, columnspan=10, sticky="w", padx=6, pady=(0, 5))

    def set_busy(busy: bool):
        """Enable/disable action buttons from any thread."""
        def _apply():
            busy_state["busy"] = bool(busy)
            for btn in action_buttons:
                try:
                    btn.configure(state="disabled" if busy else "normal")
                except Exception:
                    pass
            if cancel_button_ref["button"] is not None:
                try:
                    cancel_button_ref["button"].configure(state="normal" if busy else "disabled")
                except Exception:
                    pass
        try:
            root.after(0, _apply)
        except Exception:
            pass

    def request_stop():
        """User requested cooperative cancellation of a long calculation."""
        if not busy_state["busy"]:
            return
        stop_event.set()
        set_reco("Zatrzymywanie przeliczania...")
        try:
            log("Żądanie zatrzymania przeliczania — czekam na bezpieczne przerwanie pętli.")
        except Exception:
            pass

    # v19.13: panel log/wyniki jest po prawej stronie okna i ma być widoczny od razu,
    # niezależnie od przewijanej sekcji ustawień po lewej.
    help_text = (
        "Workflow v19.60: wybierz .mdat → dla pliku RAW ustaw crossover do symulacji → START. "
        "Tryby Dirac/predykcji są w sekcji Zaawansowane."
    )
    ttk.Label(right_fixed, text=help_text, foreground="#555", wraplength=560).pack(fill="x", pady=(0, 4))

    workflow_status_var = tk.StringVar(value="Status: wybierz .mdat albo komplet TXT, potem kliknij START.")
    status_box = ttk.LabelFrame(right_fixed, text="Status projektu")
    status_box.pack(fill="x", pady=(0, 6))
    ttk.Label(
        status_box,
        textvariable=workflow_status_var,
        foreground="#222",
        wraplength=560,
        justify="left",
    ).pack(anchor="w", padx=8, pady=4)

    mini_box = ttk.LabelFrame(bottom_outer, text="Rekomendacje do wpisania w SHD")
    mini_box.pack(fill="x", pady=(0, 6))

    columns = ("variant", "l_main", "r_main", "l_sub", "l_pol", "r_sub", "r_pol", "relative", "pbis", "pbis_safe", "natural", "rating", "loss", "note")
    minidsp_tree = ttk.Treeview(mini_box, columns=columns, show="headings", height=4)
    minidsp_tree_ref["tree"] = minidsp_tree

    headings = {
        "variant": "Wariant",
        "l_main": "L main",
        "r_main": "R main",
        "l_sub": "L sub",
        "l_pol": "L pol",
        "r_sub": "R sub",
        "r_pol": "R pol",
        "relative": "rel. sub-main L/R",
        "pbis": "PBIS",
        "pbis_safe": "PBIS Safe",
        "natural": "Naturalność",
        "rating": "Ocena",
        "loss": "Strata",
        "note": "Znaczenie / uwagi",
    }
    widths = {
        "variant": 105,
        "l_main": 68,
        "r_main": 68,
        "l_sub": 68,
        "l_pol": 45,
        "r_sub": 68,
        "r_pol": 45,
        "relative": 108,
        "pbis": 56,
        "pbis_safe": 70,
        "natural": 88,
        "rating": 118,
        "loss": 70,
        "note": 230,
    }
    for col in columns:
        minidsp_tree.heading(col, text=headings[col])
        minidsp_tree.column(col, width=widths[col], minwidth=45, stretch=(col == "note"))

    minidsp_scroll = ttk.Scrollbar(mini_box, orient="vertical", command=minidsp_tree.yview)
    minidsp_scroll_x = ttk.Scrollbar(mini_box, orient="horizontal", command=minidsp_tree.xview)
    minidsp_tree.configure(yscrollcommand=minidsp_scroll.set, xscrollcommand=minidsp_scroll_x.set)
    minidsp_tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(6, 0))
    minidsp_scroll.grid(row=0, column=1, sticky="ns", pady=(6, 0), padx=(0, 8))
    minidsp_scroll_x.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 2))
    mini_box.columnconfigure(0, weight=1)

    ttk.Label(
        mini_box,
        text=(
            "PBIS = ocena punktu 0–100, zgodna z kalkulatorem ręcznym. "
            "PBIS Safe = ostrożniejsza ocena: uwzględnia nadmiar basu, różnice L/R, zgranie fazy i stabilność ustawienia. "
            "Tabela pokazuje wszystkie cztery delaye SHD oraz relację sub-main. Czwarty wiersz wskazuje wariant OFF/OFF do kontroli spójności fazowej L/R przy crossoverze; nie jest to pomiar szerokości ani głębokości sceny."
        ),
        foreground="#555555",
        wraplength=640,
        justify="left",
    ).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 3))

    ttk.Label(
        mini_box,
        textvariable=reco_var,
        foreground="#0B5D1E",
        wraplength=620,
        justify="left",
    ).grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

    update_minidsp_table([
        {
            "variant": "Po obliczeniu",
            "l_main": "—",
            "r_main": "—",
            "l_sub": "—",
            "l_pol": "—",
            "r_sub": "—",
            "r_pol": "—",
            "relative": "—",
            "pbis": "—",
            "pbis_safe": "—",
            "natural": "—",
            "rating": "—",
            "loss": "—",
            "note": "tu pojawią się finalne wartości i skala znaczenia",
        }
    ])

    bottom_box = ttk.LabelFrame(right_fixed, text="Workflow główny — zawsze widoczny")
    bottom_box.pack(fill="x", pady=(0, 6))

    plot_box = ttk.LabelFrame(bottom_outer, text="Najważniejsze wykresy — predykcja dla głównego wariantu OFF/OFF")
    plot_box.pack(fill="x", pady=(0, 6))

    advanced_box = ttk.LabelFrame(bottom_outer, text="Zaawansowane / walidacje")
    advanced_box.pack(fill="x", pady=(0, 6))

    result_box = ttk.LabelFrame(right_fixed, text="Log / wyniki — zawsze widoczny panel")
    result_box.pack(fill="x", pady=(0, 6))

    result_text = tk.Text(
        result_box,
        wrap="none",
        height=8,
        font=("Consolas", 9),
        relief="solid",
        borderwidth=1,
        background="#FFFFFF",
        foreground="#222222",
        insertbackground="#222222",
    )
    result_scroll_y = ttk.Scrollbar(result_box, orient="vertical", command=result_text.yview)
    result_scroll_x = ttk.Scrollbar(result_box, orient="horizontal", command=result_text.xview)
    result_text.configure(yscrollcommand=result_scroll_y.set, xscrollcommand=result_scroll_x.set)

    result_text.grid(row=0, column=0, sticky="nsew")
    result_scroll_y.grid(row=0, column=1, sticky="ns")
    result_scroll_x.grid(row=1, column=0, sticky="ew")
    result_box.rowconfigure(0, weight=1)
    result_box.columnconfigure(0, weight=1)

    def log(msg: str):
        clean = _simple_user_message(msg)
        if not clean:
            return
        def _append():
            try:
                result_text.insert("end", clean + "\n")
                result_text.see("end")
                # Wymuś odświeżenie po dłuższym obliczeniu, żeby log nie wyglądał na pusty.
                result_text.update_idletasks()
            except Exception:
                pass
        ui_call(_append)

    def update_workflow_status():
        """v19.16: jeden krótki status zamiast szukania po wielu katalogach."""
        try:
            out_dir = Path(cfg_vars["out_dir"].get().strip())
            mdat_selected = bool(mdat_path_var.get().strip())
            txt_complete = all(bool(v.get().strip()) for v in vars_paths.values())
            input_label = ".mdat" if mdat_selected else ("TXT komplet" if txt_complete else "brak kompletu wejścia")

            ranking = find_result_file(out_dir, "ranking.csv")
            pred = find_result_file(out_dir, "best_predicted_curves.csv")
            final_txt = find_result_file(out_dir, "MINIDSP_SETTINGS_FINAL.txt")
            project_status = find_result_file(out_dir, "PROJECT_STATUS.md")

            parts = [
                f"Wejście: {input_label}",
                f"Profil: {cfg_vars['crossover'].get()} / {cfg_vars['filter_type'].get()}{cfg_vars['slope'].get()} / {cfg_vars['score_profile'].get()} / Safe {'ON' if bool(cfg_vars['sound_quality_safe'].get()) else 'OFF'}",
                f"Optymalizacja: {'OK' if ranking else 'brak'}",
                f"Predykcja: {'OK' if pred else 'brak'}",
                f"miniDSP: {'OK' if final_txt else 'brak'}",
                f"Status: {'OK' if project_status else 'brak'}",
            ]
            next_step = "Kliknij START."
            if ranking and pred:
                next_step = "Podstawowe wyniki są gotowe. Opcjonalnie: Predykcja vs pomiar albo Dirac."
            elif ranking and not pred:
                next_step = "Uruchom ponownie optymalizację, żeby odtworzyć plik predykcji."
            elif not mdat_selected and not txt_complete:
                next_step = "Wybierz .mdat albo komplet TXT."
            workflow_status_var.set("\n".join(parts) + "\nNastępny krok: " + next_step)
        except Exception as exc:
            workflow_status_var.set("Status: nie udało się odświeżyć — " + str(exc))

    def show_modes_help():
        help_win = tk.Toplevel(root)
        help_win.title("Opis trybów i przycisków")
        help_win.geometry("980x720")
        help_win.minsize(760, 520)

        frame = ttk.Frame(help_win, padding=10)
        frame.pack(fill="both", expand=True)

        keyboard_hint = ttk.Label(
            frame,
            text="Przewijanie klawiaturą: ↑/↓, PageUp/PageDown, Home/End, Spacja, Shift+Spacja. Esc zamyka okno.",
            anchor="w",
        )
        keyboard_hint.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))

        text = tk.Text(
            frame,
            wrap="word",
            takefocus=True,
            relief="solid",
            borderwidth=1,
            background="#FFFFFF",
            foreground="#222222",
            insertbackground="#222222",
        )
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=yscroll.set)

        text.grid(row=1, column=0, sticky="nsew")
        yscroll.grid(row=1, column=1, sticky="ns")
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        text.insert("1.0", MODE_DESCRIPTIONS.strip())
        text.configure(state="disabled")

        def _scroll_units(units: int):
            text.yview_scroll(units, "units")
            return "break"

        def _scroll_pages(pages: int):
            text.yview_scroll(pages, "pages")
            return "break"

        def _scroll_to(fraction: float):
            text.yview_moveto(fraction)
            return "break"

        key_bindings = {
            "<Up>": lambda _event: _scroll_units(-3),
            "<Down>": lambda _event: _scroll_units(3),
            "<Prior>": lambda _event: _scroll_pages(-1),  # PageUp
            "<Next>": lambda _event: _scroll_pages(1),    # PageDown
            "<Home>": lambda _event: _scroll_to(0.0),
            "<End>": lambda _event: _scroll_to(1.0),
            "<space>": lambda _event: _scroll_pages(1),
            "<Shift-space>": lambda _event: _scroll_pages(-1),
            "<Shift-KeyPress-space>": lambda _event: _scroll_pages(-1),
            "<Escape>": lambda _event: (help_win.destroy(), "break")[-1],
        }
        for sequence, handler in key_bindings.items():
            help_win.bind(sequence, handler)
            text.bind(sequence, handler)

        text.focus_set()
        help_win.after(80, text.focus_set)

        # v19.13: bez dodatkowego przycisku "Zamknij"; działa Esc i standardowy X okna.

    def read_cfg_from_gui_for_extra():
        apply_mdat_baseline_from_path(mdat_path_var.get().strip(), log_result=False)
        apply_crossover_preset(force=True)
        crossover_label = cfg_vars["crossover"].get()
        filter_type = cfg_vars["filter_type"].get()
        slope_db_oct = int(cfg_vars["slope"].get())
        cfg = OptimizerConfig(
            delay_min_ms=float(cfg_vars["delay_min"].get().replace(",", ".")),
            delay_max_ms=float(cfg_vars["delay_max"].get().replace(",", ".")),
            delay_step_ms=float(cfg_vars["delay_step"].get().replace(",", ".")),
            eval_low_hz=float(cfg_vars["eval_low"].get().replace(",", ".")),
            eval_high_hz=float(cfg_vars["eval_high"].get().replace(",", ".")),
            bulge_low_hz=float(cfg_vars["bulge_low"].get().replace(",", ".")),
            bulge_high_hz=float(cfg_vars["bulge_high"].get().replace(",", ".")),
            top_n=int(cfg_vars["top_n"].get()),
            prefer_smoothness=float(filter_profile_defaults(filter_type, slope_db_oct)["prefer_smoothness"]),
            prefer_lr_sum=float(filter_profile_defaults(filter_type, slope_db_oct)["prefer_lr_sum"]),
            dip_penalty_weight=float(filter_profile_defaults(filter_type, slope_db_oct)["dip_penalty_weight"]),
            bulge_tolerance_db=float(cfg_vars["bulge_tolerance"].get().replace(",", ".")),
            bulge_penalty_weight=float(cfg_vars["bulge_penalty_weight"].get().replace(",", ".")),
            bulge_peak_penalty_weight=(float(cfg_vars["bulge_peak_penalty_weight"].get().replace(",", ".")) if bool(cfg_vars["bulge_peak"].get()) else 0.0),
            use_psycho_bulge_model=bool(cfg_vars["psycho_bulge"].get()),
            filter_type=filter_type,
            slope_db_oct=slope_db_oct,
            safe_midrange_priority=bool(cfg_vars["safe_midrange"].get()),
            scoring_profile=cfg_vars["score_profile"].get(),
            sound_quality_safe=bool(cfg_vars["sound_quality_safe"].get()),
            relative_delay_shd_mode=bool(cfg_vars["relative_delay_shd_mode"].get()),
            shd_delay_limit_ms=float(cfg_vars["shd_delay_limit"].get().replace(",", ".")),
            baseline_l_main_ms=float(cfg_vars["l_main_base_delay"].get().replace(",", ".")),
            baseline_r_main_ms=float(cfg_vars["r_main_base_delay"].get().replace(",", ".")),
            baseline_l_sub_ms=float(cfg_vars["l_base_delay"].get().replace(",", ".")),
            baseline_r_sub_ms=float(cfg_vars["r_base_delay"].get().replace(",", ".")),
        )

        return cfg, Path(cfg_vars["out_dir"].get().strip()), crossover_label, filter_type, slope_db_oct

    def gui_evaluate_manual_pbis():
        """v19.22: PBIS dla ręcznie wpisanych finalnych delayów miniDSP — spójny z tabelą rekomendacji."""
        try:
            cfg, out_dir, crossover_label, filter_type, slope_db_oct = read_cfg_from_gui_for_extra()
            # Ręczna ocena ma zawsze odpowiadać modelowi PBIS, niezależnie od trybu rankingowego w GUI.
            cfg.scoring_profile = "PBIS"
            l_base_delay_ms = float(cfg_vars["l_base_delay"].get().replace(",", "."))
            r_base_delay_ms = float(cfg_vars["r_base_delay"].get().replace(",", "."))
            l_main_final = float(cfg_vars["manual_l_main_final"].get().replace(",", "."))
            r_main_final = float(cfg_vars["manual_r_main_final"].get().replace(",", "."))
            l_final = float(cfg_vars["manual_l_final"].get().replace(",", "."))
            r_final = float(cfg_vars["manual_r_final"].get().replace(",", "."))
            l_pol = cfg_vars["manual_l_polarity"].get().strip().upper() or "OFF"
            r_pol = cfg_vars["manual_r_polarity"].get().strip().upper() or "OFF"
            if l_pol not in {"OFF", "INV"} or r_pol not in {"OFF", "INV"}:
                raise ValueError("Polaryzacja musi być OFF albo INV.")
            paths = {k: v.get().strip() for k, v in vars_paths.items()}
            mdat_input_path = mdat_path_var.get().strip()
            if not mdat_input_path and not all(paths.values()):
                messagebox.showerror("Brak danych", "Wybierz .mdat albo komplet TXT przed obliczeniem ręcznego PBIS.")
                return
        except Exception as e:
            messagebox.showerror("Błąd ustawień PBIS", str(e))
            return

        if busy_state["busy"]:
            messagebox.showwarning("Przeliczanie trwa", "Najpierw zatrzymaj albo poczekaj na zakończenie obecnego przeliczania.")
            return

        stop_event.clear()
        set_busy(True)

        def worker():
            try:
                log("")
                log("Ręczny PBIS: liczę surowy PBIS wybranego wariantu miniDSP.")
                log(f"Wariant SHD: main L/R {l_main_final:.2f}/{r_main_final:.2f} ms | sub L/R {l_final:.2f}/{r_final:.2f} ms {l_pol}/{r_pol}")
                log(f"Relacja sub-main: L {l_final-l_main_final:+.2f} ms | R {r_final-r_main_final:+.2f} ms")
                base = baseline_shd_settings(cfg, l_base_delay_ms, r_base_delay_ms)
                log(
                    f"Baza fizyczna MDAT: main L/R {float(base['l_main_ms']):.2f}/{float(base['r_main_ms']):.2f} ms | "
                    f"sub L/R {float(base['l_sub_ms']):.2f}/{float(base['r_sub_ms']):.2f} ms | "
                    f"rel. sub-main {float(base['l_sub_minus_main_ms']):+.2f}/{float(base['r_sub_minus_main_ms']):+.2f} ms"
                )

                if mdat_input_path:
                    l_main, l_sub, r_main, r_sub, mdat_project = load_optimizer_measurements_from_mdat(mdat_input_path)
                    health_errors, health_warnings = mdat_import_health_checks(mdat_project)
                    if health_errors:
                        raise ValueError("MDAT health-check nie przeszedł: " + "; ".join(health_errors))
                    for w in health_warnings:
                        log("OSTRZEŻENIE MDAT: " + w)
                else:
                    l_main = parse_rew_text(paths["L main"], "L_main")
                    l_sub = parse_rew_text(paths["L sub"], "L_sub")
                    r_main = parse_rew_text(paths["R main"], "R_main")
                    r_sub = parse_rew_text(paths["R sub"], "R_sub")

                freq, responses, cand, metrics = evaluate_manual_candidate_shd(
                    l_main, l_sub, r_main, r_sub, cfg,
                    l_main_final_ms=l_main_final, r_main_final_ms=r_main_final,
                    l_sub_final_ms=l_final, r_sub_final_ms=r_final,
                    l_base_delay_ms=l_base_delay_ms, r_base_delay_ms=r_base_delay_ms,
                    l_polarity=l_pol, r_polarity=r_pol,
                )
                matched_scan_candidate = find_scanned_candidate_by_final_values(
                    list(last_scan_cache.get("candidates") or []),
                    l_final - l_main_final,
                    r_final - r_main_final,
                    float(last_scan_cache.get("l_base_delay_ms", l_base_delay_ms)),
                    float(last_scan_cache.get("r_base_delay_ms", r_base_delay_ms)),
                    l_pol,
                    r_pol,
                    cfg=cfg,
                )
                if matched_scan_candidate is not None:
                    sync_pbis_safe_from_scanned_candidate(cand, matched_scan_candidate)
                    metrics["manual_matched_full_scan"] = 1.0
                    log("Ręczna ocena: znaleziono to samo ustawienie w ostatnim pełnym skanie. Wynik jest zgodny z automatem.")
                else:
                    metrics["manual_matched_full_scan"] = 0.0
                    log("Ręczna ocena: tego ustawienia nie było w ostatnim pełnym skanie. Traktuj wynik jako wstępny i sprawdź go pomiarem REW.")
                out_dir.mkdir(parents=True, exist_ok=True)
                md_path, csv_path = write_manual_pbis_report(
                    out_dir,
                    cand,
                    metrics,
                    cfg,
                    l_base_delay_ms,
                    r_base_delay_ms,
                    crossover_label,
                    filter_type,
                    slope_db_oct,
                )
                try:
                    plot_candidate(
                        freq,
                        responses,
                        cand,
                        out_dir / "custom_pbis",
                        cfg,
                        l_base_delay_ms,
                        r_base_delay_ms,
                        plot_label="Ręczny PBIS / wybrany wariant",
                    )
                    save_predicted_curves(freq, responses, cand, out_dir / "custom_pbis_predicted_curves.csv")
                except Exception:
                    log("Nie udało się przygotować wykresu ręcznej oceny. Sam wynik liczbowy jest nadal dostępny.")

                pbis = float(getattr(cand, "pbis_score", 0.0))
                label = str(getattr(cand, "pbis_label", ""))
                risks = str(getattr(cand, "pbis_risk_summary", ""))
                best_reference_pbis = best_pbis_from_minidsp_table(default=pbis)
                manual_variant_label = f"Ręczny SHD M {l_main_final:.2f} / S {l_final:.2f}/{r_final:.2f}"
                row = candidate_to_minidsp_row(
                    manual_variant_label,
                    cand,
                    l_base_delay_ms,
                    r_base_delay_ms,
                    "ręcznie obliczony wariant | " + risks,
                    best_score=best_reference_pbis,
                    cfg=cfg,
                )
                append_minidsp_table_row(row)
                safe = float(getattr(cand, "pbis_safe_score", pbis))
                set_reco(f"Ręczny wariant SHD: main L/R {l_main_final:.2f}/{r_main_final:.2f} ms; sub L/R {l_final:.2f}/{r_final:.2f} ms. PBIS {pbis:.1f}/100 — {label}.")

                log("")
                log(f"RĘCZNA OCENA USTAWIENIA OFF/OFF")
                log(f"PBIS: {pbis:.1f}/100 — {label}")
                for line in pbis_risk_user_lines(cand):
                    log("- " + line)
                try:
                    update_workflow_status()
                except Exception:
                    pass
            except Exception as e:
                log("BŁĄD ręcznego PBIS: " + str(e))
                set_reco("Błąd ręcznego PBIS — szczegóły w logu.")
                messagebox.showerror("Błąd ręcznego PBIS", str(e))
            finally:
                set_busy(False)

        threading.Thread(target=worker, daemon=True).start()

    def gui_validate_dirac():
        try:
            cfg, out_dir, crossover_label, filter_type, slope_db_oct = read_cfg_from_gui_for_extra()
            l_path = filedialog.askopenfilename(title="Wybierz L Dirac ON", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not l_path:
                return
            r_path = filedialog.askopenfilename(title="Wybierz R Dirac ON", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not r_path:
                return
            lr_path = filedialog.askopenfilename(title="Wybierz L+R Dirac ON", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not lr_path:
                return
            out = out_dir / "dirac_validation"
            verdict, metrics = validate_dirac_measurements(l_path, r_path, lr_path, out, cfg, label=profile_label(crossover_label, filter_type, slope_db_oct))
            log(f"Walidacja Dirac: {verdict}")
            for k, v in metrics.items():
                log(f"- {k}: {v:.2f}")
            messagebox.showinfo("Walidacja Dirac", f"Werdykt: {verdict}")
        except Exception as e:
            log("BŁĄD walidacji Dirac: " + str(e))
            messagebox.showerror("Błąd walidacji Dirac", str(e))

    def _read_dirac_curtain_options():
        curtain_high = float(cfg_vars["dirac_curtain_high"].get().replace(",", "."))
        ignore_bands = parse_frequency_bands(cfg_vars["dirac_ignore_bands"].get())
        return curtain_high, ignore_bands

    def gui_analyze_dirac_curtain():
        try:
            cfg, out_dir, crossover_label, filter_type, slope_db_oct = read_cfg_from_gui_for_extra()
            curtain_high, ignore_bands = _read_dirac_curtain_options()
            l_path = filedialog.askopenfilename(title="Wybierz L Dirac ON dla analizy kurtyny", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not l_path:
                return
            r_path = filedialog.askopenfilename(title="Wybierz R Dirac ON dla analizy kurtyny", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not r_path:
                return
            lr_path = filedialog.askopenfilename(title="Wybierz L+R Dirac ON dla analizy kurtyny", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not lr_path:
                return
            out = out_dir / "dirac_curtain_analysis"
            verdict, metrics = analyze_dirac_curtain_measurements(
                l_path,
                r_path,
                lr_path,
                out,
                cfg,
                curtain_high_hz=curtain_high,
                ignore_bands=ignore_bands,
                label=f"{profile_label(crossover_label, filter_type, slope_db_oct)} / kurtyna {curtain_high:.0f} Hz",
            )
            log(f"Analiza kurtyny Dirac: {verdict}")
            log(f"- kurtyna: {curtain_high:.0f} Hz")
            log(f"- score bezpieczeństwa: {metrics['curtain_safety_score']:.2f}")
            log(f"- margin mean/p05: {metrics['curtain_mean_margin_db']:.2f} / {metrics['curtain_p05_margin_db']:.2f} dB")
            log(f"- dip/roughness: {metrics['curtain_dip_db']:.2f} / {metrics['curtain_roughness_db']:.2f} dB")
            log(f"- tonal shift: {metrics['curtain_tonal_shift_vs_alignment_db']:+.2f} dB")
            messagebox.showinfo("Analiza kurtyny Dirac", f"Werdykt: {verdict}\nOcena: {metrics['curtain_safety_score']:.2f}")
        except Exception as e:
            log("BŁĄD analizy kurtyny Dirac: " + str(e))
            messagebox.showerror("Błąd analizy kurtyny Dirac", str(e))

    def gui_compare_dirac_curtains():
        try:
            cfg, out_dir, crossover_label, filter_type, slope_db_oct = read_cfg_from_gui_for_extra()
            curtain_high, ignore_bands = _read_dirac_curtain_options()
            folder = filedialog.askdirectory(title="Wybierz folder z zestawami Dirac ON L/R/L+R dla różnych kurtyn")
            if not folder:
                return
            out = out_dir / "dirac_curtain_comparison"
        except Exception as e:
            log("BŁĄD ustawień porównania kurtyn Dirac: " + str(e))
            messagebox.showerror("Błąd porównania kurtyn Dirac", str(e))
            return

        if busy_state["busy"]:
            messagebox.showwarning("Przeliczanie trwa", "Najpierw zatrzymaj albo poczekaj na zakończenie obecnego przeliczania.")
            return

        stop_event.clear()
        set_busy(True)

        def prog(done, total):
            set_progress(done, total)

        def worker():
            try:
                set_progress(0)
                log("Porównuję kurtyny Dirac z folderu: " + folder)
                summary = compare_dirac_curtains_folder(
                    folder,
                    out,
                    cfg,
                    default_curtain_high_hz=curtain_high,
                    ignore_bands=ignore_bands,
                    stop_event=stop_event,
                    progress_callback=prog,
                )
                if not summary:
                    messagebox.showwarning(
                        "Porównanie kurtyn Dirac",
                        "Nie znaleziono kompletnych zestawów L/R/L+R. Przykład nazw: Dirac_200_L.txt, Dirac_200_R.txt, Dirac_200_LR.txt.",
                    )
                    log("Nie znaleziono kompletnych zestawów L/R/L+R.")
                    return
                log("Ranking kurtyn Dirac:")
                for i, row in enumerate(summary[:10], start=1):
                    log(
                        f"{i}. {row['group']} kurtyna={float(row['curtain_high_hz']):.0f} Hz "
                        f"werdykt={row['verdict']} score={float(row['curtain_safety_score']):.2f} "
                        f"margin={float(row['curtain_mean_margin_db']):.2f} dB dip={float(row['curtain_dip_db']):.2f} dB"
                    )
                    best = summary[0]
                messagebox.showinfo(
                    "Porównanie kurtyn Dirac",
                    f"Najbezpieczniejsza: {best['group']} ({best['verdict']})",
                )
            except CalculationCancelled as e:
                log("ZATRZYMANO porównanie kurtyn Dirac: " + str(e))
                set_reco("Zatrzymano porównanie kurtyn Dirac.")
            except Exception as e:
                log("BŁĄD porównania kurtyn Dirac: " + str(e))
                messagebox.showerror("Błąd porównania kurtyn Dirac", str(e))
            finally:
                set_busy(False)

        threading.Thread(target=worker, daemon=True).start()

    def gui_compare_prediction_real():
        try:
            cfg, out_dir, crossover_label, filter_type, slope_db_oct = read_cfg_from_gui_for_extra()
            pred = find_result_file(out_dir, "best_predicted_curves.csv")
            if pred is None:
                expected = out_dir / "best_predicted_curves.csv"
                expected_org = out_dir / "03_GLOWNA_REKOMENDACJA_BEST" / "best_predicted_curves.csv"
                messagebox.showerror(
                    "Brak predykcji",
                    "Nie znaleziono pliku predykcji.\n\n"
                    f"Sprawdzałem m.in.:\n{expected}\n{expected_org}\n\n"
                    "Najpierw uruchom optymalizację."
                )
                return
            l_path = filedialog.askopenfilename(title="Wybierz realny L_sum", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not l_path:
                return
            r_path = filedialog.askopenfilename(title="Wybierz realny R_sum", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not r_path:
                return
            lr_path = filedialog.askopenfilename(title="Wybierz realny L+R", filetypes=[("Text files", "*.txt *.csv *.dat"), ("All files", "*.*")])
            if not lr_path:
                return
            out = out_dir / "prediction_vs_real"
            metrics = compare_prediction_to_real_measurements(pred, l_path, r_path, lr_path, out, cfg, organize_output=False)
            log("Predykcja vs realny pomiar:")
            for k, v in metrics.items():
                log(f"- {k}: {v:.2f}")
            messagebox.showinfo("Predykcja vs real", f"Różnica pomiaru: {metrics['LR_rms_error_db']:.2f} dB")
        except Exception as e:
            log("BŁĄD porównania predykcji: " + str(e))
            messagebox.showerror("Błąd predykcji vs real", str(e))

    def gui_compare_presets():
        try:
            cfg, out_dir, crossover_label, filter_type, slope_db_oct = read_cfg_from_gui_for_extra()
            folder = filedialog.askdirectory(title="Wybierz folder z plikami presetów")
            if not folder:
                return
            out = out_dir / "preset_comparison"
        except Exception as e:
            log("BŁĄD ustawień porównania presetów: " + str(e))
            messagebox.showerror("Błąd porównania presetów", str(e))
            return

        if busy_state["busy"]:
            messagebox.showwarning("Przeliczanie trwa", "Najpierw zatrzymaj albo poczekaj na zakończenie obecnego przeliczania.")
            return

        stop_event.clear()
        set_busy(True)

        def prog(done, total):
            set_progress(done, total)

        def worker():
            try:
                set_progress(0)
                log("Porównuję presety z folderu: " + folder)
                summary = compare_presets_folder(folder, out, cfg, stop_event=stop_event, progress_callback=prog)
                if not summary:
                    messagebox.showwarning("Porównanie presetów", "Nie znaleziono kompletnych grup plików.")
                    log("Nie znaleziono kompletnych grup plików.")
                    return
                log("Ranking presetów:")
                for i, row in enumerate(summary[:10], start=1):
                    log(f"{i}. {row['preset']} {row['filter']} score={float(row['score']):.2f} "
                        f"L={float(row['L_final_ms']):.2f} {row['L_pol']} R={float(row['R_final_ms']):.2f} {row['R_pol']} "
                        f"conf={row['confidence']}")
                    messagebox.showinfo("Porównanie presetów", f"Znaleziono {len(summary)} presetów.")
            except CalculationCancelled as e:
                log("ZATRZYMANO porównanie presetów: " + str(e))
                set_reco("Zatrzymano porównanie presetów.")
            except Exception as e:
                log("BŁĄD porównania presetów: " + str(e))
                messagebox.showerror("Błąd porównania presetów", str(e))
            finally:
                set_busy(False)

        threading.Thread(target=worker, daemon=True).start()

    def validate_and_run():
        try:
            paths = {k: v.get().strip() for k, v in vars_paths.items()}
            mdat_input_path = mdat_path_var.get().strip()
            missing = [k for k, pth in paths.items() if not pth]
            use_mdat_input = bool(mdat_input_path)
            if use_mdat_input:
                apply_mdat_baseline_from_path(mdat_input_path, log_result=False)
            if missing and not use_mdat_input:
                messagebox.showerror("Brak plików", "Brak: " + ", ".join(missing) + "\n\nWybierz .mdat albo komplet eksportów TXT.")
                return

            auto_profile_warnings: List[str] = []
            if bool(cfg_vars["auto_detect_profile"].get()):
                if use_mdat_input:
                    scan = scan_mdat_project(mdat_input_path)
                    profile_paths = {}
                    for m in list(scan.get("measurements", [])):  # type: ignore[arg-type]
                        if isinstance(m, MdatMeasurementInfo):
                            profile_paths[m.role] = m.name
                    detected_cross, detected_filter, detected_slope, auto_profile_warnings = infer_profile_from_paths(profile_paths)
                    if detected_cross:
                        cfg_vars["crossover"].set(detected_cross)
                    if detected_filter:
                        cfg_vars["filter_type"].set(detected_filter)
                    if detected_slope:
                        cfg_vars["slope"].set(str(int(detected_slope)))
                else:
                    auto_profile_warnings = autodetect_profile_from_loaded_files(log_result=False)
            apply_crossover_preset(force=True)

            crossover_label = cfg_vars["crossover"].get()
            filter_type = cfg_vars["filter_type"].get()
            slope_db_oct = int(cfg_vars["slope"].get())

            defaults = filter_profile_defaults(filter_type, slope_db_oct)
            cfg = OptimizerConfig(
                delay_min_ms=float(cfg_vars["delay_min"].get().replace(",", ".")),
                delay_max_ms=float(cfg_vars["delay_max"].get().replace(",", ".")),
                delay_step_ms=float(cfg_vars["delay_step"].get().replace(",", ".")),
                eval_low_hz=float(cfg_vars["eval_low"].get().replace(",", ".")),
                eval_high_hz=float(cfg_vars["eval_high"].get().replace(",", ".")),
                bulge_low_hz=float(cfg_vars["bulge_low"].get().replace(",", ".")),
                bulge_high_hz=float(cfg_vars["bulge_high"].get().replace(",", ".")),
                top_n=int(cfg_vars["top_n"].get()),
                prefer_smoothness=float(defaults["prefer_smoothness"]),
                prefer_lr_sum=float(defaults["prefer_lr_sum"]),
                dip_penalty_weight=float(defaults["dip_penalty_weight"]),
                bulge_tolerance_db=float(cfg_vars["bulge_tolerance"].get().replace(",", ".")),
                bulge_penalty_weight=float(cfg_vars["bulge_penalty_weight"].get().replace(",", ".")),
                bulge_peak_penalty_weight=(float(cfg_vars["bulge_peak_penalty_weight"].get().replace(",", ".")) if bool(cfg_vars["bulge_peak"].get()) else 0.0),
                use_psycho_bulge_model=bool(cfg_vars["psycho_bulge"].get()),
                filter_type=filter_type,
                slope_db_oct=slope_db_oct,
                safe_midrange_priority=bool(cfg_vars["safe_midrange"].get()),
                scoring_profile=cfg_vars["score_profile"].get(),
                sound_quality_safe=bool(cfg_vars["sound_quality_safe"].get()),
                relative_delay_shd_mode=bool(cfg_vars["relative_delay_shd_mode"].get()),
                shd_delay_limit_ms=float(cfg_vars["shd_delay_limit"].get().replace(",", ".")),
                baseline_l_main_ms=float(cfg_vars["l_main_base_delay"].get().replace(",", ".")),
                baseline_r_main_ms=float(cfg_vars["r_main_base_delay"].get().replace(",", ".")),
                baseline_l_sub_ms=float(cfg_vars["l_base_delay"].get().replace(",", ".")),
                baseline_r_sub_ms=float(cfg_vars["r_base_delay"].get().replace(",", ".")),
            )
            save_current_gui_preferences()
            out_dir = Path(cfg_vars["out_dir"].get().strip())
            l_base_delay_ms = float(cfg_vars["l_base_delay"].get().replace(",", "."))
            r_base_delay_ms = float(cfg_vars["r_base_delay"].get().replace(",", "."))
        except Exception as e:
            messagebox.showerror("Błąd ustawień", str(e))
            return

        if busy_state["busy"]:
            messagebox.showwarning("Przeliczanie trwa", "Najpierw zatrzymaj albo poczekaj na zakończenie obecnego przeliczania.")
            return

        stop_event.clear()
        set_busy(True)

        def worker():
            try:
                ui_call(result_text.delete, "1.0", "end")
                clear_minidsp_table()
                set_reco("Liczenie — tabela SHD zostanie odświeżona po zakończeniu.")
                set_progress(0)

                log("Wczytuję dane REW...")
                log(f"Crossover preset: {crossover_label}")
                log(f"Filtr: {filter_type}{slope_db_oct}")
                if auto_profile_warnings:
                    log("Autodetekcja profilu:")
                    for w in auto_profile_warnings:
                        log("- " + w)
                base = baseline_shd_settings(cfg, l_base_delay_ms, r_base_delay_ms)
                log(
                    f"Baza fizyczna MDAT: main L/R {float(base['l_main_ms']):.2f}/{float(base['r_main_ms']):.2f} ms | "
                    f"sub L/R {float(base['l_sub_ms']):.2f}/{float(base['r_sub_ms']):.2f} ms | "
                    f"rel. sub-main {float(base['l_sub_minus_main_ms']):+.2f}/{float(base['r_sub_minus_main_ms']):+.2f} ms"
                )
                if bool(getattr(cfg, "relative_delay_shd_mode", False)):
                    log(f"Tryb RAW / relatywny SHD: ON | relacja sub-main {cfg.delay_min_ms:+.2f}..{cfg.delay_max_ms:+.2f} ms | limit wyjść 0..{cfg.shd_delay_limit_ms:.2f} ms")
                else:
                    log("Tryb standardowy: delaye skanowane jako dodatkowe opóźnienie subów.")
                log("Wyniki końcowe będą podane jako cztery wartości gotowe do wpisania w SHD.")
                log(f"Model bulwy: {'psycho' if cfg.use_psycho_bulge_model else 'prosty'} | tolerancja {cfg.bulge_tolerance_db:.2f} dB | siła {cfg.bulge_penalty_weight:.2f} | peak {cfg.bulge_peak_penalty_weight:.2f}")
                log("")

                if use_mdat_input:
                    log("Tryb wejścia: bezpośredni import .mdat bez eksportów TXT z REW.")
                    l_main, l_sub, r_main, r_sub, mdat_project = load_optimizer_measurements_from_mdat(mdat_input_path)
                    md_num, csv_num = write_mdat_numeric_import_report(mdat_project, out_dir)
                    health_report = write_mdat_health_report(mdat_project, out_dir)
                    health_errors, health_warnings = mdat_import_health_checks(mdat_project)
                    if health_errors:
                        for e in health_errors:
                            log("BŁĄD MDAT: " + e)
                        raise ValueError("MDAT health-check nie przeszedł. Szczegóły w MDAT_HEALTH_CHECK.md.")
                    input_paths = mdat_measurement_path_labels(mdat_project)
                    log(f"Zaimportowano .mdat: {mdat_project.points} punktów, {mdat_project.freq_start_hz:.2f}–{mdat_project.freq_end_hz:.2f} Hz.")
                    for w in health_warnings:
                        log("OSTRZEŻENIE MDAT: " + w)
                    for role in ["L main", "L sub", "R main", "R sub"]:
                        log(f"{role}: {mdat_project.role_to_name.get(role, '')}")
                    if is_raw_xover_off_project(mdat_project):
                        if crossover_label in {"Auto", "Custom"}:
                            raise ValueError("Dla pomiaru RAW wybierz konkretny preset crossovera, np. 90 Hz.")
                        cfg.relative_delay_shd_mode = True
                        cfg.shd_delay_limit_ms = float(cfg_vars["shd_delay_limit"].get().replace(",", "."))
                        if float(cfg.delay_min_ms) >= 0.0:
                            cfg.delay_min_ms = -30.0
                            cfg.delay_max_ms = 30.0
                        xo_hz = parse_crossover_hz(crossover_label, cfg)
                        l_main, l_sub, r_main, r_sub = simulate_shd_crossover_from_raw(
                            l_main, l_sub, r_main, r_sub, xo_hz, filter_type, slope_db_oct
                        )
                        setattr(cfg, "raw_crossover_simulation", True)
                        setattr(cfg, "raw_crossover_hz", float(xo_hz))
                        write_raw_simulation_note(out_dir, xo_hz, filter_type, slope_db_oct)
                        log(raw_simulation_description(xo_hz, filter_type, slope_db_oct))
                else:
                    l_main = parse_rew_text(paths["L main"], "L_main")
                    l_sub = parse_rew_text(paths["L sub"], "L_sub")
                    r_main = parse_rew_text(paths["R main"], "R_main")
                    r_sub = parse_rew_text(paths["R sub"], "R_sub")
                    input_paths = paths

                input_warnings = build_input_warnings(
                    input_paths,
                    {"L_main": l_main, "L_sub": l_sub, "R_main": r_main, "R_sub": r_sub},
                    cfg,
                    l_base_delay_ms,
                    r_base_delay_ms,
                )
                if input_warnings:
                    log("OSTRZEŻENIA WEJŚCIA:")
                    for w in input_warnings:
                        log("- " + w)
                    log("")

                out_dir.mkdir(parents=True, exist_ok=True)

                def prog(done, total):
                    progress["maximum"] = total
                    progress["value"] = done

                timings: Dict[str, float] = {}
                validation_confidence = ""
                smart_status = ""

                log(optimizer_mode_summary(cfg))
                pass
                log("Liczenie ustawień...")
                t0 = time.perf_counter()
                freq, responses, candidates = optimize_delays(l_main, l_sub, r_main, r_sub, cfg, progress_callback=prog, stop_event=stop_event)
                timings["optimization"] = time.perf_counter() - t0
                log(f"Czas optymalizacji: {timings['optimization']:.2f} s")
                last_scan_cache["candidates"] = list(candidates or [])
                last_scan_cache["l_base_delay_ms"] = float(l_base_delay_ms)
                last_scan_cache["r_base_delay_ms"] = float(r_base_delay_ms)

                if candidates:
                    t0 = time.perf_counter()
                    validation_confidence, validation_path, validation_notes = write_fast_result_validation(
                        candidates,
                        freq,
                        responses,
                        out_dir,
                        cfg,
                        l_base_delay_ms,
                        r_base_delay_ms,
                        crossover_label,
                        filter_type,
                        slope_db_oct,
                    )
                    timings["fast_validation"] = time.perf_counter() - t0
                    log(f"Walidacja FAST 2-STAGE: {validation_confidence} — {validation_path}")
                    pass

                    t0 = time.perf_counter()
                    candidates_after_smart, smart_status, smart_path, smart_notes = write_smart_rescan_if_needed(
                        candidates,
                        freq,
                        responses,
                        out_dir,
                        cfg,
                        l_base_delay_ms,
                        r_base_delay_ms,
                        crossover_label,
                        filter_type,
                        slope_db_oct,
                        validation_confidence=validation_confidence,
                        validation_notes=validation_notes,
                    )
                    timings["smart_rescan"] = time.perf_counter() - t0
                    log(f"Dodatkowe sprawdzenie wyniku: {simple_rescan_label(smart_status)}")
                    pass
                    if smart_status not in {"SKIPPED", "DISABLED"}:
                        candidates = candidates_after_smart
                        last_scan_cache["candidates"] = list(candidates or [])
                        t0 = time.perf_counter()
                        validation_confidence, validation_path, validation_notes = write_fast_result_validation(
                            candidates,
                            freq,
                            responses,
                            out_dir,
                            cfg,
                            l_base_delay_ms,
                            r_base_delay_ms,
                            crossover_label,
                            filter_type,
                            slope_db_oct,
                        )
                        timings["fast_validation_after_smart"] = time.perf_counter() - t0
                        log(f"Kontrola wyniku po dodatkowym sprawdzeniu: {simple_validation_label(validation_confidence)}")

                log("Przygotowuję wynik...")
                t0 = time.perf_counter()
                refresh_candidate_outputs(
                    candidates, freq, responses, out_dir, cfg,
                    l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct,
                    input_warnings, log_callback=log,
                )
                timings["write_outputs_final_once"] = time.perf_counter() - t0
                pass

                perf_path, perf_notes = write_performance_diagnostics(
                    out_dir, cfg, timings,
                    validation_confidence=validation_confidence,
                    smart_status=smart_status,
                    final_candidates_count=len(candidates or []),
                )
                pass
                pass

                if candidates:
                    off = best_both_off(candidates)
                    if off is None:
                        update_minidsp_table([])
                        set_reco("Nie znaleziono wariantu OFF/OFF. Sprawdź dane wejściowe i zakres opóźnień.")
                        log("Nie znaleziono wariantu OFF/OFF. Sprawdź dane wejściowe i zakres opóźnień.")
                    else:
                        log("")
                        log(simple_off_off_summary_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg))
                        rows = off_off_candidates(candidates, limit=max(1, min(3, cfg.top_n)))
                        best_score_for_table = float(getattr(off, "pbis_score", off.score))
                        minidsp_rows = []
                        labels = ["Główny OFF/OFF", "Zapasowy OFF/OFF", "Dodatkowy OFF/OFF"]
                        for i, cand in enumerate(rows):
                            minidsp_rows.append(candidate_to_minidsp_row(
                                labels[i] if i < len(labels) else f"OFF/OFF {i+1}",
                                cand,
                                l_base_delay_ms,
                                r_base_delay_ms,
                                "do sprawdzenia pomiarem REW",
                                best_score=best_score_for_table,
                                cfg=cfg,
                            ))

                        # v19.44: czwarty, jawny wiersz pokazuje najlepszy wariant
                        # OFF/OFF do odsłuchu pod kątem spójności czasowej i fazowej.
                        # Wiersz pozostaje widoczny również wtedy, gdy wskazuje to samo
                        # ustawienie co rekomendacja główna — użytkownik od razu widzi,
                        # że pierwszy wariant spełnia także ten dodatkowy cel.
                        spatial_cand, _spatial_reason = select_spatial_coherence_candidate(
                            candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg
                        )
                        if spatial_cand is not None:
                            if same_candidate(spatial_cand, off):
                                spatial_note = "także do kontroli spójności L/R; taki sam jak główny"
                            else:
                                spatial_note = "do kontroli spójności L/R przy XO"
                            minidsp_rows.append(candidate_to_minidsp_row(
                                "Spójność L/R przy XO OFF/OFF",
                                spatial_cand,
                                l_base_delay_ms,
                                r_base_delay_ms,
                                spatial_note,
                                best_score=best_score_for_table,
                                cfg=cfg,
                            ))
                        update_minidsp_table(minidsp_rows)
                        mapped = candidate_shd_settings(off, l_base_delay_ms, r_base_delay_ms, cfg)
                        # v19.51: kalkulator ręczny startuje od dokładnie tego samego
                        # zestawu czterech wartości, który pokazuje rekomendacja.
                        ui_call(cfg_vars["manual_l_main_final"].set, f"{float(mapped['l_main_ms']):.2f}")
                        ui_call(cfg_vars["manual_r_main_final"].set, f"{float(mapped['r_main_ms']):.2f}")
                        ui_call(cfg_vars["manual_l_final"].set, f"{float(mapped['l_sub_ms']):.2f}")
                        ui_call(cfg_vars["manual_r_final"].set, f"{float(mapped['r_sub_ms']):.2f}")
                        ui_call(cfg_vars["manual_l_polarity"].set, str(getattr(off, "l_polarity", "OFF")))
                        ui_call(cfg_vars["manual_r_polarity"].set, str(getattr(off, "r_polarity", "OFF")))
                        set_reco(
                            f"Gotowe. Wpisz OFF/OFF: main L/R {float(mapped['l_main_ms']):.2f}/{float(mapped['r_main_ms']):.2f} ms; "
                            f"sub L/R {float(mapped['l_sub_ms']):.2f}/{float(mapped['r_sub_ms']):.2f} ms. "
                            f"PBIS {float(getattr(off, 'pbis_score', 0.0)):.1f}/100. Następnie zrób pomiar REW."
                        )
                        log("Gotowe.")
            except CalculationCancelled as e:
                log("\nZATRZYMANO:")
                log(str(e))
                set_reco("Zatrzymano przez użytkownika.")
            except Exception as e:
                log("\nBŁĄD:")
                log(str(e))
                set_reco("Błąd — szczegóły w logu poniżej.")
                messagebox.showerror("Błąd", str(e))
            finally:
                try:
                    save_current_gui_preferences()
                except Exception:
                    pass
                set_busy(False)
                try:
                    root.after(0, update_workflow_status)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def clear_log():
        try:
            ui_call(result_text.delete, "1.0", "end")
        except Exception:
            pass

    def open_results_folder():
        path = Path(cfg_vars["out_dir"].get().strip())
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", str(path)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror("Nie mogę otworzyć folderu wyników", str(e))

    def open_result_file_by_name(filename: str):
        out_dir = Path(cfg_vars["out_dir"].get().strip())
        path = find_result_file(out_dir, filename)
        if path is None and filename == "START_TUTAJ.md":
            alt = out_dir / "00_START_TUTAJ" / "START_TUTAJ.md"
            path = alt if alt.exists() else None
        if path is None:
            messagebox.showwarning("Brak pliku", f"Nie znaleziono: {filename}\nNajpierw uruchom START albo odśwież status.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", str(path)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror("Nie mogę otworzyć pliku", str(e))

    def preview_result_image(filename: str, title: str | None = None):
        """Otwórz wykres w GUI z automatycznym skalowaniem do okna.

        v19.16:
        - domyślnie dopasowuje obraz do aktualnego rozmiaru okna,
        - obsługuje powiększanie/pomniejszanie i tryb 100%,
        - przy zmianie rozmiaru okna ponownie skaluje wykres.
        """
        out_dir = Path(cfg_vars["out_dir"].get().strip())
        initial_path = find_result_file(out_dir, filename)
        if initial_path is None:
            messagebox.showwarning(
                "Brak wykresu",
                f"Nie znaleziono: {filename}\nNajpierw uruchom START / optymalizację."
            )
            return

        try:
            from PIL import Image, ImageTk
        except Exception:
            Image = None
            ImageTk = None

        win = tk.Toplevel(root)
        win.title(title or filename)
        win.geometry("1120x780")
        win.minsize(720, 480)

        top = ttk.Frame(win, padding=(8, 6))
        top.pack(fill="x")

        status_var = tk.StringVar(value=Path(initial_path).name)
        ttk.Label(top, textvariable=status_var, anchor="w").pack(side="left", fill="x", expand=True)

        body = ttk.Frame(win, padding=(8, 0, 8, 8))
        body.pack(fill="both", expand=True)

        canvas = tk.Canvas(body, background="#F2F2F2", highlightthickness=0)
        yscroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        xscroll = ttk.Scrollbar(body, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        state = {
            "pil": None,
            "tk_photo": None,
            "path": initial_path,
            "image_id": None,
            "scale_mode": "fit",  # fit albo manual
            "manual_scale": 1.0,
            "after_id": None,
            "fallback_photo": None,
        }

        def _open_external():
            open_result_file_by_name(filename)

        def _get_resample_filter():
            if Image is None:
                return None
            try:
                return Image.Resampling.LANCZOS
            except Exception:
                return getattr(Image, "LANCZOS", 1)

        def _load_original():
            current_path = find_result_file(out_dir, filename)
            if current_path is None:
                messagebox.showwarning("Brak wykresu", f"Nie znaleziono: {filename}")
                return False
            state["path"] = current_path

            # Najlepsza ścieżka: Pillow daje płynne skalowanie.
            if Image is not None and ImageTk is not None:
                try:
                    pil_img = Image.open(current_path)
                    pil_img.load()
                    state["pil"] = pil_img.convert("RGBA")
                    state["fallback_photo"] = None
                    return True
                except Exception as exc:
                    messagebox.showerror("Nie mogę wczytać wykresu", str(exc))
                    return False

            # Fallback bez Pillow: działa jak stary podgląd, bez płynnego skalowania.
            try:
                photo = tk.PhotoImage(file=str(current_path))
                state["fallback_photo"] = photo
                state["tk_photo"] = photo
                win._preview_photo = photo
                canvas.delete("all")
                canvas.create_image(0, 0, image=photo, anchor="nw")
                canvas.configure(scrollregion=(0, 0, max(photo.width(), 1), max(photo.height(), 1)))
                status_var.set(
                    f"{Path(current_path).name}   |   {photo.width()} × {photo.height()} px   |   "
                    "brak Pillow — podgląd bez skalowania"
                )
                return False
            except Exception as exc:
                messagebox.showerror("Nie mogę wyświetlić wykresu", str(exc))
                return False

        def _compute_scale():
            pil_img = state.get("pil")
            if pil_img is None:
                return 1.0

            ow, oh = pil_img.size
            cw = max(canvas.winfo_width(), 1)
            ch = max(canvas.winfo_height(), 1)

            if state["scale_mode"] == "fit":
                # Mały margines, żeby obraz nie dotykał ramek i nie wymuszał
                # pasków przewijania przez różnicę 1–2 pikseli.
                scale = min((cw - 8) / max(ow, 1), (ch - 8) / max(oh, 1))
                return max(0.05, min(scale, 4.0))

            return max(0.05, min(float(state["manual_scale"]), 8.0))

        def _render_image():
            pil_img = state.get("pil")
            if pil_img is None:
                return

            try:
                scale = _compute_scale()
                ow, oh = pil_img.size
                nw = max(1, int(round(ow * scale)))
                nh = max(1, int(round(oh * scale)))

                if nw == ow and nh == oh:
                    resized = pil_img
                else:
                    resized = pil_img.resize((nw, nh), _get_resample_filter())

                photo = ImageTk.PhotoImage(resized)
                state["tk_photo"] = photo
                win._preview_photo = photo  # utrzymaj referencję; inaczej Tk potrafi zgubić obraz

                canvas.delete("all")
                cw = max(canvas.winfo_width(), 1)
                ch = max(canvas.winfo_height(), 1)
                x = max((cw - nw) // 2, 0)
                y = max((ch - nh) // 2, 0)
                state["image_id"] = canvas.create_image(x, y, image=photo, anchor="nw")
                canvas.configure(scrollregion=(0, 0, max(nw + x, cw), max(nh + y, ch)))

                mode_label = "dopasuj" if state["scale_mode"] == "fit" else "ręcznie"
                status_var.set(
                    f"{state['path']}   |   oryg. {ow} × {oh}px   |   widok {nw} × {nh}px   |   "
                    f"skala {scale * 100:.0f}% ({mode_label})"
                )
            except Exception as exc:
                messagebox.showerror("Nie mogę przeskalować wykresu", str(exc))

        def _reload_image():
            if _load_original():
                # Poczekaj aż canvas dostanie realny rozmiar po otwarciu okna.
                win.after(40, _render_image)

        def _fit_to_window():
            state["scale_mode"] = "fit"
            _render_image()

        def _set_100_percent():
            state["scale_mode"] = "manual"
            state["manual_scale"] = 1.0
            _render_image()

        def _zoom(factor: float):
            current_scale = _compute_scale()
            state["scale_mode"] = "manual"
            state["manual_scale"] = max(0.05, min(current_scale * factor, 8.0))
            _render_image()

        def _schedule_fit_render(_event=None):
            if state["scale_mode"] != "fit":
                return
            after_id = state.get("after_id")
            if after_id is not None:
                try:
                    win.after_cancel(after_id)
                except Exception:
                    pass
            state["after_id"] = win.after(120, _render_image)

        ttk.Button(top, text="Dopasuj", command=_fit_to_window).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="100%", command=_set_100_percent).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="+", width=3, command=lambda: _zoom(1.25)).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="−", width=3, command=lambda: _zoom(1 / 1.25)).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Odśwież", command=_reload_image).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Otwórz zewnętrznie", command=_open_external).pack(side="right", padx=(6, 0))

        _reload_image()

        def _wheel(event):
            # Ctrl + rolka = zoom, sama rolka = przewijanie pionowe.
            ctrl_pressed = bool(getattr(event, "state", 0) & 0x0004)
            delta = getattr(event, "delta", 0)
            if ctrl_pressed and delta:
                _zoom(1.12 if delta > 0 else 1 / 1.12)
            elif delta:
                canvas.yview_scroll(int(-1 * (delta / 120)), "units")
            return "break"

        def _shift_wheel(event):
            delta = getattr(event, "delta", 0)
            if delta:
                canvas.xview_scroll(int(-1 * (delta / 120)), "units")
            return "break"

        def _wheel_linux_up(event):
            ctrl_pressed = bool(getattr(event, "state", 0) & 0x0004)
            if ctrl_pressed:
                _zoom(1.12)
            else:
                canvas.yview_scroll(-3, "units")
            return "break"

        def _wheel_linux_down(event):
            ctrl_pressed = bool(getattr(event, "state", 0) & 0x0004)
            if ctrl_pressed:
                _zoom(1 / 1.12)
            else:
                canvas.yview_scroll(3, "units")
            return "break"

        canvas.bind("<Configure>", _schedule_fit_render)
        canvas.bind("<MouseWheel>", _wheel)
        canvas.bind("<Shift-MouseWheel>", _shift_wheel)
        canvas.bind("<Button-4>", _wheel_linux_up)
        canvas.bind("<Button-5>", _wheel_linux_down)

        win.bind("<Escape>", lambda _event: (win.destroy(), "break")[-1])
        win.bind("<Control-r>", lambda _event: (_reload_image(), "break")[-1])
        win.bind("<Control-0>", lambda _event: (_set_100_percent(), "break")[-1])
        win.bind("<Control-1>", lambda _event: (_fit_to_window(), "break")[-1])
        win.bind("<Control-plus>", lambda _event: (_zoom(1.25), "break")[-1])
        win.bind("<Control-equal>", lambda _event: (_zoom(1.25), "break")[-1])
        win.bind("<Control-minus>", lambda _event: (_zoom(1 / 1.25), "break")[-1])
        canvas.focus_set()

    def gui_start_main_workflow():
        """v19.16: jeden przycisk dla normalnego workflow."""
        if busy_state["busy"]:
            messagebox.showwarning("Przeliczanie trwa", "Najpierw zatrzymaj albo poczekaj na zakończenie obecnego przeliczania.")
            return
        if mdat_path_var.get().strip():
            log("Uruchamiam obliczenia.")
            if not gui_scan_mdat(show_dialog=False):
                messagebox.showerror("START przerwany", "Nie udało się wczytać danych pomiarowych. Szczegóły są w logu.")
                update_workflow_status()
                return
        else:
            log("Uruchamiam obliczenia.")
        update_workflow_status()
        validate_and_run()

    # v19.13: przyciski nie są już pakowane w jednym poziomym rzędzie.
    # Grid z kilkoma kolumnami nie chowa przycisków przy skalowaniu okna.
    for col in range(4):
        bottom_box.columnconfigure(col, weight=1)

    start_button = ttk.Button(bottom_box, text="START: import + optymalizacja", command=gui_start_main_workflow, style="Primary.TButton")
    start_button.grid(row=0, column=0, sticky="ew", padx=4, pady=3)
    action_buttons.append(start_button)

    run_button = ttk.Button(bottom_box, text="Optymalizacja tylko", command=validate_and_run)
    run_button.grid(row=0, column=1, sticky="ew", padx=4, pady=3)
    action_buttons.append(run_button)

    cancel_button = ttk.Button(bottom_box, text="Zatrzymaj", command=request_stop, state="disabled")
    cancel_button.grid(row=0, column=2, sticky="ew", padx=4, pady=3)
    cancel_button_ref["button"] = cancel_button

    ttk.Button(bottom_box, text="Odśwież status", command=update_workflow_status).grid(row=0, column=3, sticky="ew", padx=4, pady=3)

    ttk.Button(bottom_box, text="Folder wyników", command=open_results_folder).grid(row=1, column=0, sticky="ew", padx=4, pady=3)
    ttk.Button(bottom_box, text="START_TUTAJ.md", command=lambda: open_result_file_by_name("START_TUTAJ.md")).grid(row=1, column=1, sticky="ew", padx=4, pady=3)
    ttk.Button(bottom_box, text="SHD final", command=lambda: open_result_file_by_name("MINIDSP_SETTINGS_FINAL.txt")).grid(row=1, column=2, sticky="ew", padx=4, pady=3)
    ttk.Button(bottom_box, text="Wyczyść log", command=clear_log).grid(row=1, column=3, sticky="ew", padx=4, pady=3)

    help_button = ttk.Button(bottom_box, text="Opis trybów", command=show_modes_help)
    help_button.grid(row=2, column=0, sticky="ew", padx=4, pady=3)
    action_buttons.append(help_button)

    for col in range(4):
        plot_box.columnconfigure(col, weight=1)

    def show_main_charts_help():
        messagebox.showinfo("Jak czytać najważniejsze wykresy", MAIN_CHARTS_HELP)

    ttk.Button(
        plot_box,
        text="Mapa PBIS (0–100)",
        command=lambda: preview_result_image("pbis_score_heatmap.png", "Mapa surowego PBIS — wyżej = lepiej"),
    ).grid(row=0, column=0, sticky="ew", padx=4, pady=3)

    ttk.Button(
        plot_box,
        text="Mapa jakości / strata",
        command=lambda: preview_result_image("delay_score_heatmap.png", "Mapa jakości — strata punktów względem rekomendacji"),
    ).grid(row=0, column=1, sticky="ew", padx=4, pady=3)

    ttk.Button(
        plot_box,
        text="Sumy SPL: L, R i L+R",
        command=lambda: preview_result_image("best_sums.png", "Predykcja sum SPL — L, R oraz L+R"),
    ).grid(row=0, column=2, sticky="ew", padx=4, pady=3)

    ttk.Button(
        plot_box,
        text="Zgodność fazy L/R",
        command=lambda: preview_result_image("best_phase_lr.png", "Zgodność fazy L/R — różnica fazy sum kanałów"),
    ).grid(row=0, column=3, sticky="ew", padx=4, pady=3)

    ttk.Label(
        plot_box,
        text="Mapy: współrzędne = relacja sub − main. Wykresy SPL i fazy: predykcja dla pierwszego wiersza tabeli; potwierdź ją pomiarem REW.",
        foreground="#555555",
        wraplength=690,
        justify="left",
    ).grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 4))

    ttk.Button(plot_box, text="Opis wykresów", command=show_main_charts_help).grid(row=1, column=3, sticky="ew", padx=4, pady=(0, 4))

    for col in range(3):
        advanced_box.columnconfigure(col, weight=1)

    pred_button = ttk.Button(advanced_box, text="Predykcja vs pomiar", command=gui_compare_prediction_real)
    pred_button.grid(row=0, column=0, sticky="ew", padx=4, pady=3)
    action_buttons.append(pred_button)

    dirac_button = ttk.Button(advanced_box, text="Waliduj Dirac ON", command=gui_validate_dirac)
    dirac_button.grid(row=0, column=1, sticky="ew", padx=4, pady=3)
    action_buttons.append(dirac_button)

    curtain_button = ttk.Button(advanced_box, text="Analizuj kurtynę Dirac", command=gui_analyze_dirac_curtain)
    curtain_button.grid(row=0, column=2, sticky="ew", padx=4, pady=3)
    action_buttons.append(curtain_button)

    curtain_compare_button = ttk.Button(advanced_box, text="Porównaj kurtyny", command=gui_compare_dirac_curtains)
    curtain_compare_button.grid(row=1, column=0, sticky="ew", padx=4, pady=3)
    action_buttons.append(curtain_compare_button)

    compare_button = ttk.Button(advanced_box, text="Porównaj presety", command=gui_compare_presets)
    compare_button.grid(row=1, column=1, sticky="ew", padx=4, pady=3)
    action_buttons.append(compare_button)

    apply_crossover_preset(force=True)
    update_workflow_status()

    def _set_initial_pane_sizes():
        try:
            # v19.13: lewa część to ustawienia, prawa to status + miniDSP table + log.
            # Dajemy prawemu panelowi minimum ~520 px, żeby log był widoczny od startu.
            w = root.winfo_width() or 1320
            left_width = max(540, min(720, w - 600))
            main_pane.sashpos(0, left_width)
            result_text.focus_set()
        except Exception:
            pass

    def _on_close_gui():
        try:
            save_current_gui_preferences()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close_gui)
    root.after(250, _set_initial_pane_sizes)
    root.mainloop()

def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        run_gui()
        return 0
    args = parser.parse_args(argv)
    if args.gui:
        run_gui()
        return 0
    if getattr(args, "self_test", False):
        return run_self_test(args)

    if (
        getattr(args, "compare_dirac_curtains_folder", None)
        or getattr(args, "dirac_curtain_l", None)
        or getattr(args, "dirac_curtain_r", None)
        or getattr(args, "dirac_curtain_lr", None)
    ):
        return run_cli_dirac_curtain(args)

    # v19.13: sam plik .mdat jest pełnym źródłem danych do optymalizacji.
    if getattr(args, "mdat", None):
        return run_cli(args)

    required = [args.l_main, args.l_sub, args.r_main, args.r_sub]
    if any(x is None for x in required):
        parser.error("W trybie CLI wymagane są --l-main --l-sub --r-main --r-sub. Bez argumentów uruchamia się GUI.")
    return run_cli(args)
