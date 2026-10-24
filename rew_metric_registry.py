# -*- coding: utf-8 -*-
"""Wspólny rejestr nazw, zakresów i porównywalności metryk REWDO v20.23.

Zasada aplikacji:
- ta sama nazwa bez dopisku etapu oznacza ten sam wzór i tę samą skalę;
- metryki z różnych etapów są zawsze oznaczone jako PREDYKCJA RAW,
  SYMULACJA PROJEKTU albo POMIAR POST-DIRAC;
- wynik 0–100 jest porównywalny między trybami tylko wtedy, gdy ma ten sam
  ``comparison_group``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping

import csv
import numpy as np

PREDYKCJA_RAW = "PREDYKCJA RAW"
SYMULACJA_PROJEKTU = "SYMULACJA PROJEKTU"
POMIAR_POST_DIRAC = "POMIAR POST-DIRAC"
METRYKA_FIZYCZNA = "WARTOŚĆ FIZYCZNA"


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    public_name: str
    stage: str
    scale: str
    direction: str
    comparison_group: str
    modes: str
    meaning: str
    comparable_note: str

    @property
    def full_label(self) -> str:
        return f"{self.public_name} — {self.stage}"


METRICS: Dict[str, MetricDefinition] = {
    # WSPÓLNA SKALA TRYBÓW RAW/PROJEKT: identyczny wzór, różny etap dowodowy.
    "side_integration_raw": MetricDefinition(
        "side_integration_raw", "Sklejenie stron", PREDYKCJA_RAW, "0–100", "więcej = lepiej",
        "SIDE_INTEGRATION_V1", "1, 3, 5, 6",
        "Osobno ocenia L main+L sub i R main+R sub z konstruktywności oraz głębokości dołku; wynik ogólny mocniej waży słabszą stronę.",
        "Skala jest matematycznie zgodna z trybami 8–9, lecz RAW i projekt pozostają innymi etapami analizy.",
    ),
    "side_integration_project": MetricDefinition(
        "side_integration_project", "Sklejenie stron", SYMULACJA_PROJEKTU, "0–100", "więcej = lepiej",
        "SIDE_INTEGRATION_V1", "8, 9",
        "Ten sam wzór co w rankingu RAW, liczony po zasymulowaniu projektowanego HPF/LPF, delayu i polaryzacji.",
        "Liczba ma tę samą skalę co w trybach 1/3/5/6; dopisek etapu informuje, że jest to symulacja, a nie pomiar po Dirac.",
    ),
    "lr_coherence_raw": MetricDefinition(
        "lr_coherence_raw", "Spójność sum L/R przy XO", PREDYKCJA_RAW, "0–100", "więcej = lepiej",
        "LR_COHERENCE_RAW_V1", "1, 3, 5, 6",
        "Kontroluje wzajemną fazę, balans i sumowanie gotowych stron L i R przy crossoverze.",
        "Nie jest zamiennikiem sklejenia main–sub jednej strony i nie jest porównywalna z bezpośrednią fazą main–sub trybu 9.",
    ),
    "lr_sum_project": MetricDefinition(
        "lr_sum_project", "Jakość sumy stereo L+P", SYMULACJA_PROJEKTU, "0–100", "więcej = lepiej",
        "LR_SUM_PROJECT_V1", "8, 9",
        "Ocena konstruktywności, dołku i asymetrii sumy dwóch gotowych stron po zasymulowaniu zwrotnicy.",
        "Porównywalna między trybem 8 i 9; nie jest tą samą metryką co Spójność sum L/R przy XO w rankingu RAW.",
    ),
    "main_sub_phase_project": MetricDefinition(
        "main_sub_phase_project", "Zgodność fazowa main–sub", SYMULACJA_PROJEKTU, "0–100", "więcej = lepiej",
        "MAIN_SUB_PHASE_PROJECT_V1", "9",
        "Bezpośrednio porównuje fazę maina i subwoofera każdej strony w paśmie, w którym obie gałęzie mają istotny udział.",
        "Metryka specyficzna dla projektanta biquadów; nie porównuj jej z fazą L/R sumy stereo.",
    ),
    # KORPUS DOLNEJ ŚREDNICY: różne wzory -> celowo różne nazwy.
    "body_support_raw": MetricDefinition(
        "body_support_raw", "Podparcie dolnej średnicy", PREDYKCJA_RAW, "0–100", "więcej = lepiej",
        "BODY_SUPPORT_RAW_V1", "1, 3, 5, 6",
        "Wieloskładnikowa predykcja masy 100–220 Hz względem basu, z ochroną przed odchudzeniem i maskowaniem.",
        "Nie porównuj liczbowo z ciągłością 100–220 Hz trybu 8/9 ani z teksturą po Dirac.",
    ),
    "body_continuity_project": MetricDefinition(
        "body_continuity_project", "Ciągłość korpusu 100–220 Hz", SYMULACJA_PROJEKTU, "0–100", "więcej = lepiej",
        "BODY_CONTINUITY_PROJECT_V1", "8, 9",
        "Ocena ciągłości pasm 100–160 i 160–220 Hz oraz lokalnej nierówności po zasymulowaniu filtru.",
        "Porównywalna między trybem 8 i 9; nie jest skalą Podparcia dolnej średnicy rankingu RAW.",
    ),
    "mid_texture_post": MetricDefinition(
        "mid_texture_post", "Zachowanie tekstury dolnej średnicy", POMIAR_POST_DIRAC, "0–100 / różnica dB", "więcej = lepiej dla wyniku; 0 dB = referencja",
        "MID_TEXTURE_POST_V1", "2",
        "Ocena rzeczywistego pomiaru po Dirac względem 90 BW48, przede wszystkim w 125–220 Hz.",
        "Jest końcową oceną pomiarową; nie porównuj jej 1:1 z predykcją RAW ani symulacją projektu.",
    ),
    # ODPORNOŚĆ: trzy różne rodzaje, jednoznaczne nazwy.
    "spatial_robustness_raw": MetricDefinition(
        "spatial_robustness_raw", "Przewidywana odporność przestrzenna", PREDYKCJA_RAW, "0–100", "więcej = lepiej",
        "SPATIAL_ROBUSTNESS_RAW_V1", "1, 3, 5, 6",
        "Predykcja z jednego RAW na podstawie asymetrii, sumowania i lokalnej stabilności.",
        "Nie jest rzeczywistym pomiarem kilku pozycji.",
    ),
    "parameter_robustness_project": MetricDefinition(
        "parameter_robustness_project", "Odporność na błąd opóźnienia i poziomu", SYMULACJA_PROJEKTU, "0–100 (20. percentyl)", "więcej = lepiej",
        "PARAMETER_ROBUSTNESS_PROJECT_V1", "9",
        "Stabilność wyniku po perturbacji delayu ±0,2 ms i poziomu subów ±0,5 dB.",
        "Nie porównuj z odpornością przestrzenną z kilku pozycji mikrofonu.",
    ),
    "spatial_robustness_post": MetricDefinition(
        "spatial_robustness_post", "Zmierzona odporność przestrzenna L/MLP/P", POMIAR_POST_DIRAC, "0–100", "więcej = lepiej",
        "SPATIAL_ROBUSTNESS_POST_V1", "2",
        "Ocena rzeczywistych pomiarów L+P w pozycjach lewa/MLP/prawa; przy braku kompletu raport wyraźnie oznacza predykcję.",
        "Najwyższy poziom wiarygodności przestrzennej; nie porównuj bezpośrednio z zmianami opóźnienia/poziomu.",
    ),
    # DIRAC: wspólny kierunek, różne modele.
    "dirac_burden_raw": MetricDefinition(
        "dirac_burden_raw", "Przewidywane obciążenie korekcji Dirac", PREDYKCJA_RAW, "0–100", "mniej = lepiej",
        "DIRAC_BURDEN_RAW_V1", "1, 3, 5, 6",
        "Predykcja ryzyka, że Dirac będzie musiał ratować słaby punkt startowy.",
        "Nie porównuj liczbowo z lokalnym wskaźnikiem projektu ani z wynikiem po Dirac.",
    ),
    "dirac_burden_project": MetricDefinition(
        "dirac_burden_project", "Symulowane obciążenie korekcji Dirac", SYMULACJA_PROJEKTU, "0–100", "mniej = lepiej",
        "DIRAC_BURDEN_PROJECT_V1", "8, 9",
        "Wskaźnik lokalnych reszt, dołków i nierówności po usunięciu łagodnego trendu tonalnego.",
        "Porównywalny między trybem 8 i 9; nie oznacza liczby dB korekcji.",
    ),
    "dirac_result_post": MetricDefinition(
        "dirac_result_post", "Jakość wyniku po korekcji Dirac", POMIAR_POST_DIRAC, "wiele wartości fizycznych / lokalne wyniki 0–100", "zależnie od pola",
        "DIRAC_RESULT_POST_V1", "2",
        "Rzeczywiste SPL, balans L/R, faza, opóźnienie grupowe, dołki oraz zgodność przestrzenna po korekcji.",
        "Nie jest prognozą obciążenia Dirac i nie powinna używać tej samej krótkiej nazwy.",
    ),
    # DZIURA L+P.
    "lr_null_risk_raw": MetricDefinition(
        "lr_null_risk_raw", "Przewidywane ryzyko dziury L+P po Dirac", PREDYKCJA_RAW, "0–100", "mniej = lepiej",
        "LR_NULL_RISK_RAW_V1", "1, 3, 5, 6",
        "Predyktor kasowania w sumie stereo po korekcji, liczony z RAW.",
        "Nie porównuj liczbowo z symulacją zwrotnicy ani z rzeczywistym pomiarem po Dirac.",
    ),
    "lr_null_risk_project": MetricDefinition(
        "lr_null_risk_project", "Symulowane ryzyko dziury L+P", SYMULACJA_PROJEKTU, "0–100", "mniej = lepiej",
        "LR_NULL_RISK_PROJECT_V1", "8, 9",
        "Ryzyko wynikające z dołku, konstruktywności i asymetrii zasymulowanej sumy L+P.",
        "Porównywalne między trybem 8 i 9.",
    ),
    "lr_null_post": MetricDefinition(
        "lr_null_post", "Zmierzona dziura L+P po Dirac", POMIAR_POST_DIRAC, "dB / pomocnicze 0–100", "mniejsza dziura = lepiej",
        "LR_NULL_POST_V1", "2",
        "Ocena rzeczywistej sumy L+P po korekcji, również w kilku pozycjach, jeżeli są dostępne.",
        "To pomiar wyniku, nie ryzyko prognostyczne.",
    ),
    # WYNIKI GŁÓWNE: wszystkie mają unikalne nazwy.
    "music_score_raw": MetricDefinition(
        "music_score_raw", "Wynik muzyczny rankingu RAW", PREDYKCJA_RAW, "0–100", "więcej = lepiej",
        "MUSIC_SCORE_RAW_V1", "1, 3, 5, 6",
        "Kolejność kandydatów w obrębie tego samego pliku RAW i profilu.",
        "Nie jest absolutną oceną systemu i nie porównuj go z wynikiem selektora lub projektanta.",
    ),
    "selector_score_project": MetricDefinition(
        "selector_score_project", "Wynik selektora standardowych filtrów", SYMULACJA_PROJEKTU, "0–100", "więcej = lepiej",
        "SELECTOR_SCORE_PROJECT_V1", "8",
        "Ogólny wynik lokalnej symulacji standardowych BW/LR.",
        "Porównuj tylko wewnątrz trybu 8 dla tego samego RAW.",
    ),
    "biquad_score_project": MetricDefinition(
        "biquad_score_project", "Wynik projektanta biquadów", SYMULACJA_PROJEKTU, "0–100", "więcej = lepiej",
        "BIQUAD_SCORE_PROJECT_V1", "9",
        "Połączenie integracji technicznej, profilu tonalnego i odporności na perturbacje.",
        "Porównuj tylko między kandydatami tego samego profilu i tego samego RAW.",
    ),
    "replacement_score_post": MetricDefinition(
        "replacement_score_post", "Wynik zastąpienia referencji 90 BW48", POMIAR_POST_DIRAC, "0–100", "więcej = lepiej",
        "REPLACEMENT_SCORE_POST_V1", "2",
        "Końcowa pomocnicza ocena rzeczywistego slotu względem referencji po Dirac.",
        "Nie porównuj z wynikiem muzycznym RAW ani wynikami projektantów.",
    ),
}


def metric(metric_id: str) -> MetricDefinition:
    return METRICS[metric_id]


def metric_label(metric_id: str) -> str:
    return metric(metric_id).full_label


def same_scale(metric_a: str, metric_b: str) -> bool:
    a = metric(metric_a)
    b = metric(metric_b)
    return bool(a.comparison_group and a.comparison_group == b.comparison_group)


def linear_score_between(value: float, *, good: float, bad: float, higher_is_better: bool = True) -> float:
    v = float(value)
    if higher_is_better:
        if v >= good:
            return 100.0
        if v <= bad:
            return 0.0
        return float(100.0 * (v - bad) / max(1e-9, good - bad))
    if v <= good:
        return 100.0
    if v >= bad:
        return 0.0
    return float(100.0 * (bad - v) / max(1e-9, bad - good))


def side_integration_from_construct_dip(
    l_construct_db: float,
    r_construct_db: float,
    l_dip_db: float,
    r_dip_db: float,
) -> Dict[str, float | str | bool]:
    """Wspólna skala sklejenia stron używana w trybach 1, 3, 5, 6, 8 i 9."""
    l_construct_score = linear_score_between(l_construct_db, good=2.5, bad=-1.5, higher_is_better=True)
    r_construct_score = linear_score_between(r_construct_db, good=2.5, bad=-1.5, higher_is_better=True)
    l_dip_score = linear_score_between(l_dip_db, good=4.0, bad=10.0, higher_is_better=False)
    r_dip_score = linear_score_between(r_dip_db, good=4.0, bad=10.0, higher_is_better=False)

    l_score = float(np.clip(0.62 * l_construct_score + 0.38 * l_dip_score, 0.0, 100.0))
    r_score = float(np.clip(0.62 * r_construct_score + 0.38 * r_dip_score, 0.0, 100.0))
    side_min = min(l_score, r_score)
    side_avg = 0.5 * (l_score + r_score)
    asym = abs(l_score - r_score)
    side_score = float(np.clip(0.68 * side_min + 0.32 * side_avg - 0.35 * max(0.0, asym - 18.0), 0.0, 100.0))

    reasons: List[str] = []
    if l_score < 55.0:
        reasons.append(f"L sub/main {l_score:.1f} < 55,0")
    if r_score < 55.0:
        reasons.append(f"R sub/main {r_score:.1f} < 55,0")
    if side_min < 60.0 and asym > 28.0:
        reasons.append(f"asymetria stron {asym:.1f} pkt przy słabszej stronie {side_min:.1f}")

    if side_min >= 76.0 and asym <= 18.0:
        label = "bardzo dobre sklejenie stron"
    elif side_min >= 65.0:
        label = "dobre sklejenie stron"
    elif side_min >= 55.0:
        label = "warunkowe sklejenie stron"
    else:
        label = "słaba jedna strona — tylko diagnostyka"

    return {
        "l_score": l_score,
        "r_score": r_score,
        "side_score": side_score,
        "side_min": side_min,
        "side_mean": side_avg,
        "asymmetry": asym,
        "asymmetry_penalty": float(max(0.0, asym - 18.0)),
        "gate_pass": not reasons,
        "gate_reasons": "; ".join(reasons) if reasons else "OK",
        "label": label,
    }


def registry_rows() -> Iterable[MetricDefinition]:
    return METRICS.values()


def write_metric_registry(out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = [
        "# Rejestr metryk i skal REWDO v20.31",
        "",
        "## Zasada porównywania",
        "",
        "Wyniki `/100` wolno porównywać bezpośrednio tylko wtedy, gdy w kolumnie `Grupa porównawcza` mają ten sam identyfikator. Dopisek `PREDYKCJA RAW`, `SYMULACJA PROJEKTU` albo `POMIAR POST-DIRAC` określa poziom dowodu. Identyczna matematyka nie zamienia symulacji w rzeczywisty pomiar.",
        "",
        "| Identyfikator | Nazwa w raporcie | Etap | Skala | Kierunek | Tryby | Grupa porównawcza | Porównywalność |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d in METRICS.values():
        md.append(
            f"| `{d.metric_id}` | {d.public_name} | {d.stage} | {d.scale} | {d.direction} | {d.modes} | `{d.comparison_group}` | {d.comparable_note} |"
        )
    md += ["", "## Znaczenie metryk", ""]
    for d in METRICS.values():
        md += [f"### {d.full_label}", "", d.meaning, "", f"**Porównywalność:** {d.comparable_note}", ""]
    (out / "METRYKI_I_SKALY.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    with (out / "METRYKI_I_SKALY.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["metric_id", "nazwa", "etap", "skala", "kierunek", "tryby", "grupa_porownawcza", "znaczenie", "porownywalnosc"])
        for d in METRICS.values():
            w.writerow([d.metric_id, d.public_name, d.stage, d.scale, d.direction, d.modes, d.comparison_group, d.meaning, d.comparable_note])
