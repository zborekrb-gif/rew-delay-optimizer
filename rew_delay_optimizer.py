#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REW Delay Optimizer — lekki punkt startowy.

Kod aplikacji jest podzielony na moduły:
- rew_common.py — dataclassy, presety, stałe i proste funkcje konfiguracyjne,
- rew_mdat.py — import i diagnostyka plików .mdat,
- rew_scoring.py — matematyka audio, optymalizacja, PBIS i PBIS Safe,
- rew_crossover.py — symulacja HPF/LPF SHD na pomiarach bazowych RAW,
- rew_reports.py — raporty, wykresy, organizacja wyników i analizy dodatkowe,
- rew_cli_gui.py — CLI, GUI i główne sterowanie aplikacją.
"""
from __future__ import annotations

from rew_cli_gui import main


if __name__ == "__main__":
    raise SystemExit(main())
