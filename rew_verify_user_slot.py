#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify User Slot v20.31.

Pomaga po sprawdzeniu konsoli miniDSP SHD: wpisujesz cztery delaye ze slotu,
narzędzie wylicza relacje sub-main, opcjonalnie znajduje najbliższy wiersz
w RANKING_CROSSOVEROW_RAW.csv oraz może zapisać realną referencję do
USER_REFERENCE_90BW48.yaml.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


def _float(v: object, default: float = float("nan")) -> float:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return default


def _read_ranking_csv(path: str | Path) -> List[Dict[str, str]]:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ";"
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=delimiter))


def _filter_row(row: Dict[str, str], xo: Optional[int], filt: Optional[str]) -> bool:
    if xo is not None and int(_float(row.get("crossover_hz", ""), -999)) != int(xo):
        return False
    if filt:
        rf = str(row.get("filtr", "")).strip().upper()
        if rf != str(filt).strip().upper():
            return False
    return True


def _distance(row: Dict[str, str], l_rel: float, r_rel: float) -> float:
    rl = _float(row.get("l_sub_minus_main_ms", ""))
    rr = _float(row.get("r_sub_minus_main_ms", ""))
    if not math.isfinite(rl) or not math.isfinite(rr):
        return 999999.0
    return math.sqrt((rl - l_rel) ** 2 + (rr - r_rel) ** 2)


def _filter_label(raw_filter: Optional[str]) -> str:
    text = str(raw_filter or "BW48").strip().upper().replace(" ", "")
    if text in {"BW24", "BW48", "LR24", "LR48"}:
        return text
    if text in {"BW", "LR"}:
        return text + "48"
    return "BW48"


def _write_user_reference_yaml(
    path: str | Path,
    *,
    l_main: float,
    r_main: float,
    l_sub: float,
    r_sub: float,
    l_pol: str,
    r_pol: str,
    xo: Optional[int],
    filt: Optional[str],
    verification_source: Optional[str | Path],
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    xo_val = int(xo or 90)
    filter_val = _filter_label(filt)
    source = Path(verification_source) if verification_source else None
    if source is not None:
        from rew_mdat import scan_mdat_project
        if not source.is_file() or source.suffix.lower() not in {".mdat", ".json"}:
            raise ValueError("Dowodem może być odczytywalny pomiar MDAT/API, nie dowolny plik.")
        scan_mdat_project(source)  # format check; attaching is NOT verification
        measurement_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        verified = False
        verification_status = "MEASUREMENT_ATTACHED_UNVERIFIED"
        verified_at = None
    else:
        measurement_sha256 = None
        verified = False
        verification_status = "USER_ENTERED_UNVERIFIED"
        verified_at = None
    lines = [
        "# USER_REFERENCE_90BW48.yaml — REWDO v20.31 REFERENCE RECORD",
        "# verified=true wymaga identyfikowalnego pliku pomiarowego podanego przez --verification-source.",
        f"name: {xo_val}_{filter_val}_slot_real",
        "description: Ustawienie wpisane z konsoli; bez automatycznej oceny odsłuchowej.",
        f"crossover_hz: {xo_val}",
        f"filter: {filter_val}",
        f"verified: {'true' if verified else 'false'}",
        f"verification_status: {verification_status}",
        f"verified_by_measurement_sha256: {measurement_sha256 or 'null'}",
        f"verified_at: {verified_at or 'null'}",
        f"l_main_ms: {float(l_main):.2f}",
        f"r_main_ms: {float(r_main):.2f}",
        f"l_sub_ms: {float(l_sub):.2f}",
        f"r_sub_ms: {float(r_sub):.2f}",
        f"l_sub_polarity: {str(l_pol or 'OFF').upper()}",
        f"r_sub_polarity: {str(r_pol or 'OFF').upper()}",
        "subjective_notes: null",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v20.31: sprawdź slot SHD, porównaj z rankingiem RAW i opcjonalnie zapisz referencję z metadanymi")
    p.add_argument("--l-main", required=True, help="Delay L main [ms] z konsoli SHD")
    p.add_argument("--r-main", required=True, help="Delay R main [ms] z konsoli SHD")
    p.add_argument("--l-sub", required=True, help="Delay L sub [ms] z konsoli SHD")
    p.add_argument("--r-sub", required=True, help="Delay R sub [ms] z konsoli SHD")
    p.add_argument("--l-pol", default="OFF", help="Polaryzacja L sub, domyślnie OFF")
    p.add_argument("--r-pol", default="OFF", help="Polaryzacja R sub, domyślnie OFF")
    p.add_argument("--xo", type=int, default=None, help="Opcjonalnie crossover, np. 90")
    p.add_argument("--filter", default=None, help="Opcjonalnie filtr, np. BW48")
    p.add_argument("--ranking-csv", default=None, help="Opcjonalnie RANKING_CROSSOVEROW_RAW.csv")
    p.add_argument("--save-user-reference", default=None, help="Zapisz te wartości jako USER_REFERENCE_90BW48.yaml")
    p.add_argument("--verification-source", default=None, help="Dołącz pomiar; sama tożsamość pliku nie weryfikuje konfiguracji")
    p.add_argument("--out", default="VERIFY_USER_SLOT.md", help="Plik raportu markdown")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    l_main = _float(args.l_main)
    r_main = _float(args.r_main)
    l_sub = _float(args.l_sub)
    r_sub = _float(args.r_sub)
    l_rel = l_sub - l_main
    r_rel = r_sub - r_main

    lines = [
        "# VERIFY USER SLOT — REWDO v20.17",
        "",
        "## Wartości z konsoli SHD",
        "",
        f"- L main: **{l_main:.2f} ms**",
        f"- R main: **{r_main:.2f} ms**",
        f"- L sub: **{l_sub:.2f} ms**, polaryzacja **{args.l_pol}**",
        f"- R sub: **{r_sub:.2f} ms**, polaryzacja **{args.r_pol}**",
        "",
        "## Relacja kontrolna sub - main",
        "",
        f"- L sub - L main: **{l_rel:+.2f} ms**",
        f"- R sub - R main: **{r_rel:+.2f} ms**",
        "",
    ]

    if args.xo or args.filter:
        lines.extend([
            "## Opis slotu",
            "",
            f"- XO: **{args.xo if args.xo is not None else '?'} Hz**",
            f"- Filtr: **{args.filter or '?'}**",
            "",
        ])

    if args.ranking_csv:
        rows = _read_ranking_csv(args.ranking_csv)
        candidates = [r for r in rows if _filter_row(r, args.xo, args.filter)]
        if not candidates:
            candidates = rows
            lines.append("Nie znaleziono wierszy dokładnie zgodnych z XO/filtrem — szukam najbliższego w całym CSV.")
        if candidates:
            best = min(candidates, key=lambda r: _distance(r, l_rel, r_rel))
            dist = _distance(best, l_rel, r_rel)
            if dist > 1.0:
                warn = "⚠️ Realny slot SHD różni się od najbliższego wariantu RAW o >1 ms. Slot A powinien pozostać zablokowany z YAML, nie z rankingu."
            else:
                warn = "Realny slot jest blisko wariantu RAW."
            lines.extend([
                "## Najbliższy wariant w rankingu",
                "",
                f"- Miejsce: **{best.get('miejsce','?')}**",
                f"- Wariant: **{best.get('crossover_hz','?')} Hz {best.get('filtr','?')} {best.get('polaryzacja','')}**",
                f"- Relacja w rankingu: L **{_float(best.get('l_sub_minus_main_ms','')):+.2f} ms**, R **{_float(best.get('r_sub_minus_main_ms','')):+.2f} ms**",
                f"- Odległość delayów od Twojego slotu: **{dist:.2f} ms RMS**",
                f"- Ostrzeżenie: **{warn}**",
                f"- Wynik muzyczny: **{best.get('wynik_muzyczny_0_100','?')}**",
                f"- Confidence: **{best.get('confidence_kandydata','?')}**",
                f"- Podparcie dolnej średnicy — PREDYKCJA RAW: **{best.get('midrange_glow_score_0_100','?')}**",
                f"- SHD z rankingu: main L {best.get('l_main_shd_ms','?')} ms, main R {best.get('r_main_shd_ms','?')} ms, sub L {best.get('l_sub_shd_ms','?')} ms, sub R {best.get('r_sub_shd_ms','?')} ms",
                "",
            ])
        else:
            lines.append("Ranking CSV nie zawiera wierszy do porównania.")

    if args.save_user_reference:
        saved = _write_user_reference_yaml(
            args.save_user_reference,
            l_main=l_main, r_main=r_main, l_sub=l_sub, r_sub=r_sub,
            l_pol=args.l_pol, r_pol=args.r_pol, xo=args.xo, filt=args.filter,
            verification_source=args.verification_source,
        )
        lines.extend([
            "## Zapis referencji",
            "",
            f"Zapisano referencję do: `{saved}`",
            ("Status: **MEASUREMENT_ATTACHED_UNVERIFIED** — dołączono pomiar; zgodność konfiguracji wymaga kontroli w projekcie."
             if args.verification_source else
             "Status: **USER_ENTERED_UNVERIFIED** — bez pliku pomiarowego wpis nie może być automatycznym anchorem."),
            "",
        ])

    lines.extend([
        "## Interpretacja",
        "",
        "- Dla Twojej referencji 90 BW48 z mainami 30 ms spodziewany obszar to około L sub 20 ms / R sub 19 ms, czyli relacje mniej więcej -10 ms / -11 ms.",
        "- Wartości około L sub 12 ms przy mainach 30 ms oznaczają inną relację i prawdopodobnie inny slot lub inny wariant.",
        "- v20.31: tylko referencja z metadanymi pomiaru i bez konfliktu może zostać automatycznym anchorem.",
        "",
    ])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("VERIFY USER SLOT gotowy:", out)
    print(f"Relacja sub-main: L {l_rel:+.2f} ms, R {r_rel:+.2f} ms")
    if args.save_user_reference:
        print("Zapisano USER_REFERENCE:", args.save_user_reference)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
