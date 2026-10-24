# -*- coding: utf-8 -*-
"""Raporty, wykresy, organizacja wyników oraz analizy pomocnicze."""
from __future__ import annotations

from rew_common import *
from rew_mdat import *
from rew_scoring import *


def write_performance_diagnostics(
    out_dir: str | Path,
    cfg: OptimizerConfig,
    timings: Dict[str, float],
    validation_confidence: str = "",
    smart_status: str = "",
    final_candidates_count: int = 0,
) -> Tuple[Path, List[str]]:
    """v19.44: zapisuje prosty raport wydajności bez dodawania niczego do GUI."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    info = optimizer_workload_info(cfg)
    md_path = out / "PERFORMANCE_DIAGNOSTICS.md"
    txt_path = out / "PERFORMANCE_DIAGNOSTICS.txt"
    csv_path = out / "PERFORMANCE_DIAGNOSTICS.csv"

    def fmt_time(v: float) -> str:
        try:
            v = float(v)
        except Exception:
            return "-"
        if v < 1.0:
            return f"{v*1000.0:.0f} ms"
        if v < 60.0:
            return f"{v:.2f} s"
        return f"{v/60.0:.2f} min"

    notes: List[str] = []
    if info.get("coarse_safety_adjusted"):
        notes.append(
            f"Bezpiecznik wydajności podniósł krok zgrubny FAST z "
            f"{float(info['coarse_step_requested_ms']):g} do {float(info['coarse_step_effective_ms']):g} ms."
        )
    if str(info.get("load_label", "")).lower() in {"ciężki", "bardzo ciężki"}:
        notes.append("Skan ma wysoką ciężkość obliczeniową — przy kolejnych testach rozważ większy krok albo węższy zakres delayu.")
    if str(validation_confidence or "").upper() == "LOW":
        notes.append("Walidator FAST zwrócił LOW — wynik powinien być sprawdzony dokładniejszym pomiarem albo lokalnym skanem.")
    if str(smart_status or "").upper() in {"IMPROVED", "REFINED"}:
        notes.append("SMART RESCAN zmienił lub poprawił wynik, więc raporty końcowe bazują na wersji po dogrywce.")
    if not notes:
        notes.append("Brak krytycznych ostrzeżeń wydajnościowych.")

    rows = [
        ("app_section", "PERFORMANCE_DIAGNOSTICS"),
        ("scan_mode", info.get("mode")),
        ("load_label", info.get("load_label")),
        ("fine_step_ms", info.get("fine_step_ms")),
        ("fine_delay_points", info.get("fine_delay_points")),
        ("exhaustive_total", info.get("exhaustive_total")),
        ("coarse_step_requested_ms", info.get("coarse_step_requested_ms")),
        ("coarse_step_effective_ms", info.get("coarse_step_effective_ms")),
        ("coarse_safety_adjusted", info.get("coarse_safety_adjusted")),
        ("coarse_total", info.get("coarse_total")),
        ("refine_estimate", info.get("refine_estimate")),
        ("estimated_total", info.get("estimated_total")),
        ("validation_confidence", validation_confidence),
        ("smart_rescan_status", smart_status),
        ("final_candidates_count", int(final_candidates_count or 0)),
    ]
    for key in sorted(timings):
        rows.append((f"time_{key}_sec", f"{float(timings[key]):.6f}"))

    lines: List[str] = []
    lines.append("# PERFORMANCE DIAGNOSTICS — v19.44\n")
    lines.append("\n## Tryb i ciężkość skanu\n\n")
    lines.append(f"- Tryb skanu: **{info.get('mode')}**\n")
    lines.append(f"- Ciężkość: **{info.get('load_label')}**\n")
    lines.append(f"- Finalny krok dokładny: **{float(info.get('fine_step_ms', 0.0)):.4g} ms**\n")
    lines.append(f"- Pełny brute-force: **{int(info.get('exhaustive_total', 0)):,} kombinacji**\n".replace(",", " "))
    if info.get("mode") == "FAST_2_STAGE":
        lines.append(f"- FAST coarse: **{float(info.get('coarse_step_effective_ms', 0.0)):.4g} ms**, około **{int(info.get('coarse_total', 0)):,} kombinacji**\n".replace(",", " "))
        lines.append(f"- Szacowane dogęszczenie: około **{int(info.get('refine_estimate', 0)):,} kombinacji**\n".replace(",", " "))
        lines.append(f"- Szacowany łączny nakład FAST: około **{int(info.get('estimated_total', 0)):,} kombinacji**\n".replace(",", " "))
    lines.append("\n## Czasy etapów\n\n")
    if timings:
        for key in sorted(timings):
            lines.append(f"- {key}: **{fmt_time(float(timings[key]))}**\n")
    else:
        lines.append("- Brak pomiarów czasu.\n")
    lines.append("\n## Walidacja i dogrywka\n\n")
    lines.append(f"- FAST validation: **{str(validation_confidence or '-').upper()}**\n")
    lines.append(f"- SMART RESCAN: **{str(smart_status or '-').upper()}**\n")
    lines.append(f"- Liczba kandydatów końcowych: **{int(final_candidates_count or 0)}**\n")
    lines.append("\n## Uwagi\n\n")
    for note in notes:
        lines.append(f"- {note}\n")

    text = "".join(lines)
    md_path.write_text(text, encoding="utf-8")
    txt_path.write_text(
        "PODSUMOWANIE PRACY\n"
        "==================\n\n"
        f"Sposób liczenia: {'szybki' if str(info.get('mode', '')).upper() == 'FAST_2_STAGE' else 'dokładny'}\n"
        f"Kontrola wyniku: {simple_validation_label(validation_confidence)}\n"
        f"Dodatkowe sprawdzenie: {simple_rescan_label(smart_status)}\n",
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for k, v in rows:
            writer.writerow([k, v])
    return md_path, notes

def save_candidates_csv(
    candidates: List[Candidate],
    path: str | Path,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    crossover_label: str = "Custom",
    filter_type: str = "BW",
    slope_db_oct: int = 48,
    cfg: Optional[OptimizerConfig] = None,
) -> None:
    path = Path(path)
    visible_candidates = [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"]
    rows = [candidate_to_dict(c, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg) for c in visible_candidates]
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _validation_delay_grid(center_ms: float, radius_ms: float, step_ms: float, min_ms: float, max_ms: float) -> np.ndarray:
    """Lokalna siatka delayów dla walidatora FAST 2-STAGE."""
    step = max(float(step_ms), 0.001)
    radius = max(float(radius_ms), step)
    lo = max(float(min_ms), float(center_ms) - radius)
    hi = min(float(max_ms), float(center_ms) + radius)
    vals = np.arange(lo, hi + step / 2.0, step)
    if vals.size == 0:
        vals = np.array([float(center_ms)], dtype=float)
    vals = vals[(vals >= lo - 1e-9) & (vals <= hi + 1e-9)]
    vals = np.round(vals, 6)
    if len(vals) == 0 or abs(float(vals[-1]) - hi) > 1e-6:
        vals = np.append(vals, round(hi, 6))
    return np.unique(np.round(vals, 6))


def _candidate_from_validation_metrics(ld: float, linv: bool, rd: float, rinv: bool, score: float, metrics: Dict[str, float]) -> Candidate:
    """Kopia minimalnego konstruktora kandydata dla lokalnego EXACT-check."""
    return Candidate(
        l_delay_ms=float(ld),
        l_polarity="INV" if linv else "OFF",
        r_delay_ms=float(rd),
        r_polarity="INV" if rinv else "OFF",
        score=float(score),
        l_construct_db=metrics["l_construct_db"],
        r_construct_db=metrics["r_construct_db"],
        lr_construct_db=metrics["lr_construct_db"],
        lr_std_db=metrics["lr_std_db"],
        lr_dip_db=metrics["lr_dip_db"],
        bulge_db=metrics["bulge_db"],
        bulge_peak_db=metrics["bulge_peak_db"],
        lr_mean_db=metrics["lr_mean_db"],
        pbis_score=metrics.get("pbis_score", 0.0),
        pbis_score_raw=metrics.get("pbis_score_raw", metrics.get("pbis_score", 0.0)),
        pbis_rank_score=metrics.get("pbis_rank_score", metrics.get("pbis_score", 0.0)),
        pbis_label=metrics.get("pbis_label", ""),
        pbis_hole_risk=metrics.get("pbis_hole_risk", ""),
        pbis_bulge_risk=metrics.get("pbis_bulge_risk", ""),
        pbis_phase_risk=metrics.get("pbis_phase_risk", ""),
        pbis_symmetry_risk=metrics.get("pbis_symmetry_risk", ""),
        pbis_risk_summary=metrics.get("pbis_risk_summary", ""),
    )


def _candidate_short(c: Candidate, l_base_delay_ms: float, r_base_delay_ms: float, cfg: Optional[OptimizerConfig] = None) -> str:
    mapped = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
    return (
        f"main {float(mapped['l_main_ms']):.2f}/{float(mapped['r_main_ms']):.2f} ms | "
        f"sub {float(mapped['l_sub_ms']):.2f}/{float(mapped['r_sub_ms']):.2f} ms {c.l_polarity}/{c.r_polarity} | "
        f"rel. {float(mapped['l_sub_minus_main_ms']):+.2f}/{float(mapped['r_sub_minus_main_ms']):+.2f} ms | "
        f"PBIS {float(getattr(c, 'pbis_score', 0.0)):.1f}/100 — {getattr(c, 'pbis_label', '')}"
    )


def write_fast_result_validation(
    candidates: List[Candidate],
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    out_dir: str | Path,
    cfg: OptimizerConfig,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    crossover_label: str = "Custom",
    filter_type: str = "BW",
    slope_db_oct: int = 48,
) -> Tuple[str, Path, List[str]]:
    """v19.28: walidator wyniku FAST 2-STAGE.

    Zapisuje FAST_2_STAGE_VALIDATION.md/txt/csv. Dla trybu FAST robi krótki lokalny
    EXACT-check wokół zwycięzcy na osobnych siatkach L/R, więc nie używa ograniczenia
    wspólnego zakresu delayu z głównego optimizera.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "FAST_2_STAGE_VALIDATION.md"
    txt_path = out / "FAST_2_STAGE_VALIDATION.txt"
    csv_path = out / "FAST_2_STAGE_VALIDATION.csv"

    notes: List[str] = []
    rows: List[Dict[str, object]] = []

    def _write_files(confidence: str, lines: List[str]) -> Tuple[str, Path, List[str]]:
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # TXT bez tabel markdown — prościej do szybkiego podejrzenia.
        txt_lines = []
        for line in lines:
            clean = line.replace("**", "").replace("`", "")
            if clean.startswith("#"):
                clean = clean.replace("#", "").strip().upper()
            txt_lines.append(clean)
        txt_path.write_text(
            "KONTROLA WYNIKU OFF/OFF\n"
            "=====================\n\n"
            f"Ocena: {simple_validation_label(confidence)}\n\n"
            "Następny krok: wpisz wskazany wariant OFF/OFF do SHD i potwierdź go pomiarem REW.\n",
            encoding="utf-8",
        )
        if rows:
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
        else:
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["metric", "value"])
                writer.writerow(["confidence", confidence])
        return confidence, md_path, notes

    if not candidates:
        notes.append("Brak kandydatów — walidacja niemożliwa.")
        return _write_files("LOW", ["# FAST 2-STAGE VALIDATION", "", "Confidence: **LOW**", "", "Brak kandydatów."])

    enabled = bool(getattr(cfg, "fast_validation_enabled", True))
    fine_step = max(float(getattr(cfg, "delay_step_ms", 0.1)), 0.001)
    full_points = int(round(abs(float(cfg.delay_max_ms) - float(cfg.delay_min_ms)) / fine_step)) + 1
    exhaustive_total = full_points * full_points
    fast_active = bool(getattr(cfg, "fast_two_stage", True)) and exhaustive_total > int(getattr(cfg, "fast_exact_threshold", 30000))

    off_candidates = sorted(
        [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"],
        key=lambda c: float(c.score), reverse=True,
    )
    if not off_candidates:
        notes.append("Brak wariantu OFF/OFF — walidacja niemożliwa.")
        return _write_files("LOW", ["# FAST 2-STAGE VALIDATION", "", "Confidence: **LOW**", "", "Brak wariantu OFF/OFF."])
    best = off_candidates[0]
    top_margin = float(best.score) - float(off_candidates[1].score) if len(off_candidates) > 1 else float("nan")
    close_score = max(float(getattr(cfg, "fast_validation_close_score", 1.0)), 0.01)
    close_auto = [c for c in off_candidates if (float(best.score) - float(c.score)) <= close_score + 1e-9]
    close_families = ["OFF/OFF"] if close_auto else []

    if not enabled:
        notes.append("Walidator FAST został wyłączony w konfiguracji.")
        lines = [
            "# FAST 2-STAGE VALIDATION",
            "",
            "Confidence: **DISABLED**",
            "",
            f"Best z głównej optymalizacji: `{_candidate_short(best, l_base_delay_ms, r_base_delay_ms, cfg)}`",
            "",
            "Walidator był wyłączony opcją konfiguracji.",
        ]
        rows.append({"type": "summary", "metric": "enabled", "value": "false"})
        return _write_files("DISABLED", lines)

    if not fast_active:
        notes.append("Główna optymalizacja działała w trybie EXACT; FAST-check nie jest potrzebny.")
        lines = [
            "# FAST 2-STAGE VALIDATION",
            "",
            "Confidence: **HIGH**",
            "",
            "Główna optymalizacja nie użyła przybliżenia FAST 2-STAGE, tylko trybu EXACT.",
            f"Best: `{_candidate_short(best, l_base_delay_ms, r_base_delay_ms, cfg)}`",
            f"Liczba podobnych kandydatów w progu {close_score:.2f}: **{len(close_auto)}**; rodziny: {', '.join(close_families) if close_families else '-'}.",
        ]
        rows.append({"type": "summary", "metric": "fast_active", "value": "false"})
        rows.append({"type": "summary", "metric": "confidence", "value": "HIGH"})
        return _write_files("HIGH", lines)

    try:
        radius = max(float(getattr(cfg, "fast_validation_radius_ms", 0.6)), fine_step * 2.0)
        l_grid = _validation_delay_grid(best.l_delay_ms, radius, fine_step, cfg.delay_min_ms, cfg.delay_max_ms)
        r_grid = _validation_delay_grid(best.r_delay_ms, radius, fine_step, cfg.delay_min_ms, cfg.delay_max_ms)
        l_variants: Dict[Tuple[float, bool], np.ndarray] = {}
        r_variants: Dict[Tuple[float, bool], np.ndarray] = {}
        for ld in l_grid:
            l_variants[(float(ld), False)] = apply_delay_and_polarity(responses["L_sub"], freq, float(ld), False)
        for rd in r_grid:
            r_variants[(float(rd), False)] = apply_delay_and_polarity(responses["R_sub"], freq, float(rd), False)

        local_candidates: List[Candidate] = []
        for ld in l_grid:
            lsub_s = l_variants[(float(ld), False)]
            for rd in r_grid:
                rsub_s = r_variants[(float(rd), False)]
                score, metrics = score_candidate(freq, responses["L_main"], lsub_s, responses["R_main"], rsub_s, cfg)
                local_candidates.append(_candidate_from_validation_metrics(float(ld), False, float(rd), False, score, metrics))

        annotate_pbis_plateau(local_candidates, cfg)
        annotate_sound_quality_safe(local_candidates, cfg)
        local_candidates.sort(key=lambda c: float(c.score), reverse=True)
        local_best = local_candidates[0]

        margin_threshold = max(float(getattr(cfg, "fast_validation_margin", 0.15)), 0.01)
        local_gain = float(local_best.score) - float(best.score)
        same = same_candidate(local_best, best)
        local_close = [c for c in local_candidates if (float(local_best.score) - float(c.score)) <= close_score + 1e-9]
        local_close_families = ["OFF/OFF"] if local_close else []

        edge_l = abs(float(local_best.l_delay_ms) - float(l_grid[0])) <= fine_step / 2.0 or abs(float(local_best.l_delay_ms) - float(l_grid[-1])) <= fine_step / 2.0
        edge_r = abs(float(local_best.r_delay_ms) - float(r_grid[0])) <= fine_step / 2.0 or abs(float(local_best.r_delay_ms) - float(r_grid[-1])) <= fine_step / 2.0
        global_edge = (
            abs(float(best.l_delay_ms) - float(cfg.delay_min_ms)) <= fine_step / 2.0
            or abs(float(best.l_delay_ms) - float(cfg.delay_max_ms)) <= fine_step / 2.0
            or abs(float(best.r_delay_ms) - float(cfg.delay_min_ms)) <= fine_step / 2.0
            or abs(float(best.r_delay_ms) - float(cfg.delay_max_ms)) <= fine_step / 2.0
        )

        confidence_score = 2  # 0 LOW, 1 MEDIUM, 2 HIGH
        if (not same) and local_gain > margin_threshold:
            confidence_score = 0
            notes.append(f"Lokalny EXACT-check znalazł lepszy punkt o {local_gain:.3f} score — zwiększ seed/radius albo uruchom EXACT w tej okolicy.")
        elif (not same) and local_gain > 0.02:
            confidence_score = min(confidence_score, 1)
            notes.append(f"Lokalny EXACT-check wskazał minimalnie inny punkt (+{local_gain:.3f}). Warto go zmierzyć A/B.")
        else:
            notes.append("Lokalny EXACT-check potwierdza zwycięzcę FAST albo nie znajduje istotnie lepszego punktu.")

        if edge_l or edge_r:
            confidence_score = min(confidence_score, 1)
            notes.append("Najlepszy punkt lokalnego checku leży na krawędzi okna walidacyjnego — rozważ większy fast-refine-radius / fast-validation-radius.")
        if global_edge:
            confidence_score = min(confidence_score, 1)
            notes.append("Zwycięzca leży blisko globalnej granicy zakresu delayu — sprawdź, czy zakres skanu nie jest za wąski.")
        if len(off_candidates) > 1 and np.isfinite(top_margin):
            if top_margin < 0.10:
                confidence_score = min(confidence_score, 1)
                notes.append(f"Bardzo mała przewaga #1 nad #2 w głównej liście: {top_margin:.3f}; wybierz 2–3 warianty do pomiaru.")
            elif top_margin < 0.30:
                notes.append(f"Niewielka przewaga #1 nad #2: {top_margin:.3f}; warto sprawdzić A/B.")
        if len(close_auto) >= 8:
            notes.append(f"W progu {close_score:.2f} jest dużo podobnych ustawień OFF/OFF — pomiar realny rozstrzygnie lepiej niż ranking.")
        if "Safe D" in str(getattr(best, "pbis_safe_label", "")) or "Safe E" in str(getattr(best, "pbis_safe_label", "")):
            confidence_score = min(confidence_score, 1)
            notes.append(f"PBIS Safe zwycięzcy ma etykietę: {getattr(best, 'pbis_safe_label', '')}; wynik wymaga ostrożności.")

        confidence = ["LOW", "MEDIUM", "HIGH"][confidence_score]
        if confidence == "HIGH":
            notes.append("Wniosek: wynik FAST 2-STAGE wygląda stabilnie. Nadal warto zrobić realny pomiar kontrolny w REW.")
        elif confidence == "MEDIUM":
            notes.append("Wniosek: wynik jest użyteczny, ale zalecany jest pomiar A/B 2–3 najbliższych wariantów.")
        else:
            notes.append("Wniosek: nie traktuj wyniku FAST jako finalnego; zawęź skan albo uruchom dokładny check większego obszaru.")

        rows.extend([
            {"type": "summary", "metric": "confidence", "value": confidence},
            {"type": "summary", "metric": "local_exact_combinations", "value": len(local_candidates)},
            {"type": "summary", "metric": "validation_radius_ms", "value": f"{radius:.3f}"},
            {"type": "summary", "metric": "fine_step_ms", "value": f"{fine_step:.3f}"},
            {"type": "summary", "metric": "local_gain_score", "value": f"{local_gain:.6f}"},
            {"type": "summary", "metric": "local_best_same_as_fast", "value": str(bool(same))},
            {"type": "summary", "metric": "local_best_on_window_edge", "value": str(bool(edge_l or edge_r))},
            {"type": "summary", "metric": "global_best_on_scan_edge", "value": str(bool(global_edge))},
            {"type": "summary", "metric": "auto_close_candidates", "value": len(close_auto)},
            {"type": "summary", "metric": "local_close_candidates", "value": len(local_close)},
        ])
        for rank, c in enumerate(local_candidates[:20], start=1):
            rows.append({
                "type": "local_top20",
                "metric": f"rank_{rank}",
                "value": _candidate_short(c, l_base_delay_ms, r_base_delay_ms, cfg),
            })

        lines = [
            "# FAST 2-STAGE VALIDATION",
            "",
            f"Confidence: **{confidence}**",
            "",
            "## Co sprawdzono",
            "",
            f"- Tryb głównej optymalizacji: **FAST 2-STAGE OFF/OFF**.",
            f"- Lokalny dokładny check: L ±{radius:.2f} ms i R ±{radius:.2f} ms wokół rekomendacji OFF/OFF, krok {fine_step:.3f} ms.",
            f"- Liczba lokalnie sprawdzonych kombinacji: **{len(local_candidates)}**.",
            "",
            "## Wynik główny vs lokalny EXACT-check",
            "",
            f"- FAST best: `{_candidate_short(best, l_base_delay_ms, r_base_delay_ms, cfg)}`",
            f"- Local exact best: `{_candidate_short(local_best, l_base_delay_ms, r_base_delay_ms, cfg)}`",
            f"- Różnica local-best minus FAST-best: **{local_gain:+.3f} score**.",
            f"- Ten sam punkt: **{'TAK' if same else 'NIE'}**.",
            "",
            "## Krawędzie i podobne warianty",
            "",
            f"- Local best na krawędzi okna: **{'TAK' if (edge_l or edge_r) else 'NIE'}**.",
            f"- FAST best przy granicy globalnego zakresu delayu: **{'TAK' if global_edge else 'NIE'}**.",
            f"- Podobne warianty w głównej liście ≤{close_score:.2f} score: **{len(close_auto)}**; rodziny: {', '.join(close_families) if close_families else '-'}.",
            f"- Podobne warianty w lokalnym EXACT-check ≤{close_score:.2f} score: **{len(local_close)}**; rodziny: {', '.join(local_close_families) if local_close_families else '-'}.",
            "",
            "## Wnioski",
            "",
        ]
        for n in notes:
            lines.append(f"- {n}")
        lines.extend([
            "",
            "## Top 10 lokalnego EXACT-check",
            "",
            "| # | Wariant |",
            "|---:|---|",
        ])
        for rank, c in enumerate(local_candidates[:10], start=1):
            lines.append(f"| {rank} | `{_candidate_short(c, l_base_delay_ms, r_base_delay_ms, cfg)}` |")
        lines.extend([
            "",
            "Uwaga: walidator nie zastępuje realnego pomiaru REW po wpisaniu ustawień do miniDSP. Jego zadanie to wykryć, czy szybki FAST 2-STAGE nie ominął lepszego lokalnego punktu albo czy wynik nie leży na podejrzanej krawędzi.",
        ])
        return _write_files(confidence, lines)

    except Exception as exc:
        notes.append(f"Błąd walidatora FAST: {exc}")
        lines = [
            "# FAST 2-STAGE VALIDATION",
            "",
            "Confidence: **LOW**",
            "",
            "Walidator nie mógł wykonać lokalnego EXACT-check.",
            f"Błąd: `{exc}`",
        ]
        rows.append({"type": "error", "metric": "exception", "value": str(exc)})
        return _write_files("LOW", lines)


def _smart_rescan_triggered(validation_confidence: str, validation_notes: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Decyzja, czy SMART RESCAN ma się uruchomić bez dodatkowych kontrolek GUI."""
    conf = str(validation_confidence or "").strip().upper()
    notes = "\n".join(validation_notes or []).lower()
    if conf == "LOW":
        return True, "confidence LOW"
    risk_markers = [
        "znalazł lepszy punkt",
        "wskazał minimalnie inny punkt",
        "krawędzi okna walidacyjnego",
        "globalnej granicy",
        "nie traktuj wyniku fast jako finalnego",
        "bardzo mała przewaga",
        "niewielka przewaga",
        "dużo podobnych wariantów",
    ]
    for marker in risk_markers:
        if marker in notes:
            return True, marker
    return False, "no trigger"


def write_smart_rescan_if_needed(
    candidates: List[Candidate],
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    out_dir: str | Path,
    cfg: OptimizerConfig,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    crossover_label: str = "Custom",
    filter_type: str = "BW",
    slope_db_oct: int = 48,
    validation_confidence: str = "",
    validation_notes: Optional[List[str]] = None,
) -> Tuple[List[Candidate], str, Path, List[str]]:
    """v19.29 SMART RESCAN.

    Bez dokładania elementów do GUI: po walidatorze FAST funkcja automatycznie wykonuje większy,
    lokalny EXACT-rescan wokół kilku najlepszych kandydatów, ale tylko gdy walidator wykrył LOW
    albo konkretny sygnał ryzyka. Zwraca nową listę kandydatów, żeby CLI/GUI mogły odświeżyć
    ranking i rekomendacje miniDSP, jeśli dogrywka znalazła lepszy punkt.
    """
    out = Path(out_dir)
    md_path = out / "SMART_RESCAN.md"
    txt_path = out / "SMART_RESCAN.txt"
    csv_path = out / "SMART_RESCAN.csv"
    notes: List[str] = []
    rows: List[Dict[str, object]] = []

    def _write(status: str, lines: List[str], updated: Optional[List[Candidate]] = None) -> Tuple[List[Candidate], str, Path, List[str]]:
        out.mkdir(parents=True, exist_ok=True)
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        txt_path.write_text(
            "DODATKOWE SPRAWDZENIE OFF/OFF\n"
            "===========================\n\n"
            f"Wynik: {simple_rescan_label(status)}\n\n"
            "Użyj wskazanego wariantu OFF/OFF i potwierdź go pomiarem REW.\n",
            encoding="utf-8",
        )
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            fieldnames = ["type", "metric", "value"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({"type": "summary", "metric": "status", "value": status})
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        return updated if updated is not None else candidates, status, md_path, notes

    if not candidates:
        notes.append("Brak kandydatów — SMART RESCAN pominięty.")
        return _write("SKIPPED", ["# SMART RESCAN", "", "Status: **SKIPPED**", "", "Brak kandydatów."])

    if not bool(getattr(cfg, "fast_smart_rescan_enabled", True)):
        notes.append("SMART RESCAN wyłączony w konfiguracji.")
        return _write("DISABLED", ["# SMART RESCAN", "", "Status: **DISABLED**", "", "Mechanizm został wyłączony w konfiguracji."])

    fine_step = max(float(getattr(cfg, "delay_step_ms", 0.1)), 0.001)
    exhaustive_delays = _validation_delay_grid(
        (float(getattr(cfg, "delay_min_ms", 0.0)) + float(getattr(cfg, "delay_max_ms", 30.0))) / 2.0,
        abs(float(getattr(cfg, "delay_max_ms", 30.0)) - float(getattr(cfg, "delay_min_ms", 0.0))) / 2.0,
        fine_step,
        float(getattr(cfg, "delay_min_ms", 0.0)),
        float(getattr(cfg, "delay_max_ms", 30.0)),
    )
    exhaustive_total = len(exhaustive_delays) * len(exhaustive_delays)
    fast_active = bool(getattr(cfg, "fast_two_stage", True)) and exhaustive_total > int(getattr(cfg, "fast_exact_threshold", 30000))
    if not fast_active:
        notes.append("Główna optymalizacja była trybem EXACT — SMART RESCAN nie jest potrzebny.")
        return _write("SKIPPED", ["# SMART RESCAN", "", "Status: **SKIPPED**", "", "Główna optymalizacja była trybem EXACT."])

    should_run, trigger = _smart_rescan_triggered(validation_confidence, validation_notes)
    if not should_run:
        notes.append("Walidator nie zgłosił ryzyka wymagającego SMART RESCAN.")
        return _write("SKIPPED", ["# SMART RESCAN", "", "Status: **SKIPPED**", "", "Walidator nie zgłosił LOW ani ryzyka krawędzi/lepszego punktu."])

    try:
        before = sorted(candidates, key=lambda c: float(c.score), reverse=True)
        before_off = sorted(
            [c for c in before if c.l_polarity == "OFF" and c.r_polarity == "OFF"],
            key=lambda c: float(c.score), reverse=True,
        )
        if not before_off:
            notes.append("Brak wariantu OFF/OFF — dodatkowe sprawdzenie pominięte.")
            return _write("SKIPPED", ["# SMART RESCAN", "", "Status: **SKIPPED**", "", "Brak wariantu OFF/OFF."])
        before_best = before_off[0]
        seed_n = max(1, int(getattr(cfg, "fast_smart_rescan_seed_n", 8)))
        radius = max(float(getattr(cfg, "fast_smart_rescan_radius_ms", 1.5)), float(getattr(cfg, "fast_validation_radius_ms", 0.6)), fine_step * 3.0)
        max_combos = max(1000, int(getattr(cfg, "fast_smart_rescan_max_combinations", 80000)))

        # v19.44: użytkowo dogrywamy wyłącznie OFF/OFF. Inne polaryzacje mogą być
        # liczone w głównym silniku diagnostycznie, ale nie sterują rekomendacją do SHD.
        seeds: List[Candidate] = []
        for c in before_off[:seed_n]:
            if not any(same_candidate(c, s) for s in seeds):
                seeds.append(c)

        def build_combo_keys(local_radius: float, local_seeds: List[Candidate]) -> set:
            keys = set()
            for seed in local_seeds:
                l_grid = _validation_delay_grid(seed.l_delay_ms, local_radius, fine_step, cfg.delay_min_ms, cfg.delay_max_ms)
                r_grid = _validation_delay_grid(seed.r_delay_ms, local_radius, fine_step, cfg.delay_min_ms, cfg.delay_max_ms)
                for ld in l_grid:
                    for rd in r_grid:
                        keys.add((float(ld), False, float(rd), False))
            return keys

        combo_keys = build_combo_keys(radius, seeds)
        effective_radius = radius
        effective_seeds = list(seeds)
        capped = False
        if len(combo_keys) > max_combos:
            # Pierwsze ograniczenie: mniej centrów, promień zostaje.
            effective_seeds = seeds[:max(2, min(4, len(seeds)))]
            combo_keys = build_combo_keys(effective_radius, effective_seeds)
        if len(combo_keys) > max_combos:
            # Drugie ograniczenie: mniejszy promień, żeby nie zamrozić GUI.
            effective_radius = max(float(getattr(cfg, "fast_validation_radius_ms", 0.6)), fine_step * 4.0, radius * 0.65)
            combo_keys = build_combo_keys(effective_radius, effective_seeds)
        if len(combo_keys) > max_combos:
            capped = True
            # Ostateczny bezpiecznik: deterministycznie zachowujemy kombinacje najbliższe zwycięzcy.
            def dist_to_best(k):
                ld, _linv, rd, _rinv = k
                return abs(float(ld) - float(before_best.l_delay_ms)) + abs(float(rd) - float(before_best.r_delay_ms))
            combo_keys = set(sorted(combo_keys, key=dist_to_best)[:max_combos])

        rows.extend([
            {"type": "summary", "metric": "trigger", "value": trigger},
            {"type": "summary", "metric": "requested_radius_ms", "value": f"{radius:.3f}"},
            {"type": "summary", "metric": "effective_radius_ms", "value": f"{effective_radius:.3f}"},
            {"type": "summary", "metric": "seed_count", "value": len(effective_seeds)},
            {"type": "summary", "metric": "combination_count", "value": len(combo_keys)},
            {"type": "summary", "metric": "capped", "value": str(bool(capped))},
        ])

        l_need = sorted({(ld, linv) for (ld, linv, _rd, _rinv) in combo_keys})
        r_need = sorted({(rd, rinv) for (_ld, _linv, rd, rinv) in combo_keys})
        l_lookup: Dict[Tuple[float, bool], np.ndarray] = {}
        r_lookup: Dict[Tuple[float, bool], np.ndarray] = {}
        for ld, linv in l_need:
            l_lookup[(ld, linv)] = apply_delay_and_polarity(responses["L_sub"], freq, float(ld), bool(linv))
        for rd, rinv in r_need:
            r_lookup[(rd, rinv)] = apply_delay_and_polarity(responses["R_sub"], freq, float(rd), bool(rinv))

        smart_candidates: List[Candidate] = []
        for ld, linv, rd, rinv in sorted(combo_keys):
            score, metrics = score_candidate(freq, responses["L_main"], l_lookup[(ld, linv)], responses["R_main"], r_lookup[(rd, rinv)], cfg)
            smart_candidates.append(_candidate_from_validation_metrics(float(ld), bool(linv), float(rd), bool(rinv), score, metrics))

        annotate_pbis_plateau(smart_candidates, cfg)
        annotate_sound_quality_safe(smart_candidates, cfg)
        smart_candidates.sort(key=lambda c: float(c.score), reverse=True)
        smart_best = smart_candidates[0]

        # Łączymy stare i nowe punkty, a potem ponownie opisujemy plateau/PBIS Safe dla wspólnej puli.
        merged: Dict[Tuple[float, str, float, str], Candidate] = {}
        for c in list(candidates) + smart_candidates:
            key = (round(float(c.l_delay_ms), 6), str(c.l_polarity), round(float(c.r_delay_ms), 6), str(c.r_polarity))
            old = merged.get(key)
            if old is None or float(c.score) > float(old.score):
                merged[key] = c
        updated = list(merged.values())
        annotate_pbis_plateau(updated, cfg)
        annotate_sound_quality_safe(updated, cfg)
        updated.sort(key=lambda c: float(c.score), reverse=True)
        after_best = best_both_off(updated)
        if after_best is None:
            after_best = before_best

        gain = float(after_best.score) - float(before_best.score)
        improved = (not same_candidate(after_best, before_best)) and gain > max(float(getattr(cfg, "fast_validation_margin", 0.15)), 0.01)
        minimal_change = (not same_candidate(after_best, before_best)) and gain > 0.02
        status = "IMPROVED" if improved else ("REFINED" if minimal_change else "CONFIRMED")

        notes.append(f"SMART RESCAN uruchomiony automatycznie: {trigger}.")
        notes.append(f"Przeliczono {len(smart_candidates)} lokalnych kombinacji wokół {len(effective_seeds)} centrów.")
        if capped:
            notes.append("Zadziałał limit kombinacji — zakres dogrywki został przycięty, żeby nie zamrozić aplikacji.")
        if status == "IMPROVED":
            notes.append(f"Dogrywka znalazła lepszy punkt: +{gain:.3f} score. Ranking i rekomendacje zostały odświeżone.")
        elif status == "REFINED":
            notes.append(f"Dogrywka wskazała minimalnie inny punkt: +{gain:.3f} score. Warto sprawdzić A/B.")
        else:
            notes.append("Dogrywka potwierdziła dotychczasowy zwycięski obszar.")

        rows.extend([
            {"type": "summary", "metric": "before_best", "value": _candidate_short(before_best, l_base_delay_ms, r_base_delay_ms, cfg)},
            {"type": "summary", "metric": "smart_best", "value": _candidate_short(smart_best, l_base_delay_ms, r_base_delay_ms, cfg)},
            {"type": "summary", "metric": "after_best", "value": _candidate_short(after_best, l_base_delay_ms, r_base_delay_ms, cfg)},
            {"type": "summary", "metric": "gain_score", "value": f"{gain:.6f}"},
            {"type": "summary", "metric": "best_changed", "value": str(not same_candidate(after_best, before_best))},
        ])
        for rank, c in enumerate(smart_candidates[:20], start=1):
            rows.append({"type": "smart_top20", "metric": f"rank_{rank}", "value": _candidate_short(c, l_base_delay_ms, r_base_delay_ms, cfg)})

        lines = [
            "# SMART RESCAN",
            "",
            f"Status: **{status}**",
            "",
            "## Dlaczego uruchomiono",
            "",
            f"- Trigger: **{trigger}**.",
            f"- Walidacja FAST przed dogrywką: **{str(validation_confidence or '').upper() or '-'}**.",
            "",
            "## Zakres dogrywki",
            "",
            f"- Promień żądany: **±{radius:.2f} ms**.",
            f"- Promień użyty: **±{effective_radius:.2f} ms**.",
            f"- Krok: **{fine_step:.3f} ms**.",
            f"- Centra skanu: **{len(effective_seeds)}**.",
            f"- Kombinacje lokalne: **{len(smart_candidates)}**.",
            f"- Limit/przycięcie: **{'TAK' if capped else 'NIE'}**.",
            "",
            "## Wynik",
            "",
            f"- Przed SMART RESCAN: `{_candidate_short(before_best, l_base_delay_ms, r_base_delay_ms, cfg)}`",
            f"- Najlepszy punkt dogrywki: `{_candidate_short(smart_best, l_base_delay_ms, r_base_delay_ms, cfg)}`",
            f"- Po połączeniu rankingu: `{_candidate_short(after_best, l_base_delay_ms, r_base_delay_ms, cfg)}`",
            f"- Zysk względem wcześniejszego #1: **{gain:+.3f} score**.",
            "",
            "## Wnioski",
            "",
        ]
        for n in notes:
            lines.append(f"- {n}")
        lines.extend([
            "",
            "## Top 10 SMART RESCAN",
            "",
            "| # | Wariant |",
            "|---:|---|",
        ])
        for rank, c in enumerate(smart_candidates[:10], start=1):
            lines.append(f"| {rank} | `{_candidate_short(c, l_base_delay_ms, r_base_delay_ms, cfg)}` |")
        lines.extend([
            "",
            "SMART RESCAN jest dogrywką numeryczną. Nadal trzeba potwierdzić wybrany wariant realnym pomiarem REW po wpisaniu ustawień do miniDSP.",
        ])
        return _write(status, lines, updated)

    except Exception as exc:
        notes.append(f"Błąd SMART RESCAN: {exc}")
        rows.append({"type": "error", "metric": "exception", "value": str(exc)})
        return _write("ERROR", ["# SMART RESCAN", "", "Status: **ERROR**", "", f"Błąd: `{exc}`"])



def recommendation_text(
    candidate: Candidate,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    crossover_label: str = "Custom",
    title: str = "REKOMENDACJA DLA OBU SUBÓW BEZ ODWRÓCENIA POLARYZACJI / OFF-OFF",
    filter_type: str = "BW",
    slope_db_oct: int = 48,
    cfg: Optional[OptimizerConfig] = None,
) -> str:
    """Kompatybilny opis rekomendacji, ale zawsze z pełnym zestawem czterech delayów."""
    if cfg is None:
        cfg = OptimizerConfig()
    mapped = candidate_shd_settings(candidate, l_base_delay_ms, r_base_delay_ms, cfg)
    return (
        f"{title} ({profile_label(crossover_label, filter_type, int(slope_db_oct))}):\n"
        f"  L main: {float(mapped['l_main_ms']):.2f} ms\n"
        f"  R main: {float(mapped['r_main_ms']):.2f} ms\n"
        f"  L sub: {float(mapped['l_sub_ms']):.2f} ms, polarity {candidate.l_polarity}\n"
        f"  R sub: {float(mapped['r_sub_ms']):.2f} ms, polarity {candidate.r_polarity}\n"
        f"  Relacja sub-main: L {float(mapped['l_sub_minus_main_ms']):+.2f} ms, R {float(mapped['r_sub_minus_main_ms']):+.2f} ms\n"
        f"  PBIS: {float(getattr(candidate, 'pbis_score', 0.0)):.1f}/100 — {getattr(candidate, 'pbis_label', '')}\n"
        f"  Co może wymagać uwagi:\n"
        f"{pbis_risk_user_block(candidate, indent='    ')}"
    )


def write_report(
    candidates: List[Candidate],
    path: str | Path,
    cfg: OptimizerConfig,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    crossover_label: str = "Custom",
    filter_type: str = "BW",
    slope_db_oct: int = 48,
    input_warnings: Optional[List[str]] = None,
) -> None:
    """v19.44: główny raport napisany prostym językiem, wyłącznie dla OFF/OFF."""
    offs = [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"]
    offs.sort(key=lambda c: float(c.score), reverse=True)
    lines: List[str] = ["# REW Delay Optimizer — wynik OFF/OFF\n\n"]
    if not offs:
        lines.append("Nie znaleziono poprawnego wariantu OFF/OFF. Sprawdź dane wejściowe i zakres opóźnień.\n")
        Path(path).write_text("".join(lines), encoding="utf-8")
        return

    lines.append("## Najważniejsza rekomendacja\n\n")
    lines.append("```text\n")
    lines.append(practical_recommendation_block(
        offs, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg, filter_type, slope_db_oct
    ))
    lines.append("\n```\n\n")

    best = offs[0]
    lines.append("## Jak czytać wynik PBIS\n\n")
    lines.append("PBIS to ocena punktu startowego od 0 do 100. Im wyżej, tym bezpieczniej zacząć od danego ustawienia.\n\n")
    lines.append("- 90–100: bardzo dobry punkt startowy;\n")
    lines.append("- 80–90: dobry;\n")
    lines.append("- 70–80: warto porównać A/B;\n")
    lines.append("- 60–70: ryzykowny;\n")
    lines.append("- poniżej 60: raczej odrzuć.\n\n")
    lines.append("PBIS nie zastępuje pomiaru kontrolnego REW ani odsłuchu.\n\n")

    lines.append("## Stabilność ustawienia\n\n")
    lines.append(f"{plateau_stability_user_label(getattr(best, 'pbis_plateau', ''))}.\n\n")
    lines.append("Jeżeli stabilność jest dobra, niewielka zmiana opóźnienia zwykle nie powinna gwałtownie pogorszyć wyniku.\n\n")

    lines.append("## Najlepsze warianty OFF/OFF do porównania\n\n")
    lines.append("| # | main L/R | sub L/R | rel. sub-main L/R | PBIS | Ocena |\n")
    lines.append("|---:|---:|---:|---:|---:|---|\n")
    for i, c in enumerate(offs[:10], start=1):
        mapped = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
        lines.append(
            f"| {i} | {float(mapped['l_main_ms']):.2f}/{float(mapped['r_main_ms']):.2f} ms | "
            f"{float(mapped['l_sub_ms']):.2f}/{float(mapped['r_sub_ms']):.2f} ms | "
            f"{float(mapped['l_sub_minus_main_ms']):+.2f}/{float(mapped['r_sub_minus_main_ms']):+.2f} ms | "
            f"{float(getattr(c, 'pbis_score', 0.0)):.1f}/100 | {getattr(c, 'pbis_label', '')} |\n"
        )
    lines.append("\n")

    if input_warnings:
        lines.append("## Ważne uwagi\n\n")
        for warning in input_warnings:
            lines.append(f"- {warning}\n")
        lines.append("\n")

    lines.append("## Co zrobić dalej\n\n")
    lines.append("1. Wpisz rekomendację główną OFF/OFF do SHD.\n")
    lines.append("2. Zrób pomiar kontrolny w REW: L main + L sub, R main + R sub oraz L + R razem.\n")
    lines.append("3. Odsłuchaj także wariant spójności L/R przy XO i sprawdź, czy relacje przestrzenne pozostają naturalne.\n")
    lines.append("4. PRE-DIRAC jest opcjonalny; przy zwykłym porównaniu slotów przejdź do Diraca i POST-DIRAC.\n\n")

    lines.append("## Ustawienia użyte do obliczeń\n\n")
    lines.append(f"- Profil: {profile_label(crossover_label, filter_type, int(slope_db_oct))}\n")
    base = baseline_shd_settings(cfg, l_base_delay_ms, r_base_delay_ms)
    lines.append(
        f"- Baza fizyczna wejścia: main L/R {float(base['l_main_ms']):.2f}/{float(base['r_main_ms']):.2f} ms; "
        f"sub L/R {float(base['l_sub_ms']):.2f}/{float(base['r_sub_ms']):.2f} ms; "
        f"rel. sub-main {float(base['l_sub_minus_main_ms']):+.2f}/{float(base['r_sub_minus_main_ms']):+.2f} ms\n"
    )
    lines.append(f"- Zakres sprawdzanej zmiany relacji: {cfg.delay_min_ms:.2f}–{cfg.delay_max_ms:.2f} ms\n")
    lines.append(f"- Krok końcowy: {cfg.delay_step_ms:.3f} ms\n")
    lines.append(f"- Pasmo oceny: {cfg.eval_low_hz:.0f}–{cfg.eval_high_hz:.0f} Hz\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def _safe_savefig(fig, path, **kwargs):
    try:
        fig.savefig(path, **kwargs)
    except Exception as exc:
        print(f"OSTRZEŻENIE: nie zapisano wykresu {path}: {exc}")



def _chart_settings_context(
    candidate: Candidate,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    cfg: Optional[OptimizerConfig] = None,
) -> Dict[str, str]:
    """Czytelny opis wykresów zgodny z czterema fizycznymi delayami SHD.

    Wartości ``candidate.l/r_delay_ms`` pozostają zmianą relacji względem wejściowego
    MDAT. Nie wolno ich podpisywać jako finalnych delayów do SHD.
    """
    mapped = candidate_shd_settings(candidate, l_base_delay_ms, r_base_delay_ms, cfg)
    return {
        "main": f"main L/R {float(mapped['l_main_ms']):.2f}/{float(mapped['r_main_ms']):.2f} ms",
        "sub": f"sub L/R {float(mapped['l_sub_ms']):.2f}/{float(mapped['r_sub_ms']):.2f} ms {candidate.l_polarity}/{candidate.r_polarity}",
        "rel": f"rel. sub-main L/R {float(mapped['l_sub_minus_main_ms']):+.2f}/{float(mapped['r_sub_minus_main_ms']):+.2f} ms",
        "baseline": (
            f"baza MDAT: main L/R {float(mapped.get('baseline_l_main_ms', 0.0)):.2f}/{float(mapped.get('baseline_r_main_ms', 0.0)):.2f} ms; "
            f"sub L/R {float(mapped.get('baseline_l_sub_ms', l_base_delay_ms)):.2f}/{float(mapped.get('baseline_r_sub_ms', r_base_delay_ms)):.2f} ms"
        ),
        "change": f"zmiana relacji względem MDAT L/R {float(candidate.l_delay_ms):+.2f}/{float(candidate.r_delay_ms):+.2f} ms",
    }


def _chart_context_box(ax, ctx: Dict[str, str], *, y: float = 0.02) -> None:
    ax.text(
        0.01, y,
        f"Do wpisania w SHD: {ctx['main']}; {ctx['sub']}\n{ctx['rel']} | {ctx['change']}",
        transform=ax.transAxes, fontsize=8, va="bottom", ha="left",
        bbox=dict(boxstyle="round,pad=0.35", alpha=0.78),
    )


def plot_candidate(
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    candidate: Candidate,
    out_prefix: str | Path,
    cfg: OptimizerConfig,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    plot_label: str = "Główna rekomendacja OFF/OFF",
) -> None:
    """Zapisuje główne wykresy predykcyjne dla jednego wariantu.

    v19.52: podpisy rozróżniają finalne cztery delaye SHD, relację ``sub-main``
    oraz zmianę nakładaną cyfrowo względem wejściowego MDAT.
    """
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_prefix = Path(out_prefix)
    ctx = _chart_settings_context(candidate, l_base_delay_ms, r_base_delay_ms, cfg)
    l_main = responses["L_main"]
    l_sub = responses["L_sub"]
    r_main = responses["R_main"]
    r_sub = responses["R_sub"]

    l_sub_s = apply_delay_and_polarity(l_sub, freq, candidate.l_delay_ms, candidate.l_polarity == "INV")
    r_sub_s = apply_delay_and_polarity(r_sub, freq, candidate.r_delay_ms, candidate.r_polarity == "INV")
    l_sum = l_main + l_sub_s
    r_sum = r_main + r_sub_s
    lr_sum = l_sum + r_sum

    fig, ax = plt.subplots(figsize=(12, 7))
    ax.semilogx(freq, db(l_sum), label="Suma L: L main + L sub")
    ax.semilogx(freq, db(r_sum), label="Suma R: R main + R sub")
    ax.semilogx(freq, db(lr_sum), linewidth=2.2, label="Suma stereo L+R")
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12, label="pasmo oceny integracji")
    ax.axvspan(cfg.bulge_low_hz, cfg.bulge_high_hz, alpha=0.08, label="kontrola nadmiaru basu")
    ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("SPL [dB]")
    ax.set_title(f"{plot_label} — przewidywane sumy SPL")
    _chart_context_box(ax, ctx)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, str(out_prefix) + "_sums.png", dpi=160)
    if EXPORT_SVG:
        _safe_savefig(fig, str(out_prefix) + "_sums.svg")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 7))
    ax.semilogx(freq, db(l_main), linestyle=":", label="L main — odpowiedź wejściowa MDAT")
    ax.semilogx(freq, db(l_sub_s), linestyle="--", label=f"L sub po zmianie relacji względem MDAT: {candidate.l_delay_ms:+.2f} ms")
    ax.semilogx(freq, db(l_sum), linewidth=2, label="Suma L")
    ax.semilogx(freq, db(r_main), linestyle=":", label="R main — odpowiedź wejściowa MDAT")
    ax.semilogx(freq, db(r_sub_s), linestyle="--", label=f"R sub po zmianie relacji względem MDAT: {candidate.r_delay_ms:+.2f} ms")
    ax.semilogx(freq, db(r_sum), linewidth=2, label="Suma R")
    ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("SPL [dB]")
    ax.set_title("Składowe i sumy kanałów — predykcja dla wybranego wariantu")
    _chart_context_box(ax, ctx)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, str(out_prefix) + "_components.png", dpi=160)
    if EXPORT_SVG:
        _safe_savefig(fig, str(out_prefix) + "_components.svg")
    plt.close(fig)

    l_phase = np.unwrap(np.angle(l_sum))
    r_phase = np.unwrap(np.angle(r_sum))
    phase_diff = wrap_phase_deg(np.rad2deg(l_phase - r_phase))
    lr_db = db(lr_sum)
    eval_mask = band_mask(freq, cfg.eval_low_hz, cfg.eval_high_hz)
    reliable = np.ones_like(freq, dtype=bool)
    if np.any(eval_mask):
        threshold = float(np.nanmedian(lr_db[eval_mask]) - 12.0)
        reliable = lr_db >= threshold
    phase_plot = phase_diff.copy()
    phase_plot[~reliable] = np.nan
    metrics = phase_reliability_metrics(freq, responses, candidate, cfg)
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.semilogx(freq, phase_plot, label="Różnica fazy: suma L − suma R; pominięte głębokie nulle")
    ax.axhline(0, linewidth=1)
    ax.axhline(180, linestyle=":", linewidth=1)
    ax.axhline(-180, linestyle=":", linewidth=1)
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12, label="pasmo oceny integracji")
    ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
    ax.set_ylim(-190, 190)
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("Różnica fazy suma L − suma R [°]")
    ax.set_title("Zgodność fazy kanałów L/R — predykcja dla wybranego wariantu")
    ax.text(
        0.01, 0.02,
        f"Średnia |różnica fazy| w paśmie oceny: {metrics['phase_lr_mean_abs_eval']:.0f}°; "
        f"udział wiarygodnych punktów: {metrics['phase_reliability_fraction']*100:.0f}%\n"
        f"{ctx['main']}; {ctx['sub']}; {ctx['rel']}",
        transform=ax.transAxes, fontsize=8, va="bottom", ha="left",
        bbox=dict(boxstyle="round,pad=0.35", alpha=0.78),
    )
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, str(out_prefix) + "_phase_lr.png", dpi=160)
    if EXPORT_SVG:
        _safe_savefig(fig, str(out_prefix) + "_phase_lr.svg")
    plt.close(fig)


def interp_spl_to_grid(m: Measurement, freq: np.ndarray) -> np.ndarray:
    order = np.argsort(m.freq)
    return np.interp(freq, m.freq[order], m.spl_db[order])


def candidate_sums(
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    candidate: Candidate,
) -> Dict[str, np.ndarray]:
    l_main = responses["L_main"]
    l_sub = apply_delay_and_polarity(
        responses["L_sub"], freq, candidate.l_delay_ms, candidate.l_polarity == "INV"
    )
    r_main = responses["R_main"]
    r_sub = apply_delay_and_polarity(
        responses["R_sub"], freq, candidate.r_delay_ms, candidate.r_polarity == "INV"
    )
    l_sum = l_main + l_sub
    r_sum = r_main + r_sub
    lr_sum = l_sum + r_sum
    return {
        "L_main": l_main,
        "L_sub_shifted": l_sub,
        "L_sum": l_sum,
        "R_main": r_main,
        "R_sub_shifted": r_sub,
        "R_sum": r_sum,
        "LR_sum": lr_sum,
    }



def _moving_average_nan(values: np.ndarray, window: int = 7) -> np.ndarray:
    """
    Lekki wygładzacz bez SciPy. Używany tylko do diagnostyki group delay,
    nie do głównego scoringu.
    """
    y = np.asarray(values, dtype=float)
    if window <= 1 or y.size < 3:
        return y
    window = int(max(3, window))
    if window % 2 == 0:
        window += 1
    valid = np.isfinite(y).astype(float)
    y0 = np.where(np.isfinite(y), y, 0.0)
    kernel = np.ones(window, dtype=float)
    denom = np.convolve(valid, kernel, mode="same")
    numer = np.convolve(y0, kernel, mode="same")
    out = np.where(denom > 0, numer / np.maximum(denom, 1e-12), np.nan)
    return out


def complex_group_delay_ms(
    freq: np.ndarray,
    response: np.ndarray,
    smooth_window: int = 9,
) -> np.ndarray:
    """
    Group delay z fazy zespolonej: GD = -1/(2*pi) * d(phi)/df.
    To diagnostyka czasowa. Nie zastępuje głównego silnika SPL+faza.
    """
    freq = np.asarray(freq, dtype=float)
    response = np.asarray(response, dtype=complex)
    order = np.argsort(freq)
    f = freq[order]
    h = response[order]
    phase = np.unwrap(np.angle(h))
    gd_s = -np.gradient(phase, f) / (2.0 * np.pi)
    gd_ms = gd_s * 1000.0
    # Odrzuć skrajne artefakty numeryczne przy głębokich nullach.
    amp_db = db(h)
    finite = np.isfinite(gd_ms) & np.isfinite(amp_db)
    if np.any(finite):
        floor = np.nanmedian(amp_db[finite]) - 35.0
        gd_ms = np.where(amp_db >= floor, gd_ms, np.nan)
    gd_smoothed = _moving_average_nan(gd_ms, smooth_window)
    out = np.empty_like(gd_smoothed)
    out[order] = gd_smoothed
    return out


def group_delay_summary(
    freq: np.ndarray,
    response: np.ndarray,
    low_hz: float,
    high_hz: float,
) -> Dict[str, float]:
    gd = complex_group_delay_ms(freq, response)
    mask = band_mask(freq, low_hz, high_hz) & np.isfinite(gd)
    if not np.any(mask):
        return {
            "gd_median_ms": float("nan"),
            "gd_std_ms": float("nan"),
            "gd_p95_abs_excess_ms": float("nan"),
            "gd_max_abs_excess_ms": float("nan"),
            "gd_reliability_fraction": 0.0,
        }
    vals = gd[mask]
    med = float(np.nanmedian(vals))
    excess = vals - med
    return {
        "gd_median_ms": med,
        "gd_std_ms": float(np.nanstd(vals)),
        "gd_p95_abs_excess_ms": float(np.nanpercentile(np.abs(excess), 95)),
        "gd_max_abs_excess_ms": float(np.nanmax(np.abs(excess))),
        "gd_reliability_fraction": float(np.mean(np.isfinite(gd[band_mask(freq, low_hz, high_hz)]))),
    }


def band_limited_impulse_envelope(
    freq: np.ndarray,
    response: np.ndarray,
    low_hz: float,
    high_hz: float,
    fft_high_hz: Optional[float] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Przybliżona obwiednia impulsu pasmowego z odpowiedzi zespolonej.
    To nie jest pełny impuls REW, tylko pomocnicza diagnostyka czasowa w paśmie integracji.
    """
    freq = np.asarray(freq, dtype=float)
    response = np.asarray(response, dtype=complex)
    finite = np.isfinite(freq) & np.isfinite(response.real) & np.isfinite(response.imag)
    if np.count_nonzero(finite) < 8:
        return np.array([]), np.array([])
    f_src = freq[finite]
    h_src = response[finite]
    order = np.argsort(f_src)
    f_src = f_src[order]
    h_src = h_src[order]

    hi = float(fft_high_hz or max(300.0, high_hz * 2.0))
    hi = min(hi, float(np.nanmax(f_src)))
    if hi <= high_hz:
        hi = min(float(np.nanmax(f_src)), high_hz + 20.0)
    if hi <= low_hz + 5.0:
        return np.array([]), np.array([])

    df = 0.5
    f_lin = np.arange(0.0, hi + df, df)
    real_i = np.interp(f_lin, f_src, h_src.real, left=0.0, right=0.0)
    imag_i = np.interp(f_lin, f_src, h_src.imag, left=0.0, right=0.0)
    h_lin = real_i + 1j * imag_i

    band = (f_lin >= low_hz) & (f_lin <= high_hz)
    if not np.any(band):
        return np.array([]), np.array([])

    # Cosine taper na krawędziach pasma, żeby nie generować sztucznego dzwonienia.
    taper = np.zeros_like(f_lin)
    width = max(2.0, min(12.0, 0.12 * (high_hz - low_hz)))
    rising = (f_lin >= low_hz) & (f_lin < low_hz + width)
    middle = (f_lin >= low_hz + width) & (f_lin <= high_hz - width)
    falling = (f_lin > high_hz - width) & (f_lin <= high_hz)
    taper[middle] = 1.0
    taper[rising] = 0.5 - 0.5 * np.cos(np.pi * (f_lin[rising] - low_hz) / width)
    taper[falling] = 0.5 + 0.5 * np.cos(np.pi * (f_lin[falling] - (high_hz - width)) / width)
    h_lin *= taper

    # Hermitian spectrum -> real impulse.
    full = np.concatenate([h_lin, np.conj(h_lin[-2:0:-1])])
    impulse = np.fft.ifft(full).real
    env = np.abs(_moving_average_nan(np.abs(impulse), 5))
    if np.nanmax(env) > 0:
        env = env / np.nanmax(env)

    n = len(full)
    sample_rate = n * df
    time_s = np.arange(n) / sample_rate
    period_s = 1.0 / df
    # Zawiń czas wokół zera, żeby długie opóźnienia nie trafiały na koniec okresu IFFT.
    time_s = np.where(time_s > period_s / 2.0, time_s - period_s, time_s)
    order_t = np.argsort(time_s)
    return time_s[order_t] * 1000.0, env[order_t]


def impulse_envelope_summary(time_ms: np.ndarray, envelope: np.ndarray) -> Dict[str, float]:
    if time_ms.size == 0 or envelope.size == 0 or not np.any(np.isfinite(envelope)):
        return {
            "impulse_peak_time_ms": float("nan"),
            "impulse_centroid_ms": float("nan"),
            "impulse_width80_ms": float("nan"),
            "impulse_tail_ratio_35ms": float("nan"),
        }
    t = np.asarray(time_ms, dtype=float)
    e = np.asarray(envelope, dtype=float)
    e = np.where(np.isfinite(e), e, 0.0)
    power = e ** 2
    if np.sum(power) <= 0:
        return {
            "impulse_peak_time_ms": float("nan"),
            "impulse_centroid_ms": float("nan"),
            "impulse_width80_ms": float("nan"),
            "impulse_tail_ratio_35ms": float("nan"),
        }
    peak_idx = int(np.nanargmax(e))
    peak_t = float(t[peak_idx])
    centroid = float(np.sum(t * power) / np.sum(power))
    cdf = np.cumsum(power) / np.sum(power)
    t10 = float(np.interp(0.10, cdf, t))
    t90 = float(np.interp(0.90, cdf, t))
    tail = float(np.sum(power[t > peak_t + 35.0]) / np.sum(power))
    return {
        "impulse_peak_time_ms": peak_t,
        "impulse_centroid_ms": centroid,
        "impulse_width80_ms": max(0.0, t90 - t10),
        "impulse_tail_ratio_35ms": tail,
    }


def time_alignment_verdict(metrics: Dict[str, float]) -> Tuple[str, List[str]]:
    """
    v18.6: diagnostyka czasowa jest informacyjna.
    Nie tworzy alarmu "INFORMACYJNIE", bo w paśmie 60–120 Hz
    group delay i obwiednia impulsu naturalnie bywają szerokie.
    """
    reasons: List[str] = []
    p95 = metrics.get("LR_sum_gd_p95_abs_excess_ms", float("nan"))
    std = metrics.get("LR_sum_gd_std_ms", float("nan"))
    width = metrics.get("LR_sum_impulse_width80_ms", float("nan"))
    tail = metrics.get("LR_sum_impulse_tail_ratio_35ms", float("nan"))

    notes = 0
    if np.isfinite(p95) and p95 > 18.0:
        notes += 1
        reasons.append(f"excess group delay L+R jest podwyższony ({p95:.1f} ms p95) — traktuj jako informację porównawczą")
    elif np.isfinite(p95) and p95 > 10.0:
        notes += 1
        reasons.append(f"excess group delay L+R jest umiarkowanie podwyższony ({p95:.1f} ms p95)")

    if np.isfinite(std) and std > 10.0:
        notes += 1
        reasons.append(f"group delay L+R ma zmienność {std:.1f} ms — typowe przy modach/dołkach, nie zmienia samo decyzji")

    if np.isfinite(width):
        if width > 80.0:
            notes += 1
            reasons.append(f"obwiednia impulsu pasmowego jest szeroka ({width:.1f} ms dla 80% energii)")
        elif width > 50.0:
            notes += 1
            reasons.append(f"obwiednia impulsu pasmowego jest umiarkowanie szeroka ({width:.1f} ms dla 80% energii)")

    if np.isfinite(tail) and tail > 0.55:
        notes += 1
        reasons.append(f"ogon obwiedni po +35 ms jest podwyższony ({tail*100:.1f}%)")

    verdict = "INFORMACYJNIE" if notes else "OK"
    if not reasons:
        reasons.append("brak dodatkowych uwag czasowych w paśmie integracji")
    reasons.append("diagnostyka czasowa jest pomocnicza; główną decyzję nadal podejmuj po SPL+faza oraz realnej walidacji L+R")
    return verdict, reasons


def candidate_time_alignment_metrics(
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    candidate: Candidate,
    cfg: OptimizerConfig,
) -> Dict[str, float]:
    sums = candidate_sums(freq, responses, candidate)
    metrics: Dict[str, float] = {
        "time_band_low_hz": float(cfg.eval_low_hz),
        "time_band_high_hz": float(cfg.eval_high_hz),
    }
    for name in ("L_sum", "R_sum", "LR_sum"):
        gd = group_delay_summary(freq, sums[name], cfg.eval_low_hz, cfg.eval_high_hz)
        t, env = band_limited_impulse_envelope(freq, sums[name], cfg.eval_low_hz, cfg.eval_high_hz)
        imp = impulse_envelope_summary(t, env)
        for k, v in gd.items():
            metrics[f"{name}_{k}"] = float(v)
        for k, v in imp.items():
            metrics[f"{name}_{k}"] = float(v)
    verdict, _ = time_alignment_verdict(metrics)
    metrics["time_alignment_risk_code"] = {"OK": 0.0, "INFORMACYJNIE": 1.0}.get(verdict, float("nan"))
    return metrics


def save_time_alignment_diagnostics(
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    candidate: Candidate,
    out_prefix: str | Path,
    cfg: OptimizerConfig,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
    label: str = "candidate",
) -> Dict[str, float]:
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_prefix = Path(out_prefix)
    sums = candidate_sums(freq, responses, candidate)
    metrics = candidate_time_alignment_metrics(freq, responses, candidate, cfg)
    verdict, reasons = time_alignment_verdict(metrics)

    # CSV metryk
    with (Path(str(out_prefix) + "_time_alignment.csv")).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for k in sorted(metrics.keys()):
            writer.writerow([k, f"{metrics[k]:.6f}" if np.isfinite(metrics[k]) else "nan"])
        writer.writerow(["verdict", verdict])
        for i, reason in enumerate(reasons, start=1):
            writer.writerow([f"reason_{i}", reason])

    # Raport markdown
    mapped = candidate_shd_settings(candidate, l_base_delay_ms, r_base_delay_ms, cfg)
    md: List[str] = []
    md.append(f"# Diagnostyka czasowa — {label}\n\n")
    md.append(f"## Status: **{verdict}**\n\n")
    md.append("To jest sekcja informacyjna. Nie zastępuje głównego wyboru SPL+faza ani realnej walidacji L+R.\n\n")
    md.append(f"- SHD main L/R: {float(mapped['l_main_ms']):.2f}/{float(mapped['r_main_ms']):.2f} ms\n")
    md.append(f"- SHD sub L/R: {float(mapped['l_sub_ms']):.2f}/{float(mapped['r_sub_ms']):.2f} ms {candidate.l_polarity}/{candidate.r_polarity}\n")
    md.append(f"- Relacja sub-main L/R: {float(mapped['l_sub_minus_main_ms']):+.2f}/{float(mapped['r_sub_minus_main_ms']):+.2f} ms\n")
    md.append(f"- Pasmo czasowe: {cfg.eval_low_hz:.1f}–{cfg.eval_high_hz:.1f} Hz\n")
    md.append(f"- L+R GD median: {metrics.get('LR_sum_gd_median_ms', float('nan')):.2f} ms\n")
    md.append(f"- L+R excess GD p95: {metrics.get('LR_sum_gd_p95_abs_excess_ms', float('nan')):.2f} ms\n")
    md.append(f"- L+R GD std: {metrics.get('LR_sum_gd_std_ms', float('nan')):.2f} ms\n")
    md.append(f"- L+R impulse width80: {metrics.get('LR_sum_impulse_width80_ms', float('nan')):.2f} ms\n")
    md.append(f"- L+R impulse tail +35 ms: {metrics.get('LR_sum_impulse_tail_ratio_35ms', float('nan'))*100:.1f}%\n\n")
    md.append("## Uwagi\n\n")
    for r in reasons:
        md.append(f"- {r}\n")
    md.append("\nUwaga: porównuj te liczby tylko między bliskimi kandydatami A/B. Szeroka obwiednia w 60–120 Hz nie jest sama w sobie błędem.\n")
    Path(str(out_prefix) + "_time_alignment.md").write_text("".join(md), encoding="utf-8")

    # Group delay CSV + plot
    gd_cols = {}
    for name in ("L_sum", "R_sum", "LR_sum"):
        gd_cols[name] = complex_group_delay_ms(freq, sums[name])
    with (Path(str(out_prefix) + "_group_delay.csv")).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frequency_hz", "L_sum_gd_ms", "R_sum_gd_ms", "LR_sum_gd_ms"])
        for i, fr in enumerate(freq):
            writer.writerow([
                f"{fr:.6f}",
                f"{gd_cols['L_sum'][i]:.5f}" if np.isfinite(gd_cols["L_sum"][i]) else "",
                f"{gd_cols['R_sum'][i]:.5f}" if np.isfinite(gd_cols["R_sum"][i]) else "",
                f"{gd_cols['LR_sum'][i]:.5f}" if np.isfinite(gd_cols["LR_sum"][i]) else "",
            ])

    fig, ax = plt.subplots(figsize=(12, 6))
    for name, lbl in [("L_sum", "Suma L"), ("R_sum", "Suma R"), ("LR_sum", "Suma stereo L+R")]:
        gd = gd_cols[name]
        ax.semilogx(freq, gd, label=lbl)
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12, label="pasmo oceny")
    ax.axhline(0, linewidth=1)
    ax.set_xlim(max(20, float(np.nanmin(freq))), min(300, float(np.nanmax(freq))))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("Opóźnienie grupowe [ms]")
    ax.set_title(f"Diagnostyka opóźnienia grupowego — {label} — {verdict}")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, str(out_prefix) + "_group_delay.png", dpi=160)
    plt.close(fig)

    # Impulse envelope plot
    fig, ax = plt.subplots(figsize=(12, 5))
    for name, lbl in [("L_sum", "Suma L"), ("R_sum", "Suma R"), ("LR_sum", "Suma stereo L+R")]:
        t, env = band_limited_impulse_envelope(freq, sums[name], cfg.eval_low_hz, cfg.eval_high_hz)
        if t.size:
            mask = (t >= -120.0) & (t <= 160.0)
            ax.plot(t[mask], env[mask], label=lbl)
    ax.axvline(0, linewidth=1)
    ax.set_xlabel("Czas [ms]")
    ax.set_ylabel("Znormalizowana obwiednia")
    ax.set_title(f"Przybliżona obwiednia impulsu w paśmie {cfg.eval_low_hz:.0f}–{cfg.eval_high_hz:.0f} Hz — {label}")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, str(out_prefix) + "_impulse_envelope.png", dpi=160)
    plt.close(fig)

    return metrics


def append_time_alignment_to_report(report_path: str | Path, title: str, metrics: Dict[str, float]) -> None:
    """Dopisuje do raportu głównego krótki, zrozumiały komentarz czasowy."""
    report_path = Path(report_path)
    if not report_path.exists():
        return
    verdict, reasons = time_alignment_verdict(metrics)
    lines: List[str] = ["\n## Dodatkowa kontrola zachowania w czasie\n\n"]
    if verdict == "OK":
        lines.append("Nie widać wyraźnego problemu czasowego w paśmie łączenia kolumn z subami.\n\n")
    else:
        lines.append("Wynik wymaga ostrożności. Zwróć uwagę na zwartość basu oraz stabilność sceny podczas odsłuchu A/B.\n\n")
    lines.append("Ta kontrola jest pomocnicza. Ostatecznie decydują pomiar REW i odsłuch.\n")
    report_path.write_text(report_path.read_text(encoding="utf-8") + "".join(lines), encoding="utf-8")


def save_predicted_curves(
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    candidate: Candidate,
    path: str | Path,
) -> None:
    sums = candidate_sums(freq, responses, candidate)
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "frequency_hz",
            "L_sum_pred_db",
            "R_sum_pred_db",
            "LR_sum_pred_db",
            "L_sum_pred_phase_deg",
            "R_sum_pred_phase_deg",
            "LR_sum_pred_phase_deg",
            "L_main_db",
            "L_sub_shifted_db",
            "R_main_db",
            "R_sub_shifted_db",
            "L_main_phase_deg",
            "L_sub_shifted_phase_deg",
            "R_main_phase_deg",
            "R_sub_shifted_phase_deg",
        ])
        l_sum = db(sums["L_sum"])
        r_sum = db(sums["R_sum"])
        lr_sum = db(sums["LR_sum"])
        l_main = db(sums["L_main"])
        l_sub = db(sums["L_sub_shifted"])
        r_main = db(sums["R_main"])
        r_sub = db(sums["R_sub_shifted"])

        def phase_deg(x: np.ndarray) -> np.ndarray:
            return wrap_phase_deg(np.rad2deg(np.angle(x)))

        l_sum_ph = phase_deg(sums["L_sum"])
        r_sum_ph = phase_deg(sums["R_sum"])
        lr_sum_ph = phase_deg(sums["LR_sum"])
        l_main_ph = phase_deg(sums["L_main"])
        l_sub_ph = phase_deg(sums["L_sub_shifted"])
        r_main_ph = phase_deg(sums["R_main"])
        r_sub_ph = phase_deg(sums["R_sub_shifted"])

        for i, fr in enumerate(freq):
            writer.writerow([
                f"{fr:.6f}",
                f"{l_sum[i]:.4f}",
                f"{r_sum[i]:.4f}",
                f"{lr_sum[i]:.4f}",
                f"{l_sum_ph[i]:.4f}",
                f"{r_sum_ph[i]:.4f}",
                f"{lr_sum_ph[i]:.4f}",
                f"{l_main[i]:.4f}",
                f"{l_sub[i]:.4f}",
                f"{r_main[i]:.4f}",
                f"{r_sub[i]:.4f}",
                f"{l_main_ph[i]:.4f}",
                f"{l_sub_ph[i]:.4f}",
                f"{r_main_ph[i]:.4f}",
                f"{r_sub_ph[i]:.4f}",
            ])



def unique_measurement_candidates(candidates: List[Candidate], limit: int = 4) -> List[Tuple[Candidate, str]]:
    """v19.44: do planu pomiarów trafiają tylko najlepsze warianty OFF/OFF."""
    selected: List[Tuple[Candidate, str]] = []
    offs = [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"]
    offs.sort(key=lambda c: float(c.score), reverse=True)
    for i, c in enumerate(offs):
        if any(same_candidate(c, prev) for prev, _ in selected):
            continue
        reason = "główny wariant" if i == 0 else "wariant zapasowy do sprawdzenia"
        selected.append((c, reason))
        if len(selected) >= max(1, int(limit)):
            break
    return selected



def write_measurement_plan(
    candidates: List[Candidate],
    path: str | Path,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: Optional[OptimizerConfig] = None,
) -> None:
    """v19.44: prosty plan realnych pomiarów REW dla wariantów OFF/OFF."""
    plan = unique_measurement_candidates(candidates, limit=3)
    lines: List[str] = ["# Plan pomiarów REW — OFF/OFF\n\n"]
    if not plan:
        lines.append("Brak wariantu OFF/OFF.\n")
    else:
        lines.append("Najpierw zmierz wariant 1. Jeżeli wynik jest niejasny, porównaj wariant 2.\n\n")
        lines.append("| Wariant | main L/R | sub L/R | rel. sub-main L/R | Po co sprawdzić |\n")
        lines.append("|---:|---:|---:|---:|---|\n")
        for i, (c, reason) in enumerate(plan, start=1):
            mapped = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
            lines.append(
                f"| {i} | {float(mapped['l_main_ms']):.2f}/{float(mapped['r_main_ms']):.2f} ms | "
                f"{float(mapped['l_sub_ms']):.2f}/{float(mapped['r_sub_ms']):.2f} ms, OFF/OFF | "
                f"{float(mapped['l_sub_minus_main_ms']):+.2f}/{float(mapped['r_sub_minus_main_ms']):+.2f} ms | {reason} |\n"
            )
        lines.append("\nDla wybranego wariantu zmierz: L main + L sub, R main + R sub oraz L + R razem.\n")
    Path(path).write_text("".join(lines), encoding="utf-8")

    csv_path = Path(path).with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["rank", "L_main_SHD_ms", "R_main_SHD_ms", "L_sub_SHD_ms", "L_polarity", "R_sub_SHD_ms", "R_polarity", "L_sub_minus_main_ms", "R_sub_minus_main_ms", "reason", "score"])
        for i, (c, reason) in enumerate(plan, start=1):
            mapped = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
            writer.writerow([
                i,
                f"{float(mapped['l_main_ms']):.4f}",
                f"{float(mapped['r_main_ms']):.4f}",
                f"{float(mapped['l_sub_ms']):.4f}",
                "OFF",
                f"{float(mapped['r_sub_ms']):.4f}",
                "OFF",
                f"{float(mapped['l_sub_minus_main_ms']):.4f}",
                f"{float(mapped['r_sub_minus_main_ms']):.4f}",
                reason,
                f"{c.score:.4f}",
            ])


def candidate_cluster_rows(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
    max_rows: int = 12,
) -> List[Dict[str, object]]:
    """
    Grupuje bliskie score kandydaty wg liczby okresów delayu przy crossoverze i polaryzacji.
    To nie jest akustyczna prawda absolutna — to praktyczny sposób wykrycia równoważnych
    rozwiązań opartych o pełne cykle fazy.
    """
    if not candidates:
        return []
    xo = parse_crossover_hz(crossover_label, cfg)
    period = delay_period_ms(xo)
    best_score = candidates[0].score
    tolerance = effective_score_tolerance(cfg)
    groups: Dict[Tuple[str, str, int, int], List[Candidate]] = {}
    for c in candidates:
        if best_score - c.score > tolerance:
            continue
        lf, rf = _candidate_plot_xy(c, l_base_delay_ms, r_base_delay_ms, cfg)
        key = (c.l_polarity, c.r_polarity, int(round(lf / period)), int(round(rf / period)))
        groups.setdefault(key, []).append(c)

    rows: List[Dict[str, object]] = []
    for key, items in groups.items():
        items_sorted = sorted(items, key=lambda c: c.score, reverse=True)
        rep = items_sorted[0]
        shortest = min(
            items,
            key=lambda c: (
                physical_delay_safety(c, l_base_delay_ms, r_base_delay_ms, xo, cfg)["severity"],
                physical_delay_safety(c, l_base_delay_ms, r_base_delay_ms, xo, cfg)["delay_burden"],
                best_score - c.score,
            )
        )
        safety = physical_delay_safety(shortest, l_base_delay_ms, r_base_delay_ms, xo, cfg)
        rows.append({
            "cluster": f"{key[0]}/{key[1]} L~{key[2]}cy R~{key[3]}cy",
            "count": len(items),
            "best_score": rep.score,
            "best_score_loss": best_score - rep.score,
            "representative_L_relation_ms": _candidate_plot_xy(rep, l_base_delay_ms, r_base_delay_ms, cfg)[0],
            "representative_R_relation_ms": _candidate_plot_xy(rep, l_base_delay_ms, r_base_delay_ms, cfg)[1],
            "plateau_alt_L_relation_ms": _candidate_plot_xy(shortest, l_base_delay_ms, r_base_delay_ms, cfg)[0],
            "plateau_alt_R_relation_ms": _candidate_plot_xy(shortest, l_base_delay_ms, r_base_delay_ms, cfg)[1],
            "plateau_alt_score_loss": best_score - shortest.score,
            "delay_safety_status": safety["status"],
            "LR_diff_ms": safety["lr_diff_ms"],
            "polarity": f"{key[0]}/{key[1]}",
        })
    rows.sort(key=lambda r: (float(r["best_score_loss"]), int(physical_status_order(str(r["delay_safety_status"]))), float(r["plateau_alt_score_loss"])))
    return rows[:max_rows]


def physical_status_order(status: str) -> int:
    return {"OK": 0, "PHASE-CYCLE": 1, "SPRAWDŹ": 1, "WYMAGA WALIDACJI": 2, "BŁĄD DELAYU": 3}.get(str(status), 9)


def write_candidate_clusters(
    candidates: List[Candidate],
    out_prefix: str | Path,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
) -> None:
    out_prefix = Path(out_prefix)
    rows = candidate_cluster_rows(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg)
    csv_path = Path(str(out_prefix) + ".csv")
    md_path = Path(str(out_prefix) + ".md")
    fieldnames = [
        "cluster", "count", "best_score", "best_score_loss",
        "representative_L_relation_ms", "representative_R_relation_ms",
        "plateau_alt_L_relation_ms", "plateau_alt_R_relation_ms",
        "plateau_alt_score_loss", "delay_safety_status", "LR_diff_ms", "polarity",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})

    lines: List[str] = []
    lines.append("# Klastry kandydatów v18.4\n\n")
    lines.append(
        "Klastry grupują kandydatów bliskich najlepszemu wynikowi według liczby okresów delayu przy crossoverze. "
        "Pomagają rozpoznać sytuację, w której długi delay jest tylko wariantem fazowym z pełnym cyklem, "
        "a krótszy wynik jest tylko neutralną alternatywą A/B w tej samej rodzinie polaryzacji, nie automatycznie lepszą rekomendacją.\n\n"
    )
    if not rows:
        lines.append("Brak klastrów w obrębie tolerancji score.\n")
    else:
        lines.append("| # | Klaster | Liczba | Best loss | Relacja reprezentanta L/R | Relacja alternatywy A/B L/R | Alt loss | Status | L/R diff |\n")
        lines.append("|---:|---|---:|---:|---:|---:|---:|---|---:|\n")
        for i, r in enumerate(rows, start=1):
            lines.append(
                f"| {i} | {r['cluster']} | {r['count']} | {float(r['best_score_loss']):.3f} | "
                f"{float(r['representative_L_relation_ms']):.2f}/{float(r['representative_R_relation_ms']):.2f} ms | "
                f"{float(r['plateau_alt_L_relation_ms']):.2f}/{float(r['plateau_alt_R_relation_ms']):.2f} ms | "
                f"{float(r['plateau_alt_score_loss']):.3f} | {r['delay_safety_status']} | {float(r['LR_diff_ms']):.2f} ms |\n"
            )
    md_path.write_text("".join(lines), encoding="utf-8")



def _candidate_family(c: Candidate) -> str:
    return f"{str(c.l_polarity).upper()}/{str(c.r_polarity).upper()}"


def _candidate_plot_xy(
    c: Candidate,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    cfg: Optional[OptimizerConfig] = None,
) -> Tuple[float, float]:
    """Współrzędne mapy: w trybie relatywnym końcowa relacja sub-main.

    Dzięki temu mapa pozostaje porównywalna dla RAW i dla pomiaru kontrolnego
    wykonanego z aktywną bazą delayów, np. main30.
    """
    if bool(getattr(cfg, "relative_delay_shd_mode", False)):
        return candidate_final_relative_delays(c, l_base_delay_ms, r_base_delay_ms, cfg)
    return (
        float(final_delay(l_base_delay_ms, c.l_delay_ms)),
        float(final_delay(r_base_delay_ms, c.r_delay_ms)),
    )


def _select_clean_heatmap_candidates(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    cfg: OptimizerConfig,
    score_attr: str = "score",
) -> List[Candidate]:
    """v19.44: utrzymuje główną heatmapę jako czytelną mapę wynikową.

    FAST 2-STAGE i SMART RESCAN naturalnie tworzą prostokątne lokalne okna
    dogęszczania. To jest dobra diagnostyka pracy algorytmu, ale zła główna
    heatmapa odsłuchowa. Dlatego mapa wynikowa pokazuje najlepszych kandydatów
    i reprezentantów rodzin polaryzacji, a okna algorytmu są rysowane osobno w
    `fast_rescan_windows.png`.
    """
    candidates = [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"]
    if not candidates:
        return []

    def score_value(c: Candidate) -> float:
        try:
            v = float(getattr(c, score_attr, float("nan")))
        except Exception:
            v = float("nan")
        if not np.isfinite(v):
            try:
                return float(c.score)
            except Exception:
                return -1e99
        return v

    # Reprezentant dla każdego punktu finalnego i rodziny polaryzacji.
    # Zaokrąglenie do 1 µs usuwa duble po kolejnych dogrywkach, bez zmiany wyniku.
    by_key: Dict[Tuple[float, float, str], Candidate] = {}
    for c in candidates:
        try:
            x, y = _candidate_plot_xy(c, l_base_delay_ms, r_base_delay_ms, cfg)
        except Exception:
            continue
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        key = (round(x, 6), round(y, 6), _candidate_family(c))
        old = by_key.get(key)
        if old is None or score_value(c) > score_value(old):
            by_key[key] = c

    unique = list(by_key.values())
    unique.sort(key=score_value, reverse=True)

    keep_n = max(120, min(420, int(getattr(cfg, "analysis_pool_n", 1000)) // 2))
    selected: List[Candidate] = unique[:keep_n]

    # Dopilnuj, żeby każda rodzina polaryzacji miała swój najlepszy punkt.
    fam_best: Dict[str, Candidate] = {}
    for c in unique:
        fam = _candidate_family(c)
        if fam not in fam_best or score_value(c) > score_value(fam_best[fam]):
            fam_best[fam] = c
    for c in fam_best.values():
        if not any(same_candidate(c, s) for s in selected):
            selected.append(c)

    # Dopilnuj punktu OFF/OFF, bo to praktyczny punkt odniesienia.
    off = best_both_off(candidates)
    if off is not None and not any(same_candidate(off, s) for s in selected):
        selected.append(off)

    return selected



def _write_heatmap_cleanup_note(out_dir: str | Path) -> None:
    """Zapisuje prosty przewodnik po głównych wykresach bez zmiany nazw plików."""
    out = Path(out_dir)
    lines = [
        "# JAK CZYTAĆ NAJWAŻNIEJSZE WYKRESY — v19.57\n\n",
        "Wykresy są predykcją dla głównego wariantu OFF/OFF z bieżącego MDAT. Nie zastępują pomiaru kontrolnego REW.\n\n",
        "## Cztery przyciski w GUI\n\n",
        "1. `Mapa PBIS (0–100)` → `pbis_score_heatmap.png`\n",
        "   - pokazuje surowy PBIS pojedynczych punktów; więcej = lepiej;\n",
        "   - gwiazdka oznacza główną rekomendację OFF/OFF;\n",
        "   - PBIS Safe z tabeli jest ostrożniejszy, bo uwzględnia dodatkowe ryzyka i stabilność otoczenia.\n\n",
        "2. `Mapa jakości / strata` → `delay_score_heatmap.png`\n",
        "   - pokazuje stratę wyniku względem głównej rekomendacji; mniej = lepiej;\n",
        "   - to nie jest drugi PBIS, lecz mapa różnicy wyniku rankingowego;\n",
        "   - romb oznacza alternatywę A/B, a znacznik P kandydata spójności L/R przy XO.\n\n",
        "3. `Sumy SPL: L, R i L+R` → `best_sums.png`\n",
        "   - pokazuje przewidywane sumy L main + L sub, R main + R sub oraz stereo L+R;\n",
        "   - podpis na wykresie zawiera komplet czterech delayów do wpisania w SHD.\n\n",
        "4. `Zgodność fazy L/R` → `best_phase_lr.png`\n",
        "   - pokazuje różnicę fazy pomiędzy sumą lewego i prawego kanału;\n",
        "   - głębokie nulle są pomijane, ponieważ faza jest tam mało wiarygodna.\n\n",
        "## Ważne rozróżnienie\n\n",
        "Współrzędne map oznaczają końcową relację `sub − main`, a nie cztery wartości do bezpośredniego wpisania w SHD. ",
        "Pełny zestaw `L main`, `R main`, `L sub`, `R sub` odczytaj z pierwszego wiersza tabeli lub z `MINIDSP_SETTINGS_FINAL.txt`.\n\n",
        "Dla pomiaru kontrolnego wykonanego z aktywną bazą, np. `main30`, wykresy uwzględniają bazę zapisaną już w fazie MDAT i nakładają tylko zmianę względem tego pomiaru.\n\n",
        "## Diagnostyka pracy algorytmu\n\n",
        "`fast_rescan_windows.png` pokazuje prostokątne okna dogęszczania FAST/SMART. To diagnostyka sposobu liczenia, a nie mapa jakości brzmienia.\n",
    ]
    try:
        text = "".join(lines)
        (out / "JAK_CZYTAC_NAJWAZNIEJSZE_WYKRESY.md").write_text(text, encoding="utf-8")
        (out / "JAK_CZYTAC_NAJWAZNIEJSZE_WYKRESY.txt").write_text(text, encoding="utf-8")
        # Zachowaj starszą nazwę, żeby linki z wcześniejszych wersji nie zniknęły.
        (out / "HEATMAP_CLEANUP_NOTES.md").write_text(text, encoding="utf-8")
        (out / "HEATMAP_CLEANUP_NOTES.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass


def plot_delay_score_heatmap(
    candidates: List[Candidate],
    out_path: str | Path,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
) -> None:
    """Czysta mapa straty score OFF/OFF zgodna z rekomendacją główną."""
    if not candidates:
        return
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_path = Path(out_path)
    best = best_both_off(candidates)
    if best is None:
        return
    plot_candidates = _select_clean_heatmap_candidates(
        candidates, l_base_delay_ms, r_base_delay_ms, cfg, score_attr="score"
    )
    if not plot_candidates:
        return

    plateau_alt, _ = select_practical_candidate(
        candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg
    )
    spatial, _ = select_spatial_coherence_candidate(
        candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg
    )

    xs = np.array([_candidate_plot_xy(c, l_base_delay_ms, r_base_delay_ms, cfg)[0] for c in plot_candidates], dtype=float)
    ys = np.array([_candidate_plot_xy(c, l_base_delay_ms, r_base_delay_ms, cfg)[1] for c in plot_candidates], dtype=float)
    losses = np.array([float(best.score) - float(c.score) for c in plot_candidates], dtype=float)
    finite_losses = losses[np.isfinite(losses)]
    if finite_losses.size == 0:
        finite_losses = np.array([0.0], dtype=float)
    losses = np.nan_to_num(losses, nan=float(np.nanmax(finite_losses)), posinf=float(np.nanmax(finite_losses)), neginf=0.0)
    sizes = np.clip(70.0 - 28.0 * losses, 14.0, 75.0)

    xo = parse_crossover_hz(crossover_label, cfg)
    fig, ax = plt.subplots(figsize=(9, 7))
    sc = ax.scatter(xs, ys, c=losses, s=sizes, alpha=0.82, marker="o")
    cb = fig.colorbar(sc, ax=ax)
    cb.set_label("Strata wyniku względem rekomendacji [pkt]; mniej = lepiej")

    bx, by = _candidate_plot_xy(best, l_base_delay_ms, r_base_delay_ms, cfg)
    ax.scatter([bx], [by], marker="*", s=210, label=f"Główna rekomendacja: rel. L {bx:+.2f}, R {by:+.2f} ms")

    if plateau_alt is not None and not same_candidate(plateau_alt, best):
        ax.scatter(
            [_candidate_plot_xy(plateau_alt, l_base_delay_ms, r_base_delay_ms, cfg)[0]],
            [_candidate_plot_xy(plateau_alt, l_base_delay_ms, r_base_delay_ms, cfg)[1]],
            marker="D", s=120, label="Alternatywa A/B",
        )
    if spatial is not None and not same_candidate(spatial, best) and (plateau_alt is None or not same_candidate(spatial, plateau_alt)):
        ax.scatter(
            [_candidate_plot_xy(spatial, l_base_delay_ms, r_base_delay_ms, cfg)[0]],
            [_candidate_plot_xy(spatial, l_base_delay_ms, r_base_delay_ms, cfg)[1]],
            marker="P", s=135, label="Kandydat spójności L/R przy XO",
        )

    ax.set_xlabel("Relacja L: sub − main [ms]")
    ax.set_ylabel("Relacja R: sub − main [ms]")
    ax.set_title(f"Mapa jakości ustawień OFF/OFF — crossover {xo:.0f} Hz")
    ax.text(0.02, 0.02, "Kolor pokazuje stratę punktów, nie PBIS. Współrzędne to relacja sub − main, nie cztery delaye SHD.", transform=ax.transAxes, fontsize=8, va="bottom")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_path, dpi=160)
    plt.close(fig)



def plot_pbis_score_heatmap(
    candidates: List[Candidate],
    out_path: str | Path,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
) -> None:
    """Czysta mapa surowego PBIS; gwiazdka oznacza główną rekomendację OFF/OFF."""
    if not candidates:
        return
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_path = Path(out_path)
    best = best_both_off(candidates)
    if best is None:
        return
    plot_candidates = _select_clean_heatmap_candidates(
        candidates, l_base_delay_ms, r_base_delay_ms, cfg, score_attr="pbis_score"
    )
    if not plot_candidates:
        return

    xs = np.array([_candidate_plot_xy(c, l_base_delay_ms, r_base_delay_ms, cfg)[0] for c in plot_candidates], dtype=float)
    ys = np.array([_candidate_plot_xy(c, l_base_delay_ms, r_base_delay_ms, cfg)[1] for c in plot_candidates], dtype=float)
    pbis = np.array([float(getattr(c, "pbis_score", 0.0)) for c in plot_candidates], dtype=float)
    pbis = np.nan_to_num(pbis, nan=0.0, posinf=100.0, neginf=0.0)
    pbis_min = float(np.nanmin(pbis)) if pbis.size else 0.0
    pbis_max = float(np.nanmax(pbis)) if pbis.size else 100.0
    sizes = np.clip(24.0 + (pbis - pbis_min) * 1.8, 18.0, 110.0)

    fig, ax = plt.subplots(figsize=(9, 7))
    sc = ax.scatter(xs, ys, c=pbis, s=sizes, alpha=0.84, marker="o", vmin=max(0.0, pbis_min), vmax=min(100.0, max(100.0, pbis_max)))
    cb = fig.colorbar(sc, ax=ax)
    cb.set_label("Surowy PBIS [0–100]; więcej = lepiej")

    bx, by = _candidate_plot_xy(best, l_base_delay_ms, r_base_delay_ms, cfg)
    best_pbis = float(getattr(best, "pbis_score", 0.0))
    ax.scatter([bx], [by], marker="*", s=210, label=f"Główna rekomendacja: rel. L {bx:+.2f}, R {by:+.2f} ms; PBIS {best_pbis:.1f}")
    x_mid = float(np.nanmin(xs) + np.nanmax(xs)) / 2.0 if xs.size else bx
    dx = -8 if bx >= x_mid else 8
    ax.annotate(
        f"Główna OFF/OFF\nrel. L {bx:+.2f} / R {by:+.2f} ms\nPBIS {best_pbis:.1f}",
        xy=(bx, by), xytext=(dx, 8), textcoords="offset points", fontsize=8,
        ha="right" if dx < 0 else "left", va="bottom",
    )

    xo = parse_crossover_hz(crossover_label, cfg)
    ax.set_xlabel("Relacja L: sub − main [ms]")
    ax.set_ylabel("Relacja R: sub − main [ms]")
    ax.set_title(f"Mapa surowego PBIS dla ustawień OFF/OFF — crossover {xo:.0f} Hz")
    ax.text(0.02, 0.02, "PBIS ocenia pojedynczy punkt. Ostrożniejszy PBIS Safe z tabeli uwzględnia dodatkowo ryzyka i stabilność otoczenia.", transform=ax.transAxes, fontsize=8, va="bottom")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_path, dpi=160)
    plt.close(fig)


def plot_fast_rescan_windows(
    candidates: List[Candidate],
    out_path: str | Path,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
) -> None:
    """v19.44: osobna mapa diagnostyczna okien FAST/SMART widocznych dla OFF/OFF."""
    if not candidates:
        return
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    out_path = Path(out_path)
    best = best_both_off(candidates)
    if best is None:
        return
    plot_candidates = _select_clean_heatmap_candidates(
        candidates, l_base_delay_ms, r_base_delay_ms, cfg, score_attr="score"
    )
    if not plot_candidates:
        return

    xs = np.array([_candidate_plot_xy(c, l_base_delay_ms, r_base_delay_ms, cfg)[0] for c in plot_candidates], dtype=float)
    ys = np.array([_candidate_plot_xy(c, l_base_delay_ms, r_base_delay_ms, cfg)[1] for c in plot_candidates], dtype=float)
    losses = np.array([float(best.score) - float(c.score) for c in plot_candidates], dtype=float)
    losses = np.nan_to_num(losses, nan=0.0, posinf=0.0, neginf=0.0)

    fig, ax = plt.subplots(figsize=(9, 7))
    sc = ax.scatter(xs, ys, c=losses, s=28, alpha=0.72, marker="o")
    cb = fig.colorbar(sc, ax=ax)
    cb.set_label("Różnica względem głównej rekomendacji OFF/OFF")

    # Widoczna diagnostyka pokazuje tylko okna OFF/OFF. Silnik może liczyć szerzej w tle.
    seeds: List[Candidate] = []
    for c in sorted(plot_candidates, key=lambda x: float(x.score), reverse=True)[: max(1, min(8, len(plot_candidates)))]:
        if not any(same_candidate(c, prev) for prev in seeds):
            seeds.append(c)

    radius = max(
        float(getattr(cfg, "fast_refine_radius_ms", 0.6)),
        float(getattr(cfg, "fast_validation_radius_ms", 0.6)),
        float(getattr(cfg, "delay_step_ms", 0.1)) * 3.0,
    )
    smart_radius = max(float(getattr(cfg, "fast_smart_rescan_radius_ms", radius)), radius)
    if bool(getattr(cfg, "relative_delay_shd_mode", False)):
        base_l_rel, base_r_rel = baseline_relative_delays(cfg, l_base_delay_ms, r_base_delay_ms)
        x_min = base_l_rel + float(getattr(cfg, "delay_min_ms", 0.0))
        x_max = base_l_rel + float(getattr(cfg, "delay_max_ms", 30.0))
        y_min = base_r_rel + float(getattr(cfg, "delay_min_ms", 0.0))
        y_max = base_r_rel + float(getattr(cfg, "delay_max_ms", 30.0))
    else:
        x_min = final_delay(l_base_delay_ms, float(getattr(cfg, "delay_min_ms", 0.0)))
        x_max = final_delay(l_base_delay_ms, float(getattr(cfg, "delay_max_ms", 30.0)))
        y_min = final_delay(r_base_delay_ms, float(getattr(cfg, "delay_min_ms", 0.0)))
        y_max = final_delay(r_base_delay_ms, float(getattr(cfg, "delay_max_ms", 30.0)))

    for idx, seed in enumerate(seeds[:8], start=1):
        sx, sy = _candidate_plot_xy(seed, l_base_delay_ms, r_base_delay_ms, cfg)
        win_radius = smart_radius if idx <= max(1, int(getattr(cfg, "fast_smart_rescan_seed_n", 8))) else radius
        rx0 = max(x_min, sx - win_radius); rx1 = min(x_max, sx + win_radius)
        ry0 = max(y_min, sy - win_radius); ry1 = min(y_max, sy + win_radius)
        if rx1 <= rx0 or ry1 <= ry0:
            continue
        ax.add_patch(Rectangle((rx0, ry0), rx1-rx0, ry1-ry0, fill=False, linewidth=1.15, linestyle="--", alpha=0.75))
        ax.annotate(str(idx), xy=(sx, sy), xytext=(4, 4), textcoords="offset points", fontsize=8)

    bx, by = _candidate_plot_xy(best, l_base_delay_ms, r_base_delay_ms, cfg)
    ax.scatter([bx], [by], marker="*", s=210, label=f"Główna rekomendacja OFF/OFF: L {bx:.2f}, R {by:.2f} ms")
    xo = parse_crossover_hz(crossover_label, cfg)
    ax.set_xlabel("Końcowa relacja L sub-main [ms]")
    ax.set_ylabel("Końcowa relacja R sub-main [ms]")
    ax.set_title(f"Diagnostyka okien FAST/SMART dla OFF/OFF — crossover {xo:.0f} Hz")
    ax.text(0.02, 0.02, "Prostokąty pokazują miejsca dogęszczania obliczeń, a nie jakość brzmienia.", transform=ax.transAxes, fontsize=8, va="bottom")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_path, dpi=160)
    plt.close(fig)




ORGANIZED_RESULT_DIRS = [
    "00_START_TUTAJ",
    "01_RAPORTY_MD",
    "02_DANE_CSV",
    "03_GLOWNA_REKOMENDACJA_BEST",
    "04_ALTERNATYWA_PLATEAU_AB",
    "07_MAPY_I_POROWNANIA",
    "08_WALIDACJA_PREDYKCJI",
    "09_DIRAC",
    "10_INNE_WYKRESY",
    "11_INNE_PLIKI",
]

FLAT_WORKFLOW_DIRS = {
    "prediction_vs_real",
    "dirac_validation",
    "dirac_curtain_analysis",
    "dirac_curtain_comparison",
    "preset_comparison",
}


MAIN_FOLDER_DESCRIPTIONS = {
    "00_START_TUTAJ": "Najprostsze pliki: werdykt, status projektu, końcowe ustawienia SHD i checklista.",
    "01_RAPORTY_MD": "Raporty tekstowe Markdown.",
    "02_DANE_CSV": "Zbiorcze dane CSV i tabele rankingowe.",
    "03_GLOWNA_REKOMENDACJA_BEST": "Wykresy i dane dla głównej rekomendacji.",
    "04_ALTERNATYWA_PLATEAU_AB": "Wykresy i dane dla neutralnej alternatywy plateau A/B.",
    "07_MAPY_I_POROWNANIA": "Mapy relacji sub-main, surowego PBIS, straty wyniku i wykresy różnic.",
    "08_WALIDACJA_PREDYKCJI": "Predykcja vs realny pomiar i status potwierdzenia kandydata.",
    "09_DIRAC": "Analiza Dirac ON i kurtyn Dirac.",
    "10_INNE_WYKRESY": "Pozostałe wykresy.",
    "11_INNE_PLIKI": "Pozostałe pliki.",
}


class ResultsManager:
    """
    v19.44: centralna polityka wyników bez zbędnych kopii.

    Zasada:
    - główna optymalizacja ma pełną strukturę katalogów,
    - małe moduły walidacyjne pozostają płaskie,
    - pliki operacyjne są przechowywane wyłącznie w 00_START_TUTAJ,
    - katalog główny zawiera tylko dwa krótkie pliki nawigacyjne.
    """

    def __init__(self) -> None:
        self.organized_dirs = list(ORGANIZED_RESULT_DIRS)
        self.flat_dirs = set(FLAT_WORKFLOW_DIRS)
        self.main_folder_descriptions = dict(MAIN_FOLDER_DESCRIPTIONS)

    def is_flat_workflow_dir(self, out_dir: str | Path) -> bool:
        return Path(out_dir).name.lower() in self.flat_dirs

    def prepare_module_folder(
        self,
        out_dir: str | Path,
        stale_patterns: Optional[List[str]] = None,
        purge_existing_children: bool = False,
    ) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        _remove_organized_result_dirs(out_dir)

        stale_root_files = [
            "START_TUTAJ.md",
            "README_WYNIKOW.md",
            "MINIDSP_SETTINGS_FINAL.txt",
            "PROJECT_STATUS.md",
            "PROJECT_WORKFLOW_STATUS.md",
        ]
        for file_name in stale_root_files:
            p = out_dir / file_name
            if p.exists() and p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass

        if purge_existing_children:
            for p in list(out_dir.iterdir()):
                if p.is_dir():
                    try:
                        shutil.rmtree(p)
                    except Exception:
                        pass
                elif p.is_file():
                    try:
                        p.unlink()
                    except Exception:
                        pass

        if stale_patterns:
            _remove_matching_files(out_dir, stale_patterns)

        return out_dir

    def organize_main_folder(self, out_dir: str | Path) -> List[str]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if self.is_flat_workflow_dir(out_dir):
            self.prepare_module_folder(out_dir)
            return []

        for folder in self.main_folder_descriptions:
            (out_dir / folder).mkdir(parents=True, exist_ok=True)

        moved: List[str] = []
        for item in list(out_dir.iterdir()):
            if item.is_dir():
                continue
            target_rel = _organized_target_for_file(item.name)
            if target_rel is None:
                continue
            target = out_dir / target_rel / item.name
            try:
                if target.exists():
                    target.unlink()
                shutil.move(str(item), str(target))
                moved.append(f"{item.name} -> {target_rel}/")
            except Exception as exc:
                print(f"OSTRZEŻENIE: nie mogę uporządkować {item.name}: {exc}")

        self.ensure_main_shortcuts(out_dir)
        self.write_main_start_files(out_dir)
        self.write_manifest(out_dir)
        return moved

    def ensure_main_shortcuts(self, out_dir: str | Path) -> None:
        """v19.44: usuwa dawne kopie z katalogu głównego.

        Kanoniczne pliki operacyjne pozostają w 00_START_TUTAJ lub w odpowiednich
        podfolderach. Funkcja zachowuje nazwę dla kompatybilności starszych wywołań,
        ale nie tworzy już skrótowych kopii.
        """
        out_dir = Path(out_dir)
        duplicate_root_names = [
            "MY_SYSTEM_WORKFLOW.md",
            "01_WPISZ_DO_MINIDSP.txt",
            "02_ZMIERZ_W_REW.txt",
            "03_CZY_MOZNA_ROBIC_DIRAC.txt",
            "04_ODSLUCH_AB.txt",
            "05_POLITYKA_POLARYZACJI.txt",
            "MINIDSP_SETTINGS_FINAL.txt",
            "PROJECT_STATUS.md",
            "PROJECT_WORKFLOW_STATUS.md",
            "best_predicted_curves.csv",
            "plateau_alt_predicted_curves.csv",
            "SMART_RESCAN.md", "SMART_RESCAN.txt", "SMART_RESCAN.csv",
            "FAST_2_STAGE_VALIDATION.md", "FAST_2_STAGE_VALIDATION.txt", "FAST_2_STAGE_VALIDATION.csv",
            "PERFORMANCE_DIAGNOSTICS.md", "PERFORMANCE_DIAGNOSTICS.txt", "PERFORMANCE_DIAGNOSTICS.csv",
        ]
        for name in duplicate_root_names:
            p = out_dir / name
            if p.exists() and p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass


    def write_main_start_files(self, out_dir: str | Path) -> None:
        out_dir = Path(out_dir)
        start_lines = [
            "# START TUTAJ\n\n",
            "Otwórz folder `00_START_TUTAJ`. Tam są najważniejsze pliki.\n\n",
            "1. Wpisz wartości OFF/OFF do SHD.\n",
            "2. Zrób pomiar kontrolny w REW.\n",
            "3. Sprawdź checklistę przed Dirac.\n",
            "4. Jeżeli wynik jest niejasny, porównaj dwa warianty OFF/OFF odsłuchem A/B.\n",
        ]
        (out_dir / "START_TUTAJ.md").write_text("".join(start_lines), encoding="utf-8")

        readme_lines = [
            "# Wyniki\n\n",
            "Najważniejsze są ustawienia OFF/OFF do SHD oraz pomiar kontrolny REW przed uruchomieniem Dirac.\n",
        ]
        (out_dir / "README_WYNIKOW.md").write_text("".join(readme_lines), encoding="utf-8")

    def write_manifest(self, out_dir: str | Path) -> Path:
        out_dir = Path(out_dir)
        manifest_rows = []
        for root, dirs, files in os.walk(out_dir):
            for fn in files:
                p = Path(root) / fn
                rel = p.relative_to(out_dir).as_posix()
                try:
                    size = p.stat().st_size
                except Exception:
                    size = 0
                manifest_rows.append((rel, p.suffix.lower().lstrip("."), size))
        manifest_dir = out_dir / "02_DANE_CSV"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "00_MANIFEST_PLIKOW.csv"
        with manifest_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["plik", "typ", "rozmiar_bajtow"])
            for rel, ext, size in sorted(manifest_rows):
                writer.writerow([rel, ext, size])
        return manifest_path


    def write_project_status(
        self,
        out_dir: str | Path,
        *,
        input_mode: str = "",
        mdat_path: Optional[str | Path] = None,
        mdat_project: Optional[object] = None,
        candidates: Optional[List[object]] = None,
        stage: str = "unknown",
        extra_notes: Optional[List[str]] = None,
    ) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        lines: List[str] = ["# Status\n\n"]
        if candidates:
            try:
                off = best_both_off(candidates)  # type: ignore[arg-type]
            except Exception:
                off = None
            if off is not None:
                lines.append("Wynik OFF/OFF jest gotowy do pomiaru kontrolnego REW.\n\n")
            else:
                lines.append("Nie znaleziono wariantu OFF/OFF. Sprawdź dane wejściowe.\n\n")
        else:
            lines.append("Najpierw uruchom optymalizację.\n\n")
        lines.append("Następny krok: wykonaj pomiar kontrolny REW przed uruchomieniem Dirac.\n")
        start_dir = out_dir / "00_START_TUTAJ"
        start_dir.mkdir(parents=True, exist_ok=True)
        status_path = start_dir / "PROJECT_STATUS.md"
        status_path.write_text("".join(lines), encoding="utf-8")
        # v19.44: status ma jedno miejsce. Usuwamy dawne kopie z katalogu głównego.
        for stale in [out_dir / "PROJECT_STATUS.md", out_dir / "PROJECT_WORKFLOW_STATUS.md", start_dir / "PROJECT_WORKFLOW_STATUS.md"]:
            if stale.exists() and stale.is_file():
                try:
                    stale.unlink()
                except Exception:
                    pass
        return status_path

    def assert_folder_policy(self, out_dir: str | Path) -> List[str]:
        """Zwraca listę błędów polityki folderów. Pusta lista = OK."""
        out_dir = Path(out_dir)
        errors: List[str] = []

        # Główny folder po optymalizacji ma mieć pełną strukturę.
        if not (out_dir / "00_START_TUTAJ").exists():
            errors.append("Główny folder nie zawiera 00_START_TUTAJ.")
        if not (out_dir / "00_START_TUTAJ" / "PROJECT_STATUS.md").exists():
            errors.append("Brak PROJECT_STATUS.md w 00_START_TUTAJ.")
        forbidden_root_duplicates = [
            "MY_SYSTEM_WORKFLOW.md", "01_WPISZ_DO_MINIDSP.txt", "02_ZMIERZ_W_REW.txt",
            "03_CZY_MOZNA_ROBIC_DIRAC.txt", "04_ODSLUCH_AB.txt", "05_POLITYKA_POLARYZACJI.txt",
            "MINIDSP_SETTINGS_FINAL.txt", "PROJECT_STATUS.md", "PROJECT_WORKFLOW_STATUS.md",
            "best_predicted_curves.csv", "plateau_alt_predicted_curves.csv",
        ]
        for duplicate in forbidden_root_duplicates:
            if (out_dir / duplicate).exists():
                errors.append(f"Zbędna kopia w katalogu głównym: {duplicate}.")

        # Małe moduły nie mogą zawierać pełnego organizera.
        for name in sorted(self.flat_dirs):
            module_dir = out_dir / name
            if not module_dir.exists():
                continue
            for organized in self.organized_dirs:
                if (module_dir / organized).exists():
                    errors.append(f"Folder {name} zawiera zakazany podfolder organizera: {organized}.")
        return errors


RESULTS_MANAGER = ResultsManager()


def _remove_organized_result_dirs(out_dir: str | Path) -> None:
    """Usuwa stare podfoldery organizera z małych modułów walidacyjnych."""
    out_dir = Path(out_dir)
    for folder_name in ORGANIZED_RESULT_DIRS:
        p = out_dir / folder_name
        if p.exists() and p.is_dir():
            try:
                shutil.rmtree(p)
            except Exception:
                pass


def _remove_matching_files(out_dir: str | Path, patterns: List[str]) -> None:
    out_dir = Path(out_dir)
    for pattern in patterns:
        for p in out_dir.glob(pattern):
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass


def prepare_flat_module_folder(
    out_dir: str | Path,
    stale_patterns: Optional[List[str]] = None,
    purge_existing_children: bool = False,
) -> Path:
    """
    v19.6: wrapper kompatybilności. Cała polityka folderów modułów idzie przez ResultsManager.
    """
    return RESULTS_MANAGER.prepare_module_folder(
        out_dir,
        stale_patterns=stale_patterns,
        purge_existing_children=purge_existing_children,
    )


def write_dirac_validation_readme(out_dir: str | Path, metrics: Dict[str, float], verdict: str, label: str) -> None:
    out_dir = Path(out_dir)
    lines: List[str] = []
    lines.append("# OTWÓRZ NAJPIERW — walidacja Dirac ON\n\n")
    lines.append("Ten folder jest celowo płaski. Nie ma tu dodatkowych podkatalogów organizera.\n\n")
    lines.append("## Najważniejsze pliki\n\n")
    lines.append("1. `dirac_validation.md` — raport i werdykt.\n")
    lines.append("2. `dirac_validation.png` — L/R/L+R Dirac ON.\n")
    lines.append("3. `dirac_validation_margin.png` — margines L+R względem mocniejszego kanału.\n")
    lines.append("4. `dirac_validation.csv` — dane liczbowe.\n\n")
    lines.append("## Wynik skrócony\n\n")
    lines.append(f"- Etykieta: **{label}**\n")
    lines.append(f"- Werdykt: **{verdict}**\n")
    lines.append(f"- Średni margines: {float(metrics.get('mean_margin_db', float('nan'))):.2f} dB\n")
    lines.append(f"- Najgorszy margines: {float(metrics.get('min_margin_db', float('nan'))):.2f} dB\n")
    lines.append(f"- Dołek L+R: {float(metrics.get('dip_db', float('nan'))):.2f} dB\n")
    lines.append(f"- Punkty poniżej -3 dB: {float(metrics.get('bad_fraction_below_minus3db', float('nan'))) * 100:.1f}%\n")
    (out_dir / "OTWORZ_NAJPIERW.md").write_text("".join(lines), encoding="utf-8")
    (out_dir / "README_DIRAC_VALIDATION.md").write_text("".join(lines), encoding="utf-8")


def write_dirac_curtain_readme(
    out_dir: str | Path,
    metrics: Dict[str, float],
    verdict: str,
    label: str,
    curtain_high_hz: float,
) -> None:
    out_dir = Path(out_dir)
    lines: List[str] = []
    lines.append("# OTWÓRZ NAJPIERW — analiza kurtyny Dirac\n\n")
    lines.append("Ten folder jest celowo płaski. Nie ma tu dodatkowych podkatalogów organizera.\n\n")
    lines.append("## Najważniejsze pliki\n\n")
    lines.append("1. `DIRAC_CURTAIN_SIMPLE_VERDICT.md` — najprostszy werdykt.\n")
    lines.append("2. `dirac_curtain_analysis.md` — pełny raport.\n")
    lines.append("3. `dirac_curtain_analysis.png` — L/R/L+R w strefie kurtyny.\n")
    lines.append("4. `dirac_curtain_margin.png` — margines L+R.\n")
    lines.append("5. `dirac_curtain_analysis.csv` — dane liczbowe.\n")
    lines.append("6. `dirac_time_diagnostics.*` — diagnostyka czasowa, jeśli została wygenerowana.\n\n")
    lines.append("## Wynik skrócony\n\n")
    lines.append(f"- Etykieta: **{label}**\n")
    lines.append(f"- Kurtyna: **{float(curtain_high_hz):.0f} Hz**\n")
    lines.append(f"- Werdykt: **{verdict}**\n")
    lines.append(f"- Score bezpieczeństwa: {float(metrics.get('curtain_safety_score', float('nan'))):.2f}\n")
    lines.append(f"- Margines p05: {float(metrics.get('curtain_p05_margin_db', float('nan'))):.2f} dB\n")
    lines.append(f"- Dołek w strefie kurtyny: {float(metrics.get('curtain_dip_db', float('nan'))):.2f} dB\n")
    (out_dir / "OTWORZ_NAJPIERW.md").write_text("".join(lines), encoding="utf-8")
    (out_dir / "README_DIRAC_CURTAIN_ANALYSIS.md").write_text("".join(lines), encoding="utf-8")


def write_dirac_curtain_comparison_readme(out_dir: str | Path, summary: List[Dict[str, object]]) -> None:
    out_dir = Path(out_dir)
    lines: List[str] = []
    lines.append("# OTWÓRZ NAJPIERW — porównanie kurtyn Dirac\n\n")
    lines.append("Ten folder ma prostą strukturę: pliki podsumowania w katalogu głównym oraz folder `curtains/` z detalami poszczególnych kurtyn.\n")
    lines.append("Nie ma tu katalogów organizera typu `00_START_TUTAJ`, `01_RAPORTY_MD` ani `09_DIRAC`.\n\n")
    lines.append("## Najważniejsze pliki\n\n")
    lines.append("1. `DIRAC_CURTAINS_SIMPLE_COMPARISON.md` — najprostszy ranking.\n")
    lines.append("2. `dirac_curtain_comparison.md` — pełniejsze porównanie.\n")
    lines.append("3. `dirac_curtain_comparison.csv` — tabela metryk.\n")
    lines.append("4. `curtains/<nazwa>/OTWORZ_NAJPIERW.md` — detale danej kurtyny.\n\n")
    if summary:
        best = summary[0]
        lines.append("## Wynik skrócony\n\n")
        lines.append(f"- Najlepsza pomiarowo: **{best.get('group')}**\n")
        lines.append(f"- Kurtyna: **{float(best.get('curtain_high_hz', float('nan'))):.0f} Hz**\n")
        lines.append(f"- Werdykt: **{best.get('verdict')}**\n")
        lines.append(f"- Score: {float(best.get('curtain_safety_score', float('nan'))):.2f}\n")
    else:
        lines.append("## Wynik skrócony\n\nNie znaleziono kompletnych zestawów L/R/L+R.\n")
    (out_dir / "OTWORZ_NAJPIERW.md").write_text("".join(lines), encoding="utf-8")
    (out_dir / "README_DIRAC_CURTAIN_COMPARISON.md").write_text("".join(lines), encoding="utf-8")


def write_preset_comparison_readme(out_dir: str | Path, summary: List[Dict[str, object]]) -> None:
    out_dir = Path(out_dir)
    lines: List[str] = []
    lines.append("# OTWÓRZ NAJPIERW — porównanie presetów\n\n")
    lines.append("Ten folder ma prostą strukturę: podsumowanie w katalogu głównym oraz folder `presets/` z detalami poszczególnych presetów.\n")
    lines.append("Nie ma tu pełnego organizera wyników wewnątrz każdego presetu.\n\n")
    lines.append("## Najważniejsze pliki\n\n")
    lines.append("1. `preset_comparison.md` — ranking presetów.\n")
    lines.append("2. `preset_comparison.csv` — tabela metryk.\n")
    lines.append("3. `presets/<preset>/raport.md` — szczegóły danego presetu.\n\n")
    if summary:
        best = summary[0]
        lines.append("## Wynik skrócony\n\n")
        lines.append(f"- Najlepszy preset: **{best.get('preset')}**\n")
        lines.append(f"- Filtr: **{best.get('filter')}**\n")
        lines.append(f"- Score: {float(best.get('score', float('nan'))):.3f}\n")
        lines.append(f"- Finalne L/R: {float(best.get('L_final_ms', float('nan'))):.2f} / {float(best.get('R_final_ms', float('nan'))):.2f} ms\n")
    else:
        lines.append("## Wynik skrócony\n\nNie znaleziono kompletnych grup presetów.\n")
    (out_dir / "OTWORZ_NAJPIERW.md").write_text("".join(lines), encoding="utf-8")
    (out_dir / "README_PRESET_COMPARISON.md").write_text("".join(lines), encoding="utf-8")




def _organized_target_for_file(name: str) -> Optional[str]:
    """Zwraca folder docelowy dla pliku wynikowego v18.9. None = zostaw w katalogu głównym."""
    lower = name.lower()

    # Pliki, które mają zostać łatwo dostępne z głównego folderu.
    if name in {"START_TUTAJ.md", "README_WYNIKOW.md"}:
        return None

    # Start i rekomendacja.
    if name in {
        "simple_verdict.md",
        "MINIDSP_SETTINGS_FINAL.txt",
        "PROJECT_STATUS.md",
        "MY_SYSTEM_WORKFLOW.md",
        "01_WPISZ_DO_MINIDSP.txt",
        "02_ZMIERZ_W_REW.txt",
        "03_CZY_MOZNA_ROBIC_DIRAC.txt",
        "04_ODSLUCH_AB.txt",
        "WORKFLOW_ASSISTANT.md",
        "REW_MEASUREMENT_CHECKLIST.md",
        "LISTENING_AB_SCORECARD.md",
        "JAK_CZYTAC_NAJWAZNIEJSZE_WYKRESY.md",
    }:
        return "00_START_TUTAJ"

    # Walidacja predykcja vs realny pomiar.
    if (
        name.startswith("prediction_vs_real")
        or name.startswith("candidate_validation_status")
        or name.startswith("PROJECT_STATUS_AFTER_VALIDATION")
    ):
        return "08_WALIDACJA_PREDYKCJI"

    # Dirac / kurtyny.
    if name.lower().startswith("dirac") or name.startswith("DIRAC_"):
        return "09_DIRAC"

    # Kandydaci i ich diagnostyka.
    if name.startswith("best_INV_INV"):
        return "05_INV_INV"
    if name.startswith("plateau_alt"):
        return "04_ALTERNATYWA_PLATEAU_AB"
    if name.startswith("mixed_polarity_test"):
        return "06_MIXED_POLARITY_TEST"
    if name.startswith("best_") or name in {"best_time_alignment.md", "best_time_alignment.csv"}:
        return "03_GLOWNA_REKOMENDACJA_BEST"

    # Mapy, porównania, heatmapy.
    if (
        name.startswith("delay_score_heatmap")
        or name.startswith("pbis_score_heatmap")
        or name.startswith("fast_rescan_windows")
        or name.startswith("diff_")
        or name.startswith("best_OFF_OFF")
    ):
        return "07_MAPY_I_POROWNANIA"

    # Raporty i workflow tekstowy.
    if name in {"raport.md", "candidate_clusters.md", "measurement_plan_AB.md", "PBIS_SUMMARY.md", "SOUND_QUALITY_SAFE_SUMMARY.md", "AB_LISTENING_PLAN.md"}:
        return "01_RAPORTY_MD"

    # Dane zbiorcze.
    if name in {"ranking.csv", "candidate_clusters.csv", "measurement_plan_AB.csv", "phase_reliability.csv"}:
        return "02_DANE_CSV"

    # Pozostałe pliki według typu.
    if lower.endswith(".md"):
        return "01_RAPORTY_MD"
    if lower.endswith(".csv"):
        return "02_DANE_CSV"
    if lower.endswith((".png", ".jpg", ".jpeg", ".svg")):
        return "10_INNE_WYKRESY"
    if lower.endswith(".txt"):
        return "00_START_TUTAJ"
    return "11_INNE_PLIKI"


def organize_results_folder(out_dir: str | Path) -> List[str]:
    """
    v19.6: wrapper kompatybilności. Pełna organizacja wyników jest scentralizowana
    w RESULTS_MANAGER.organize_main_folder().
    """
    return RESULTS_MANAGER.organize_main_folder(out_dir)

def find_main_report_path(out_dir: str | Path) -> Optional[Path]:
    """Znajduje raport.md zarówno w starym płaskim układzie, jak i w układzie v18.8/v18.9."""
    out_dir = Path(out_dir)
    candidates = [
        out_dir / "raport.md",
        out_dir / "01_RAPORTY_MD" / "raport.md",
        out_dir.parent / "raport.md",
        out_dir.parent / "01_RAPORTY_MD" / "raport.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def find_result_file(out_dir: str | Path, filename: str) -> Optional[Path]:
    """Finds a result file in both the old flat layout and the organized v18.9+ layout.

    v19.3 fix: after optimization, organize_results_folder() moves files such as
    best_predicted_curves.csv into subfolders. GUI buttons must therefore not
    assume that every generated file remains directly in rew_delay_results.
    """
    out_dir = Path(out_dir)
    filename = str(filename)

    # Fast direct checks for the most common files.
    candidates = [
        out_dir / filename,
        out_dir / "03_GLOWNA_REKOMENDACJA_BEST" / filename,
        out_dir / "04_ALTERNATYWA_PLATEAU_AB" / filename,
        out_dir / "08_WALIDACJA_PREDYKCJI" / filename,
        out_dir / "02_DANE_CSV" / filename,
        out_dir / "01_RAPORTY_MD" / filename,
        out_dir.parent / filename,
        out_dir.parent / "03_GLOWNA_REKOMENDACJA_BEST" / filename,
        out_dir.parent / "04_ALTERNATYWA_PLATEAU_AB" / filename,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    # Last-resort recursive search, limited to the output folder.
    try:
        for candidate in out_dir.rglob(filename):
            if candidate.exists() and candidate.is_file():
                return candidate
    except Exception:
        pass
    return None


def ensure_root_shortcut_copy(out_dir: str | Path, filename: str) -> Optional[Path]:
    """Copies a key generated file back to the output root for older GUI actions/scripts.

    The organized folder remains the canonical location; the root copy is only
    a compatibility shortcut so buttons and users can find the file easily.
    """
    out_dir = Path(out_dir)
    found = find_result_file(out_dir, filename)
    if found is None:
        return None
    target = out_dir / filename
    try:
        if found.resolve() != target.resolve():
            shutil.copy2(found, target)
        return target
    except Exception:
        return found



def phase_reliability_metrics(
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    candidate: Candidate,
    cfg: OptimizerConfig,
) -> Dict[str, float]:
    sums = candidate_sums(freq, responses, candidate)
    l_sum = sums["L_sum"]
    r_sum = sums["R_sum"]
    lr_sum_db = db(sums["LR_sum"])
    phase_diff = wrap_phase_deg(np.rad2deg(np.unwrap(np.angle(l_sum)) - np.unwrap(np.angle(r_sum))))
    m_eval = band_mask(freq, cfg.eval_low_hz, cfg.eval_high_hz)
    if not np.any(m_eval):
        return {"phase_lr_mean_abs_eval": float("nan"), "phase_lr_std_eval": float("nan"), "phase_reliability_fraction": 0.0}
    eval_lr = lr_sum_db[m_eval]
    # Odrzucamy punkty dużo poniżej mediany L+R, bo faza w głębokich nullach jest mało wiarygodna.
    threshold = float(np.nanmedian(eval_lr) - 12.0)
    reliable = m_eval & (lr_sum_db >= threshold)
    if not np.any(reliable):
        reliable = m_eval
    ph = phase_diff[reliable]
    return {
        "phase_lr_mean_abs_eval": float(np.nanmean(np.abs(ph))),
        "phase_lr_std_eval": float(np.nanstd(ph)),
        "phase_reliability_fraction": float(np.sum(reliable) / max(1, np.sum(m_eval))),
    }





def plot_candidate_difference(
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    c_a: Candidate,
    c_b: Candidate,
    out_prefix: str | Path,
    label_a: str = "A",
    label_b: str = "B",
    cfg: Optional[OptimizerConfig] = None,
    l_base_delay_ms: float = 0.0,
    r_base_delay_ms: float = 0.0,
) -> None:
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_prefix = Path(out_prefix)
    a = candidate_sums(freq, responses, c_a)
    b = candidate_sums(freq, responses, c_b)
    lr_a = db(a["LR_sum"])
    lr_b = db(b["LR_sum"])
    diff = lr_a - lr_b
    a_ctx = _chart_settings_context(c_a, l_base_delay_ms, r_base_delay_ms, cfg)
    b_ctx = _chart_settings_context(c_b, l_base_delay_ms, r_base_delay_ms, cfg)

    with (Path(str(out_prefix) + "_diff.csv")).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frequency_hz", f"{label_a}_LR_db", f"{label_b}_LR_db", "diff_A_minus_B_db"])
        for i, fr in enumerate(freq):
            writer.writerow([f"{fr:.6f}", f"{lr_a[i]:.4f}", f"{lr_b[i]:.4f}", f"{diff[i]:.4f}"])

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.semilogx(freq, diff, linewidth=2, label=f"{label_a} − {label_b}")
    ax.axhline(0, linewidth=1)
    if cfg is not None:
        ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12, label="pasmo oceny integracji")
        ax.axvspan(cfg.bulge_low_hz, cfg.bulge_high_hz, alpha=0.08, label="kontrola nadmiaru basu")
    ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("Różnica przewidywanej sumy L+R [dB]")
    ax.set_title("Różnica przewidywanej sumy stereo L+R między kandydatami")
    ax.text(
        0.01, 0.02,
        f"{label_a}: {a_ctx['main']}; {a_ctx['sub']}; {a_ctx['rel']}\n"
        f"{label_b}: {b_ctx['main']}; {b_ctx['sub']}; {b_ctx['rel']}",
        transform=ax.transAxes, fontsize=8, va="bottom", ha="left",
        bbox=dict(boxstyle="round,pad=0.35", alpha=0.78),
    )
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, str(out_prefix) + "_diff.png", dpi=160)
    plt.close(fig)



def candidate_setting_text(
    c: Candidate,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    cfg: Optional[OptimizerConfig] = None,
) -> str:
    """Kompatybilny, krótki opis kandydata z pełnymi wartościami SHD."""
    m = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
    return (
        f"main L/R {float(m['l_main_ms']):.2f}/{float(m['r_main_ms']):.2f} ms | "
        f"sub L/R {float(m['l_sub_ms']):.2f}/{float(m['r_sub_ms']):.2f} ms {c.l_polarity}/{c.r_polarity} | "
        f"rel. sub-main {float(m['l_sub_minus_main_ms']):+.2f}/{float(m['r_sub_minus_main_ms']):+.2f} ms"
    )


def dirac_project_status_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
    validation_status: Optional[str] = None,
) -> str:
    """v19.44: prosty status przed uruchomieniem Dirac."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    lines: List[str] = ["# Czy można robić Dirac?\n\n"]
    if best is None:
        lines.append("**NIE.** " + note + "\n")
        return "".join(lines)
    status = (validation_status or "").strip().upper()
    if status.startswith("POTWIERDZONY"):
        lines.append("**TAK.** Wynik został potwierdzony pomiarem REW.\n\n")
    else:
        lines.append("**JESZCZE NIE.** Najpierw zrób pomiar kontrolny w REW.\n\n")
    for line in shd_settings_lines(best, l_base_delay_ms, r_base_delay_ms, cfg, indent=""):
        lines.append(line + "\n\n")
    lines.append("Sprawdź L main + L sub, R main + R sub oraz L + R razem.\n")
    return "".join(lines)




def workflow_assistant_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    cfg: OptimizerConfig,
    filter_type: str,
    slope_db_oct: int,
) -> str:
    """Krótka instrukcja dalszych działań z pełnym zestawem czterech delayów SHD."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    lines: List[str] = ["# Co zrobić dalej\n\n"]
    if best is None:
        lines.append(note + "\n")
        return "".join(lines)
    lines.append("1. Wpisz do SHD pełny zestaw czterech wartości:\n")
    for line in shd_settings_lines(best, l_base_delay_ms, r_base_delay_ms, cfg, indent="   - "):
        lines.append(line + "\n")
    lines.append("2. Zrób pomiar kontrolny w REW.\n")
    lines.append("3. Sprawdź, czy nie ma szerokiej dziury około 60–90 Hz i zbyt grubego basu około 90–160 Hz.\n")
    lines.append("4. Przy zwykłym porównaniu slotów przejdź do Diraca i POST-DIRAC; PRE-DIRAC rób tylko dla podejrzanych topologii.\n")
    return "".join(lines)



def rew_measurement_checklist_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: OptimizerConfig,
) -> str:
    """Prosta checklista pomiarów REW — pełne ustawienia SHD dla OFF/OFF."""
    plan = unique_measurement_candidates(candidates, limit=3)
    lines: List[str] = ["# Checklista pomiarów REW\n\n"]
    if not plan:
        lines.append("Brak wariantu OFF/OFF.\n")
        return "".join(lines)
    labels = ["A", "B", "C"]
    for i, (c, _reason) in enumerate(plan):
        lines.append(f"{labels[i]}: {_candidate_operational_row(c, l_base_delay_ms, r_base_delay_ms, cfg)}\n")
    lines.append("\nDla wariantu A zmierz:\n")
    lines.append("1. L main + L sub.\n")
    lines.append("2. R main + R sub.\n")
    lines.append("3. L + R razem.\n")
    lines.append("\nJeżeli wynik jest niejasny, porównaj wariant B.\n")
    return "".join(lines)



def listening_ab_scorecard_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    cfg: Optional[OptimizerConfig] = None,
) -> str:
    """Prosta karta odsłuchu OFF/OFF z kompletem delayów SHD."""
    if cfg is None:
        cfg = OptimizerConfig()
    rows = unique_measurement_candidates(candidates, limit=2)
    lines: List[str] = ["# Karta odsłuchu A/B\n\n"]
    for i, (c, _reason) in enumerate(rows):
        label = "A" if i == 0 else "B"
        lines.append(f"{label}: {_candidate_operational_row(c, l_base_delay_ms, r_base_delay_ms, cfg)}\n")
    lines.append("\nOceń: zwartość stopy, kontur kontrabasu, naturalność wokalu i stabilność sceny.\n")
    return "".join(lines)


def _simple_quality_label(c: Candidate) -> str:
    score = float(getattr(c, "pbis_safe_score", getattr(c, "pbis_score", 0.0)))
    if score >= 85.0:
        return "bardzo dobra"
    if score >= 75.0:
        return "dobra"
    if score >= 65.0:
        return "średnia — sprawdź dokładnie pomiarem"
    return "słaba — wynik wymaga ostrożności"


def simple_quality_label(c: Candidate) -> str:
    """Publiczny helper dla GUI/CLI."""
    return _simple_quality_label(c)


def simple_validation_label(value: str) -> str:
    mapping = {
        "HIGH": "dobra",
        "MEDIUM": "średnia — sprawdź pomiarem",
        "LOW": "niska — potrzebne dodatkowe sprawdzenie",
        "DISABLED": "pominięta",
    }
    return mapping.get(str(value or "").upper(), str(value or "-").lower())


def simple_rescan_label(value: str) -> str:
    mapping = {
        "SKIPPED": "nie było potrzebne",
        "DISABLED": "pominięte",
        "CONFIRMED": "wynik potwierdzony",
        "REFINED": "wynik doprecyzowany",
        "IMPROVED": "wynik poprawiony",
        "ERROR": "nie udało się wykonać",
    }
    return mapping.get(str(value or "").upper(), str(value or "-").lower())


def _off_off_candidates(candidates: List[Candidate], limit: int = 3) -> List[Candidate]:
    rows = [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"]
    rows.sort(key=lambda c: float(getattr(c, "score", 0.0)), reverse=True)
    unique: List[Candidate] = []
    for c in rows:
        if not any(same_candidate(c, prev) for prev in unique):
            unique.append(c)
        if len(unique) >= max(1, int(limit)):
            break
    return unique


def off_off_candidates(candidates: List[Candidate], limit: int = 3) -> List[Candidate]:
    """Publiczny helper dla GUI/CLI: najlepsze unikalne warianty OFF/OFF."""
    return _off_off_candidates(candidates, limit=limit)


def simple_off_off_summary_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str = "Custom",
    filter_type: str = "BW",
    slope_db_oct: int = 48,
    cfg: Optional[OptimizerConfig] = None,
) -> str:
    """Czytelny komunikat do panelu wyników GUI."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    if best is None:
        return "Nie znaleziono wariantu OFF/OFF. Sprawdź dane wejściowe i uruchom obliczenia ponownie."
    if cfg is None:
        cfg = OptimizerConfig()
    lines = [
        f"REKOMENDACJA OFF/OFF DO WPISANIA W SHD ({profile_label(crossover_label, filter_type, int(slope_db_oct))}):",
        *shd_settings_lines(best, l_base_delay_ms, r_base_delay_ms, cfg, indent="  "),
        f"  PBIS: {float(getattr(best, 'pbis_score', 0.0)):.1f}/100 — {getattr(best, 'pbis_label', '')}",
        "  Co może wymagać uwagi:",
        pbis_risk_user_block(best, indent="    "),
        "",
        spatial_coherence_candidate_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg),
        "",
        "Następny krok: dla zwykłego porównania slotów przejdź do Diraca i POST-DIRAC. PRE-DIRAC rób tylko dla nowej albo podejrzanej topologii.",
    ]
    return "\n".join(lines)


def minidsp_final_settings_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: OptimizerConfig,
) -> str:
    """v19.44: główny, czytelny raport OFF/OFF do SHD."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    lines: List[str] = []
    if best is None:
        return note + "\n"
    lines.append(practical_recommendation_block(
        candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg, filter_type, slope_db_oct
    ))
    lines.append("\n\nCO ZROBIĆ DALEJ:\n")
    lines.append("1. Wpisz rekomendację główną OFF/OFF do SHD.\n")
    lines.append("2. Zrób pomiar kontrolny w REW.\n")
    lines.append("3. Odsłuchaj również wskazany wariant spójności L/R przy XO i sprawdź naturalność relacji przestrzennych.\n")
    lines.append("4. PRE-DIRAC jest opcjonalny; przy zwykłym porównaniu slotów przejdź do Diraca i POST-DIRAC.\n")
    return "".join(lines)


def _my_system_primary_candidates(candidates: List[Candidate]) -> List[Tuple[str, Candidate]]:
    """v19.44: maksymalnie trzy warianty OFF/OFF do prostego testu A/B."""
    offs = _off_off_candidates(candidates, limit=3)
    labels = ["A — główny", "B — zapasowy", "C — dodatkowy"]
    return [(labels[i], c) for i, c in enumerate(offs)]



def _candidate_operational_row(c: Candidate, l_base_delay_ms: float, r_base_delay_ms: float, cfg: Optional[OptimizerConfig] = None) -> str:
    m = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
    return (
        f"main L/R {float(m['l_main_ms']):.2f}/{float(m['r_main_ms']):.2f} ms; "
        f"sub L/R {float(m['l_sub_ms']):.2f}/{float(m['r_sub_ms']):.2f} ms OFF/OFF; "
        f"rel. sub-main {float(m['l_sub_minus_main_ms']):+.2f}/{float(m['r_sub_minus_main_ms']):+.2f} ms; "
        f"ocena {_simple_quality_label(c)}"
    )



def my_system_minidsp_steps_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: OptimizerConfig,
) -> str:
    """v19.44: czytelny komunikat — co wpisać do SHD i co porównać."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    if best is None:
        return note + "\n"
    return practical_recommendation_block(
        candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg, filter_type, slope_db_oct
    ) + "\n\nNastępny krok: zrób pomiar kontrolny w REW. Jeszcze nie uruchamiaj Dirac.\n"


def my_system_rew_measurement_steps_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: OptimizerConfig,
) -> str:
    """v19.44: prosta instrukcja pomiaru kontrolnego."""
    lines: List[str] = ["02 — ZRÓB POMIAR W REW\n", "=====================\n\n"]
    rows = _my_system_primary_candidates(candidates)
    if not rows:
        lines.append("Brak wariantu OFF/OFF. Najpierw uruchom optymalizację.\n")
        return "".join(lines)
    lines.append("Najpierw sprawdź wariant A.\n\n")
    for label, c in rows:
        lines.append(f"{label}: {_candidate_operational_row(c, l_base_delay_ms, r_base_delay_ms, cfg)}\n")
    lines.append("\nDla wariantu A zmierz:\n")
    lines.append("1. L main + L sub.\n")
    lines.append("2. R main + R sub.\n")
    lines.append("3. L + R razem.\n")
    lines.append("4. Opcjonalnie: oba suby razem.\n\n")
    lines.append("Sprawdź, czy nie ma szerokiej dziury około 60–90 Hz i czy bas nie jest za gruby około 90–160 Hz.\n")
    lines.append("Jeżeli wynik jest niejasny, porównaj wariant B.\n")
    return "".join(lines)



def my_system_dirac_ready_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: OptimizerConfig,
) -> str:
    """v19.44: prosta checklista przed Dirac."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    lines: List[str] = ["03 — CZY MOŻNA ROBIĆ DIRAC?\n", "==========================\n\n"]
    if best is None:
        lines.append("NIE. " + note + "\n")
        return "".join(lines)
    lines.append("Jeszcze nie. Najpierw sprawdź wynik pomiarem REW.\n\n")
    lines.append("Przed uruchomieniem Dirac potwierdź:\n")
    lines.append("[ ] Wpisałem delaye OFF/OFF do SHD.\n")
    lines.append("[ ] Zmierzyłem L main + L sub.\n")
    lines.append("[ ] Zmierzyłem R main + R sub.\n")
    lines.append("[ ] Zmierzyłem L + R razem.\n")
    lines.append("[ ] Nie ma szerokiej dziury około 60–90 Hz.\n")
    lines.append("[ ] Bas nie jest za gruby około 90–160 Hz.\n\n")
    lines.append("Jeżeli wszystkie punkty są spełnione, możesz uruchomić Dirac.\n")
    return "".join(lines)



def my_system_ab_listening_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str = "Custom",
    cfg: Optional[OptimizerConfig] = None,
) -> str:
    """v19.44: prosty plan odsłuchu OFF/OFF z kandydatem przestrzennym."""
    if cfg is None:
        cfg = OptimizerConfig()
    lines: List[str] = ["04 — ODSŁUCH A/B\n", "===============\n\n"]
    best, _policy, note = off_off_priority_main_candidate(candidates)
    if best is None:
        return "".join(lines) + note + "\n"
    alt, _reason = select_practical_candidate(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg)
    lines.append("A — rekomendacja główna:\n")
    lines.append(f"{_candidate_operational_row(best, l_base_delay_ms, r_base_delay_ms, cfg)}\n")
    if alt is not None and not same_candidate(alt, best):
        loss = float(best.score) - float(alt.score)
        lines.append("\nB — podobne ustawienie do porównania:\n")
        lines.append(f"{_candidate_operational_row(alt, l_base_delay_ms, r_base_delay_ms, cfg)}\n")
        lines.append(f"Różnica względem A: -{loss:.2f} pkt. To nadal sensowny wariant do odsłuchu.\n")
    lines.append("\n")
    lines.append(spatial_coherence_candidate_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg))
    lines.append("\n\nSłuchaj przede wszystkim:\n")
    lines.append("- czy stopa jest szybka i zwarta;\n")
    lines.append("- czy kontrabas nie dudni;\n")
    lines.append("- czy wokal nie robi się zbyt gruby;\n")
    lines.append("- czy scena jest stabilna, a instrumenty mają czytelne miejsca.\n")
    return "".join(lines)


def my_system_workflow_index_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: OptimizerConfig,
) -> str:
    """v19.44: prosty indeks kolejnych kroków."""
    lines: List[str] = ["# CO ZROBIĆ DALEJ\n\n"]
    lines.append("1. Wpisz ustawienia OFF/OFF do SHD.\n")
    lines.append("2. Zrób pomiar kontrolny w REW.\n")
    lines.append("3. Sprawdź checklistę przed Dirac.\n")
    lines.append("4. Jeżeli trzeba, porównaj dwa warianty OFF/OFF odsłuchem A/B.\n\n")
    best, _policy, note = off_off_priority_main_candidate(candidates)
    if best is None:
        lines.append(note + "\n")
    else:
        lines.append("## Wynik\n\n")
        for line in shd_settings_lines(best, l_base_delay_ms, r_base_delay_ms, cfg, indent="- "):
            lines.append(line + "\n")
        lines.append(f"- Ocena: {_simple_quality_label(best)}\n")
    return "".join(lines)


def dirac_curtain_simple_verdict(metrics: Dict[str, float], verdict: str, cfg: OptimizerConfig, curtain_high_hz: float) -> str:
    """v18.7: uproszczony komunikat Dirac Curtains."""
    safe = bool(getattr(cfg, "safe_midrange_priority", True))
    lines: List[str] = ["# Prosty werdykt Dirac Curtains\n\n"]
    lines.append(f"Kurtyna: **{curtain_high_hz:.0f} Hz**\n\n")
    lines.append(f"Werdykt techniczny: **{verdict}**\n\n")
    score = float(metrics.get("curtain_safety_score", float("nan")))
    dip = float(metrics.get("curtain_dip_db", float("nan")))
    rough = float(metrics.get("curtain_roughness_db", float("nan")))
    bad = float(metrics.get("curtain_bad_fraction_below_minus3db", float("nan"))) * 100.0
    lines.append("## Co to znaczy praktycznie\n\n")
    if verdict == "OK":
        lines.append("- Kurtyna wygląda pomiarowo bezpiecznie.\n")
    elif verdict == "RYZYKO":
        lines.append("- Kurtyna wymaga porównania z niższą kurtyną i odsłuchu.\n")
    else:
        lines.append("- Kurtyna wygląda problematycznie; zacznij od niższej.\n")
    if safe:
        if curtain_high_hz >= 300:
            lines.append("- Tryb bezpieczny dla średnicy: 320 Hz traktuj kontrolnie, nie jako domyślne ustawienie.\n")
        elif curtain_high_hz <= 250:
            lines.append("- Tryb bezpieczny dla średnicy: ta kurtyna jest dobrym kandydatem startowym.\n")
        lines.append("- Zakres 135–240 Hz: nie naprawiaj agresywnie targetem ani delayem, jeśli są tam nulle.\n")
    lines.append("\n## Najważniejsze liczby\n\n")
    lines.append(f"- Score bezpieczeństwa: {score:.2f}\n")
    lines.append(f"- Dołek w strefie kurtyny: {dip:.2f} dB\n")
    lines.append(f"- Chropowatość / roughness: {rough:.2f} dB\n")
    lines.append(f"- Udział punktów L+R poniżej -3 dB względem mocniejszego kanału: {bad:.1f}%\n\n")
    lines.append("## Odsłuch kontrolny\n\n")
    lines.append("Sprawdź wokal, fortepian, kontrabas i czy niższa średnica nie robi się chuda albo sztucznie wygładzona.\n")
    return "".join(lines)




def pbis_summary_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: OptimizerConfig,
) -> str:
    """v19.44: uproszczone zestawienie wariantów OFF/OFF."""
    lines = ["# Ocena wariantów OFF/OFF\n\n"]
    rows = _off_off_candidates(candidates, limit=10)
    if not rows:
        lines.append("Brak wariantu OFF/OFF.\n")
        return "".join(lines)
    lines.append("Wyższa ocena oznacza bezpieczniejszy punkt startowy do pomiaru REW.\n\n")
    lines.append("| # | main L/R | sub L/R | rel. sub-main L/R | Ocena |\n")
    lines.append("|---:|---:|---:|---:|---|\n")
    for i, c in enumerate(rows, start=1):
        m = candidate_shd_settings(c, l_base_delay_ms, r_base_delay_ms, cfg)
        lines.append(
            f"| {i} | {float(m['l_main_ms']):.2f}/{float(m['r_main_ms']):.2f} ms | "
            f"{float(m['l_sub_ms']):.2f}/{float(m['r_sub_ms']):.2f} ms | "
            f"{float(m['l_sub_minus_main_ms']):+.2f}/{float(m['r_sub_minus_main_ms']):+.2f} ms | {_simple_quality_label(c)} |\n"
        )
    return "".join(lines)



def sound_quality_safe_summary_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    cfg: OptimizerConfig,
) -> str:
    """v19.44: prosty opis ostrożnej oceny wariantu OFF/OFF."""
    best, _policy, note = off_off_priority_main_candidate(candidates)
    lines = ["# Ostrożna ocena wyniku OFF/OFF\n\n"]
    if best is None:
        lines.append(note + "\n")
        return "".join(lines)
    mapped = candidate_shd_settings(best, l_base_delay_ms, r_base_delay_ms, cfg)
    lines.append(f"main L/R: {float(mapped['l_main_ms']):.2f}/{float(mapped['r_main_ms']):.2f} ms\n\n")
    lines.append(f"sub L/R: {float(mapped['l_sub_ms']):.2f}/{float(mapped['r_sub_ms']):.2f} ms, OFF/OFF\n\n")
    lines.append(f"rel. sub-main L/R: {float(mapped['l_sub_minus_main_ms']):+.2f}/{float(mapped['r_sub_minus_main_ms']):+.2f} ms\n\n")
    lines.append(f"Ocena: {_simple_quality_label(best)}\n\n")
    lines.append("To jest punkt startowy. Potwierdź go pomiarem REW przed uruchomieniem Dirac.\n")
    return "".join(lines)



def ab_listening_plan_block(
    candidates: List[Candidate],
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    cfg: Optional[OptimizerConfig] = None,
) -> str:
    """v19.50: prosty plan odsłuchu dwóch najlepszych wariantów OFF/OFF z pełnymi delayami SHD."""
    if cfg is None:
        cfg = OptimizerConfig()
    rows = _off_off_candidates(candidates, limit=2)
    lines = ["# Plan odsłuchu A/B — OFF/OFF\n\n"]
    if not rows:
        lines.append("Brak wariantu OFF/OFF.\n")
        return "".join(lines)
    labels = ["A — główny", "B — zapasowy"]
    for i, c in enumerate(rows):
        lines.append(f"- {labels[i]}: {_candidate_operational_row(c, l_base_delay_ms, r_base_delay_ms, cfg)}\n")
    lines.append("\nWybierz wariant z bardziej zwartym basem, naturalniejszym wokalem i stabilniejszą sceną.\n")
    return "".join(lines)


def write_workflow_outputs(
    candidates: List[Candidate],
    out_dir: str | Path,
    cfg: OptimizerConfig,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
) -> List[str]:
    """v18.7: pliki użytkowe do prowadzenia workflow."""
    out_dir = Path(out_dir)
    written: List[str] = []
    files = {
        "MY_SYSTEM_WORKFLOW.md": my_system_workflow_index_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg),
        "01_WPISZ_DO_MINIDSP.txt": my_system_minidsp_steps_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg),
        "02_ZMIERZ_W_REW.txt": my_system_rew_measurement_steps_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg),
        "03_CZY_MOZNA_ROBIC_DIRAC.txt": my_system_dirac_ready_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg),
        "04_ODSLUCH_AB.txt": my_system_ab_listening_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg),
        "05_POLITYKA_POLARYZACJI.txt": off_off_priority_summary_block(candidates, l_base_delay_ms, r_base_delay_ms, cfg),
        "WORKFLOW_ASSISTANT.md": workflow_assistant_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg, filter_type, slope_db_oct),
        "PROJECT_STATUS.md": dirac_project_status_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg),
        "REW_MEASUREMENT_CHECKLIST.md": rew_measurement_checklist_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg),
        "LISTENING_AB_SCORECARD.md": listening_ab_scorecard_block(candidates, l_base_delay_ms, r_base_delay_ms, cfg),
        "MINIDSP_SETTINGS_FINAL.txt": minidsp_final_settings_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg),
        "PBIS_SUMMARY.md": pbis_summary_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg),
        "SOUND_QUALITY_SAFE_SUMMARY.md": sound_quality_safe_summary_block(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, filter_type, slope_db_oct, cfg),
        "AB_LISTENING_PLAN.md": ab_listening_plan_block(candidates, l_base_delay_ms, r_base_delay_ms, cfg),
    }
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8")
        written.append(name)
    return written


def create_followup_outputs(
    freq: np.ndarray,
    responses: Dict[str, np.ndarray],
    candidates: List[Candidate],
    out_dir: str | Path,
    cfg: OptimizerConfig,
    l_base_delay_ms: float,
    r_base_delay_ms: float,
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    organize_output: bool = True,
) -> List[str]:
    """
    Tworzy dodatkowe pliki po optymalizacji:
    - plan pomiaru top 3,
    - predykcję krzywych dla najlepszego wyniku,
    - wykresy różnicy: best vs #2 oraz best vs OFF/OFF.
    """
    out_dir = Path(out_dir)
    messages: List[str] = []
    if not candidates:
        return messages
    operational, _policy, _note = off_off_priority_main_candidate(candidates)
    if operational is None:
        return messages
    off_candidates = [c for c in candidates if c.l_polarity == "OFF" and c.r_polarity == "OFF"]
    off_candidates.sort(key=lambda c: float(c.score), reverse=True)

    write_measurement_plan(
        candidates,
        out_dir / "measurement_plan_AB.md",
        l_base_delay_ms,
        r_base_delay_ms,
        crossover_label,
        filter_type,
        slope_db_oct,
        cfg,
    )
    pass

    (out_dir / "simple_verdict.md").write_text(
        simple_verdict_block(
            candidates,
            l_base_delay_ms,
            r_base_delay_ms,
            crossover_label,
            cfg,
            filter_type,
            slope_db_oct,
        ),
        encoding="utf-8",
    )
    pass

    workflow_files = write_workflow_outputs(
        candidates,
        out_dir,
        cfg,
        l_base_delay_ms,
        r_base_delay_ms,
        crossover_label,
        filter_type,
        slope_db_oct,
    )
    pass

    save_predicted_curves(freq, responses, operational, out_dir / "best_predicted_curves.csv")
    pass

    best_time = save_time_alignment_diagnostics(
        freq, responses, operational, out_dir / "best", cfg,
        l_base_delay_ms, r_base_delay_ms, label="rekomendacja OFF/OFF"
    )
    append_time_alignment_to_report(out_dir / "raport.md", "rekomendacja OFF/OFF", best_time)
    pass

    plateau_alt, _ = select_practical_candidate(candidates, l_base_delay_ms, r_base_delay_ms, crossover_label, cfg)
    if plateau_alt is not None and not same_candidate(plateau_alt, operational):
        save_predicted_curves(freq, responses, plateau_alt, out_dir / "plateau_alt_predicted_curves.csv")
        plot_candidate(freq, responses, plateau_alt, out_dir / "plateau_alt", cfg, l_base_delay_ms, r_base_delay_ms, plot_label="Alternatywa plateau A/B")
        save_time_alignment_diagnostics(
            freq, responses, plateau_alt, out_dir / "plateau_alt", cfg,
            l_base_delay_ms, r_base_delay_ms, label="alternatywa plateau A/B"
        )
        pass

    write_candidate_clusters(
        candidates,
        out_dir / "candidate_clusters",
        l_base_delay_ms,
        r_base_delay_ms,
        crossover_label,
        cfg,
    )
    pass

    plot_delay_score_heatmap(
        candidates,
        out_dir / "delay_score_heatmap.png",
        l_base_delay_ms,
        r_base_delay_ms,
        crossover_label,
        cfg,
    )

    plot_pbis_score_heatmap(
        candidates,
        out_dir / "pbis_score_heatmap.png",
        l_base_delay_ms,
        r_base_delay_ms,
        crossover_label,
        cfg,
    )

    plot_fast_rescan_windows(
        candidates,
        out_dir / "fast_rescan_windows.png",
        l_base_delay_ms,
        r_base_delay_ms,
        crossover_label,
        cfg,
    )
    _write_heatmap_cleanup_note(out_dir)

    # Metryki fazy L/R z maskowaniem głębokich nulli.
    phase_metrics = phase_reliability_metrics(freq, responses, operational, cfg)
    with (out_dir / "phase_reliability.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for k, v in phase_metrics.items():
            writer.writerow([k, f"{float(v):.4f}"])

    if len(off_candidates) > 1:
        plot_candidate_difference(
            freq, responses, operational, off_candidates[1],
            out_dir / "diff_best_vs_2", "główny OFF/OFF", "zapasowy OFF/OFF", cfg, l_base_delay_ms, r_base_delay_ms
        )

    if organize_output:
        moved = organize_results_folder(out_dir)
        if moved:
            pass

    if "Wynik jest gotowy." not in messages:
        messages.append("Wynik jest gotowy.")
    return messages


def validate_dirac_measurements(
    l_path: str | Path,
    r_path: str | Path,
    lr_path: str | Path,
    out_dir: str | Path,
    cfg: OptimizerConfig,
    label: str = "Dirac ON",
) -> Tuple[str, Dict[str, float]]:
    """
    Waliduje pomiary L, R, L+R po Dirac. Szuka problemu:
    L osobno OK, R osobno OK, ale L+R się kasuje.
    """
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_dir = prepare_flat_module_folder(
        out_dir,
        stale_patterns=[
            "dirac_validation*.*",
            "OTWORZ_NAJPIERW.md",
            "README_DIRAC_VALIDATION.md",
        ],
    )

    l_m = parse_rew_text(l_path, "L_Dirac_ON")
    r_m = parse_rew_text(r_path, "R_Dirac_ON")
    lr_m = parse_rew_text(lr_path, "LR_Dirac_ON")
    freq = common_grid([l_m, r_m, lr_m], 1400)

    l_db = interp_spl_to_grid(l_m, freq)
    r_db = interp_spl_to_grid(r_m, freq)
    lr_db = interp_spl_to_grid(lr_m, freq)
    mask = band_mask(freq, cfg.eval_low_hz, cfg.eval_high_hz)
    if not np.any(mask):
        raise ValueError("Pasmo oceny nie pokrywa się z zakresem danych Dirac.")

    max_lr = np.maximum(l_db, r_db)
    margin = lr_db[mask] - max_lr[mask]
    mean_margin = float(np.nanmean(margin))
    min_margin = float(np.nanmin(margin))
    dip = float(np.nanmedian(lr_db[mask]) - np.nanmin(lr_db[mask]))
    bad_fraction = float(np.nanmean(margin < -3.0))

    if mean_margin < -1.0 or min_margin < -10.0 or bad_fraction > 0.35:
        verdict = "ODRZUCIĆ"
    elif mean_margin < 1.0 or min_margin < -6.0 or bad_fraction > 0.18 or dip > 8.0:
        verdict = "RYZYKOWNY"
    else:
        verdict = "OK"

    metrics = {
        "mean_margin_db": mean_margin,
        "min_margin_db": min_margin,
        "dip_db": dip,
        "bad_fraction_below_minus3db": bad_fraction,
    }

    report = []
    report.append(f"# Walidacja Dirac — {label}\n\n")
    report.append(f"## Werdykt: **{verdict}**\n\n")
    report.append(f"- Pasmo oceny: {cfg.eval_low_hz}–{cfg.eval_high_hz} Hz\n")
    report.append(f"- Średni margines L+R względem mocniejszego kanału: {mean_margin:.2f} dB\n")
    report.append(f"- Najgorszy margines: {min_margin:.2f} dB\n")
    report.append(f"- Głębokość dołka L+R: {dip:.2f} dB\n")
    report.append(f"- Udział punktów, gdzie L+R jest >3 dB poniżej mocniejszego kanału: {bad_fraction*100:.1f}%\n\n")
    if verdict == "OK":
        report.append("Interpretacja: L+R po Dirac nie wygląda na destrukcyjnie kasujące w paśmie oceny.\n")
    elif verdict == "RYZYKOWNY":
        report.append("Interpretacja: są sygnały ryzyka. Zmierz/porównaj z inną kurtyną albo innym presetem.\n")
    else:
        report.append("Interpretacja: typowy objaw kasowania L+R. Nie naprawiaj tego targetem — odrzuć filtr albo zmień bazę/delay.\n")
    (out_dir / "dirac_validation.md").write_text("".join(report), encoding="utf-8")

    with (out_dir / "dirac_validation.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frequency_hz", "L_db", "R_db", "LR_db", "LR_minus_max_LR_db"])
        for i, fr in enumerate(freq):
            writer.writerow([f"{fr:.6f}", f"{l_db[i]:.4f}", f"{r_db[i]:.4f}", f"{lr_db[i]:.4f}", f"{(lr_db[i]-max_lr[i]):.4f}"])

    fig, ax = plt.subplots(figsize=(12, 7))
    ax.semilogx(freq, l_db, label="L Dirac ON")
    ax.semilogx(freq, r_db, label="R Dirac ON")
    ax.semilogx(freq, lr_db, linewidth=2.2, label="L+R Dirac ON")
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12, label="pasmo oceny")
    ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("SPL [dB]")
    ax.set_title(f"Walidacja Dirac — {verdict}")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_dir / "dirac_validation.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.semilogx(freq, lr_db - max_lr, label="L+R − max(L,R)")
    ax.axhline(0, linewidth=1)
    ax.axhline(-3, linestyle=":", linewidth=1)
    ax.axhline(-6, linestyle=":", linewidth=1)
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12)
    ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("Margines [dB]")
    ax.set_title("Czy L+R kasuje się względem pojedynczych kanałów?")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_dir / "dirac_validation_margin.png", dpi=160)
    plt.close(fig)

    write_dirac_validation_readme(out_dir, metrics, verdict, label)
    return verdict, metrics


def parse_frequency_bands(value: str | None) -> List[Tuple[float, float]]:
    """
    Parsuje listę pasm typu "160-240, 300:340" do listy krotek.
    Pasma są używane w v18 jako strefy "nie goń delayem" przy analizie kurtyny Dirac.
    """
    bands: List[Tuple[float, float]] = []
    if not value:
        return bands
    for chunk in re.split(r"[,;]+", str(value)):
        chunk = chunk.strip().replace(",", ".")
        if not chunk:
            continue
        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:-|:|–|—|do)\s*([0-9]+(?:\.[0-9]+)?)\s*$", chunk, re.IGNORECASE)
        if not m:
            continue
        a = float(m.group(1))
        b = float(m.group(2))
        if a > b:
            a, b = b, a
        bands.append((a, b))
    return bands


def mask_without_ignored_bands(
    freq: np.ndarray,
    low_hz: float,
    high_hz: float,
    ignore_bands: Optional[List[Tuple[float, float]]] = None,
) -> np.ndarray:
    mask = band_mask(freq, low_hz, high_hz)
    for lo, hi in (ignore_bands or []):
        mask &= ~band_mask(freq, lo, hi)
    return mask


def _dirac_band_metrics(
    freq: np.ndarray,
    l_db: np.ndarray,
    r_db: np.ndarray,
    lr_db: np.ndarray,
    mask: np.ndarray,
) -> Dict[str, float]:
    if not np.any(mask):
        return {
            "mean_margin_db": float("nan"),
            "min_margin_db": float("nan"),
            "p05_margin_db": float("nan"),
            "dip_db": float("nan"),
            "std_db": float("nan"),
            "roughness_db": float("nan"),
            "bad_fraction_below_minus3db": float("nan"),
            "mean_lr_db": float("nan"),
        }
    max_lr = np.maximum(l_db, r_db)
    margin = lr_db[mask] - max_lr[mask]
    return {
        "mean_margin_db": float(np.nanmean(margin)),
        "min_margin_db": float(np.nanmin(margin)),
        "p05_margin_db": float(np.nanpercentile(margin, 5)),
        "dip_db": float(dip_depth(lr_db[mask])),
        "std_db": float(np.nanstd(lr_db[mask])),
        "roughness_db": float(roughness(lr_db[mask])),
        "bad_fraction_below_minus3db": float(np.nanmean(margin < -3.0)),
        "mean_lr_db": float(np.nanmean(lr_db[mask])),
    }


def dirac_time_risk_metrics(
    freq: np.ndarray,
    l_response: np.ndarray,
    r_response: np.ndarray,
    lr_response: np.ndarray,
    low_hz: float,
    high_hz: float,
) -> Dict[str, float]:
    """
    v18.6: diagnostyka czasowa po Dirac w strefie kurtyny.
    """
    metrics: Dict[str, float] = {
        "time_low_hz": float(low_hz),
        "time_high_hz": float(high_hz),
    }
    for name, resp in [("L", l_response), ("R", r_response), ("LR", lr_response)]:
        gd = group_delay_summary(freq, resp, low_hz, high_hz)
        t, env = band_limited_impulse_envelope(freq, resp, low_hz, high_hz, fft_high_hz=max(320.0, high_hz * 1.5))
        imp = impulse_envelope_summary(t, env)
        for k, v in gd.items():
            metrics[f"{name}_{k}"] = float(v)
        for k, v in imp.items():
            metrics[f"{name}_{k}"] = float(v)
    return metrics


def dirac_time_risk_verdict(metrics: Dict[str, float]) -> Tuple[str, List[str]]:
    """
    v18.6: czas kurtyny Dirac raportowany jako prosta informacja.
    Nie używa alarmu "INFORMACYJNIE"; sugeruje tylko, czy warto porównać kurtyny.
    """
    reasons: List[str] = []
    p95 = metrics.get("LR_gd_p95_abs_excess_ms", float("nan"))
    width = metrics.get("LR_impulse_width80_ms", float("nan"))
    tail = metrics.get("LR_impulse_tail_ratio_35ms", float("nan"))

    notes = 0
    if np.isfinite(p95) and p95 > 22.0:
        notes += 1
        reasons.append(f"excess group delay L+R w strefie kurtyny jest podwyższony ({p95:.1f} ms p95)")
    elif np.isfinite(p95) and p95 > 14.0:
        notes += 1
        reasons.append(f"excess group delay L+R w strefie kurtyny jest umiarkowanie podwyższony ({p95:.1f} ms p95)")
    if np.isfinite(width):
        if width > 90.0:
            notes += 1
            reasons.append(f"obwiednia impulsu L+R w strefie kurtyny jest szeroka ({width:.1f} ms)")
        elif width > 60.0:
            notes += 1
            reasons.append(f"obwiednia impulsu L+R w strefie kurtyny jest umiarkowanie szeroka ({width:.1f} ms)")
    if np.isfinite(tail) and tail > 0.60:
        notes += 1
        reasons.append(f"ogon obwiedni po +35 ms jest podwyższony ({tail*100:.1f}%)")

    verdict = "INFORMACYJNIE" if notes else "OK"
    if not reasons:
        reasons.append("brak dodatkowych uwag czasowych w strefie kurtyny")
    reasons.append("traktuj to jako pomoc przy wyborze 200/250/320 Hz, nie jako samodzielny werdykt o jakości Dirac")
    return verdict, reasons


def plot_dirac_time_diagnostics(
    freq: np.ndarray,
    l_response: np.ndarray,
    r_response: np.ndarray,
    lr_response: np.ndarray,
    out_dir: str | Path,
    low_hz: float,
    high_hz: float,
    cfg: OptimizerConfig,
    label: str,
) -> Dict[str, float]:
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_dir = Path(out_dir)
    metrics = dirac_time_risk_metrics(freq, l_response, r_response, lr_response, low_hz, high_hz)
    verdict, reasons = dirac_time_risk_verdict(metrics)

    with (out_dir / "dirac_curtain_time_metrics.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for k in sorted(metrics.keys()):
            writer.writerow([k, f"{metrics[k]:.6f}" if np.isfinite(metrics[k]) else "nan"])
        writer.writerow(["time_risk_verdict", verdict])
        for i, reason in enumerate(reasons, start=1):
            writer.writerow([f"reason_{i}", reason])

    gd_l = complex_group_delay_ms(freq, l_response)
    gd_r = complex_group_delay_ms(freq, r_response)
    gd_lr = complex_group_delay_ms(freq, lr_response)
    with (out_dir / "dirac_curtain_group_delay.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frequency_hz", "L_gd_ms", "R_gd_ms", "LR_gd_ms"])
        for i, fr in enumerate(freq):
            writer.writerow([
                f"{fr:.6f}",
                f"{gd_l[i]:.5f}" if np.isfinite(gd_l[i]) else "",
                f"{gd_r[i]:.5f}" if np.isfinite(gd_r[i]) else "",
                f"{gd_lr[i]:.5f}" if np.isfinite(gd_lr[i]) else "",
            ])

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.semilogx(freq, gd_l, label="L Dirac ON")
    ax.semilogx(freq, gd_r, label="R Dirac ON")
    ax.semilogx(freq, gd_lr, linewidth=2.0, label="L+R Dirac ON")
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12, label="integracja")
    ax.axvspan(low_hz, high_hz, alpha=0.08, label="strefa kurtyny / czasu")
    ax.axhline(0, linewidth=1)
    ax.set_xlim(max(20, float(np.nanmin(freq))), min(max(300, high_hz * 1.1), float(np.nanmax(freq))))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("Opóźnienie grupowe [ms]")
    ax.set_title(f"Kurtyna Dirac — opóźnienie grupowe — {label} — {verdict}")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_dir / "dirac_curtain_group_delay.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5))
    for resp, lbl in [(l_response, "L"), (r_response, "R"), (lr_response, "L+R")]:
        t, env = band_limited_impulse_envelope(freq, resp, low_hz, high_hz, fft_high_hz=max(320.0, high_hz * 1.5))
        if t.size:
            mask = (t >= -140.0) & (t <= 180.0)
            ax.plot(t[mask], env[mask], label=lbl)
    ax.axvline(0, linewidth=1)
    ax.set_xlabel("Czas [ms]")
    ax.set_ylabel("Znormalizowana obwiednia")
    ax.set_title(f"Kurtyna Dirac — przybliżona obwiednia impulsu {low_hz:.0f}–{high_hz:.0f} Hz — {verdict}")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_dir / "dirac_curtain_impulse_envelope.png", dpi=160)
    plt.close(fig)

    return metrics



def _dirac_curtain_verdict(metrics: Dict[str, float]) -> Tuple[str, List[str]]:
    """
    Werdykt dla strefy od końca pasma integracji do górnej kurtyny Dirac.
    To jest kontrola ryzyka po Dirac, nie scoring do ustawiania delayu.
    """
    reasons: List[str] = []
    mean_margin = metrics.get("curtain_mean_margin_db", float("nan"))
    p05_margin = metrics.get("curtain_p05_margin_db", float("nan"))
    dip = metrics.get("curtain_dip_db", float("nan"))
    rough = metrics.get("curtain_roughness_db", float("nan"))
    bad_fraction = metrics.get("curtain_bad_fraction_below_minus3db", float("nan"))
    tonal_shift = metrics.get("curtain_tonal_shift_vs_alignment_db", float("nan"))
    time_excess = metrics.get("curtain_lr_gd_p95_abs_excess_ms", float("nan"))
    time_width = metrics.get("curtain_lr_impulse_width80_ms", float("nan"))
    time_tail = metrics.get("curtain_lr_impulse_tail_ratio_35ms", float("nan"))

    risk = 0
    reject = 0

    if np.isfinite(mean_margin) and mean_margin < -1.0:
        reject += 1
        reasons.append(f"średni margines L+R w strefie kurtyny jest ujemny ({mean_margin:.2f} dB)")
    elif np.isfinite(mean_margin) and mean_margin < 0.8:
        risk += 1
        reasons.append(f"średni margines L+R jest niski ({mean_margin:.2f} dB)")

    if np.isfinite(p05_margin) and p05_margin < -8.0:
        reject += 1
        reasons.append(f"5 percentyl marginesu L+R jest bardzo niski ({p05_margin:.2f} dB)")
    elif np.isfinite(p05_margin) and p05_margin < -5.0:
        risk += 1
        reasons.append(f"5 percentyl marginesu L+R jest niski ({p05_margin:.2f} dB)")

    if np.isfinite(bad_fraction) and bad_fraction > 0.30:
        reject += 1
        reasons.append(f"zbyt duży udział punktów z L+R poniżej -3 dB ({bad_fraction*100:.1f}%)")
    elif np.isfinite(bad_fraction) and bad_fraction > 0.16:
        risk += 1
        reasons.append(f"spory udział punktów z L+R poniżej -3 dB ({bad_fraction*100:.1f}%)")

    if np.isfinite(dip) and dip > 7.5:
        reject += 1
        reasons.append(f"głęboki dołek L+R w strefie kurtyny ({dip:.2f} dB)")
    elif np.isfinite(dip) and dip > 5.0:
        risk += 1
        reasons.append(f"podwyższona głębokość dołka L+R ({dip:.2f} dB)")

    if np.isfinite(rough) and rough > 4.0:
        risk += 1
        reasons.append(f"duża chropowatość odpowiedzi do kurtyny ({rough:.2f} dB)")

    if np.isfinite(tonal_shift) and abs(tonal_shift) > 4.0:
        reject += 1
        reasons.append(f"duża zmiana balansu względem pasma integracji ({tonal_shift:+.2f} dB)")
    elif np.isfinite(tonal_shift) and abs(tonal_shift) > 2.5:
        risk += 1
        reasons.append(f"wyczuwalna zmiana balansu względem pasma integracji ({tonal_shift:+.2f} dB)")

    # v18.6: diagnostyka czasowa po Dirac. To nie zastępuje SPL/marginesów,
    # ale ma wyłapać kurtyny, które warto porównać odsłuchowo.
    if np.isfinite(time_excess):
        if time_excess > 22.0:
            reject += 1
            reasons.append(f"duże excess group delay L+R w strefie kurtyny ({time_excess:.1f} ms p95)")
        elif time_excess > 14.0:
            risk += 1
            reasons.append(f"podwyższone excess group delay L+R ({time_excess:.1f} ms p95)")
    if np.isfinite(time_width):
        if time_width > 90.0:
            reject += 1
            reasons.append(f"szeroka obwiednia impulsu L+R w strefie kurtyny ({time_width:.1f} ms)")
        elif time_width > 60.0:
            risk += 1
            reasons.append(f"umiarkowanie szeroka obwiednia impulsu L+R ({time_width:.1f} ms)")
    if np.isfinite(time_tail) and time_tail > 0.60:
        risk += 1
        reasons.append(f"podwyższony ogon odpowiedzi czasowej L+R po +35 ms ({time_tail*100:.1f}%)")

    if reject >= 2:
        verdict = "ODRZUCIĆ KURTYNĘ"
    elif reject >= 1 or risk >= 2:
        verdict = "RYZYKO"
    else:
        verdict = "OK"

    if not reasons:
        reasons.append("brak mocnych sygnałów kasowania albo nadmiernej ingerencji w strefie kurtyny")
    return verdict, reasons


def analyze_dirac_curtain_measurements(
    l_path: str | Path,
    r_path: str | Path,
    lr_path: str | Path,
    out_dir: str | Path,
    cfg: OptimizerConfig,
    curtain_high_hz: float = 250.0,
    ignore_bands: Optional[List[Tuple[float, float]]] = None,
    label: str = "Dirac ON",
) -> Tuple[str, Dict[str, float]]:
    """
    v18: analiza bezpieczeństwa kurtyny Dirac.

    Uwaga praktyczna:
    - nie służy do ustawiania delayu subów,
    - ocenia pomiar Dirac ON i wskazuje, czy dana górna kurtyna nie robi problemów
      w zakresie od końca integracji sub/monitor do górnej kurtyny.
    """
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_dir = prepare_flat_module_folder(
        out_dir,
        stale_patterns=[
            "dirac_curtain*.*",
            "DIRAC_CURTAIN_SIMPLE_VERDICT.md",
            "dirac_time_diagnostics*.*",
            "OTWORZ_NAJPIERW.md",
            "README_DIRAC_CURTAIN_ANALYSIS.md",
        ],
    )

    l_m = parse_rew_text(l_path, "L_Dirac_ON")
    r_m = parse_rew_text(r_path, "R_Dirac_ON")
    lr_m = parse_rew_text(lr_path, "LR_Dirac_ON")
    freq = common_grid([l_m, r_m, lr_m], 1600)

    l_db = interp_spl_to_grid(l_m, freq)
    r_db = interp_spl_to_grid(r_m, freq)
    lr_db = interp_spl_to_grid(lr_m, freq)
    l_complex = measurement_to_complex(l_m, freq)
    r_complex = measurement_to_complex(r_m, freq)
    lr_complex = measurement_to_complex(lr_m, freq)
    max_lr = np.maximum(l_db, r_db)
    margin_full = lr_db - max_lr

    ignore_bands = ignore_bands or []
    alignment_mask = band_mask(freq, cfg.eval_low_hz, cfg.eval_high_hz)
    if not np.any(alignment_mask):
        raise ValueError("Pasmo integracji nie pokrywa się z zakresem danych Dirac.")

    curtain_start = max(float(cfg.eval_high_hz), float(cfg.bulge_low_hz), 120.0)
    curtain_high_hz = float(curtain_high_hz)
    if curtain_high_hz <= curtain_start + 5.0:
        # Minimalny bezpiecznik: dla niskich kurtyn nadal ocenimy 120–curtain,
        # jeśli tylko dane na to pozwalają.
        curtain_start = min(float(cfg.eval_high_hz), curtain_high_hz - 5.0)

    curtain_mask_raw = band_mask(freq, curtain_start, curtain_high_hz)
    curtain_mask = mask_without_ignored_bands(freq, curtain_start, curtain_high_hz, ignore_bands)
    if not np.any(curtain_mask):
        raise ValueError(
            "Strefa kurtyny nie pokrywa się z zakresem danych albo została w całości wykluczona pasmami ignorowanymi."
        )

    full_curtain_mask = band_mask(freq, max(40.0, float(freq.min())), curtain_high_hz)
    ignored_mask = curtain_mask_raw & ~curtain_mask

    align = _dirac_band_metrics(freq, l_db, r_db, lr_db, alignment_mask)
    curtain = _dirac_band_metrics(freq, l_db, r_db, lr_db, curtain_mask)
    full = _dirac_band_metrics(freq, l_db, r_db, lr_db, full_curtain_mask)
    ignored = _dirac_band_metrics(freq, l_db, r_db, lr_db, ignored_mask) if np.any(ignored_mask) else {}

    metrics: Dict[str, float] = {
        "alignment_low_hz": float(cfg.eval_low_hz),
        "alignment_high_hz": float(cfg.eval_high_hz),
        "curtain_start_hz": float(curtain_start),
        "curtain_high_hz": float(curtain_high_hz),
        "alignment_mean_margin_db": align["mean_margin_db"],
        "alignment_p05_margin_db": align["p05_margin_db"],
        "alignment_dip_db": align["dip_db"],
        "alignment_roughness_db": align["roughness_db"],
        "alignment_bad_fraction_below_minus3db": align["bad_fraction_below_minus3db"],
        "curtain_mean_margin_db": curtain["mean_margin_db"],
        "curtain_p05_margin_db": curtain["p05_margin_db"],
        "curtain_min_margin_db": curtain["min_margin_db"],
        "curtain_dip_db": curtain["dip_db"],
        "curtain_std_db": curtain["std_db"],
        "curtain_roughness_db": curtain["roughness_db"],
        "curtain_bad_fraction_below_minus3db": curtain["bad_fraction_below_minus3db"],
        "curtain_mean_lr_db": curtain["mean_lr_db"],
        "full_to_curtain_mean_margin_db": full["mean_margin_db"],
        "full_to_curtain_dip_db": full["dip_db"],
        "curtain_tonal_shift_vs_alignment_db": float(curtain["mean_lr_db"] - align["mean_lr_db"]),
        "ignored_bad_fraction_below_minus3db": float(ignored.get("bad_fraction_below_minus3db", float("nan"))),
        "ignored_dip_db": float(ignored.get("dip_db", float("nan"))),
    }

    # v18.6: group delay / pseudo-impulse envelope w strefie kurtyny.
    time_metrics = plot_dirac_time_diagnostics(
        freq, l_complex, r_complex, lr_complex, out_dir,
        curtain_start, curtain_high_hz, cfg, label
    )
    time_verdict, time_reasons = dirac_time_risk_verdict(time_metrics)
    metrics["curtain_time_risk_code"] = {"OK": 0.0, "INFORMACYJNIE": 1.0}.get(time_verdict, float("nan"))
    metrics["curtain_lr_gd_p95_abs_excess_ms"] = float(time_metrics.get("LR_gd_p95_abs_excess_ms", float("nan")))
    metrics["curtain_lr_gd_std_ms"] = float(time_metrics.get("LR_gd_std_ms", float("nan")))
    metrics["curtain_lr_impulse_width80_ms"] = float(time_metrics.get("LR_impulse_width80_ms", float("nan")))
    metrics["curtain_lr_impulse_tail_ratio_35ms"] = float(time_metrics.get("LR_impulse_tail_ratio_35ms", float("nan")))

    # Prosty score porównawczy dla wielu kurtyn. Nie jest to score delayu.
    metrics["curtain_safety_score"] = float(
        metrics["curtain_mean_margin_db"]
        + 0.35 * metrics["curtain_p05_margin_db"]
        - 0.80 * metrics["curtain_dip_db"]
        - 0.55 * metrics["curtain_roughness_db"]
        - 4.00 * metrics["curtain_bad_fraction_below_minus3db"]
        - 0.45 * abs(metrics["curtain_tonal_shift_vs_alignment_db"])
        - 0.08 * (metrics["curtain_lr_gd_p95_abs_excess_ms"] if np.isfinite(metrics["curtain_lr_gd_p95_abs_excess_ms"]) else 0.0)
        - 0.02 * (metrics["curtain_lr_impulse_width80_ms"] if np.isfinite(metrics["curtain_lr_impulse_width80_ms"]) else 0.0)
        - 1.50 * (metrics["curtain_lr_impulse_tail_ratio_35ms"] if np.isfinite(metrics["curtain_lr_impulse_tail_ratio_35ms"]) else 0.0)
    )

    verdict, reasons = _dirac_curtain_verdict(metrics)

    report: List[str] = []
    report.append(f"# Analiza kurtyny Dirac — {label}\n\n")
    report.append(f"## Werdykt: **{verdict}**\n\n")
    report.append("To jest kontrola bezpieczeństwa po Dirac, a nie algorytm do wyboru delayu subwooferów.\n\n")
    report.append("## Zakresy\n\n")
    report.append(f"- Pasmo integracji sub/monitor: {cfg.eval_low_hz:.1f}–{cfg.eval_high_hz:.1f} Hz\n")
    report.append(f"- Strefa kurtyny oceniana: {curtain_start:.1f}–{curtain_high_hz:.1f} Hz\n")
    if ignore_bands:
        report.append("- Pasma ignorowane / nie goń delayem: " + ", ".join(f"{a:.0f}-{b:.0f} Hz" for a, b in ignore_bands) + "\n")
    else:
        report.append("- Pasma ignorowane / nie goń delayem: brak\n")
    report.append("\n## Metryki\n\n")
    report.append(f"- Margines L+R w strefie kurtyny, średnio: {metrics['curtain_mean_margin_db']:.2f} dB\n")
    report.append(f"- Margines L+R w strefie kurtyny, 5 percentyl: {metrics['curtain_p05_margin_db']:.2f} dB\n")
    report.append(f"- Dołek L+R w strefie kurtyny: {metrics['curtain_dip_db']:.2f} dB\n")
    report.append(f"- Chropowatość / roughness w strefie kurtyny: {metrics['curtain_roughness_db']:.2f} dB\n")
    report.append(f"- Udział punktów L+R poniżej -3 dB względem mocniejszego kanału: {metrics['curtain_bad_fraction_below_minus3db']*100:.1f}%\n")
    report.append(f"- Zmiana balansu względem pasma integracji: {metrics['curtain_tonal_shift_vs_alignment_db']:+.2f} dB\n")
    report.append(f"- Score bezpieczeństwa kurtyny: {metrics['curtain_safety_score']:.2f}\n\n")

    report.append("## Diagnostyka czasowa v18.6\n\n")
    report.append(f"- Status czasowy: **{time_verdict}**\n")
    report.append(f"- L+R excess group delay p95: {metrics['curtain_lr_gd_p95_abs_excess_ms']:.2f} ms\n")
    report.append(f"- L+R group delay std: {metrics['curtain_lr_gd_std_ms']:.2f} ms\n")
    report.append(f"- L+R pseudo impulse width80: {metrics['curtain_lr_impulse_width80_ms']:.2f} ms\n")
    report.append(f"- L+R ogon impulsu po +35 ms: {metrics['curtain_lr_impulse_tail_ratio_35ms']*100:.1f}%\n")
    for r in time_reasons:
        report.append(f"- {r}\n")
    report.append("\n")

    report.append("## Powody werdyktu\n\n")
    for r in reasons:
        report.append(f"- {r}\n")
    report.append("\n## Interpretacja praktyczna\n\n")
    if verdict == "OK":
        report.append("Ta kurtyna wygląda bezpiecznie pomiarowo. Nadal warto potwierdzić odsłuchem wokale, fortepian i kontrabas.\n")
    elif verdict == "RYZYKO":
        report.append("Ta kurtyna wymaga porównania z niższą/wyższą kurtyną. Nie naprawiaj problemów 150–300 Hz delayem subów, jeśli pasmo integracji 60–120 Hz jest już dobre.\n")
    else:
        report.append("Ta kurtyna wygląda problematycznie. Najpierw porównaj niższą kurtynę albo inną bazę/preset; nie próbuj maskować tego targetem Dirac.\n")
    (out_dir / "dirac_curtain_analysis.md").write_text("".join(report), encoding="utf-8")
    (out_dir / "DIRAC_CURTAIN_SIMPLE_VERDICT.md").write_text(
        dirac_curtain_simple_verdict(metrics, verdict, cfg, curtain_high_hz),
        encoding="utf-8",
    )

    gd_l_csv = complex_group_delay_ms(freq, l_complex)
    gd_r_csv = complex_group_delay_ms(freq, r_complex)
    gd_lr_csv = complex_group_delay_ms(freq, lr_complex)

    with (out_dir / "dirac_curtain_analysis.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "frequency_hz", "L_db", "R_db", "LR_db", "LR_minus_max_LR_db",
            "L_group_delay_ms", "R_group_delay_ms", "LR_group_delay_ms",
            "in_alignment_band", "in_curtain_band_scored", "in_ignored_band",
        ])
        for i, fr in enumerate(freq):
            writer.writerow([
                f"{fr:.6f}",
                f"{l_db[i]:.4f}",
                f"{r_db[i]:.4f}",
                f"{lr_db[i]:.4f}",
                f"{margin_full[i]:.4f}",
                f"{gd_l_csv[i]:.5f}" if np.isfinite(gd_l_csv[i]) else "",
                f"{gd_r_csv[i]:.5f}" if np.isfinite(gd_r_csv[i]) else "",
                f"{gd_lr_csv[i]:.5f}" if np.isfinite(gd_lr_csv[i]) else "",
                int(bool(alignment_mask[i])),
                int(bool(curtain_mask[i])),
                int(bool(ignored_mask[i])),
            ])

    fig, ax = plt.subplots(figsize=(12, 7))
    ax.semilogx(freq, l_db, label="L Dirac ON")
    ax.semilogx(freq, r_db, label="R Dirac ON")
    ax.semilogx(freq, lr_db, linewidth=2.2, label="L+R Dirac ON")
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12, label="integracja")
    ax.axvspan(curtain_start, curtain_high_hz, alpha=0.08, label="strefa kurtyny")
    for lo, hi in ignore_bands:
        ax.axvspan(lo, hi, alpha=0.06, hatch="//", label="ignorowane / nie goń" if lo == ignore_bands[0][0] else None)
    ax.set_xlim(max(20, freq.min()), min(max(300, curtain_high_hz * 1.1), freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("SPL [dB]")
    ax.set_title(f"Analiza kurtyny Dirac — {verdict}")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_dir / "dirac_curtain_analysis.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.semilogx(freq, margin_full, label="L+R − max(L,R)")
    ax.axhline(0, linewidth=1)
    ax.axhline(-3, linestyle=":", linewidth=1)
    ax.axhline(-6, linestyle=":", linewidth=1)
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12, label="integracja")
    ax.axvspan(curtain_start, curtain_high_hz, alpha=0.08, label="strefa kurtyny")
    for lo, hi in ignore_bands:
        ax.axvspan(lo, hi, alpha=0.06, hatch="//", label="ignorowane / nie goń" if lo == ignore_bands[0][0] else None)
    ax.set_xlim(max(20, freq.min()), min(max(300, curtain_high_hz * 1.1), freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("Margines [dB]")
    ax.set_title("Margines L+R względem mocniejszego kanału")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_dir / "dirac_curtain_margin.png", dpi=160)
    plt.close(fig)

    write_dirac_curtain_readme(out_dir, metrics, verdict, label, curtain_high_hz)
    return verdict, metrics


def _classify_dirac_curtain_file(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    Zwraca (group_key, side) dla plików porównania kurtyn.
    Obsługuje nazwy typu:
      Dirac_200_L.txt, Dirac_200_R.txt, Dirac_200_LR.txt
      250Hz_L_DiracON.txt, 250Hz_R_DiracON.txt, 250Hz_L+R_DiracON.txt
    """
    stem = path.stem.lower().replace("+", "plus")
    tokens = [t for t in re.split(r"[_\-\s]+", stem) if t]
    if not tokens:
        return None, None

    side: Optional[str] = None
    side_aliases = {
        "l": "L",
        "left": "L",
        "r": "R",
        "right": "R",
        "lr": "LR",
        "lpr": "LR",
        "lplusr": "LR",
        "lplusr": "LR",
        "lplus": "LR",
        "sum": "LR",
        "both": "LR",
    }

    side_idx: Optional[int] = None
    for i, tok in enumerate(tokens):
        norm = tok.replace("plus", "plus")
        if norm in side_aliases:
            # Preferujemy token L/R/LR blisko końca nazwy.
            side = side_aliases[norm]
            side_idx = i
    if side is None:
        return None, None

    group_tokens = list(tokens)
    if side_idx is not None:
        group_tokens.pop(side_idx)
    # Usuwamy generyczne słowa, które czasem pojawiają się po side.
    group_tokens = [t for t in group_tokens if t not in {"on", "diracon", "dirac_on", "measure", "measurement", "rew"}]
    group_key = "_".join(group_tokens).strip("_")
    if not group_key:
        group_key = "dirac_curtain"
    return group_key, side


def _infer_curtain_hz_from_group(group_key: str, default_hz: float) -> float:
    low = str(group_key).lower().replace(",", ".")
    candidates: List[float] = []
    for m in re.finditer(r"(?:curtain|kurtyna|dirac|dl|filter|filtr)?[_\-\s]*([0-9]{3}(?:\.[0-9]+)?)\s*(?:hz)?", low):
        val = float(m.group(1))
        if 120.0 <= val <= 500.0:
            candidates.append(val)
    if candidates:
        return candidates[-1]
    return float(default_hz)


def find_dirac_curtain_groups(folder: str | Path) -> Dict[str, Dict[str, Path]]:
    folder = Path(folder)
    groups: Dict[str, Dict[str, Path]] = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in [".txt", ".csv", ".dat"]:
            continue
        group_key, side = _classify_dirac_curtain_file(p)
        if not group_key or not side:
            continue
        groups.setdefault(group_key, {})
        if side not in groups[group_key]:
            groups[group_key][side] = p
    return groups


def compare_dirac_curtains_folder(
    folder: str | Path,
    out_dir: str | Path,
    cfg: OptimizerConfig,
    default_curtain_high_hz: float = 250.0,
    ignore_bands: Optional[List[Tuple[float, float]]] = None,
    stop_event=None,
    progress_callback=None,
) -> List[Dict[str, object]]:
    out_dir = prepare_flat_module_folder(
        out_dir,
        stale_patterns=[
            "dirac_curtain_comparison.*",
            "DIRAC_CURTAINS_SIMPLE_COMPARISON.md",
            "OTWORZ_NAJPIERW.md",
            "README_DIRAC_CURTAIN_COMPARISON.md",
        ],
    )
    curtains_dir = out_dir / "curtains"
    if curtains_dir.exists() and curtains_dir.is_dir():
        try:
            shutil.rmtree(curtains_dir)
        except Exception:
            pass
    curtains_dir.mkdir(parents=True, exist_ok=True)
    groups = find_dirac_curtain_groups(folder)
    complete_groups = [
        (key, files)
        for key, files in groups.items()
        if all(side in files for side in ("L", "R", "LR"))
    ]

    summary: List[Dict[str, object]] = []
    total = max(len(complete_groups), 1)
    for idx, (key, files) in enumerate(complete_groups, start=1):
        if stop_event is not None and stop_event.is_set():
            raise CalculationCancelled("Porównanie kurtyn Dirac zatrzymane przez użytkownika.")
        if progress_callback:
            progress_callback(idx - 1, total)
        curtain_hz = _infer_curtain_hz_from_group(key, default_curtain_high_hz)
        sub_out = curtains_dir / re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
        verdict, metrics = analyze_dirac_curtain_measurements(
            files["L"],
            files["R"],
            files["LR"],
            sub_out,
            cfg,
            curtain_high_hz=curtain_hz,
            ignore_bands=ignore_bands,
            label=f"{key} / kurtyna {curtain_hz:.0f} Hz",
        )
        summary.append({
            "group": key,
            "curtain_high_hz": curtain_hz,
            "verdict": verdict,
            "curtain_safety_score": metrics["curtain_safety_score"],
            "curtain_mean_margin_db": metrics["curtain_mean_margin_db"],
            "curtain_p05_margin_db": metrics["curtain_p05_margin_db"],
            "curtain_dip_db": metrics["curtain_dip_db"],
            "curtain_roughness_db": metrics["curtain_roughness_db"],
            "curtain_bad_fraction_below_minus3db": metrics["curtain_bad_fraction_below_minus3db"],
            "curtain_tonal_shift_vs_alignment_db": metrics["curtain_tonal_shift_vs_alignment_db"],
            "curtain_lr_gd_p95_abs_excess_ms": metrics.get("curtain_lr_gd_p95_abs_excess_ms", float("nan")),
            "curtain_lr_impulse_width80_ms": metrics.get("curtain_lr_impulse_width80_ms", float("nan")),
            "curtain_time_risk_code": metrics.get("curtain_time_risk_code", float("nan")),
            "folder": str(sub_out),
        })

    if progress_callback:
        progress_callback(total, total)

    verdict_rank = {"OK": 0, "RYZYKO": 1, "ODRZUCIĆ KURTYNĘ": 2}
    summary.sort(key=lambda r: (verdict_rank.get(str(r["verdict"]), 9), -float(r["curtain_safety_score"])))

    with (out_dir / "dirac_curtain_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        if summary:
            writer = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            writer.writeheader()
            writer.writerows(summary)

    lines: List[str] = ["# Porównanie kurtyn Dirac\n\n"]
    if not summary:
        lines.append("Nie znaleziono kompletnych zestawów L/R/L+R.\n\n")
        lines.append("Przykładowe nazwy: `Dirac_200_L.txt`, `Dirac_200_R.txt`, `Dirac_200_LR.txt`.\n")
    else:
        best = summary[0]
        lines.append(f"## Najbezpieczniejsza pomiarowo: **{best['group']}** — {best['verdict']}\n\n")
        lines.append("| # | Grupa | Kurtyna | Werdykt | Score | Mean margin | P05 margin | Dip | Rough | Bad % | Tonal shift | GD p95 | Imp width |\n")
        lines.append("|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for i, row in enumerate(summary, start=1):
            lines.append(
                f"| {i} | {row['group']} | {float(row['curtain_high_hz']):.0f} Hz | {row['verdict']} | "
                f"{float(row['curtain_safety_score']):.2f} | {float(row['curtain_mean_margin_db']):.2f} | "
                f"{float(row['curtain_p05_margin_db']):.2f} | {float(row['curtain_dip_db']):.2f} | "
                f"{float(row['curtain_roughness_db']):.2f} | {float(row['curtain_bad_fraction_below_minus3db'])*100:.1f}% | "
                f"{float(row['curtain_tonal_shift_vs_alignment_db']):+.2f} | "
                f"{float(row.get('curtain_lr_gd_p95_abs_excess_ms', float('nan'))):.1f} ms | "
                f"{float(row.get('curtain_lr_impulse_width80_ms', float('nan'))):.1f} ms |\n"
            )
        lines.append("\nInterpretacja: ranking służy do wyboru bezpieczniejszej kurtyny Dirac po pomiarach Dirac ON. Nie zastępuje optymalizacji delayu Dirac OFF.\n")
    (out_dir / "dirac_curtain_comparison.md").write_text("".join(lines), encoding="utf-8")
    # v18.7: krótki werdykt do otwarcia zamiast tabeli metryk.
    simple_lines: List[str] = ["# Prosty werdykt — porównanie kurtyn Dirac\n\n"]
    if not summary:
        simple_lines.append("Nie znaleziono kompletnych zestawów L/R/L+R.\n")
    else:
        best = summary[0]
        simple_lines.append(f"**Najbezpieczniejsza pomiarowo:** {best['group']} — {best['verdict']} ({float(best['curtain_high_hz']):.0f} Hz)\n\n")
        simple_lines.append("## Kolejność odsłuchu\n\n")
        if bool(getattr(cfg, "safe_midrange_priority", True)):
            simple_lines.append("1. Zacznij od 200 / 250 Hz, jeśli są dostępne.\n")
            simple_lines.append("2. 320 Hz traktuj kontrolnie — tylko jeśli nie pogarsza barwy, wokali i niższej średnicy.\n")
        else:
            simple_lines.append("1. Porównaj najwyżej ocenioną kurtynę z sąsiednią niższą i wyższą.\n")
        simple_lines.append("\n## Tabela skrócona\n\n")
        simple_lines.append("| # | Kurtyna | Werdykt | Score | Komentarz |\n")
        simple_lines.append("|---:|---:|---|---:|---|\n")
        for i, row in enumerate(summary, start=1):
            comment = "bezpieczna" if row["verdict"] == "OK" else ("sprawdź odsłuchem" if row["verdict"] == "RYZYKO" else "raczej odrzuć")
            simple_lines.append(f"| {i} | {float(row['curtain_high_hz']):.0f} Hz | {row['verdict']} | {float(row['curtain_safety_score']):.2f} | {comment} |\n")
        simple_lines.append("\nZakres 135–240 Hz traktuj jako strefę ostrożności; nie naprawiaj głębokich nulli delayem subów.\n")
    (out_dir / "DIRAC_CURTAINS_SIMPLE_COMPARISON.md").write_text("".join(simple_lines), encoding="utf-8")
    write_dirac_curtain_comparison_readme(out_dir, summary)
    return summary



def prediction_validation_status(metrics: Dict[str, float]) -> Tuple[str, List[str]]:
    """Zamienia metryki predykcja-vs-real na status wyboru delayu."""
    lr_rms = float(metrics.get("LR_rms_error_db", float("nan")))
    lr_max = float(metrics.get("LR_max_abs_error_db", float("nan")))
    l_rms = float(metrics.get("L_rms_error_db", float("nan")))
    r_rms = float(metrics.get("R_rms_error_db", float("nan")))
    notes: List[str] = []

    if np.isfinite(lr_rms) and lr_rms <= 1.5 and (not np.isfinite(lr_max) or lr_max <= 3.0):
        status = "POTWIERDZONY REALNYM POMIAREM"
        notes.append("L+R w paśmie oceny zgadza się z predykcją bardzo dobrze; długi delay nie jest sam w sobie problemem.")
    elif np.isfinite(lr_rms) and lr_rms <= 3.0:
        status = "CZĘŚCIOWO POTWIERDZONY"
        notes.append("L+R jest umiarkowanie zgodne z predykcją; przed Dirac warto sprawdzić top 2–3 lub powtórzyć pomiar.")
    else:
        status = "NIEPOTWIERDZONY"
        notes.append("L+R nie zgadza się wystarczająco z predykcją; sprawdź timing reference, slot miniDSP, poziomy i właściwe pliki.")

    if np.isfinite(l_rms) and np.isfinite(r_rms) and max(l_rms, r_rms) > 2.5 and np.isfinite(lr_rms) and lr_rms <= 1.5:
        notes.append(
            "Uwaga: osobne L/R mają większy błąd niż L+R. To nie dyskwalifikuje delayu, "
            "ale warto sprawdzić nazwy plików, poziomy kanałów i identyczny preset pomiarowy."
        )
    return status, notes


def predicted_candidate_label(predicted_csv: str | Path) -> str:
    name = Path(predicted_csv).name.lower()
    if "best" in name:
        return "BEST TECHNICAL"
    if "plateau_alt" in name:
        return "ALTERNATYWA A/B Z PLATEAU"
    if "practical" in name:
        return "ALTERNATYWA A/B Z PLATEAU"
    return "KANDYDAT"


def update_report_with_prediction_validation(
    out_dir: str | Path,
    predicted_csv: str | Path,
    metrics: Dict[str, float],
    cfg: OptimizerConfig,
) -> None:
    """Zapisuje osobny status i, jeśli istnieje raport główny, dopisuje sekcję walidacji."""
    out_dir = Path(out_dir)
    status, notes = prediction_validation_status(metrics)
    candidate_label = predicted_candidate_label(predicted_csv)

    block_lines = []
    block_lines.append("## Status walidacji predykcji vs realny pomiar\n\n")
    block_lines.append(f"- Kandydat: **{candidate_label}**\n")
    block_lines.append(f"- Status: **{status}**\n")
    block_lines.append(f"- Pasmo oceny: {cfg.eval_low_hz}–{cfg.eval_high_hz} Hz\n")
    block_lines.append(f"- L+R RMS error: {float(metrics.get('LR_rms_error_db', float('nan'))):.2f} dB\n")
    block_lines.append(f"- L+R max abs error: {float(metrics.get('LR_max_abs_error_db', float('nan'))):.2f} dB\n")
    block_lines.append(f"- L/R osobno RMS: {float(metrics.get('L_rms_error_db', float('nan'))):.2f} / {float(metrics.get('R_rms_error_db', float('nan'))):.2f} dB\n\n")
    for note in notes:
        block_lines.append(f"- {note}\n")
    block_lines.append("\n")
    block = "".join(block_lines)

    (out_dir / "candidate_validation_status.md").write_text("# Status kandydata po walidacji\n\n" + block, encoding="utf-8")
    # v18.7: prosty status projektu po walidacji — do otwarcia przed Dirac.
    (out_dir / "PROJECT_STATUS_AFTER_VALIDATION.md").write_text(
        "# Status projektu po walidacji\n\n"
        + f"**{status}**\n\n"
        + f"Kandydat: {candidate_label}\n\n"
        + f"L+R RMS error: {float(metrics.get('LR_rms_error_db', float('nan'))):.2f} dB\n\n"
        + ("Można przejść do Dirac i porównać kurtyny 200 / 250 / 320 Hz.\n" if status.startswith("POTWIERDZONY") else "Przed Dirac dopomierz A/B albo sprawdź timing reference.\n"),
        encoding="utf-8",
    )
    with (out_dir / "candidate_validation_status.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["candidate", "status", "metric", "value"])
        for k, v in metrics.items():
            writer.writerow([candidate_label, status, k, f"{float(v):.4f}"])

    report_path = find_main_report_path(out_dir)
    if report_path is not None and report_path.exists():
        original = report_path.read_text(encoding="utf-8")
        # Usuwamy starą sekcję walidacji, jeśli była dopisana wcześniej.
        original = re.sub(
            r"\n?## Status walidacji predykcji vs realny pomiar\n[\s\S]*?(?=\n## |\Z)",
            "\n",
            original,
            count=1,
        )
        marker = "## Rekomendacja\n\n"
        if marker in original:
            updated = original.replace(marker, marker + block, 1)
        else:
            updated = block + "\n" + original
        report_path.write_text(updated, encoding="utf-8")


def prepare_prediction_vs_real_folder(out_dir: str | Path) -> Path:
    """
    v19.4: prediction_vs_real ma być małym, jednoznacznym folderem roboczym,
    a nie miniaturową kopią całej struktury rew_delay_results.

    Przy ponownym uruchomieniu czyścimy stare podfoldery utworzone przez
    organizer v18.9/v19.3, żeby użytkownik nie widział tam 00_START_TUTAJ,
    01_RAPORTY_MD, 02_DANE_CSV itd.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    old_organized_dirs = [
        "00_START_TUTAJ",
        "01_RAPORTY_MD",
        "02_DANE_CSV",
        "03_GLOWNA_REKOMENDACJA_BEST",
        "04_ALTERNATYWA_PLATEAU_AB",
                "07_MAPY_I_POROWNANIA",
        "08_WALIDACJA_PREDYKCJI",
        "09_DIRAC",
        "10_INNE_WYKRESY",
        "11_INNE_PLIKI",
    ]
    for folder_name in old_organized_dirs:
        p = out_dir / folder_name
        if p.exists() and p.is_dir():
            try:
                shutil.rmtree(p)
            except Exception:
                pass

    # Pliki indeksu wygenerowane kiedyś przez organizer nie są tu potrzebne.
    stale_root_files = [
        "START_TUTAJ.md",
        "README_WYNIKOW.md",
        "MINIDSP_SETTINGS_FINAL.txt",
    ]
    for file_name in stale_root_files:
        p = out_dir / file_name
        if p.exists() and p.is_file():
            try:
                p.unlink()
            except Exception:
                pass

    # Usuwamy stare pliki walidacji, żeby nowy przebieg nie mieszał wyników
    # z poprzednim kandydatem lub poprzednim pomiarem.
    stale_patterns = [
        "prediction_vs_real*.*",
        "candidate_validation_status.*",
        "PROJECT_STATUS_AFTER_VALIDATION.md",
        "OTWORZ_NAJPIERW.md",
        "README_PREDICTION_VS_REAL.md",
    ]
    for pattern in stale_patterns:
        for p in out_dir.glob(pattern):
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass

    return out_dir


def write_prediction_vs_real_readme(
    out_dir: str | Path,
    metrics: Dict[str, float],
    verdict: str,
    validation_status: str,
    predicted_csv: str | Path,
) -> None:
    """v19.4: prosty indeks bez podfolderów i bez nazw katalogów organizera."""
    out_dir = Path(out_dir)
    lines: List[str] = []
    lines.append("# OTWÓRZ NAJPIERW — predykcja vs realny pomiar\n\n")
    lines.append("Ten folder jest celowo płaski. Nie ma tu dodatkowych podkatalogów.\n\n")
    lines.append("## Najważniejsze pliki\n\n")
    lines.append("1. `prediction_vs_real.md` — raport i werdykt.\n")
    lines.append("2. `prediction_vs_real.png` — L+R predykcja vs realny pomiar.\n")
    lines.append("3. `prediction_vs_real_error.png` — błąd real minus predykcja.\n")
    lines.append("4. `candidate_validation_status.md` — status kandydata po walidacji.\n")
    lines.append("5. `prediction_vs_real.csv` — dane liczbowe.\n\n")
    lines.append("## Wynik skrócony\n\n")
    lines.append(f"- Werdykt: **{verdict}**\n")
    lines.append(f"- Status kandydata: **{validation_status}**\n")
    lines.append(f"- L+R RMS error: {float(metrics.get('LR_rms_error_db', float('nan'))):.2f} dB\n")
    lines.append(f"- L+R max abs error: {float(metrics.get('LR_max_abs_error_db', float('nan'))):.2f} dB\n")
    lines.append(f"- Predykcja: `{Path(predicted_csv).name}`\n\n")
    lines.append("## Uwaga porządkowa\n\n")
    lines.append(
        "Od v19.4 moduł `Predykcja vs realny pomiar` nie uruchamia pełnego organizera wyników. "
        "Dzięki temu w tym folderze nie pojawiają się katalogi typu `00_START_TUTAJ`, "
        "`01_RAPORTY_MD` ani `08_WALIDACJA_PREDYKCJI`.\n"
    )
    (out_dir / "OTWORZ_NAJPIERW.md").write_text("".join(lines), encoding="utf-8")
    (out_dir / "README_PREDICTION_VS_REAL.md").write_text("".join(lines), encoding="utf-8")



def compare_prediction_to_real_measurements(
    predicted_csv: str | Path,
    l_real_path: str | Path,
    r_real_path: str | Path,
    lr_real_path: str | Path,
    out_dir: str | Path,
    cfg: OptimizerConfig,
    organize_output: bool = False,
) -> Dict[str, float]:
    """
    Porównuje predykcję aplikacji z realnymi pomiarami REW L_sum/R_sum/L+R.
    """
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_dir = Path(out_dir)
    if out_dir.name.lower() == "prediction_vs_real":
        prepare_prediction_vs_real_folder(out_dir)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)

    data = np.genfromtxt(predicted_csv, delimiter=",", names=True, dtype=float, encoding="utf-8")
    freq = np.asarray(data["frequency_hz"], dtype=float)
    l_pred = np.asarray(data["L_sum_pred_db"], dtype=float)
    r_pred = np.asarray(data["R_sum_pred_db"], dtype=float)
    lr_pred = np.asarray(data["LR_sum_pred_db"], dtype=float)

    l_real_m = parse_rew_text(l_real_path, "L_sum_real")
    r_real_m = parse_rew_text(r_real_path, "R_sum_real")
    lr_real_m = parse_rew_text(lr_real_path, "LR_sum_real")
    l_real = interp_spl_to_grid(l_real_m, freq)
    r_real = interp_spl_to_grid(r_real_m, freq)
    lr_real = interp_spl_to_grid(lr_real_m, freq)

    mask = band_mask(freq, cfg.eval_low_hz, cfg.eval_high_hz)
    if not np.any(mask):
        raise ValueError("Pasmo oceny nie pokrywa się z zakresem danych.")

    err_lr = lr_real - lr_pred
    err_l = l_real - l_pred
    err_r = r_real - r_pred

    def rms(x):
        return float(np.sqrt(np.nanmean(x[mask] ** 2)))

    metrics = {
        "LR_rms_error_db": rms(err_lr),
        "L_rms_error_db": rms(err_l),
        "R_rms_error_db": rms(err_r),
        "LR_mean_error_db": float(np.nanmean(err_lr[mask])),
        "LR_max_abs_error_db": float(np.nanmax(np.abs(err_lr[mask]))),
    }

    # v18.6: jeśli predykcja ma zapisane fazy, porównujemy też group delay i pseudo-impulse envelope.
    time_report_lines: List[str] = []
    time_verdict = "BRAK FAZY W PREDYKCJI"
    time_metrics: Dict[str, float] = {}
    names = set(data.dtype.names or [])
    if "LR_sum_pred_phase_deg" in names:
        def _complex_from_db_phase(mag_db: np.ndarray, phase_deg: np.ndarray) -> np.ndarray:
            phase_rad = np.unwrap(np.deg2rad(np.asarray(phase_deg, dtype=float)))
            mag = 10.0 ** (np.asarray(mag_db, dtype=float) / 20.0)
            return mag * np.exp(1j * phase_rad)

        lr_pred_complex = _complex_from_db_phase(lr_pred, np.asarray(data["LR_sum_pred_phase_deg"], dtype=float))
        lr_real_complex = measurement_to_complex(lr_real_m, freq)
        gd_pred = complex_group_delay_ms(freq, lr_pred_complex)
        gd_real = complex_group_delay_ms(freq, lr_real_complex)
        gd_mask = mask & np.isfinite(gd_pred) & np.isfinite(gd_real)
        if np.any(gd_mask):
            gd_err = gd_real - gd_pred
            time_metrics["LR_gd_rms_error_ms"] = float(np.sqrt(np.nanmean(gd_err[gd_mask] ** 2)))
            time_metrics["LR_gd_mean_error_ms"] = float(np.nanmean(gd_err[gd_mask]))
            time_metrics["LR_gd_max_abs_error_ms"] = float(np.nanmax(np.abs(gd_err[gd_mask])))
        else:
            time_metrics["LR_gd_rms_error_ms"] = float("nan")
            time_metrics["LR_gd_mean_error_ms"] = float("nan")
            time_metrics["LR_gd_max_abs_error_ms"] = float("nan")

        t_pred, env_pred = band_limited_impulse_envelope(freq, lr_pred_complex, cfg.eval_low_hz, cfg.eval_high_hz)
        t_real, env_real = band_limited_impulse_envelope(freq, lr_real_complex, cfg.eval_low_hz, cfg.eval_high_hz)
        imp_pred = impulse_envelope_summary(t_pred, env_pred)
        imp_real = impulse_envelope_summary(t_real, env_real)
        time_metrics["LR_impulse_peak_delta_ms"] = float(imp_real["impulse_peak_time_ms"] - imp_pred["impulse_peak_time_ms"])
        time_metrics["LR_impulse_centroid_delta_ms"] = float(imp_real["impulse_centroid_ms"] - imp_pred["impulse_centroid_ms"])
        time_metrics["LR_impulse_width80_pred_ms"] = float(imp_pred["impulse_width80_ms"])
        time_metrics["LR_impulse_width80_real_ms"] = float(imp_real["impulse_width80_ms"])

        gd_rms = time_metrics.get("LR_gd_rms_error_ms", float("nan"))
        peak_delta = abs(time_metrics.get("LR_impulse_peak_delta_ms", float("nan")))
        if np.isfinite(gd_rms) and gd_rms <= 4.0 and (not np.isfinite(peak_delta) or peak_delta <= 8.0):
            time_verdict = "ZGODNOŚĆ CZASOWA DOBRA"
        elif np.isfinite(gd_rms) and gd_rms <= 9.0:
            time_verdict = "ZGODNOŚĆ CZASOWA ŚREDNIA"
        else:
            time_verdict = "ZGODNOŚĆ CZASOWA SŁABA"

        with (out_dir / "prediction_vs_real_time.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["metric", "value"])
            for k in sorted(time_metrics.keys()):
                writer.writerow([k, f"{time_metrics[k]:.6f}" if np.isfinite(time_metrics[k]) else "nan"])
            writer.writerow(["time_verdict", time_verdict])

        with (out_dir / "prediction_vs_real_group_delay.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["frequency_hz", "LR_pred_gd_ms", "LR_real_gd_ms", "LR_gd_error_real_minus_pred_ms"])
            for i, fr in enumerate(freq):
                err = gd_real[i] - gd_pred[i] if np.isfinite(gd_real[i]) and np.isfinite(gd_pred[i]) else float("nan")
                writer.writerow([
                    f"{fr:.6f}",
                    f"{gd_pred[i]:.5f}" if np.isfinite(gd_pred[i]) else "",
                    f"{gd_real[i]:.5f}" if np.isfinite(gd_real[i]) else "",
                    f"{err:.5f}" if np.isfinite(err) else "",
                ])

        fig, ax = plt.subplots(figsize=(12, 5))
        ax.semilogx(freq, gd_pred, label="L+R predykcja GD")
        ax.semilogx(freq, gd_real, linewidth=2, label="L+R realny GD")
        ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12)
        ax.axhline(0, linewidth=1)
        ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
        ax.set_xlabel("Częstotliwość [Hz]")
        ax.set_ylabel("Opóźnienie grupowe [ms]")
        ax.set_title(f"Predykcja vs realny pomiar — opóźnienie grupowe — {time_verdict}")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend()
        fig.tight_layout()
        _safe_savefig(fig, out_dir / "prediction_vs_real_group_delay.png", dpi=160)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(12, 5))
        if t_pred.size:
            m = (t_pred >= -120.0) & (t_pred <= 160.0)
            ax.plot(t_pred[m], env_pred[m], label="L+R predykcja")
        if t_real.size:
            m = (t_real >= -120.0) & (t_real <= 160.0)
            ax.plot(t_real[m], env_real[m], linewidth=2, label="L+R realny")
        ax.axvline(0, linewidth=1)
        ax.set_xlabel("Czas [ms]")
        ax.set_ylabel("Znormalizowana obwiednia")
        ax.set_title(f"Predykcja vs realny pomiar — przybliżona obwiednia impulsu {cfg.eval_low_hz:.0f}–{cfg.eval_high_hz:.0f} Hz")
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.tight_layout()
        _safe_savefig(fig, out_dir / "prediction_vs_real_impulse_envelope.png", dpi=160)
        plt.close(fig)

        time_report_lines.append("\n## Walidacja czasowa v18.6\n\n")
        time_report_lines.append(f"- Status czasowy: **{time_verdict}**\n")
        for k in sorted(time_metrics.keys()):
            time_report_lines.append(f"- {k}: {time_metrics[k]:.2f}\n")
        time_report_lines.append("\n")
        metrics.update(time_metrics)

    if metrics["LR_rms_error_db"] <= 1.5:
        verdict = "ZGODNOŚĆ DOBRA"
    elif metrics["LR_rms_error_db"] <= 3.0:
        verdict = "ZGODNOŚĆ ŚREDNIA"
    else:
        verdict = "ZGODNOŚĆ SŁABA"

    report = []
    report.append("# Predykcja vs realny pomiar\n\n")
    report.append(f"## Werdykt: **{verdict}**\n\n")
    validation_status, validation_notes = prediction_validation_status(metrics)
    report.append(f"- Pasmo oceny: {cfg.eval_low_hz}–{cfg.eval_high_hz} Hz\n")
    for k, v in metrics.items():
        report.append(f"- {k}: {v:.2f}\n")
    if time_report_lines:
        report.extend(time_report_lines)
    report.append(f"\n## Status kandydata: **{validation_status}**\n\n")
    for note in validation_notes:
        report.append(f"- {note}\n")
    report.append("\nJeśli zgodność jest słaba, sprawdź timing reference, slot miniDSP, poziomy, nazwę pliku sub/main oraz czy pomiar realny był wykonany z tym samym presetem.\n")
    (out_dir / "prediction_vs_real.md").write_text("".join(report), encoding="utf-8")
    update_report_with_prediction_validation(out_dir, predicted_csv, metrics, cfg)

    with (out_dir / "prediction_vs_real.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frequency_hz", "LR_pred_db", "LR_real_db", "LR_error_real_minus_pred_db", "L_error_db", "R_error_db"])
        for i, fr in enumerate(freq):
            writer.writerow([f"{fr:.6f}", f"{lr_pred[i]:.4f}", f"{lr_real[i]:.4f}", f"{err_lr[i]:.4f}", f"{err_l[i]:.4f}", f"{err_r[i]:.4f}"])

    fig, ax = plt.subplots(figsize=(12, 7))
    ax.semilogx(freq, lr_pred, label="L+R predykcja")
    ax.semilogx(freq, lr_real, linewidth=2, label="L+R realny pomiar")
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12)
    ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("SPL [dB]")
    ax.set_title(f"Predykcja vs realny pomiar — {verdict}")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_dir / "prediction_vs_real.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.semilogx(freq, err_lr, label="L+R pomiar − predykcja")
    ax.axhline(0, linewidth=1)
    ax.axhline(3, linestyle=":", linewidth=1)
    ax.axhline(-3, linestyle=":", linewidth=1)
    ax.axvspan(cfg.eval_low_hz, cfg.eval_high_hz, alpha=0.12)
    ax.set_xlim(max(20, freq.min()), min(300, freq.max()))
    ax.set_xlabel("Częstotliwość [Hz]")
    ax.set_ylabel("Błąd [dB]")
    ax.set_title("Błąd predykcji L+R")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    _safe_savefig(fig, out_dir / "prediction_vs_real_error.png", dpi=160)
    plt.close(fig)

    write_prediction_vs_real_readme(out_dir, metrics, verdict, validation_status, predicted_csv)

    # v19.4: dla folderu prediction_vs_real NIE uruchamiamy pełnego organizera.
    # Ten moduł ma pozostać płaski i jednoznaczny. Opcję zostawiamy tylko dla
    # kompatybilności z ewentualnymi zewnętrznymi skryptami.
    if organize_output:
        organize_results_folder(out_dir)
    return metrics


def infer_preset_from_prefix(prefix: str) -> Tuple[str, str, int]:
    low = prefix.lower()
    hz_match = re.search(r"([0-9]{2,3})\s*hz?", low)
    if not hz_match:
        hz_match = re.search(r"(^|[_-])([0-9]{2,3})([_-]|$)", low)
        hz = hz_match.group(2) if hz_match else "Custom"
    else:
        hz = hz_match.group(1)
    crossover = f"{hz} Hz" if hz != "Custom" and f"{hz} Hz" in CROSSOVER_PRESETS else "Custom"

    if "lr" in low:
        ft = "LR"
    elif "bw" in low or "butter" in low:
        ft = "BW"
    else:
        ft = "Other"

    slope_match = re.search(r"(24|48)", low)
    slope = int(slope_match.group(1)) if slope_match else 48
    return crossover, ft, slope


def find_preset_groups(folder: str | Path) -> Dict[str, Dict[str, Path]]:
    folder = Path(folder)
    groups: Dict[str, Dict[str, Path]] = {}
    pattern = re.compile(r"(.+?)_([lr])_(main|sub)(?:[_-].*)?$", re.IGNORECASE)
    for p in folder.iterdir():
        if not p.is_file() or p.suffix.lower() not in [".txt", ".csv", ".dat"]:
            continue
        m = pattern.match(p.stem)
        if not m:
            continue
        prefix = m.group(1)
        side = m.group(2).upper()
        part = m.group(3).lower()
        key = f"{side}_{part}"
        groups.setdefault(prefix, {})
        # Jeśli jest więcej niż jeden wariant, bierzemy pierwszy alfabetycznie,
        # ale użytkownik powinien dawać jeden bazowy L_sub/R_sub na preset.
        if key not in groups[prefix] or str(p) < str(groups[prefix][key]):
            groups[prefix][key] = p
    return groups


def cfg_for_preset(
    crossover_label: str,
    filter_type: str,
    slope_db_oct: int,
    base_cfg: OptimizerConfig,
) -> OptimizerConfig:
    if crossover_label in CROSSOVER_PRESETS and CROSSOVER_PRESETS[crossover_label] is not None:
        e_low, e_high, b_low, b_high = CROSSOVER_PRESETS[crossover_label]
    else:
        e_low, e_high, b_low, b_high = base_cfg.eval_low_hz, base_cfg.eval_high_hz, base_cfg.bulge_low_hz, base_cfg.bulge_high_hz

    defaults = filter_profile_defaults(filter_type, slope_db_oct)
    return OptimizerConfig(
        delay_min_ms=base_cfg.delay_min_ms,
        delay_max_ms=base_cfg.delay_max_ms,
        delay_step_ms=base_cfg.delay_step_ms,
        eval_low_hz=e_low,
        eval_high_hz=e_high,
        bulge_low_hz=b_low,
        bulge_high_hz=b_high,
        top_n=base_cfg.top_n,
        freq_points=base_cfg.freq_points,
        max_cartesian=base_cfg.max_cartesian,
        prefer_smoothness=float(defaults["prefer_smoothness"]),
        prefer_lr_sum=float(defaults["prefer_lr_sum"]),
        dip_penalty_weight=float(defaults["dip_penalty_weight"]),
        bulge_penalty_weight=float(defaults["bulge_penalty_weight"]),
        bulge_tolerance_db=float(defaults["bulge_tolerance_db"]),
        bulge_peak_penalty_weight=float(defaults["bulge_peak_penalty_weight"]),
        use_psycho_bulge_model=bool(defaults["use_psycho_bulge_model"]),
        filter_type=filter_type,
        slope_db_oct=int(slope_db_oct),
        scoring_profile=getattr(base_cfg, "scoring_profile", "PBIS"),
    )


def infer_profile_from_paths(paths: Dict[str, str | Path]) -> Tuple[Optional[str], Optional[str], Optional[int], List[str]]:
    """
    Autodetekcja cross/filtr/slope z nazw plików w trybie pojedynczej optymalizacji.

    Obsługiwane przykłady:
    - 70_BW48_L_main.txt
    - 70hz_BW48_R_sub_0.0.txt
    - cross70_LR48_L_main.txt
    - 75_LR48_slot4_R_sub_0.2.txt

    Zwraca: crossover_label, filter_type, slope, warnings.
    """
    warnings: List[str] = []
    stems = [Path(p).stem.lower().replace(",", ".") for p in paths.values() if str(p).strip()]
    joined = " ".join(stems)

    # Crossover
    hz_values: List[int] = []
    for s in stems:
        for m in re.finditer(r"(?<![0-9])([0-9]{2,3})\s*hz(?![a-z])", s):
            hz_values.append(int(m.group(1)))
        for m in re.finditer(r"(?:^|[_-])(?:cross)?([0-9]{2,3})(?:[_-]|$)", s):
            val = int(m.group(1))
            # Odrzucamy typowe slope/delay z nazw, np. 48, 24, 0, 1, 2.
            if 55 <= val <= 120:
                hz_values.append(val)

    crossover_label: Optional[str] = None
    if hz_values:
        # Najczęstsza wartość; przy remisie najniższa różnica nieistotna.
        counts = {}
        for v in hz_values:
            counts[v] = counts.get(v, 0) + 1
        hz = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        candidate = f"{hz} Hz"
        if candidate in CROSSOVER_PRESETS:
            crossover_label = candidate
        else:
            warnings.append(f"Wykryto crossover {hz} Hz, ale nie ma go na liście presetów.")
    else:
        warnings.append("Nie wykryto crossovera z nazw plików.")

    # Filter type
    filter_hits: List[str] = []
    for s in stems:
        if re.search(r"(^|[_-])lr\s*([0-9]{2})?", s):
            filter_hits.append("LR")
        if re.search(r"(^|[_-])bw\s*([0-9]{2})?", s) or "butter" in s:
            filter_hits.append("BW")
    filter_type: Optional[str] = None
    if filter_hits:
        filter_type = max(set(filter_hits), key=filter_hits.count)
    else:
        warnings.append("Nie wykryto typu filtra BW/LR z nazw plików.")

    # Slope
    slope_hits: List[int] = []
    for s in stems:
        for m in re.finditer(r"(?:bw|lr)\s*(24|48)", s):
            slope_hits.append(int(m.group(1)))
        for m in re.finditer(r"([24]8)\s*(?:db|dboct|db_oct|db-oct)?", s):
            val = int(m.group(1))
            if val in (24, 48):
                slope_hits.append(val)

    slope: Optional[int] = None
    if slope_hits:
        slope = max(set(slope_hits), key=slope_hits.count)
    else:
        # Jeśli wykryto BW/LR, ale brak slope, zostawiamy 48 jako najczęstszy w Twojej konfiguracji.
        if filter_type in ("BW", "LR"):
            slope = 48
            warnings.append("Nie wykryto slope z nazw plików — przyjęto 48 dB/oct.")
        else:
            warnings.append("Nie wykryto slope 24/48 z nazw plików.")

    return crossover_label, filter_type, slope, warnings


def compare_presets_folder(
    folder: str | Path,
    out_dir: str | Path,
    base_cfg: OptimizerConfig,
    stop_event=None,
    progress_callback=None,
) -> List[Dict[str, object]]:
    """
    Porównuje wiele presetów z folderu.
    Oczekiwane nazwy:
    70_BW48_L_main.txt
    70_BW48_L_sub_0.0.txt
    70_BW48_R_main.txt
    70_BW48_R_sub_0.0.txt
    """
    out_dir = prepare_flat_module_folder(
        out_dir,
        stale_patterns=[
            "preset_comparison.*",
            "OTWORZ_NAJPIERW.md",
            "README_PRESET_COMPARISON.md",
        ],
    )
    presets_dir = out_dir / "presets"
    if presets_dir.exists() and presets_dir.is_dir():
        try:
            shutil.rmtree(presets_dir)
        except Exception:
            pass
    presets_dir.mkdir(parents=True, exist_ok=True)
    groups = find_preset_groups(folder)
    summary: List[Dict[str, object]] = []

    complete_groups = [
        (prefix, files)
        for prefix, files in groups.items()
        if all(k in files for k in ["L_main", "L_sub", "R_main", "R_sub"])
    ]
    total_groups = max(len(complete_groups), 1)

    for idx, (prefix, files) in enumerate(complete_groups, start=1):
        if stop_event is not None and stop_event.is_set():
            raise CalculationCancelled("Porównanie presetów zatrzymane przez użytkownika.")
        if progress_callback:
            progress_callback(idx - 1, total_groups)
        crossover, ft, slope = infer_preset_from_prefix(prefix)
        cfg = cfg_for_preset(crossover, ft, slope, base_cfg)

        l_main = parse_rew_text(files["L_main"], "L_main")
        l_sub = parse_rew_text(files["L_sub"], "L_sub")
        r_main = parse_rew_text(files["R_main"], "R_main")
        r_sub = parse_rew_text(files["R_sub"], "R_sub")
        l_base = infer_base_delay_from_filename(files["L_sub"])
        r_base = infer_base_delay_from_filename(files["R_sub"])

        freq, responses, candidates = optimize_delays(l_main, l_sub, r_main, r_sub, cfg, stop_event=stop_event)
        sub_out = presets_dir / re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix)
        sub_out.mkdir(parents=True, exist_ok=True)

        input_warnings = build_input_warnings(
            {"L main": str(files["L_main"]), "L sub": str(files["L_sub"]), "R main": str(files["R_main"]), "R sub": str(files["R_sub"])},
            {"L_main": l_main, "L_sub": l_sub, "R_main": r_main, "R_sub": r_sub},
            cfg,
            l_base,
            r_base,
        )
        save_candidates_csv(candidates, sub_out / "ranking.csv", l_base, r_base, crossover, ft, slope, cfg)
        write_report(candidates, sub_out / "raport.md", cfg, l_base, r_base, crossover, ft, slope, input_warnings)
        if candidates:
            plot_candidate(freq, responses, candidates[0], sub_out / "best", cfg, l_base, r_base)
            create_followup_outputs(freq, responses, candidates, sub_out, cfg, l_base, r_base, crossover, ft, slope, organize_output=False)

        if candidates:
            best = candidates[0]
            off = best_both_off(candidates)
            conf_label, _ = confidence_assessment(candidates)
            summary.append({
                "preset": prefix,
                "crossover": crossover,
                "filter": f"{ft}{slope}",
                "score": best.score,
                "confidence": conf_label,
                "L_final_ms": final_delay(l_base, best.l_delay_ms),
                "L_pol": best.l_polarity,
                "R_final_ms": final_delay(r_base, best.r_delay_ms),
                "R_pol": best.r_polarity,
                "LR_construct_db": best.lr_construct_db,
                "LR_dip_db": best.lr_dip_db,
                "bulge_db": best.bulge_db,
                "OFF_OFF_score": off.score if off is not None else "",
                "folder": str(sub_out),
            })

    if progress_callback:
        progress_callback(total_groups, total_groups)

    summary.sort(key=lambda x: float(x["score"]), reverse=True)
    with (out_dir / "preset_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        if summary:
            writer = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            writer.writeheader()
            writer.writerows(summary)

    lines = ["# Porównanie presetów\n\n"]
    if not summary:
        lines.append("Nie znaleziono kompletnych grup plików.\n")
    else:
        lines.append("| # | Preset | Filter | Score | Confidence | L | L pol | R | R pol | LR constr | Dip | Bulge |\n")
        lines.append("|---:|---|---|---:|---|---:|---|---:|---|---:|---:|---:|\n")
        for i, row in enumerate(summary, start=1):
            lines.append(
                f"| {i} | {row['preset']} | {row['filter']} | {float(row['score']):.3f} | {row['confidence']} | "
                f"{float(row['L_final_ms']):.2f} | {row['L_pol']} | {float(row['R_final_ms']):.2f} | {row['R_pol']} | "
                f"{float(row['LR_construct_db']):.2f} | {float(row['LR_dip_db']):.2f} | {float(row['bulge_db']):.2f} |\n"
            )
    (out_dir / "preset_comparison.md").write_text("".join(lines), encoding="utf-8")
    write_preset_comparison_readme(out_dir, summary)
    return summary
