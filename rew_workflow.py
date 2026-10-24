"""Task adapters for the three workspaces. Structured output; no log scraping.

The 20.31 DSP engines remain authoritative for their safety thresholds.
Partial SPL comparisons cannot grant phase/design/export permission.
"""
from __future__ import annotations

import copy
import json
import traceback
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from rew_project import (active_config, atomic_json, common_gains, digest, file_path, fingerprint,
                         number, sha256, uid, utc_now, verify_sources)
from rew_source_binding import bound_sources


def numeric_records(path):
    from rew_mdat import scan_mdat_project, _decode_mdat_numeric_records, _scan_java_serialized_float_arrays
    from rew_api_client import is_rew_api_snapshot, read_snapshot, decode_frequency_response, verify_snapshot_response_integrity
    from rew_common import MdatMeasurementInfo
    from rew_mdat import _MdatNumericRecord, _MdatPhaseCandidate, _snapshot_measurement_records
    from rew_measurement_validation import validate_numeric_response_arrays
    path = file_path(path)
    if is_rew_api_snapshot(path):
        payload, source_records = _snapshot_measurement_records(path)
        ok, problems = verify_snapshot_response_integrity(payload)
        if not ok:
            raise ValueError("Integralność migawki REW: " + "; ".join(problems))
        records = []
        for item in source_records:
            arrays = decode_frequency_response(item["record"]["frequencyResponse"])
            freq, spl, phase = (np.asarray(arrays[key]) for key in ("frequency_hz", "spl_db", "phase_deg"))
            info = MdatMeasurementInfo(item["index"], item["name"], item["role"], item["index"])
            records.append(_MdatNumericRecord(info, freq, spl, [_MdatPhaseCandidate(1, item["index"], freq, phase)]))
    else:
        scan = scan_mdat_project(path)
        data = path.read_bytes()
        records, warnings = _decode_mdat_numeric_records(data, scan["measurements"], _scan_java_serialized_float_arrays(data))
        if warnings:
            raise ValueError("Niepełny odczyt MDAT: " + "; ".join(warnings[:4]) + " Wczytaj migawkę z otwartego REW.")
    if not records:
        raise ValueError("Nie znaleziono obsługiwanych śladów. Wczytaj pomiary z otwartego REW, aby nadać role bez zmiany nazw.")
    for record in records:
        # SPL-only capability still validates magnitudes and its own axis.
        if not record.phase_candidates:
            result = validate_numeric_response_arrays(record.freq, record.spl_db, np.zeros_like(record.freq), name=record.info.name)
            if not result.ok:
                raise ValueError("; ".join(result.messages))
        for phase in record.phase_candidates:
            result = validate_numeric_response_arrays(record.freq, record.spl_db, phase.phase_deg, name=record.info.name)
            if not result.ok:
                raise ValueError("; ".join(result.messages))
    return records


def import_source(path):
    path = file_path(path)
    if path.suffix.lower() not in {".mdat", ".json"}:
        raise ValueError("Wybierz MDAT albo migawkę REW JSON.")
    records = numeric_records(path)
    source = {"id": uid(), "path": str(path), "name": path.name, "sha256": sha256(path),
              "stage": "Nieustalony", "configuration": "", "mapping_confirmed": False,
              "raw_confirmed": False, "session_confirmed": False, "timing_confirmed":False, "gain_state": "", "traces": []}
    for record in records:
        role = record.info.role
        if role in {"L strona", "R strona"}:
            role = role[0] + " sum"
        if role in {"L+R sum", "L+P", "L+R strona"}:
            role = "L+R"
        if role in {"L Dirac ON", "L Dirac OFF", "R Dirac ON", "R Dirac OFF"}:
            role = role[0] + " sum"
        if role in {"L+R Dirac ON", "L+R Dirac OFF"}:
            role = "L+R"
        from rew_project import ROLES
        if role not in ROLES:
            role = "Nieprzypisany"
        source["traces"].append({"id": digest([source["sha256"], int(record.info.position)]),
             "position": int(record.info.position), "name": record.info.name, "role": role,
             "microphone": "", "preset": "", "stage": "Nieustalony",
             "range_hz": [float(record.freq[0]), float(record.freq[-1])],
             "phase_candidates": len(record.phase_candidates)})
    source["checks"] = {"spl": True, "message": "Odczytano SPL. Potwierdź role, etap i pozycję mikrofonu."}
    return source


def _source(project, stage):
    sources = [s for s in project["sources"] if s["stage"] == stage]
    if len(sources) != 1:
        raise ValueError(f"Wybierz jedną sesję „{stage}” w Pomiary (obecnie: {len(sources)}). Pozostałym przypisz inny etap lub pozostaw jako historię.")
    s = sources[0]
    if not s["mapping_confirmed"]:
        raise ValueError(f"Potwierdź przypisanie śladów w {s['name']}.")
    used = [t for t in s["traces"] if t["role"] != "Nieprzypisany"]
    positions = {t["microphone"].strip() for t in used}
    if not used or "" in positions or len(positions) != 1:
        raise ValueError(f"{s['name']}: projektowanie wymaga jednej jawnie opisanej pozycji mikrofonu. Kanał L/R nie jest pozycją mikrofonu.")
    if any(t["stage"] != stage for t in used):
        raise ValueError(f"{s['name']}: ślady mają różne etapy. Popraw przypisanie sesji.")
    return s


def check_measurements(project):
    from rew_mdat import import_mdat_measurements
    results = []
    with bound_sources(project["sources"]):
        for source in project["sources"]:
            result = {"source_id": source["id"], "spl": False, "phase": False}
            try:
                if sha256(source["path"]) != source["sha256"]:
                    raise ValueError("Źródło zmieniło zawartość.")
                records = numeric_records(source["path"])
                result["spl"] = True
                if source["stage"] == "Bazowe":
                    _source(project, "Bazowe")
                    if not source["raw_confirmed"]:
                        raise ValueError("Potwierdź: Dirac/PEQ/zwrotnica OFF, delay 0, polaryzacja OFF, REL 120 Hz, wspólna referencja czasu.")
                    imported = import_mdat_measurements(source["path"])
                    from rew_mdat import _phase_truth_guard
                    _, truth = _phase_truth_guard(records, ["L main", "L sub", "R main", "R sub"])
                    result["phase"] = bool(imported.phase_metrics_allowed and len(truth["sides"])==2 and all(s["status"]=="VERIFIED" for s in truth["sides"]))
                    result["phase_details"] = truth
                    result["message"] = ("Sprawdzono odczyt, fazę i sumowanie." if result["phase"] else
                         "SPL dostępny. Brakuje jednoznacznej zgodności fazy z sumami kontrolnymi; dobór opóźnień zablokowany.")
                else:
                    result["message"] = "SPL dostępny do porównania. Nie potwierdzono absolutnych delayów ani odporności przestrzennej."
            except ValueError as exc:
                result["message"] = str(exc)
            results.append(result)
        try:
            gains = common_gains(project)
            raw, bridge = _source(project, "Bazowe"), _source(project, "Zmiana poziomu")
            verify_sources(project)
            if raw["gain_state"] != "raw" or bridge["gain_state"] != "listening":
                raise ValueError("Przypisz sesji bazowej poziom Bazowy, a sesji zmiany poziomu — Odsłuch.")
            if {t["microphone"] for t in raw["traces"] if t["role"]!="Nieprzypisany"} != {t["microphone"] for t in bridge["traces"] if t["role"]!="Nieprzypisany"}:
                raise ValueError("Pomiary poziomów mają różne pozycje mikrofonu.")
            if not raw["session_confirmed"] or not bridge["session_confirmed"]:
                raise ValueError("Potwierdź wspólny tor, głośność główną i pozycję mikrofonu dla pomiarów obu poziomów.")
            from rew_gain_bridge import analyze_gain_bridge
            audit = analyze_gain_bridge(Path(raw["path"]), Path(bridge["path"]), gains["listening"] - gains["raw"])
            bridge_result = {"status": audit.status, "message": audit.reason, "left_db": audit.left.gain_db, "right_db": audit.right.gain_db}
        except ValueError as exc:
            bridge_result = {"status": "MISSING_OR_BLOCKED", "message": str(exc)}
    return {"sources": results, "levels": bridge_result}


def _raw_and_gain(project):
    from rew_mdat import load_optimizer_measurements_from_mdat
    from rew_gain_bridge import analyze_gain_bridge
    from rew_gain_state import build_gain_state
    from rew_scoring import common_grid, measurement_to_complex
    verify_sources(project)
    gains = common_gains(project)
    raw = _source(project, "Bazowe")
    bridge = _source(project, "Zmiana poziomu")
    if raw["gain_state"] != "raw" or bridge["gain_state"] != "listening":
        raise ValueError("W Pomiary przypisz sesji bazowej poziom Bazowy, a sesji zmiany poziomu — Odsłuch.")
    if not raw["raw_confirmed"] or not raw["session_confirmed"] or not bridge["session_confirmed"]:
        raise ValueError("W Pomiary potwierdź warunki RAW i zgodność sesji przy drugim poziomie.")
    if {t["microphone"] for t in raw["traces"] if t["role"] != "Nieprzypisany"} != {t["microphone"] for t in bridge["traces"] if t["role"] != "Nieprzypisany"}:
        raise ValueError("Pomiary obu poziomów mają różne pozycje mikrofonu.")
    lm, ls, rm, rs, imported = load_optimizer_measurements_from_mdat(raw["path"])
    if not imported.phase_metrics_allowed:
        raise ValueError("Nie potwierdzono fazy i sumowania RAW. Uzupełnij rzeczywiste sumy L main+sub i R main+sub w tej samej sesji.")
    from rew_mdat import _phase_truth_guard
    _, truth = _phase_truth_guard(numeric_records(raw["path"]), ["L main", "L sub", "R main", "R sub"])
    if len(truth["sides"]) != 2 or any(s["status"] != "VERIFIED" for s in truth["sides"]):
        raise ValueError("Nie potwierdzono RAW MAIN+SUB=SUM po obu stronach. Natywna faza API ani sama poprawność formatu nie zastępuje sum kontrolnych.")
    audit = analyze_gain_bridge(Path(raw["path"]), Path(bridge["path"]), gains["listening"] - gains["raw"])
    if audit.status != "VERIFIED":
        raise ValueError(audit.reason)
    state = build_gain_state(raw_db=gains["raw"], dirac_db=gains["dirac"], listening_db=gains["listening"], source="GAIN_BRIDGE",
             verified=True, bridge_path=bridge["path"], bridge_left_delta_db=audit.left.gain_db, bridge_right_delta_db=audit.right.gain_db)
    freq = common_grid([lm, ls, rm, rs], points=2200)
    freq = freq[(freq >= 20) & (freq <= 500)]
    return raw, bridge, gains, state, freq, tuple(measurement_to_complex(m, freq) for m in (lm, rm, ls, rs))


def _response(freq, raw, sections, l_rel, r_rel, gain_state, state_name):
    from rew_filter_generator import freq_response
    from rew_scoring import apply_delay_and_polarity
    lm, rm, ls, rs = raw
    ls, rs = gain_state.scale_complex_subs(ls, rs, state_name)
    main, sub = sections
    hm, hs = freq_response(main, 96000, freq), freq_response(sub, 96000, freq)
    return lm * hm, rm * hm, apply_delay_and_polarity(ls * hs, freq, l_rel, False), apply_delay_and_polarity(rs * hs, freq, r_rel, False)


def _reference_response(project, freq, raw, state, state_name):
    from rew_filter_generator import make_sections
    c = active_config(project)
    if c is None:
        raise ValueError("W „Mój system” wpisz i wybierz ustawienie odniesienia.")
    s = c["settings"]
    sections = (make_sections("HP", 96000, s["fc"], s["filter"]), make_sections("LP", 96000, s["fc"], s["filter"]))
    return _response(freq, raw, sections, s["l_sub_ms"]-s["l_main_ms"], s["r_sub_ms"]-s["r_main_ms"], state, state_name)


def verify_reference(project):
    c = active_config(project)
    if not c or not c["console_confirmed"]:
        raise ValueError("W „Mój system” wpisz wartości z konsoli i potwierdź konfigurację odniesienia.")
    if abs(c["settings"]["l_main_ms"]-c["settings"]["r_main_ms"]) > 1e-9:
        raise ValueError("Projektant zakłada wspólny delay kolumn L/R. Zapisano różne wartości; porównanie pomiarów działa, ale ten model nie może projektować ich sumy.")
    with bound_sources(project["sources"]):
        _, _, _, state, freq, raw = _raw_and_gain(project)
        sources = [s for s in project["sources"] if s["stage"] == "Przed Dirac" and s["configuration"] == c["key"] and s["mapping_confirmed"]]
        if len(sources) != 1:
            raise ValueError("Wczytaj pomiar odniesienia przed Dirac i przypisz go do aktywnej wersji konfiguracji. Potrzebne są MAIN, SUB i SUM osobno dla L i R.")
        source = sources[0]
        if any(t["preset"] != c["key"] or t["stage"] != "Przed Dirac" for t in source["traces"] if t["role"] != "Nieprzypisany"):
            raise ValueError("Pomiar odniesienia zawiera ślady innej konfiguracji lub etapu. Popraw ich przypisanie.")
        if not source["session_confirmed"] or source["gain_state"] not in ("raw", "dirac", "listening"):
            raise ValueError("W Pomiary wskaż poziom sesji odniesienia oraz potwierdź ten sam tor, głośność i pozycję co RAW.")
        raw_source = _source(project, "Bazowe")
        reference_positions = {t["microphone"].strip() for t in source["traces"] if t["role"] != "Nieprzypisany"}
        raw_positions = {t["microphone"].strip() for t in raw_source["traces"] if t["role"] != "Nieprzypisany"}
        if reference_positions != raw_positions or "" in reference_positions:
            raise ValueError("Pomiar odniesienia musi mieć tę samą opisaną pozycję mikrofonu co RAW.")
        records = numeric_records(source["path"])
        by_role = {}
        for r in records:
            by_role.setdefault(r.info.role, []).append(r)
        from rew_mdat import _phase_truth_guard, _record_complex
        selected_phase, truth = _phase_truth_guard(records, ["L main", "R main", "L sub", "R sub"])
        if truth["status"] not in ("VERIFIED", "VERIFIED_PARTIAL") or len(truth["sides"]) != 2 or any(s["status"] != "VERIFIED" for s in truth["sides"]):
            raise ValueError("Pomiar odniesienia nie potwierdza sum obu stron. Sam hash MDAT nie dowodzi delayów; dodaj rzeczywiste L_SUM i R_SUM z tej sesji.")
        lm, rm, ls, rs = _reference_response(project, freq, raw, state, source["gain_state"])
        errors = {}
        for side, predicted_main, predicted_sub in (("L",lm,ls),("R",rm,rs)):
            mr, sr = by_role.get(side+" main",[]), by_role.get(side+" sub",[])
            if len(mr)!=1 or len(sr)!=1:
                raise ValueError(f"Odniesienie wymaga jednego MAIN i jednego SUB dla strony {side}; powtórzenia SUM są dozwolone.")
            measured_main=_record_complex(mr[0],selected_phase[side+" main"],freq)
            measured_sub=_record_complex(sr[0],selected_phase[side+" sub"],freq)
            mask=(freq>=.8*c["settings"]["fc"]) & (freq<=1.25*c["settings"]["fc"])
            phase_error=np.abs(np.angle((measured_main*np.conj(measured_sub))*np.conj(predicted_main*np.conj(predicted_sub)),deg=True))[mask]
            median,p90=float(np.median(phase_error)),float(np.percentile(phase_error,90))
            errors[side+"_relative_phase"]={"median_deg":median,"p90_deg":p90,"band_hz":[.8*c["settings"]["fc"],1.25*c["settings"]["fc"]]}
            if median>10 or p90>20:
                raise ValueError(f"Konfiguracja nie odtwarza względnej fazy {side}: mediana {median:.1f}°, P90 {p90:.1f}° (limity 10°/20°). Sprawdź delaye i powiązanie sesji.")
        for role, predicted in (("L main", lm), ("R main", rm), ("L sub", ls), ("R sub", rs), ("L sum", lm+ls), ("R sum", rm+rs)):
            selected = by_role.get(role, [])
            if not selected:
                raise ValueError(f"W pomiarze odniesienia brakuje {role}.")
            for record in selected:
                low, high = max(40, record.freq[0]), min(160, record.freq[-1])
                mask = (freq >= low) & (freq <= high)
                if low > 40.1 or high < 159.9 or mask.sum() < 40:
                    raise ValueError("Weryfikacja odniesienia wymaga pasma 40–160 Hz.")
                diff = np.abs(np.interp(freq[mask], record.freq, record.spl_db) - 20*np.log10(np.maximum(np.abs(predicted[mask]), 1e-30)))
                median, p90 = float(np.median(diff)), float(np.percentile(diff, 90))
                errors[str(record.info.position)] = {"role": role, "median_db": median, "p90_db": p90}
                if median > 1.0 or p90 > 2.0:
                    raise ValueError(f"Konfiguracja nie odtwarza {role}: błąd mediany {median:.2f} dB, P90 {p90:.2f} dB (limity 1/2 dB). Sprawdź przypisanie sesji, poziom i ustawienia.")
    return {"configuration": c["key"], "fingerprint": fingerprint(project), "scope": "CONSOLE_AND_RELATIVE_SUM", "verified_at": utc_now(),
            "source_sha256": source["sha256"], "errors": errors,
            "message": "Sprawdzono zgodność odpowiedzi i sumowania z konfiguracją. Absolutne delaye pochodzą z potwierdzonego wpisu z konsoli, nie z identyfikacji hashem."}


def _write_anchor(project, evidence, path):
    c, s = active_config(project), active_config(project)["settings"]
    if evidence.get("configuration") != c["key"] or evidence.get("fingerprint") != fingerprint(project):
        raise ValueError("Dowód odniesienia dotyczy innego stanu projektu. Sprawdź odniesienie ponownie.")
    token = digest([c["id"], c["version"], float(s["fc"]), s["filter"], s["l_main_ms"], s["r_main_ms"], s["l_sub_ms"], s["r_sub_ms"]])
    fields = {"name": "active_reference", "configuration_id": c["id"], "configuration_version": c["version"],
        "crossover_hz": s["fc"], "filter": s["filter"], "verified": "true", "verification_status": "MEASURED",
        "verification_scope": evidence["scope"], "configuration_digest": token,
        "verified_by_measurement_sha256": evidence["source_sha256"], "verified_at": evidence["verified_at"],
        **{k: s[k] for k in ("l_main_ms", "r_main_ms", "l_sub_ms", "r_sub_ms")}}
    Path(path).write_text("\n".join(f"{k}: {v}" for k, v in fields.items())+"\n", encoding="utf-8")


def _metric_summary(freq, response, reference, xo):
    def db(z):
        return 20*np.log10(np.maximum(np.abs(z), 1e-30))
    lm, rm, ls, rs = response
    blm, brm, bls, brs = reference
    result = {}
    mask = (freq >= .8*xo) & (freq <= 1.25*xo)
    for side, m, s, bm, bs in (("L", lm, ls, blm, bls), ("R", rm, rs, brm, brs)):
        result[f"{side}_sum_gain_delta_db"] = float(np.mean((db(m+s)-np.maximum(db(m), db(s)) - db(bm+bs)+np.maximum(db(bm), db(bs)))[mask]))
    for name, lo, hi in (("foundation_delta_db", 30, 60), ("body_delta_db", 100, 220), ("stereo_delta_db", 80, 110)):
        mask = (freq >= lo) & (freq <= hi)
        result[name] = float(np.mean((db(lm+rm+ls+rs)-db(blm+brm+bls+brs))[mask])) if freq[0] <= lo and freq[-1] >= hi and mask.sum() > 4 else None
    result["integration_band_hz"] = [.8*xo, 1.25*xo]
    return result


def _candidate_sections(candidate):
    from rew_filter_generator import make_sections, make_custom_sections
    s = candidate["settings"]
    if candidate["kind"] == "standard":
        return make_sections("HP", 96000, s["main_fc_hz"], s["filter_label"]), make_sections("LP", 96000, s["sub_fc_hz"], s["filter_label"])
    shape = s["shape"]
    return make_custom_sections("HP", 96000, shape["main_fc"], shape["main_q"]), make_custom_sections("LP", 96000, shape["sub_fc"], shape["sub_q"])


def anchored_delays(project, settings):
    cfg = active_config(project)["settings"]
    values = [cfg["l_main_ms"], cfg["r_main_ms"],
              cfg["l_main_ms"] + settings["l_rel_ms"],
              cfg["r_main_ms"] + settings["r_rel_ms"]]
    if not all(np.isfinite(v) and 0 <= v <= 30 for v in values):
        raise ValueError("Relacja opóźnień wymaga wartości poza 0–30 ms przy wybranych delayach kolumn. Nie zmieniono odniesienia.")
    return values


def design(project, out, kind, emit):
    from rew_candidate_safety import NoSafeCandidateError
    evidence = verify_reference(project)
    out.mkdir(parents=True, exist_ok=True)
    anchor = out / "active_reference.yaml"
    _write_anchor(project, evidence, anchor)
    with bound_sources(project["sources"]):
        raw_source, bridge, gains, gain_state, freq, raw = _raw_and_gain(project)
        cfg = active_config(project)["settings"]
        a = project["analysis"]
        if not a.get("confirmed"):
            raise ValueError("W „Mój system” potwierdź zakres szukania częstotliwości.")
        low, high, step = number(a["fc_low"], low=20, high=200), number(a["fc_high"], low=20, high=200), number(a["fc_step"], low=.5, high=20)
        if high < low or not a["families"]:
            raise ValueError("Błędny zakres poszukiwań lub brak rodzin filtrów.")
        kwargs = dict(mdat_path=Path(raw_source["path"]), out_dir=out, user_reference_path=anchor,
             raw_sub_trim_db=gains["raw"], dirac_sub_trim_db=gains["dirac"], listening_sub_trim_db=gains["listening"],
             gain_bridge_path=Path(bridge["path"]), shd_anchor_main=cfg["l_main_ms"], prepare_import_files=False)
        try:
            if kind == "standard":
                from rew_acoustic_crossover_designer import design_acoustic_crossovers
                selected = design_acoustic_crossovers(**kwargs, xo_values=sorted(set([cfg["fc"], *np.arange(low, high+.001, step)])),
                     filters=list(dict.fromkeys([cfg["filter"], *a["families"]])), top_n=6, gain_mode="fixed",
                     gain_bridge_target_delta_db=gains["dirac"]-gains["raw"],
                     progress_callback=lambda d,t,msg: emit(msg))
                pairs = [("standard", c) for c in selected]
            else:
                from rew_acoustic_biquad_designer import design_acoustic_biquads
                pairs = design_acoustic_biquads(**kwargs, search_level="normal", allpass_mode="off", empirical_calibration_enabled=False,
                    delay_refinement_enabled=False, reference_settings=cfg, search_frequencies=list(np.arange(low, high+.001, step)), progress=emit)
        except NoSafeCandidateError:
            data = json.loads((out / "QUARANTINE_DIAGNOSTIC.json").read_text(encoding="utf-8"))
            reasons = list(dict.fromkeys(reason for row in data["candidates"] for reason in row["reasons"]))
            return {"status": "no_candidate", "message": "Nie znaleziono ustawienia dopuszczonego do testu. Pozostaw obecne ustawienie.", "reasons": reasons,
                    "next": "Sprawdź przyczyny w szczegółach; nie zmieniaj aktywnego slotu.", "candidates": []}
        candidates = []
        rejected_reasons = []
        reference = _reference_response(project, freq, raw, gain_state, "dirac")
        for profile, obj in pairs:
            if not obj.candidate_safe_to_export:
                continue
            if kind == "custom":
                from rew_acoustic_biquad_designer import _export_status
                if _export_status(profile, obj)[0] == "DIAGNOSTYKA_NIE_WGRYWAC":
                    continue
                settings = {"shape": asdict(obj.shape), "l_rel_ms": obj.l_rel_ms, "r_rel_ms": obj.r_rel_ms}
            else:
                settings = {key: getattr(obj, key) for key in ("main_fc_hz", "sub_fc_hz", "filter_label", "l_rel_ms", "r_rel_ms", "xo_hz")}
            try:
                anchored_delays(project, settings)
            except ValueError as exc:
                rejected_reasons.append(str(exc))
                continue
            candidate = {"id": digest([kind, settings]), "kind": kind, "profile": profile, "settings": settings, "label": "Własna zwrotnica" if kind == "custom" else f"{obj.filter_label}: HP {obj.main_fc_hz:g} / LP {obj.sub_fc_hz:g} Hz",
                         "evidence": "Z obliczeń — do sprawdzenia pomiarem", "safe": True, "state_metrics": obj.state_metrics}
            response = _response(freq, raw, _candidate_sections(candidate), settings["l_rel_ms"], settings["r_rel_ms"], gain_state, "dirac")
            candidate["metrics"] = _metric_summary(freq, response, reference, cfg["fc"])
            candidate["curve"] = {"frequency": freq[::5].tolist(), "spl": (20*np.log10(np.maximum(np.abs(sum(response)), 1e-30)))[::5].tolist()}
            if candidate["metrics"]["L_sum_gain_delta_db"] <= .1 and candidate["metrics"]["R_sum_gain_delta_db"] <= .1:
                continue
            if all(candidate["id"] != c["id"] for c in candidates):
                candidates.append(candidate)
            if len(candidates) == 2:
                break
    return {"status": "ok" if candidates else "no_candidate", "message": "Wybrano warianty do sprawdzenia; nie są potwierdzoną poprawą brzmienia." if candidates else "Brak uzasadnionego wariantu z poprawą sumowania. Pozostaw odniesienie.",
            "reasons": ["Finaliści mają wspólne metryki fizyczne. Nie łączono ocen 0–100 w ranking brzmienia.", "To symulacja w jednej pozycji, nie pomiar odporności na ruch głowy.", *dict.fromkeys(rejected_reasons)],
            "next": "Przygotuj wybrany wariant do próby pomiarowej z Dirac OFF; później wykonaj nową kalibrację Dirac." if candidates else "Pozostaw obecne ustawienie. Szczegóły podają przyczyny odrzucenia.", "candidates": candidates,
            "reference_curve": {"frequency": freq[::5].tolist(), "spl": (20*np.log10(np.maximum(np.abs(sum(reference)), 1e-30)))[::5].tolist()}}


def compare(project, stage, config_a, config_b):
    if config_a == config_b:
        raise ValueError("Wybierz dwie różne konfiguracje A i B.")
    groups = {config_a: {}, config_b: {}}
    for source in project["sources"]:
        if not source["mapping_confirmed"]:
            continue
        if sha256(source["path"]) != source["sha256"]:
            raise ValueError("Źródło porównania zmieniło zawartość.")
        records = {int(r.info.position): r for r in numeric_records(source["path"])}
        for t in source["traces"]:
            key = t["preset"] or source["configuration"]
            if key not in groups or t["stage"] != stage:
                continue
            if not t["microphone"].strip():
                raise ValueError("Wpisz pozycję mikrofonu w przypisaniu śladów; nie można założyć jej z kanału L/R.")
            if t["role"] not in {"L sum", "R sum", "L+R"}:
                continue
            record = records.get(t["position"])
            if record:
                groups[key].setdefault((t["microphone"], t["role"]), []).append(record)
    matched = sorted(set(groups[config_a]) & set(groups[config_b]))
    if not matched:
        raise ValueError("Brak par sum A/B dla tego samego kanału, etapu i pozycji mikrofonu. Popraw przypisanie śladów w Pomiary.")
    rows, curves = [], []
    for key in matched:
        ra, rb = groups[config_a][key], groups[config_b][key]
        lo = max(r.freq[0] for r in ra+rb)
        hi = min(r.freq[-1] for r in ra+rb)
        if hi <= lo:
            raise ValueError("Brak wspólnego zakresu częstotliwości.")
        freq = np.geomspace(lo, hi, 800)
        # Explicit dB mean of repeated measurements; never presented as a coherent sum.
        a = np.mean([np.interp(freq, r.freq, r.spl_db) for r in ra], axis=0)
        b = np.mean([np.interp(freq, r.freq, r.spl_db) for r in rb], axis=0)
        metrics = {}
        for name, low, high in (("30–60 Hz", 30, 60), ("80–110 Hz", 80, 110), ("100–220 Hz", 100, 220)):
            mask = (freq >= low) & (freq <= high)
            metrics[name] = float(np.mean((b-a)[mask])) if lo <= low and hi >= high else None
        rows.append({"position": key[0], "role": key[1], "delta_db": metrics, "repeats": [len(ra), len(rb)]})
        curves.append({"position": key[0], "role": key[1], "frequency": freq.tolist(), "A": a.tolist(), "B": b.tolist()})
    positions = sorted({key[0] for key in matched})
    details = measured_details(project, stage, config_a, config_b)
    balances=[]
    for position in positions:
        left=next((c for c in curves if c["position"]==position and c["role"]=="L sum"),None)
        right=next((c for c in curves if c["position"]==position and c["role"]=="R sum"),None)
        if left and right and max(left["frequency"][0],right["frequency"][0])<=80 and min(left["frequency"][-1],right["frequency"][-1])>=110:
            grid=np.geomspace(80,110,200)
            balances.append({"position":position,"band_hz":[80,110],**{k:float(np.mean(np.interp(grid,left["frequency"],left[k])-np.interp(grid,right["frequency"],right[k]))) for k in ("A","B")}})
    summaries=[
        "Połączenie kolumn–sub: " + ("zgodność sumowania sprawdzona dla " + str(len(details["integration"])) + " par; metryki fazy w szczegółach." if details["integration"] else "dostępne SPL; brak podstaw do wniosków o fazie."),
        "Bas i dolna średnica: rzeczywiste różnice B−A w tabeli; nie wyrównano poziomu pomiarów.",
        "Kanały i pozycje: " + ("; ".join(f"{b['position']}: L−R (80–110 Hz) A {b['A']:+.2f} / B {b['B']:+.2f} dB" for b in balances) if balances else "brak kompletnej pary L/R do oceny równowagi") + (". Jedna pozycja — bez oceny ruchu głowy." if len(positions)==1 else f". Pozycje: {', '.join(positions)}; bez wniosków poza nimi."),
        "Odsłuch: Twój wybór poniżej; program nie wyznacza go z pomiaru."]
    return {"status": "ok", "message": "Porównano rzeczywiste SPL. Zmiana poziomu sumy nie jest samodzielnym dowodem poprawy fazy.",
        "stage": stage, "config_a": config_a, "config_b": config_b, "rows": rows, "curves": curves, "candidates": [],
        "measured_details": details, "channel_balance_db":balances, "summaries":summaries,
        "reasons": ["Wyniki B−A w dB, bez automatycznego wyrównania głośności.",
            "Brak wniosków o fazie, optymalnym delayu i wygaszaniu z samego SPL.",
            "Jedna pozycja — nie oceniono odporności przestrzennej." if len(positions)==1 else "Porównano tylko wskazane pozycje: " + ", ".join(positions),
            "Po zmianie zwrotnicy/delayu wymagany jest nowy pomiar i właściwy filtr Dirac dla tej wersji."],
        "next": "Wyrównaj głośność odsłuchu A/B i zapisz własny wybór."}


def measured_details(project, stage, config_a, config_b):
    """Capabilities are evaluated per domain, using IDs and explicit assignments."""
    from rew_api_client import is_rew_api_snapshot
    from rew_mdat import _phase_truth_guard, _record_complex
    grouped = {}
    time_rows = []
    for source in project["sources"]:
        if not source["mapping_confirmed"] or not source.get("timing_confirmed"):
            continue
        records = {int(r.info.position):r for r in numeric_records(source["path"])}
        relevant = [t for t in source["traces"] if t["stage"]==stage and (t["preset"] or source["configuration"]) in {config_a,config_b}]
        for trace in relevant:
            r = records.get(trace["position"])
            if r:
                key = (trace["preset"] or source["configuration"], trace["microphone"])
                grouped.setdefault(key,[]).append(replace(r,info=replace(r.info,role=trace["role"],name=trace["id"])))
        if is_rew_api_snapshot(source["path"]):
            from rew_time_domain_analyzer import load_time_measurements
            try:
                _, items, warnings = load_time_measurements(Path(source["path"]), max_time_ms=500.)
                for trace in relevant:
                    i = int(trace["position"])-1
                    if i<0 or i>=len(items):
                        continue
                    item = items[i]
                    time_rows.append({"configuration":trace["preset"] or source["configuration"],"position":trace["microphone"],"role":trace["role"],
                        "gd_source":item.gd_source,"gd_median_80_110_ms":item.gd_median_80_110_ms,
                        "impulse_available":item.impulse is not None,
                        "late100_db":item.composite_late100_db if item.impulse is not None else None,
                        "t20_ms":item.composite_t20_ms if item.impulse is not None else None,
                        "warnings":warnings})
            except ValueError as exc:
                time_rows.append({"source_id":source["id"],"message":str(exc)})
    integrations=[]
    for (cfg,mic),records in grouped.items():
        for side in ("L","R"):
            roles=[side+" main",side+" sub"]
            if any(sum(r.info.role==role for r in records)!=1 for role in roles):
                continue
            chosen,truth=_phase_truth_guard(records,roles)
            if truth["status"] not in {"VERIFIED","VERIFIED_PARTIAL"}:
                continue
            main=next(r for r in records if r.info.role==roles[0])
            sub=next(r for r in records if r.info.role==roles[1])
            if max(main.freq[0],sub.freq[0])>80 or min(main.freq[-1],sub.freq[-1])<110:
                continue
            grid=np.geomspace(80,110,200)
            m,s=_record_complex(main,chosen[roles[0]],grid),_record_complex(sub,chosen[roles[1]],grid)
            gain=20*np.log10(np.maximum(np.abs(m+s),1e-30)/np.maximum(np.maximum(np.abs(m),np.abs(s)),1e-30))
            phase=np.abs(np.angle(m*np.conj(s),deg=True))
            integrations.append({"configuration":cfg,"position":mic,"channel":side,"band_hz":[80,110],"sum_gain_db":float(np.mean(gain)),"phase_difference_deg":float(np.mean(phase)),"proof":"Zgodność MAIN+SUB z rzeczywistym SUM"})
    return {"integration":integrations,"time":time_rows,
        "scope":"Metryki fazy i czasu tylko dla jawnie potwierdzonej metody timing reference; brak impulsu nie unieważnia SPL. Żadna z tych wartości nie mierzy naturalności ani szybkości brzmienia."}


def run_task(project, out, task, args=None, emit=print):
    args = args or {}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    emit({"type": "stage", "message": {"check":"Sprawdzanie pomiarów", "standard":"Dobór zwrotnicy i opóźnień", "custom":"Projektowanie własnej zwrotnicy", "compare":"Porównanie pomiarów", "verify":"Sprawdzanie odniesienia", "export":"Ponowna kontrola wybranego wariantu"}.get(task, task)})
    result = {"schema": 1, "run_id": out.name, "fingerprint": fingerprint(project), "task": task, "created": utc_now(), "sources": [{"id":s["id"], "sha256":s["sha256"]} for s in project["sources"]], "candidates": []}
    try:
        if task == "import":
            result.update(status="ok", message="Odczytano pomiary. Sprawdź przypisanie.", source=import_source(args["path"]), next="Przypisz role i etap pomiaru.")
        elif task == "api_list":
            from rew_api_client import REWApiClient
            result.update(status="ok", message="Pobrano listę pomiarów.", measurements=[asdict(m) for m in REWApiClient(args["url"]).list_measurements()])
        elif task == "api_import":
            from rew_api_client import REWApiClient, REWApiMeasurement
            path = out / "source.rewapi.json"
            REWApiClient(args["url"]).create_snapshot([REWApiMeasurement(**m) for m in args["measurements"]], path, include_group_delay=True, include_impulse=True)
            result.update(status="ok", message="Pobrano pomiary z REW.", source=import_source(path), next="Przypisz role, konfiguracje i pozycje mikrofonu.")
        elif task == "check":
            result.update(status="ok", message="Kontrola zakończona. Zobacz dostępne czynności i ewentualne braki.", checks=check_measurements(project), next="Potwierdź przypisanie śladów i ustawienie odniesienia.")
        elif task == "verify":
            evidence = verify_reference(project)
            result.update(status="ok", message=evidence["message"], evidence=evidence, next="Znajdź ustawienia do sprawdzenia.")
        elif task in {"standard", "custom"}:
            result.update(design(project, out / "technical", task, lambda msg: emit({"type":"stage", "message":msg})))
        elif task == "compare":
            result.update(compare(project, args["stage"], args["a"], args["b"]))
        elif task == "export":
            result.update(export_candidate(project, args["result"], args["candidate_id"], out))
        else:
            raise ValueError("Nieznane zadanie.")
    except ValueError as exc:
        result.update(status="needs_data", message=str(exc), next="Uzupełnij wskazany brak w Pomiary lub Mój system.", reasons=[str(exc)])
    except Exception as exc:
        result.update(status="error", message=f"Błąd programu: {exc}", next="Zapisz szczegóły techniczne; nie używaj niekompletnego wyniku.")
        (out / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
    result = clean_result(result)
    atomic_json(out / "result.json", result)
    return result


def clean_result(value):
    """Diagnostics can contain nonfinite 'unavailable' values, never JSON NaN."""
    if isinstance(value, dict):
        return {str(k):clean_result(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [clean_result(v) for v in value]
    if isinstance(value, np.ndarray):
        return clean_result(value.tolist())
    if isinstance(value, np.generic):
        return clean_result(value.item())
    if isinstance(value,float) and not np.isfinite(value):
        return None
    return value


def export_candidate(project, original, candidate_id, out):
    from rew_candidate_safety import evaluate_candidate_safety, combine_required_state_safety
    from rew_filter_generator import write_minidsp_biquads
    if original["fingerprint"] != fingerprint(project) or original["status"] != "ok":
        raise ValueError("Wynik jest nieaktualny. Uruchom dobór ponownie po zmianie projektu.")
    selected = next((c for c in original["candidates"] if c["id"] == candidate_id), None)
    if not selected or not selected.get("safe") or digest([selected["kind"], selected["settings"]]) != selected["id"]:
        raise ValueError("Wariant nie jest dopuszczony albo zmieniono jego parametry.")
    verify_reference(project)  # recompute; never trust cached verification badges
    with bound_sources(project["sources"]):
        _, _, _, state, freq, raw = _raw_and_gain(project)
        s = selected["settings"]
        gates = {}
        for name in ("dirac", "listening"):
            ls, rs = state.scale_complex_subs(raw[2], raw[3], name)
            if selected["kind"] == "standard":
                from rew_acoustic_crossover_designer import _simulate_candidate
                c = _simulate_candidate(freq, raw[0], raw[1], ls, rs, 96000, s["xo_hz"], s["filter_label"], s["main_fc_hz"], s["sub_fc_hz"], s["l_rel_ms"], s["r_rel_ms"], gain_mode="fixed")
            else:
                from rew_acoustic_biquad_designer import EvaluationContext, FilterShape, _evaluate, _robustness, _export_status
                ref = sum(_reference_response(project, freq, raw, state, name))
                ctx = EvaluationContext(freq=freq, fs=96000, lm_raw=raw[0], rm_raw=raw[1], ls_raw=ls, rs_raw=rs, ref_sum=ref, ref_source="Aktywna konfiguracja projektu", delay_refinement_enabled=False,
                    raw_sub_trim_db=state.raw_db, gain_state_name=name.upper(), global_gain_l_db=state.side_delta_db(name,"L"), global_gain_r_db=state.side_delta_db(name,"R"))
                c = _evaluate(ctx, FilterShape(**s["shape"]), s["l_rel_ms"], s["r_rel_ms"], "export_recheck")
                _robustness(ctx, c)
            c.final_gain_l_db = state.side_delta_db(name,"L")
            c.final_gain_r_db = state.side_delta_db(name,"R")
            delays = anchored_delays(project, s)
            gates[name] = evaluate_candidate_safety(c, input_integrity_ok=True, phase_integrity_ok=True, gain_state_verified=True, gain_state_complete=True,
                reference_anchor_verified=True, shd_representable=all(0<=v<=30 for v in delays))
            if selected["kind"] == "custom":
                c.candidate_safe_to_export = gates[name].candidate_safe_to_export
                c.safety_reasons = gates[name].reasons
                if _export_status(selected["profile"], c)[0] == "DIAGNOSTYKA_NIE_WGRYWAC":
                    raise ValueError("Wybrana własna zwrotnica nie przeszła ponownej kontroli eksportu.")
        combined = combine_required_state_safety(gates["dirac"], gates)
        if not combined.candidate_safe_to_export:
            raise ValueError("Nie przygotowano filtrów: " + "; ".join(combined.reasons))
        main, sub = _candidate_sections(selected)
        target = out / "pending_export"
        target.mkdir(exist_ok=False)
        for name, sections in (("OUT1_L_MAIN",main),("OUT2_R_MAIN",main),("OUT3_L_SUB",sub),("OUT4_R_SUB",sub)):
            write_minidsp_biquads(target / (name+".txt"), sections, "REWDO: tylko do kontrolowanej próby", slots=8)
        reference = active_config(project)
        atomic_json(target / "odniesienie.json", reference)
        atomic_json(target / "ustawienia.json", {"configuration": selected, "gains": project["gains"], "delays_ms": delays, "polarity": ["OFF"]*4, "fs":96000, "block":"Crossover"})
        (target / "JAK_SPRAWDZIC.md").write_text("# Tylko do próby pomiarowej\n\nZachowaj kopię obecnego slotu. Pliki OUT1–4 odpowiadają L main, R main, L sub, R sub. Import do bloku Crossover, 96 kHz, osiem sekcji; nie dubluj filtrów w PEQ.\n\nDelaye [ms], OUT1–4: " + ", ".join(f"{v:.2f}" for v in delays) + ". Polaryzacja OFF/OFF.\n\nPoziomy sesji Dirac i odsłuchu są zapisane w ustawienia.json; są to poziomy cyfrowe subów w miniDSP, nie położenie pokrętła REL.\n\nNajpierw zmierz z Dirac OFF, następnie wykonaj nową kalibrację Dirac i pomiar kontrolny. Nie używaj starego filtra Dirac z nową zwrotnicą. Program nie zmienia sprzętu.\n", encoding="utf-8")
        published = out / "do_proby_pomiarowej"
        target.rename(published)
    return {"status":"ok", "message":"Przygotowano wybrany wariant do próby pomiarowej; ponowna kontrola przeszła.", "next":"Zachowaj kopię slotu i wykonaj pomiar przed Dirac.", "export_dir":str(published), "candidate_id":candidate_id}
