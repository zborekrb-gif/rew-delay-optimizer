"""Full time comparison from explicit project bindings, using preserved engines.

One selected snapshot, stage, microphone position and configuration pair form
the scope. Original measurement titles never determine roles or repeats.
"""
from __future__ import annotations

import math

import numpy as np

from rew_api_client import REWApiError, decode_impulse_response, is_no_smoothing_label, is_rew_api_snapshot, read_snapshot
from rew_project import digest, file_path, sha256
from rew_time_domain_analyzer import (
    MIN_REFERENCE_REPEATS, ROLE_ORDER, TIMING_ANCHOR_TOLERANCE_MS,
    _aggregate_measurements, _classify_variant, _compare_role,
    _repeatability_profile, load_time_measurements,
)

SUM_ROLES = {"L sum": "L", "R sum": "R", "L+R": "L+R"}
CONFIRMATIONS = ("conditions_consistent", "timing_reference_consistent", "microphone_stationary")
MAX_TIME_MS = 500.0
ROLE_NAMES = {"L": "Lewa strona", "R": "Prawa strona", "L+R": "Obie strony razem"}
VERDICTS = {
    "POTENCJALNIE LEPSZY": "B poprawia wskaźniki czasu ponad rozrzut A. Sprawdź brzmienie w odsłuchu.",
    "ODRZUĆ — UTRATA BODY": "B osłabia podparcie dolnej średnicy. Krótszy zanik nie wystarcza do uznania poprawy.",
    "ODRZUĆ — NIESPÓJNOŚĆ L/R": "Suma stereo wygląda lepiej, ale jedna ze stron pogarsza się. Nie wybieraj B na tej podstawie.",
    "GORSZY CZASOWO": "B pogarsza co najmniej jeden wskaźnik czasu ponad rozrzut pomiarów A.",
    "DANE CZASOWE NIESPÓJNE": "Pomiary A i B mają niespójne odniesienie czasu. Najpierw sprawdź sesję pomiarową.",
    "BRAK POTWIERDZONEJ RÓŻNICY": "Nie potwierdzono różnicy przekraczającej naturalny rozrzut pomiarów A.",
    "ZMIANA BOCZNA / DO ODSŁUCHU": "Zmiana nie potwierdza poprawy całego zestawu L, R i L+R. Porównaj brzmienie.",
}


def selection_counts(source, stage, config_a, config_b, position):
    """Cheap GUI preview: declarations and counts, never an input verdict."""
    groups = {key: {role: [] for role in ROLE_ORDER} for key in (config_a, config_b)}
    for trace in source.get("traces", []):
        key = trace.get("preset") or source.get("configuration")
        role = SUM_ROLES.get(trace.get("role"))
        if (key in groups and role and trace.get("stage") == stage
                and str(trace.get("microphone", "")).strip() == position):
            groups[key][role].append(trace)
    return groups


def _finite(value):
    return value is not None and math.isfinite(float(value))


def _check_item(item, raw):
    if item.impulse is None or item.time_s is None:
        raise ValueError(f"{item.name}: brak poprawnego impulsu. Pobierz pomiar z otwartego REW z impulsem.")
    if (not np.all(np.isfinite(item.impulse)) or not np.all(np.isfinite(item.time_s))
            or not _finite(item.sample_rate_hz) or item.sample_rate_hz <= 0
            or not np.any(item.impulse)):
        raise ValueError(f"{item.name}: impuls jest pusty, zerowy albo zawiera niepoprawne liczby.")
    if item.freq[0] > 25 or item.freq[-1] < 220:
        raise ValueError(f"{item.name}: potrzebny jest zakres SPL obejmujący 25–220 Hz, także do ochrony dolnej średnicy.")
    gd = raw.get("groupDelay")
    if gd is not None and (not isinstance(gd, dict) or item.gd_source != "API REW"):
        raise ValueError(f"{item.name}: błędne dane group delay. Pobierz je ponownie; nie zastąpiono ich po cichu inną metodą.")
    if isinstance(gd, dict) and not is_no_smoothing_label(gd.get("smoothing")):
        raise ValueError(f"{item.name}: group delay ma niepotwierdzony brak wygładzania. Pobierz natywne dane ponownie.")
    if (item.gd_freq is None or item.gd_ms is None
            or not np.all(np.isfinite(item.gd_freq)) or not np.all(np.isfinite(item.gd_ms))
            or item.gd_freq[0] > 45 or item.gd_freq[-1] < 220):
        raise ValueError(f"{item.name}: brak pełnych, poprawnych danych group delay w zakresie 45–220 Hz.")
    fast_bands = [band for band in item.bands if band.high_hz <= 110.1]
    if len(fast_bands) != 4 or any(
        band.usable_duration_ms < min(300.0, MAX_TIME_MS * .75)
        or band.noise_limited or not _finite(band.late_100_db) or not _finite(band.t20_ms)
        for band in fast_bands
    ):
        raise ValueError(f"{item.name}: impuls ma zbyt krótki lub niewiarygodny zanik w pasmach 25–110 Hz. Pobierz pełny impuls i sprawdź szum.")
    if not all(_finite(getattr(item, name)) for name in (
        "composite_late100_db", "composite_t20_ms", "gd_p90_45_110_ms", "gd_anchor_ms"
    )):
        raise ValueError(f"{item.name}: nie można wyznaczyć kompletu wskaźników czasu.")


def compare_time(project, *, source_id, stage, config_a, config_b, position, confirmations):
    configs = {c["key"]: c for c in project["configurations"]}
    if config_a == config_b or config_a not in configs or config_b not in configs:
        raise ValueError("Wybierz dwie różne, istniejące konfiguracje: A jako odniesienie i B do sprawdzenia.")
    if stage not in {"Przed Dirac", "Po Dirac"}:
        raise ValueError("Wybierz etap Przed Dirac albo Po Dirac.")
    if not isinstance(position, str) or not position.strip():
        raise ValueError("Wybierz jedno miejsce mikrofonu, np. MLP. Różne pozycje nie są powtórzeniami.")
    position = position.strip()
    selected_sources = [s for s in project["sources"] if s["id"] == source_id]
    if len(selected_sources) != 1:
        raise ValueError("Wybierz jeden plik z pomiarami obu konfiguracji A i B.")
    source = selected_sources[0]
    path = file_path(source["path"])
    if sha256(path) != source["sha256"]:
        raise ValueError("Wybrany plik zmienił zawartość. Wczytaj go ponownie jako nowe źródło.")
    if not is_rew_api_snapshot(path):
        raise ValueError("Pełna analiza czasu wymaga migawki REW API z impulsem. Otwórz MDAT w REW, potem wybierz Wczytaj pomiary → Z otwartego REW.")
    if source.get("mapping_confirmed") is not True:
        raise ValueError("Najpierw potwierdź role, konfiguracje, etap i miejsca mikrofonu przez Pomiary → Opisz pomiary.")
    if not isinstance(confirmations, dict) or any(confirmations.get(k) is not True for k in CONFIRMATIONS):
        raise ValueError("Potwierdź warunki tej serii: stały tor i głośność, wspólna metoda referencji czasu oraz nieruchomy mikrofon w wybranej pozycji.")
    groups = selection_counts(source, stage, config_a, config_b, position)
    missing = []
    for cfg, minimum in ((config_a, MIN_REFERENCE_REPEATS), (config_b, 1)):
        for role in ROLE_ORDER:
            count = len(groups[cfg][role])
            if count < minimum:
                side = "A" if cfg == config_a else "B"
                missing.append(f"{side} / {ROLE_NAMES[role]}: {count} z {minimum}")
    if missing:
        raise ValueError("Brakuje pomiarów w wybranym pliku, etapie i miejscu: " + "; ".join(missing)
                         + ". Dla A wykonaj co najmniej 3 powtórzenia L, R i L+R, dla B co najmniej jeden pełny zestaw.")

    # Reject invalid selected impulse numbers before any decay calculation.
    raw_payload = read_snapshot(path)
    for group in groups.values():
        for traces in group.values():
            for trace in traces:
                index = trace.get("position")
                if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(raw_payload.get("measurements", [])):
                    raise ValueError("Przypisanie zawiera niepoprawny indeks pomiaru.")
                raw = raw_payload['measurements'][index - 1]
                impulse = raw.get('impulseResponse') if isinstance(raw, dict) else None
                if not isinstance(impulse, dict):
                    raise ValueError(f"Pomiar {index}: brak poprawnego impulsu. Pobierz pełne dane z otwartego REW.")
                try:
                    decoded = decode_impulse_response(impulse)
                except (REWApiError, ValueError) as exc:
                    raise ValueError(f"Pomiar {index}: brak poprawnego impulsu: {exc}") from exc
                if not np.all(np.isfinite(decoded['samples'])) or not np.all(np.isfinite(decoded['time_s'])):
                    raise ValueError(f"Pomiar {index}: impuls zawiera niepoprawne liczby.")
                if not np.allclose(np.diff(decoded['time_s']), 1.0 / decoded['sample_rate_hz'], rtol=1e-6, atol=1e-12):
                    raise ValueError(f"Pomiar {index}: sampleInterval i sampleRate opisują różne skale czasu.")

    payload, all_items, _warnings = load_time_measurements(path, MAX_TIME_MS)
    state = payload.get("measurementState") or {}
    if not isinstance(state, dict):
        raise ValueError("Migawka ma niepoprawne metadane measurementState.")
    for key in ("timingReferenceConsistent", "microphoneStationary"):
        if state.get(key) is not None and state.get(key) is not True:
            raise ValueError(f"Migawka zawiera sprzeczną deklarację {key}. Sprawdź sesję i pobierz właściwy zestaw; potwierdzenie w oknie nie nadpisuje pliku.")
    raw_records = payload["measurements"]
    if len(all_items) != len(raw_records):
        raise ValueError("Odczyt nie zachował wszystkich indeksów pomiarów. Nie przypisano czasu do innego śladu.")
    grouped_items = {cfg: {role: [] for role in ROLE_ORDER} for cfg in groups}
    provenance, band_rows, seen_positions, seen_uuids = [], [], set(), set()
    selected_items = []
    for cfg in (config_a, config_b):
        for role in ROLE_ORDER:
            for trace in groups[cfg][role]:
                index = trace.get("position")
                if (isinstance(index, bool) or not isinstance(index, int)
                        or not 1 <= index <= len(raw_records) or index in seen_positions):
                    raise ValueError("Przypisanie zawiera powtórzony albo niepoprawny indeks pomiaru. Nie można policzyć go jako niezależne powtórzenie.")
                seen_positions.add(index)
                if trace.get("id") != digest([source["sha256"], index]):
                    raise ValueError("Identyfikator śladu nie odpowiada źródłu. Wczytaj źródło ponownie i potwierdź przypisanie.")
                raw, item = raw_records[index - 1], all_items[index - 1]
                summary = raw.get("summary") or {}
                measurement_uuid = str(summary.get("uuid") or summary.get("id") or "").strip()
                if measurement_uuid and measurement_uuid in seen_uuids:
                    raise ValueError("Ten sam identyfikator pomiaru REW występuje wielokrotnie. Ponowny import nie tworzy niezależnych powtórzeń.")
                if measurement_uuid:
                    seen_uuids.add(measurement_uuid)
                _check_item(item, raw)
                grouped_items[cfg][role].append(item)
                selected_items.append(item)
                provenance.append({"trace_id": trace["id"], "index": index, "measurement_uuid": measurement_uuid,
                                   "configuration": cfg, "role": role, "original_name": item.name})
                for band in item.bands:
                    band_rows.append({"trace_id": trace["id"], "configuration": cfg, "role": role,
                                      "band": band.label, "late100_db": band.late_100_db,
                                      "t20_ms": band.t20_ms, "t30_ms": band.t30_ms,
                                      "duration_ms": band.usable_duration_ms, "noise_limited": band.noise_limited})
    if len({item.sample_rate_hz for item in selected_items}) != 1:
        raise ValueError("Wybrane impulsy mają różne częstotliwości próbkowania. Użyj spójnej serii pomiarowej.")
    references = {item.timing_reference.strip() for item in selected_items if item.timing_reference.strip()}
    if len(references) > 1:
        raise ValueError("Wybrane impulsy mają sprzeczne oznaczenia timing reference. Nie wydano werdyktu.")

    # These are the same aggregation, comparison and classification functions
    # used by analyse_variants. Only their grouping input comes from project IDs.
    profiles = {role: _repeatability_profile(role, grouped_items[config_a][role]) for role in ROLE_ORDER}
    unstable = [ROLE_NAMES[role] for role, profile in profiles.items() if profile.anchor_outliers]
    if unstable:
        raise ValueError("Niestabilne odniesienie czasu w powtórzeniach A: " + ", ".join(unstable)
                         + f". Odstający pomiar przekracza {TIMING_ANCHOR_TOLERANCE_MS:g} ms względem mediany. Sprawdź referencję i powtórz serię.")
    comparisons = {role: _compare_role(_aggregate_measurements(grouped_items[config_b][role], config_b, role), profiles[role]) for role in ROLE_ORDER}
    variant = _classify_variant(config_b, comparisons, [])
    repeatability, rows = [], []
    for role in ROLE_ORDER:
        profile, comparison = profiles[role], comparisons[role]
        repeatability.append({"role": role, "count": profile.count, "reliable": profile.reliable,
                              "late_range_db": profile.late_range_db, "t20_range_ms": profile.t20_range_ms,
                              "gd_range_ms": profile.gd_range_ms, "late_threshold_db": profile.late_threshold_db,
                              "t20_threshold_ms": profile.t20_threshold_ms, "gd_threshold_ms": profile.gd_threshold_ms,
                              "anchor_range_ms": profile.anchor_range_ms})
        rows.append({"role": role, "a_count": profile.count, "b_count": len(grouped_items[config_b][role]),
                     "a_late100_db": profile.centre.composite_late100_db, "b_late100_db": comparison.candidate.composite_late100_db,
                     "a_t20_ms": profile.centre.composite_t20_ms, "b_t20_ms": comparison.candidate.composite_t20_ms,
                     "a_gd_ms": profile.centre.gd_p90_45_110_ms, "b_gd_ms": comparison.candidate.gd_p90_45_110_ms,
                     "late_improvement_db": comparison.late_improvement_db, "t20_improvement_ms": comparison.t20_improvement_ms,
                     "gd_improvement_ms": comparison.gd_improvement_ms, "body_100_160_db": comparison.spl.get("body_100_160_db"),
                     "body_125_220_db": comparison.spl.get("body_125_220_db"), "anchor_delta_ms": comparison.anchor_delta_ms,
                     "spl_anchor_hz": comparison.spl.get("anchor"),
                     "significant_good": comparison.significant_good, "significant_bad": comparison.significant_bad,
                     "gd_source_a": profile.centre.gd_source, "gd_source_b": comparison.candidate.gd_source})
    if sha256(path) != source["sha256"]:
        raise ValueError("Źródło zmieniło się w trakcie analizy. Nie zatwierdzono wyniku.")
    # Warnings from unselected measurements do not describe this comparison.
    # All selected data have passed the explicit checks above.
    selected_warnings = []
    if any(not item.timing_reference.strip() for item in selected_items):
        selected_warnings.append("Część impulsów nie ma tekstowego oznaczenia referencji; zgodność metody zadeklarował użytkownik dla tego porównania.")
    if not all(state.get(k) is True for k in ("timingReferenceConsistent", "microphoneStationary")):
        selected_warnings.append("Warunki sesji potwierdzono w projekcie dla wybranego pliku, A/B i pozycji. Migawka nie zawiera obu deklaracji; jej zawartości nie zmieniono.")
    message = VERDICTS.get(variant.verdict, variant.verdict)
    next_action = ("Wyrównaj głośność odsłuchu A/B i porównaj wokal, fundament basu, scenę oraz zmęczenie. Zapisz własny wybór."
                   if variant.verdict in {"POTENCJALNIE LEPSZY", "ZMIANA BOCZNA / DO ODSŁUCHU", "BRAK POTWIERDZONEJ RÓŻNICY"}
                   else "Zachowaj A jako odniesienie. Sprawdź wskazaną przyczynę przed kolejną próbą B.")
    return {"status": "ok", "message": message, "next": next_action,
            "stage": stage, "config_a": config_a, "config_b": config_b,
            "reasons": list(variant.reasons),
            "time_analysis": {"source_id": source_id, "source_sha256": source["sha256"], "source_name": source["name"],
                              "position": position, "max_time_ms": MAX_TIME_MS, "verdict": variant.verdict,
                              "repeatability": repeatability, "roles": rows, "bands": band_rows,
                              "bindings": provenance, "warnings": selected_warnings,
                              "confirmations": {key: confirmations[key] for key in CONFIRMATIONS},
                              "confirmation_scope": "USER_DECLARATION_FOR_SELECTED_SOURCE_CONFIGURATIONS_STAGE_POSITION",
                              "scope": "Pomiar i wniosek techniczny o czasie. Wskaźniki nie mierzą naturalności wokalu ani jakości brzmienia; jedna pozycja nie ocenia odporności przestrzennej."}}


def result_matches(result, *, source_id, stage, config_a, config_b, position):
    request = result.get('time_request', {})
    analysis = result.get("time_analysis", {})
    return (result.get("stage", request.get('stage')) == stage
            and result.get("config_a", request.get('a')) == config_a
            and result.get("config_b", request.get('b')) == config_b
            and analysis.get("source_id", request.get('source_id')) == source_id
            and analysis.get("position", request.get('position')) == position)


def report_lines(result):
    """Readable report of the measured scope, thresholds and three role results."""
    analysis = result['time_analysis']
    def fmt(value):
        return '—' if value is None else f'{value:+.2f}'
    def safe(value):
        return str(value).replace('|', '/').replace('\n', ' ')
    lines = ['', '## Czas i powtarzalność', '',
             'Etap: ' + safe(result['stage']),
             'Konfiguracje A/B: ' + safe(result['config_a']) + ' / ' + safe(result['config_b']),
             'Plik: ' + safe(analysis['source_name']),
             'Miejsce mikrofonu: ' + safe(analysis['position']),
             'Werdykt techniczny: **' + safe(analysis['verdict']) + '**', '',
             'Dodatnia poprawa oznacza mniejszą późną energię albo krótszy czas w B. '
             'Progi wyznaczono z rozrzutu powtórzeń A oraz zachowanych progów modułu. '
             'Dolna średnica: podane różnice SPL to B minus A, z dotychczasowym odniesieniem pasmowym modułu; bez wyrównywania poziomu odsłuchu.', '',
             '| Co grało | Powtórzenia A / B | Późna energia: poprawa [dB] | Spadek o 20 dB: poprawa [ms] | GD: poprawa [ms] | 125–220 Hz: zmiana [dB] |',
             '|---|---:|---:|---:|---:|---:|']
    for row in analysis['roles']:
        lines.append('| ' + ' | '.join([ROLE_NAMES[row['role']], f"{row['a_count']} / {row['b_count']}",
            *[fmt(row[k]) for k in ('late_improvement_db','t20_improvement_ms','gd_improvement_ms','body_125_220_db')]]) + ' |')
    lines += ['', '### Powtarzalność odniesienia A', '',
              '| Co grało | Liczba | Rozrzut późnej energii [dB] | Próg [dB] | Rozrzut spadku o 20 dB [ms] | Próg [ms] | Rozrzut GD [ms] | Próg [ms] |',
              '|---|---:|---:|---:|---:|---:|---:|---:|']
    for row in analysis['repeatability']:
        lines.append('| ' + ' | '.join([ROLE_NAMES[row['role']], str(row['count']),
            *[fmt(row[k]) for k in ('late_range_db','late_threshold_db','t20_range_ms','t20_threshold_ms','gd_range_ms','gd_threshold_ms')]]) + ' |')
    lines += ['', 'Późna energia: udział po 100 ms względem energii badanego ogona. '
              'Spadek o 20 dB: porównawczy czas zaniku energii liczony z impulsu; nie jest pełnym T20/RT60 z REW. '
              'GD: 90. percentyl względnego opóźnienia grupowego w 45–110 Hz, po odjęciu odniesienia pasmowego.', '', analysis['scope']]
    lines += ['', 'Pasmo wyrównania SPL w ocenie podparcia średnicy (L / R / L+R): '
              + ' / '.join(str(row['spl_anchor_hz']) for row in analysis['roles']) + '. '
              'W tym module SPL jest wyrównane w podanym paśmie; jest to inna prezentacja niż surowe B−A w głównej tabeli porównania.']
    for warning in analysis['warnings']:
        lines.append('- ' + safe(warning))
    lines += ['', 'Deklaracje warunków są zapisane dla tego pliku, konfiguracji A/B, etapu i pozycji; nie stanowią niezależnego pomiaru warunków sesji.',
              'SHA-256 źródła: `' + analysis['source_sha256'] + '`']
    return lines


def write_artifacts(out, project, result):
    """Publish a complete new report directory only after all its files exist."""
    import csv
    import shutil
    from rew_user_report import write_summary
    pending = out / 'pending_time_analysis'
    published = out / 'pelna_analiza_czasu'
    if pending.exists() or published.exists():
        raise ValueError('Folder raportu analizy czasu już istnieje. Rozpocznij nowy przebieg.')
    pending.mkdir()
    try:
        write_summary(pending / 'WYNIK_I_NASTEPNY_KROK.md', project, result)
        for filename, key in (('porownanie_L_R_LR.csv', 'roles'), ('powtarzalnosc_A.csv', 'repeatability'),
                              ('zanik_w_pasmach.csv', 'bands'), ('powiazanie_pomiarow.csv', 'bindings')):
            rows = result['time_analysis'][key]
            with (pending / filename).open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=';')
                writer.writeheader()
                writer.writerows(rows)
        pending.rename(published)
    except Exception:
        shutil.rmtree(pending)
        raise
    return str(published)
