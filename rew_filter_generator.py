#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generator filtrów zwrotnicy miniDSP SHD — REWDO v20.23.

Tworzy kontrolowane warianty HPF/LPF do importu jako biquady w miniDSP.
To nie zastępuje pomiaru POST-DIRAC: filtr jest punktem startowym do testu.
"""
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

import numpy as np

APP_VERSION = "v20.31 Generator filtrów miniDSP SHD + narzędzia all-pass"


@dataclass(frozen=True)
class Biquad:
    section: int
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float
    kind: str
    q: float


@dataclass(frozen=True)
class FilterVariant:
    code: str
    title: str
    xo_hz: float
    filter_label: str
    topology: str
    main_kind: str = "HP"
    sub_kind: str = "LP"
    description: str = ""
    goal: str = ""
    risk: str = ""
    measurement_plan: str = ""
    routing_note: str = ""


def _butter_qs(order: int) -> List[float]:
    if order % 2 != 0 or order <= 0:
        raise ValueError("Obsługuję tylko parzyste rzędy filtra.")
    n = int(order)
    qs: List[float] = []
    for k in range(1, n // 2 + 1):
        q = 1.0 / (2.0 * math.cos((2.0 * k - 1.0) * math.pi / (2.0 * n)))
        qs.append(float(q))
    return qs


def _rbj_low_high(kind: str, fs: float, fc: float, q: float) -> Tuple[float, float, float, float, float]:
    """RBJ cookbook LP/HP biquad, normalized to a0=1.

    Returned coefficients use the common REW/miniDSP convention:
        H(z) = (b0 + b1 z^-1 + b2 z^-2) / (1 + a1 z^-1 + a2 z^-2)
    """
    if not 0 < fc < fs / 2:
        raise ValueError(f"Częstotliwość {fc:g} Hz musi być w zakresie 0..fs/2 ({fs/2:g}).")
    w0 = 2.0 * math.pi * fc / fs
    cw = math.cos(w0)
    sw = math.sin(w0)
    alpha = sw / (2.0 * q)
    a0 = 1.0 + alpha
    a1 = -2.0 * cw
    a2 = 1.0 - alpha
    k = kind.strip().upper()
    if k == "LP":
        b0 = (1.0 - cw) / 2.0
        b1 = 1.0 - cw
        b2 = (1.0 - cw) / 2.0
    elif k == "HP":
        b0 = (1.0 + cw) / 2.0
        b1 = -(1.0 + cw)
        b2 = (1.0 + cw) / 2.0
    else:
        raise ValueError("kind musi być LP albo HP")
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0



def _rbj_allpass(fs: float, fc: float, q: float) -> Tuple[float, float, float, float, float]:
    """RBJ 2nd-order all-pass, normalized to a0=1.

    Magnitude is unity (apart from floating-point error); only phase and group
    delay are changed. Coefficients use REWDO's mathematical convention:
        H(z) = (b0 + b1 z^-1 + b2 z^-2) / (1 + a1 z^-1 + a2 z^-2)
    """
    if not 0 < fc < fs / 2:
        raise ValueError(f"Częstotliwość {fc:g} Hz musi być w zakresie 0..fs/2 ({fs/2:g}).")
    if not math.isfinite(q) or not 0.30 <= q <= 4.00:
        raise ValueError(f"Nieprawidłowe Q all-pass: {q!r}; dozwolone 0.30..4.00.")
    w0 = 2.0 * math.pi * fc / fs
    cw = math.cos(w0)
    sw = math.sin(w0)
    alpha = sw / (2.0 * q)
    a0 = 1.0 + alpha
    a1 = -2.0 * cw / a0
    a2 = (1.0 - alpha) / a0
    # RBJ all-pass numerator is the reversed denominator.
    b0 = a2
    b1 = a1
    b2 = 1.0
    return b0, b1, b2, a1, a2


def make_allpass_section(fs: float, fc: float, q: float, *, section: int = 1) -> Biquad:
    """Create one stable RBJ all-pass section for miniDSP/REWDO."""
    b0, b1, b2, a1, a2 = _rbj_allpass(float(fs), float(fc), float(q))
    bq = Biquad(int(section), b0, b1, b2, a1, a2, "AP", float(q))
    if max(biquad_pole_radii(bq)) >= 0.999999:
        raise ValueError("Sekcja all-pass jest niestabilna lub zbyt blisko granicy stabilności.")
    return bq

def _parse_filter_label(label: str) -> Tuple[str, int]:
    txt = str(label).strip().upper().replace(" ", "")
    if txt.startswith("BW"):
        return "BW", int(txt[2:])
    if txt.startswith("LR"):
        return "LR", int(txt[2:])
    raise ValueError(f"Nieznany filtr: {label}. Oczekuję np. BW48 albo LR48.")


def make_sections(kind: str, fs: float, fc: float, filter_label: str) -> List[Biquad]:
    ftype, slope = _parse_filter_label(filter_label)
    if slope not in (24, 48):
        raise ValueError("Generator v20.08 obsługuje BW24/BW48/LR24/LR48.")
    if ftype == "BW":
        order = slope // 6
        qs = _butter_qs(order)
    else:
        # Linkwitz-Riley 24 = 2 x BW12; LR48 = 2 x BW24.
        bw_order = slope // 12
        qs = _butter_qs(bw_order) * 2
    out: List[Biquad] = []
    for i, q in enumerate(qs, start=1):
        b0, b1, b2, a1, a2 = _rbj_low_high(kind, fs, fc, q)
        out.append(Biquad(i, b0, b1, b2, a1, a2, kind.upper(), q))
    return out


def make_custom_sections(
    kind: str,
    fs: float,
    fc_values: Iterable[float],
    q_values: Iterable[float],
) -> List[Biquad]:
    """Zbuduj kontrolowaną kaskadę niestandardowych sekcji LP/HP.

    Każda para ``fc/Q`` opisuje jedną sekcję 2. rzędu. Funkcja nie
    optymalizuje współczynników bezpośrednio: zawsze używa stabilnej postaci
    RBJ i sprawdza położenie biegunów. Dzięki temu Acoustic Biquad Designer
    może szukać różnych częstotliwości i Q bez generowania przypadkowych,
    trudnych do audytu współczynników.
    """
    fcs = [float(v) for v in fc_values]
    qs = [float(v) for v in q_values]
    if not fcs or len(fcs) != len(qs):
        raise ValueError("fc_values i q_values muszą mieć tę samą niezerową długość.")
    if len(fcs) > 8:
        raise ValueError("miniDSP SHD udostępnia maksymalnie 8 sekcji biquad na wyjście.")
    pairs = sorted(zip(fcs, qs), key=lambda item: item[0])
    out: List[Biquad] = []
    for i, (fc, q) in enumerate(pairs, start=1):
        if not math.isfinite(fc) or not 0.0 < fc < float(fs) / 2.0:
            raise ValueError(f"Nieprawidłowa częstotliwość sekcji: {fc!r} Hz.")
        if not math.isfinite(q) or not 0.30 <= q <= 4.00:
            raise ValueError(f"Nieprawidłowe Q sekcji: {q!r}; dozwolone 0.30..4.00.")
        b0, b1, b2, a1, a2 = _rbj_low_high(kind, fs, fc, q)
        bq = Biquad(i, b0, b1, b2, a1, a2, kind.upper(), q)
        if max(biquad_pole_radii(bq)) >= 0.999999:
            raise ValueError(f"Sekcja {i} jest niestabilna lub zbyt blisko granicy stabilności.")
        out.append(bq)
    return out


def biquad_pole_radii(bq: Biquad) -> Tuple[float, float]:
    """Promienie biegunów dla mianownika 1 + a1 z^-1 + a2 z^-2."""
    roots = np.roots([1.0, float(bq.a1), float(bq.a2)])
    radii = sorted((float(abs(r)) for r in roots), reverse=True)
    if len(radii) == 1:
        radii.append(0.0)
    return float(radii[0]), float(radii[1])


def _format_coef(x: float) -> str:
    # miniDSP SHD Studio jest wrażliwy na notację naukową w polu Advanced/Biquad.
    # Zapis stałopozycyjny jest dłuższy, ale bezpieczniejszy przy wklejaniu.
    return f"{x:.17f}"


def _minidsp_shd_feedback(a: float) -> float:
    """Konwersja sprzężenia zwrotnego do pola miniDSP SHD Studio Advanced/Biquad.

    Wewnętrznie REWDO liczy biquady w klasycznej konwencji DSP/REW:
        H(z) = (b0 + b1 z^-1 + b2 z^-2) / (1 + a1 z^-1 + a2 z^-2)

    Pole Biquad Programming w miniDSP SHD Studio oczekuje praktycznie przeciwnego
    znaku dla współczynników feedback a1/a2. Bez tej konwersji HPF/LPF rysuje
    w konsoli błędną charakterystykę, mimo że liczby wyglądają poprawnie.
    """
    return -float(a)


def write_minidsp_biquads(path: Path, sections: Iterable[Biquad], header: str, *, slots: int = 8) -> None:
    """Zapisuje bezpośrednio wklejalny format miniDSP SHD Studio.

    Plik celowo nie zawiera komentarzy ani nagłówków, bo wtyczka SHD Studio bywa
    wrażliwa na dodatkowe linie. Nieużyte sekcje są dopełnione filtrami identity.
    """
    sec_list = list(sections)
    if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 8:
        raise ValueError("Liczba sekcji Crossover miniDSP SHD musi być całkowita i mieścić się w zakresie 1..8.")
    if len(sec_list) > slots:
        raise ValueError(
            f"Filtr ma {len(sec_list)} sekcji, ale wybrano tylko {slots} slotów. "
            "Eksport został zatrzymany zamiast cicho obciąć końcowe sekcje."
        )
    expected_sections = list(range(1, len(sec_list) + 1))
    actual_sections = [int(bq.section) for bq in sec_list]
    if actual_sections != expected_sections:
        raise ValueError(
            f"Numery sekcji muszą być kolejne od 1; otrzymano {actual_sections or 'brak'}."
        )
    for bq in sec_list:
        coefficients = (bq.b0, bq.b1, bq.b2, bq.a1, bq.a2)
        if not all(math.isfinite(float(value)) for value in coefficients):
            raise ValueError(f"Sekcja {bq.section} zawiera NaN albo Inf.")
        if max(biquad_pole_radii(bq)) >= 0.999999:
            raise ValueError(f"Sekcja {bq.section} jest niestabilna lub zbyt blisko granicy stabilności.")
    lines = []
    for bq in sec_list:
        lines += [
            f"biquad{bq.section},",
            f"b0={_format_coef(bq.b0)},",
            f"b1={_format_coef(bq.b1)},",
            f"b2={_format_coef(bq.b2)},",
            f"a1={_format_coef(_minidsp_shd_feedback(bq.a1))},",
            f"a2={_format_coef(_minidsp_shd_feedback(bq.a2))},",
            "",
        ]
    for section in range(len(sec_list) + 1, slots + 1):
        lines += [
            f"biquad{section},",
            "b0=1.00000000000000000,",
            "b1=0.00000000000000000,",
            "b2=0.00000000000000000,",
            "a1=0.00000000000000000,",
            "a2=0.00000000000000000,",
            "",
        ]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_csv(path: Path, sections: Iterable[Biquad], fs: float, fc: float, filter_label: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "filter", "kind", "fs_hz", "fc_hz", "q", "b0", "b1", "b2", "a1_math", "a2_math", "a1_minidsp_shd", "a2_minidsp_shd"])
        for bq in sections:
            w.writerow([bq.section, filter_label, bq.kind, fs, fc, bq.q, bq.b0, bq.b1, bq.b2, bq.a1, bq.a2, _minidsp_shd_feedback(bq.a1), _minidsp_shd_feedback(bq.a2)])


def freq_response(sections: Iterable[Biquad], fs: float, freqs: np.ndarray) -> np.ndarray:
    z = np.exp(-2j * np.pi * freqs / fs)
    h = np.ones_like(z, dtype=complex)
    for bq in sections:
        num = bq.b0 + bq.b1 * z + bq.b2 * z**2
        den = 1.0 + bq.a1 * z + bq.a2 * z**2
        h *= num / den
    return h


def write_response_csv(path: Path, main_sections: List[Biquad], sub_sections: List[Biquad], fs: float) -> None:
    freqs = np.geomspace(10, 500, 320)
    hm = freq_response(main_sections, fs, freqs)
    hs = freq_response(sub_sections, fs, freqs)
    # elektryczna suma idealna przy zgodnej polaryzacji i bez opóźnień — tylko orientacyjnie
    hsum = hm + hs
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["freq_hz", "main_hp_db", "sub_lp_db", "ideal_electric_sum_db", "main_phase_deg", "sub_phase_deg"])
        for fr, m, s, sm in zip(freqs, hm, hs, hsum):
            w.writerow([
                fr,
                20 * math.log10(max(abs(m), 1e-15)),
                20 * math.log10(max(abs(s), 1e-15)),
                20 * math.log10(max(abs(sm), 1e-15)),
                math.degrees(math.atan2(m.imag, m.real)),
                math.degrees(math.atan2(s.imag, s.real)),
            ])


def default_variants() -> List[FilterVariant]:
    return [
        FilterVariant(
            code="A_ORGANICZNY_90_BW48_STEREO",
            title="A — organiczny punkt odniesienia 90 BW48 stereo",
            xo_hz=90.0,
            filter_label="BW48",
            topology="stereo L/R",
            description="Wariant najbliższy dotychczasowej filozofii: C1 zachowują body, REL-e tylko porządkują fundament.",
            goal="bezpieczna organiczna średnica, kontrolowany bas, minimalizacja ryzyka szklistości",
            risk="może być mniej efektowny scenicznie niż wyższe XO",
            measurement_plan="po Diracu: L+P posL / MLP / posP; opcjonalnie L i R w MLP",
            routing_note="sub L dostaje kanał L, sub R dostaje kanał R",
        ),
        FilterVariant(
            code="B_SREDNICA_SAFE_82_BW48_STEREO",
            title="B — bezpieczna średnica 82 BW48 stereo",
            xo_hz=82.0,
            filter_label="BW48",
            topology="stereo L/R",
            description="Niższy crossover ogranicza wejście subów w dolną średnicę i może zmniejszyć ryzyko odchudzenia wokali.",
            goal="maksymalna ochrona body i naturalności C1",
            risk="może dać mniej skali i mniej wypełnienia niż 90/96 Hz",
            measurement_plan="po Diracu: L+P posL / MLP / posP; szczególnie sprawdź 75–110 Hz",
            routing_note="sub L dostaje kanał L, sub R dostaje kanał R",
        ),
        FilterVariant(
            code="C_SCENA_96_BW48_STEREO",
            title="C — scena i punktowość 96 BW48 stereo, ostrożnie",
            xo_hz=96.0,
            filter_label="BW48",
            topology="stereo L/R",
            description="Wariant sceniczny/punktowy. Może być bardzo ciekawy, ale wymaga kontroli body i braku szklistości.",
            goal="większa czytelność i bardziej punktowe sklejenie, bez dziury L+P",
            risk="ryzyko odchudzenia średnicy, większej natarczywości albo efektownej jasności",
            measurement_plan="po Diracu: L+P posL / MLP / posP; obowiązkowo odsłuch wokali i fortepianu",
            routing_note="sub L dostaje kanał L, sub R dostaje kanał R",
        ),
        FilterVariant(
            code="D_MONO_SUB_80_BW48",
            title="D — test mono subów 80 BW48",
            xo_hz=80.0,
            filter_label="BW48",
            topology="mono dual-sub",
            description="Osobna topologia: oba REL-e pracują jako wspólny bas mono. Test stabilności L+P i odporności 3-punktowej.",
            goal="mniej dziur L+P po Diracu, stabilniejszy bas w obszarze głowy",
            risk="możliwa utrata lokalnego sklejenia REL-a z konkretną C1 i spłaszczenie sceny",
            measurement_plan="po Diracu: L+P posL / MLP / posP; porównaj scenę i punktowość z wariantem stereo",
            routing_note="w miniDSP routing/matrix zsumuj L+R do obu wyjść subów; nie rób tego samym biquadem",
        ),
        FilterVariant(
            code="E_KONTROLNY_90_LR48_STEREO",
            title="E — kontrolny 90 LR48 stereo",
            xo_hz=90.0,
            filter_label="LR48",
            topology="stereo L/R",
            description="Kontrolny wariant Linkwitz-Riley przy 90 Hz. Przydatny do porównania z BW48 bez zmiany częstotliwości.",
            goal="sprawdzenie, czy LR48 daje bardziej przewidywalną sumę niż BW48 w Twoim pokoju",
            risk="może być mniej zgodny z dotychczasową preferencją BW48 i teksturą 90",
            measurement_plan="po Diracu: L+P posL / MLP / posP; porównaj body i fokus z A_ORGANICZNY_90",
            routing_note="sub L dostaje kanał L, sub R dostaje kanał R",
        ),
    ]


def write_variant(out_dir: Path, variant: FilterVariant, fs: float) -> dict:
    vdir = out_dir / variant.code
    vdir.mkdir(parents=True, exist_ok=True)
    main = make_sections("HP", fs, variant.xo_hz, variant.filter_label)
    sub = make_sections("LP", fs, variant.xo_hz, variant.filter_label)
    write_minidsp_biquads(vdir / "MAIN_C1_HPF_MINIDSP_BIQUADS.txt", main, f"{variant.title} — MAIN/C1 HPF")
    write_minidsp_biquads(vdir / "SUB_REL_LPF_MINIDSP_BIQUADS.txt", sub, f"{variant.title} — SUB/REL LPF")
    write_csv(vdir / "MAIN_C1_HPF_coefficients.csv", main, fs, variant.xo_hz, variant.filter_label)
    write_csv(vdir / "SUB_REL_LPF_coefficients.csv", sub, fs, variant.xo_hz, variant.filter_label)
    write_response_csv(vdir / "CHARAKTERYSTYKA_ELEKTRYCZNA_ORIENTACYJNA.csv", main, sub, fs)
    readme = f"""# {variant.title}

## Parametry

- XO: **{variant.xo_hz:g} Hz**
- Filtr: **{variant.filter_label}**
- Topologia: **{variant.topology}**
- Częstotliwość próbkowania biquadów: **{fs:g} Hz**

## Cel

{variant.goal}

## Ryzyko

{variant.risk}

## Gdzie wkleić/importować

- `MAIN_C1_HPF_MINIDSP_BIQUADS.txt` — filtr HPF dla wyjść kolumn głównych C1.
- `SUB_REL_LPF_MINIDSP_BIQUADS.txt` — filtr LPF dla wyjść subwooferów REL.

## Format miniDSP SHD v20.17

Pliki `*_MINIDSP_BIQUADS.txt` są zapisane bezpośrednio pod pole `Crossover > Advanced Mode > Biquad Programming`: odwrócony znak `a1/a2`, zapis dziesiętny bez notacji naukowej i dopełnienie do 8 sekcji. Po `Process and Apply` wykres musi pokazać HPF dla C1 oraz LPF dla REL-i.

## Routing / konfiguracja

{variant.routing_note}

## Ważne

To jest **filtr elektryczny do miniDSP**, a nie finalna decyzja odsłuchowa. Po wgraniu filtra trzeba zrobić Dirac i pomiar POST-DIRAC, najlepiej L+P w trzech punktach: `posL`, `MLP`, `posP`.

## Plan pomiaru

{variant.measurement_plan}
"""
    (vdir / "README_WARIANTU.md").write_text(readme, encoding="utf-8")
    return {
        "code": variant.code,
        "title": variant.title,
        "xo_hz": variant.xo_hz,
        "filter": variant.filter_label,
        "topology": variant.topology,
        "goal": variant.goal,
        "risk": variant.risk,
        "dir": str(vdir),
    }


def write_global_reports(out_dir: Path, rows: List[dict], fs: float) -> None:
    with (out_dir / "00_PODSUMOWANIE_WARIANTOW.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["code", "title", "xo_hz", "filter", "topology", "goal", "risk", "dir"])
        w.writeheader()
        for row in rows:
            w.writerow(row)
    md_lines = [
        "# 01 — Co teraz zrobić: generator filtrów zwrotnicy miniDSP",
        "",
        f"Wersja: **{APP_VERSION}**",
        "",
        "## Najkrócej",
        "",
        "Wygenerowano kilka kontrolowanych wariantów zwrotnicy do miniDSP SHD. Nie wybieraj zwycięzcy tylko po nazwie filtra. Wgraj 2–4 warianty do slotów, wykonaj Dirac, a potem zmierz pełne L+P w trzech punktach: `posL`, `MLP`, `posP`.",
        "",
        "## Proponowana kolejność testu",
        "",
        "1. Zacznij od wariantu **A_ORGANICZNY_90_BW48_STEREO** jako punktu odniesienia.",
        "2. Dodaj **B_SREDNICA_SAFE_82_BW48_STEREO**, jeżeli chcesz mocniej chronić średnicę.",
        "3. Dodaj **C_SCENA_96_BW48_STEREO** tylko jako kandydat A/B z kontrolą body i szklistości.",
        "4. **D_MONO_SUB_80_BW48** traktuj jako osobną topologię, nie prosty zamiennik stereo.",
        "5. **E_KONTROLNY_90_LR48_STEREO** użyj, jeśli chcesz sprawdzić LR48 bez zmiany XO.",
        "",
        "## Warianty",
        "",
        "| Kod | XO / filtr | Topologia | Rola | Ryzyko |",
        "|---|---:|---|---|---|",
    ]
    for r in rows:
        md_lines.append(f"| `{r['code']}` | {float(r['xo_hz']):g} Hz {r['filter']} | {r['topology']} | {r['goal']} | {r['risk']} |")
    md_lines += [
        "",
        "## Co jest importowane do miniDSP",
        "",
        "Każdy wariant ma dwa pliki:",
        "",
        "- `MAIN_C1_HPF_MINIDSP_BIQUADS.txt` — HPF dla kolumn C1,",
        "- `SUB_REL_LPF_MINIDSP_BIQUADS.txt` — LPF dla REL-i.",
        "",
        "Częstotliwość próbkowania użyta do biquadów: **{:.0f} Hz**.".format(fs),
        "",
        "## Uwaga o topologii mono",
        "",
        "Wariant mono wymaga ustawienia routingu/matrix w miniDSP: oba suby powinny dostać sumę L+R. Sam plik LPF nie robi mono — filtr tylko ogranicza pasmo subów.",
        "",
        "## Po wgraniu filtrów",
        "",
        "Normalny workflow:",
        "",
        "1. Wgraj wybrane filtry i ustaw delaye/polaryzację zgodnie ze slot planem.",
        "2. Uruchom Dirac.",
        "3. Zmierz POST-DIRAC: L+P w `posL`, `MLP`, `posP`.",
        "4. Opcjonalnie zmierz L i R osobno w MLP do diagnostyki.",
        "5. Dopiero wtedy porównuj odsłuchowo.",
        "",
    ]
    (out_dir / "01_CO_TERAZ_ZROBIC_FILTRY.md").write_text("\n".join(md_lines), encoding="utf-8")

    instr = [
        "# 02 — Instrukcja importu filtrów do miniDSP SHD",
        "",
        "## Ważne ograniczenie",
        "",
        "Pliki biquadów są punktem startowym do testów. Nie zastępują Diraca, pomiaru POST-DIRAC ani odsłuchu.",
        "",
        "**v20.17:** eksport `*_MINIDSP_BIQUADS.txt` jest już w konwencji miniDSP SHD Studio: odwrócony znak `a1/a2`, bez komentarzy, bez notacji naukowej i z dopełnieniem do 8 sekcji.",
        "",
        "## Import / wklejenie",
        "",
        "1. Wybierz wariant filtra, np. `A_ORGANICZNY_90_BW48_STEREO`.",
        "2. Dla wyjść głównych C1 użyj pliku `MAIN_C1_HPF_MINIDSP_BIQUADS.txt`.",
        "3. Dla wyjść subów REL użyj pliku `SUB_REL_LPF_MINIDSP_BIQUADS.txt`.",
        "4. Kliknij `Process and Apply` i sprawdź wykres: Output 1/2 ma być HPF, Output 3/4 ma być LPF.",
        "5. Nie mieszaj biquadów z innymi filtrami crossovera, jeśli nie robisz świadomego testu.",
        "",
        "## Routing stereo",
        "",
        "- L main → lewa C1,",
        "- R main → prawa C1,",
        "- L sub → lewy REL,",
        "- R sub → prawy REL.",
        "",
        "## Routing mono dual-sub",
        "",
        "- oba REL-e powinny dostać sumę L+R,",
        "- filtr LPF jest taki sam jak w stereo, ale routing jest inny,",
        "- mono traktuj jako osobną topologię testową.",
        "",
        "## Kontrola po Diracu",
        "",
        "Zalecane nazwy pomiarów:",
        "",
        "```text",
        "A_ORGANICZNY_90_BW48_STEREO_POST_LP_posL",
        "A_ORGANICZNY_90_BW48_STEREO_POST_LP_MLP",
        "A_ORGANICZNY_90_BW48_STEREO_POST_LP_posP",
        "```",
        "",
    ]
    (out_dir / "02_IMPORT_MINIDSP_SHD.md").write_text("\n".join(instr), encoding="utf-8")

    caution = [
        "# 03 — Jak czytać wygenerowane filtry",
        "",
        "## A — organiczny 90 BW48 stereo",
        "Najbezpieczniejszy punkt startowy. Ma chronić średnicę i naturalność.",
        "",
        "## B — 82 BW48 stereo",
        "Niżej odciąża suby z dolnej średnicy. Dobry test, gdy 90/96 robią zbyt dużo energii lub szklistości.",
        "",
        "## C — 96 BW48 stereo",
        "Kandydat sceniczny/punktowy. Nie traktuj jako finalny bez POST-DIRAC i odsłuchu wokali.",
        "",
        "## D — mono 80 BW48",
        "Test stabilności basu i dziury L+P. Może poprawić równość, ale może osłabić lokalne sklejenie suba z kolumną.",
        "",
        "## E — 90 LR48 stereo",
        "Kontrola LR48 przy tej samej częstotliwości. Przydatne, żeby sprawdzić, czy problem dotyczy samej charakterystyki BW48.",
        "",
    ]
    (out_dir / "03_OPIS_WARIANTOW_FILTRA.md").write_text("\n".join(caution), encoding="utf-8")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="REWDO v20.31 — generator filtrów miniDSP SHD")
    ap.add_argument("--out", required=True, help="Folder wyników")
    ap.add_argument("--fs", type=float, default=96000.0, help="Częstotliwość próbkowania biquadów, domyślnie 96000 Hz")
    args = ap.parse_args(argv)

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    print("REWDO", APP_VERSION)
    print("Folder wyników:", out_dir)
    print("fs:", args.fs)
    for variant in default_variants():
        row = write_variant(out_dir, variant, args.fs)
        rows.append(row)
        print(f"[OK] {variant.code}: {variant.xo_hz:g} Hz {variant.filter_label} ({variant.topology})")
    write_global_reports(out_dir, rows, args.fs)
    print("[OK] Główny plik: 01_CO_TERAZ_ZROBIC_FILTRY.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
