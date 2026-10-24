# -*- coding: utf-8 -*-
"""Okno połączenia REWDO z lokalnym API REW 5.40."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from rew_api_client import DEFAULT_REW_API_URL, REWApiClient, REWApiError, REWApiMeasurement, read_snapshot, verify_measurement_state, verify_snapshot_response_integrity
from rew_api_parity import compare_api_snapshot_to_mdat, parity_report_markdown
from rew_mdat import infer_mdat_measurement_role


class REWApiDialog(tk.Toplevel):
    """Przeglądarka pomiarów REW i kreator lokalnej migawki API."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        initial_url: str = DEFAULT_REW_API_URL,
        initial_dir: str | Path | None = None,
        on_snapshot: Optional[Callable[[Path], None]] = None,
        time_analysis_mode: bool = False,
        include_group_delay_default: bool = True,
        include_impulse_default: bool = False,
    ) -> None:
        super().__init__(master)
        self.title("REWDO v20.31 — połączenie z REW 5.40 API")
        self.geometry("1120x860")
        self.minsize(920, 680)
        self.transient(master)

        self.initial_dir = Path(initial_dir or Path.cwd())
        self.on_snapshot = on_snapshot
        self.time_analysis_mode = bool(time_analysis_mode)
        self.measurements: List[REWApiMeasurement] = []
        self.item_to_measurement: Dict[str, REWApiMeasurement] = {}
        self.busy = False

        self.url_var = tk.StringVar(value=initial_url or DEFAULT_REW_API_URL)
        self.parity_mdat_var = tk.StringVar(value="")
        self.include_group_delay_var = tk.BooleanVar(value=bool(include_group_delay_default))
        self.include_impulse_var = tk.BooleanVar(value=bool(include_impulse_default))
        self.status_var = tk.StringVar(value="Uruchom REW 5.40, włącz API i naciśnij Połącz / odśwież.")
        self.count_var = tk.StringVar(value="0 pomiarów")

        # Stan pomiaru nie jest dostępny w API REW, dlatego użytkownik potwierdza
        # go jawnie. Bez kompletu potwierdzeń migawka pozostaje diagnostyczna.
        self.raw_confirm_var = tk.BooleanVar(value=False)
        self.dirac_off_var = tk.BooleanVar(value=False)
        self.crossover_off_var = tk.BooleanVar(value=False)
        self.peq_off_var = tk.BooleanVar(value=False)
        self.delays_zero_var = tk.BooleanVar(value=False)
        self.polarity_normal_var = tk.BooleanVar(value=False)
        self.rel120_var = tk.BooleanVar(value=False)
        self.timing_consistent_var = tk.BooleanVar(value=False)
        self.microphone_stationary_var = tk.BooleanVar(value=False)
        self.main_gain_var = tk.StringVar(value="0.0")
        self.sub_gain_var = tk.StringVar(value="-6.0")

        self._build_ui()
        if self.time_analysis_mode:
            self.status_var.set("Tryb czasowy: połącz z REW, zaznacz sumy, group delay i impuls.")
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        connection = ttk.LabelFrame(root, text="Lokalne API REW")
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Adres:").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        ttk.Entry(connection, textvariable=self.url_var).grid(row=0, column=1, sticky="ew", padx=8, pady=7)
        self.refresh_button = ttk.Button(connection, text="Połącz / odśwież", command=self._refresh)
        self.refresh_button.grid(row=0, column=2, padx=8, pady=7)
        ttk.Label(
            connection,
            text="REW: Preferences → API → Start API server, domyślnie 127.0.0.1:4735",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 7))

        table_frame = ttk.LabelFrame(root, text="Pomiary aktualnie otwarte w REW")
        table_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("nr", "role", "title", "group", "range", "date", "version")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "nr": "#",
            "role": "Rola REWDO",
            "title": "Nazwa pomiaru",
            "group": "Grupa",
            "range": "Zakres",
            "date": "Data",
            "version": "REW",
        }
        widths = {"nr": 45, "role": 110, "title": 360, "group": 150, "range": 120, "date": 150, "version": 90}
        for key in columns:
            self.tree.heading(key, text=headings[key])
            self.tree.column(key, width=widths[key], minwidth=40, stretch=key in {"title", "group"})
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        tools = ttk.Frame(root)
        tools.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(tools, text="Zaznacz podstawowe RAW", command=self._select_basic_raw).pack(side="left")
        ttk.Button(tools, text="Zaznacz sumy do analizy czasowej", command=self._select_time_sums).pack(side="left", padx=(6, 0))
        ttk.Button(tools, text="Zaznacz wszystko", command=self._select_all).pack(side="left", padx=(6, 0))
        ttk.Button(tools, text="Wyczyść zaznaczenie", command=self._clear_selection).pack(side="left", padx=(6, 0))
        ttk.Label(tools, textvariable=self.count_var).pack(side="right")

        options = ttk.LabelFrame(root, text="Migawka danych dla REWDO")
        options.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="Tryb danych:").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        ttk.Label(options, text="NATYWNA OŚ REW + BRAK WYGŁADZANIA (bez wymuszania PPO)").grid(
            row=0, column=1, sticky="w", padx=8, pady=7
        )
        ttk.Checkbutton(options, text="Pobierz group delay", variable=self.include_group_delay_var).grid(
            row=0, column=2, sticky="w", padx=8, pady=7
        )
        ttk.Checkbutton(options, text="Pobierz impuls (większy plik)", variable=self.include_impulse_var).grid(
            row=0, column=3, sticky="w", padx=8, pady=7
        )
        ttk.Label(options, text="Opcjonalny MDAT tego samego zestawu do kontroli zgodności:").grid(
            row=1, column=0, sticky="w", padx=8, pady=7
        )
        ttk.Entry(options, textvariable=self.parity_mdat_var).grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=7)
        ttk.Button(options, text="Wybierz MDAT…", command=self._choose_parity_mdat).grid(row=1, column=3, sticky="e", padx=8, pady=7)
        info_text = (
            "Tryb 10: zaznacz group delay i impuls. Wskaż referencję oraz rzeczywiście zmierzone warianty przy nieruchomym mikrofonie i tym samym timing reference."
            if self.time_analysis_mode else
            "MDAT nie jest wymagany przy każdym użyciu. Warto wykonać kontrolę po aktualizacji REW; FAIL blokuje eksport."
        )
        ttk.Label(options, text=info_text, wraplength=900).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 7)
        )

        raw_state_title = (
            "Warunki porównania czasowego — wymagane: ten sam timing reference i nieruchomy mikrofon"
            if self.time_analysis_mode else
            "Potwierdzenie stanu RAW — wymagane do eksportu biquadów"
        )
        raw_state = ttk.LabelFrame(root, text=raw_state_title)
        self.raw_state_frame = raw_state
        raw_state.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        for col in range(4):
            raw_state.columnconfigure(col, weight=1)
        if self.time_analysis_mode:
            time_checks = [
                ("Ta sama metoda timing reference we wszystkich sweepach", self.timing_consistent_var),
                ("Mikrofon nieruchomo między wszystkimi sweepami", self.microphone_stationary_var),
            ]
            for index, (label, variable) in enumerate(time_checks):
                ttk.Checkbutton(raw_state, text=label, variable=variable).grid(
                    row=0, column=index * 2, columnspan=2, sticky="w", padx=8, pady=6
                )
            ttk.Label(
                raw_state,
                text=(
                    "W tym trybie nie potwierdzasz czystego RAW. Porównujesz rzeczywiście zmierzone sumy "
                    "referencji i kandydatów. Crossover, delay, gain i Dirac muszą odpowiadać nazwie danego wariantu."
                ),
                wraplength=980,
            ).grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 7))
        else:
            checks = [
                ("Dirac OFF", self.dirac_off_var),
                ("Crossover SHD OFF", self.crossover_off_var),
                ("PEQ OFF", self.peq_off_var),
                ("Delaye wszystkich wyjść 0,00 ms", self.delays_zero_var),
                ("Polaryzacja normalna / OFF", self.polarity_normal_var),
                ("Filtr analogowy REL ustawiony na 120 Hz", self.rel120_var),
                ("Ta sama metoda timing reference", self.timing_consistent_var),
                ("Mikrofon nieruchomo między sweepami", self.microphone_stationary_var),
            ]
            for index, (label, variable) in enumerate(checks):
                ttk.Checkbutton(raw_state, text=label, variable=variable).grid(
                    row=index // 4, column=index % 4, sticky="w", padx=8, pady=3
                )
            ttk.Label(raw_state, text="Gain main [dB]:").grid(row=2, column=0, sticky="e", padx=(8, 3), pady=5)
            ttk.Entry(raw_state, textvariable=self.main_gain_var, width=8).grid(row=2, column=1, sticky="w", padx=(3, 8), pady=5)
            ttk.Label(raw_state, text="Gain sub [dB]:").grid(row=2, column=2, sticky="e", padx=(8, 3), pady=5)
            ttk.Entry(raw_state, textvariable=self.sub_gain_var, width=8).grid(row=2, column=3, sticky="w", padx=(3, 8), pady=5)
            ttk.Checkbutton(
                raw_state,
                text="Potwierdzam, że powyższe ustawienia obowiązywały podczas wszystkich zaznaczonych pomiarów RAW",
                variable=self.raw_confirm_var,
            ).grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(5, 7))
            ttk.Button(raw_state, text="Wypełnij typowy RAW 0 / −6 dB", command=self._fill_typical_raw).grid(
                row=3, column=3, sticky="e", padx=8, pady=(5, 7)
            )
            ttk.Label(
                raw_state,
                text="REWDO zapisuje to potwierdzenie w migawce. Nie odczytuje ustawień SHD automatycznie.",
            ).grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 7))

        bottom = ttk.Frame(root)
        bottom.grid(row=5, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.status_var, wraplength=760).grid(row=0, column=0, sticky="w")
        self.snapshot_button = ttk.Button(bottom, text="Utwórz migawkę i użyj w REWDO…", command=self._create_snapshot)
        self.snapshot_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(bottom, text="Zamknij", command=self._close).grid(row=0, column=2, padx=(8, 0))

        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_count())

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.refresh_button.configure(state=state)
        self.snapshot_button.configure(state=state)
        if text:
            self.status_var.set(text)
        if busy:
            self.configure(cursor="watch")
        else:
            self.configure(cursor="")

    def _refresh(self) -> None:
        if self.busy:
            return
        # Wszystkie odczyty zmiennych Tkinter muszą odbywać się w głównym wątku.
        # W v20.26 url_var.get() wykonywało się w workerze, co na Windows mogło
        # zatrzymać odświeżanie i pozostawić pustą tabelę bez komunikatu.
        requested_url = self.url_var.get()
        self._set_busy(True, "Łączenie z lokalnym API REW…")

        def worker(url: str) -> None:
            try:
                client = REWApiClient(url)
                measurements = client.list_measurements()
                self.after(
                    0,
                    lambda base_url=client.base_url, rows=measurements: self._populate(base_url, rows),
                )
            except Exception as exc:
                # Domyślny argument zachowuje wyjątek po wyjściu z bloku except.
                self.after(0, lambda error=exc: self._show_error("Nie udało się pobrać pomiarów", error))

        threading.Thread(target=worker, args=(requested_url,), daemon=True).start()

    def _populate(self, normalised_url: str, measurements: List[REWApiMeasurement]) -> None:
        self.url_var.set(normalised_url)
        self.measurements = measurements
        self.item_to_measurement.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, measurement in enumerate(measurements, start=1):
            role = infer_mdat_measurement_role(measurement.title)
            if measurement.start_freq is not None and measurement.end_freq is not None:
                range_text = f"{measurement.start_freq:g}–{measurement.end_freq:g} Hz"
            else:
                range_text = "—"
            item_id = self.tree.insert(
                "",
                "end",
                values=(
                    index,
                    role,
                    measurement.title,
                    measurement.group_name or "—",
                    range_text,
                    measurement.date or "—",
                    measurement.rew_version or "—",
                ),
            )
            self.item_to_measurement[item_id] = measurement
        self._set_busy(False)
        duplicate_count = sum(1 for m in measurements if m.duplicate_uuid)
        duplicate_note = (
            f" {duplicate_count} pozycji ma powtórzony UUID; REWDO użyje ich bieżących indeksów listy."
            if duplicate_count else ""
        )
        if measurements:
            self.status_var.set(
                f"Połączono z {normalised_url}. Odczytano {len(measurements)} pomiarów."
                + duplicate_note
                + " Zaznacz pomiary, które mają trafić do migawki."
            )
        else:
            self.status_var.set(
                f"Połączono z {normalised_url}, ale REW zwrócił 0 pomiarów. "
                "Otwórz lub wykonaj pomiary w tej samej instancji REW, a następnie naciśnij odśwież."
            )
        self._update_count()

    def _show_error(self, title: str, exc: Exception) -> None:
        self._set_busy(False)
        self.status_var.set(str(exc))
        messagebox.showerror(title, str(exc), parent=self)

    def _selected_measurements(self) -> List[REWApiMeasurement]:
        return [self.item_to_measurement[item] for item in self.tree.selection() if item in self.item_to_measurement]

    def _select_basic_raw(self) -> None:
        wanted = {"L main", "L sub", "R main", "R sub"}
        selected: List[str] = []
        seen = set()
        for item, measurement in self.item_to_measurement.items():
            role = infer_mdat_measurement_role(measurement.title)
            if role in wanted and role not in seen:
                selected.append(item)
                seen.add(role)
        self.tree.selection_set(selected)
        self._update_count()
        missing = sorted(wanted - seen)
        if missing:
            self.status_var.set("Nie znaleziono jednoznacznie: " + ", ".join(missing) + ". Sprawdź nazwy pomiarów w REW.")
        else:
            self.status_var.set("Zaznaczono komplet podstawowego RAW: L main, L sub, R main, R sub.")

    def _select_time_sums(self) -> None:
        """Zaznacz rzeczywiste sumy L, R i L+R do porównania czasowego."""
        self.tree.selection_remove(self.tree.selection())
        selected = []
        for item_id, measurement in self.item_to_measurement.items():
            role = infer_mdat_measurement_role(measurement.title)
            title = measurement.title.lower()
            is_sum_role = role in {"L sum", "R sum", "L+R", "L+R sum"}
            is_sum_name = any(token in title for token in ("sum", "l+r", "lr_sum", "baseline"))
            if is_sum_role or is_sum_name:
                selected.append(item_id)
        if selected:
            self.tree.selection_set(selected)
            self.tree.see(selected[0])
        self.include_group_delay_var.set(True)
        self.include_impulse_var.set(True)
        self._update_count()
        if not selected:
            self.status_var.set("Nie znaleziono nazw SUM. Zaznacz ręcznie pomiary L main+sub, R main+sub i L+R.")

    def _select_all(self) -> None:
        self.tree.selection_set(self.tree.get_children())
        self._update_count()

    def _clear_selection(self) -> None:
        self.tree.selection_remove(self.tree.selection())
        self._update_count()

    def _update_count(self) -> None:
        count = len(self.tree.selection())
        self.count_var.set(f"Zaznaczono: {count}")

    def _fill_typical_raw(self) -> None:
        for variable in (
            self.dirac_off_var,
            self.crossover_off_var,
            self.peq_off_var,
            self.delays_zero_var,
            self.polarity_normal_var,
            self.rel120_var,
            self.timing_consistent_var,
            self.microphone_stationary_var,
        ):
            variable.set(True)
        self.main_gain_var.set("0.0")
        self.sub_gain_var.set("-6.0")
        self.raw_confirm_var.set(True)
        self.status_var.set("Wypełniono typowy profil RAW. Sprawdź wartości przed utworzeniem migawki.")

    def _choose_parity_mdat(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Wybierz MDAT z tym samym zestawem pomiarów",
            initialdir=str(self.initial_dir), filetypes=[("REW MDAT", "*.mdat"), ("Wszystkie pliki", "*.*")],
        )
        if path:
            self.parity_mdat_var.set(path)

    def _measurement_state(self) -> Dict[str, object]:
        def parse_gain(text: str, label: str) -> float:
            try:
                return float(str(text).strip().replace(",", "."))
            except Exception as exc:
                raise ValueError(f"{label} musi być liczbą w dB.") from exc

        return {
            "schemaVersion": 1,
            "userConfirmed": bool(self.raw_confirm_var.get()),
            "raw": True,
            "diracOff": bool(self.dirac_off_var.get()),
            "crossoverOff": bool(self.crossover_off_var.get()),
            "peqOff": bool(self.peq_off_var.get()),
            "delaysZero": bool(self.delays_zero_var.get()),
            "polarityNormal": bool(self.polarity_normal_var.get()),
            "relFilter120": bool(self.rel120_var.get()),
            "timingReferenceConsistent": bool(self.timing_consistent_var.get()),
            "microphoneStationary": bool(self.microphone_stationary_var.get()),
            "mainGainDb": parse_gain(self.main_gain_var.get(), "Gain main"),
            "subGainDb": parse_gain(self.sub_gain_var.get(), "Gain sub"),
            "confirmationSource": "REWDO API dialog user confirmation",
        }

    def _create_snapshot(self) -> None:
        if self.busy:
            return
        selected = self._selected_measurements()
        if not selected:
            messagebox.showwarning("Brak pomiarów", "Zaznacz co najmniej jeden pomiar z listy REW.", parent=self)
            return
        if self.time_analysis_mode:
            if not self.timing_consistent_var.get() or not self.microphone_stationary_var.get():
                messagebox.showerror(
                    "Brak potwierdzenia porównywalności",
                    "Analiza impulsu wymaga tej samej metody timing reference oraz nieruchomego mikrofonu między wszystkimi zaznaczonymi pomiarami.",
                    parent=self,
                )
                return
            measurement_state = {
                "schemaVersion": 2,
                "analysisPurpose": "time_domain_90bw48_fast",
                "userConfirmed": True,
                "raw": False,
                "timingReferenceConsistent": True,
                "microphoneStationary": True,
                "confirmationSource": "REWDO API dialog time-domain comparison confirmation",
            }
            if not self.include_impulse_var.get():
                messagebox.showerror(
                    "Brak impulsu",
                    "Tryb czasowy wymaga zaznaczenia opcji Pobierz impuls.",
                    parent=self,
                )
                return
            self.include_group_delay_var.set(True)
        else:
            try:
                measurement_state = self._measurement_state()
            except ValueError as exc:
                messagebox.showerror("Nieprawidłowy stan RAW", str(exc), parent=self)
                return
            verified, missing = verify_measurement_state(measurement_state)
            if not verified:
                labels = ", ".join(missing)
                proceed = messagebox.askyesno(
                    "Migawka tylko diagnostyczna",
                    "Nie potwierdzono wszystkich warunków RAW wymaganych do eksportu biquadów.\n\n"
                    f"Braki: {labels}\n\n"
                    "Utworzyć migawkę diagnostyczną z zablokowanym eksportem?",
                    parent=self,
                )
                if not proceed:
                    return
        default_name = time.strftime("REW_API_SNAPSHOT_%Y%m%d_%H%M%S.rewapi.json")
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Zapisz migawkę REW API dla REWDO",
            initialdir=str(self.initial_dir),
            initialfile=default_name,
            defaultextension=".rewapi.json",
            filetypes=[("Migawka REW API", "*.rewapi.json"), ("JSON", "*.json"), ("Wszystkie pliki", "*.*")],
        )
        if not path:
            return
        # Skopiuj ustawienia GUI przed uruchomieniem wątku roboczego.
        requested_url = self.url_var.get()
        parity_mdat = self.parity_mdat_var.get().strip()
        include_group_delay = bool(self.include_group_delay_var.get())
        include_impulse = bool(self.include_impulse_var.get())
        self._set_busy(True, f"Pobieranie {len(selected)} pomiarów z REW…")

        def worker(
            url: str,
            parity_mdat_path: str,
            with_group_delay: bool,
            with_impulse: bool,
            raw_state_values: Dict[str, object],
        ) -> None:
            try:
                client = REWApiClient(url, timeout=15.0)
                output = client.create_snapshot(
                    selected,
                    path,
                    ppo=None,
                    force_no_smoothing=True,
                    include_group_delay=with_group_delay,
                    include_impulse=with_impulse,
                    measurement_state=raw_state_values,
                )
                parity_result = None
                if parity_mdat_path:
                    parity_result = compare_api_snapshot_to_mdat(output, parity_mdat_path, update_snapshot=True)
                    report_path = output.with_name("API_MDAT_PARITY.md")
                    report_path.write_text(parity_report_markdown(parity_result), encoding="utf-8")
                self.after(0, lambda result=output, parity=parity_result: self._snapshot_done(result, parity))
            except Exception as exc:
                self.after(0, lambda error=exc: self._show_error("Nie udało się utworzyć migawki", error))

        threading.Thread(
            target=worker,
            args=(requested_url, parity_mdat, include_group_delay, include_impulse, measurement_state),
            daemon=True,
        ).start()

    def _snapshot_done(self, path: Path, parity_result: Optional[Dict[str, object]] = None) -> None:
        self._set_busy(False)
        payload = read_snapshot(path)
        verified, missing = verify_measurement_state(payload.get("measurementState"))
        response_ok, response_problems = verify_snapshot_response_integrity(payload)
        if self.time_analysis_mode:
            state = payload.get("measurementState", {})
            comparable = (
                isinstance(state, dict)
                and state.get("timingReferenceConsistent") is True
                and state.get("microphoneStationary") is True
                and response_ok
            )
            state_text = (
                "DANE CZASOWE GOTOWE DO PORÓWNANIA"
                if comparable else
                "DANE CZASOWE NIEPEŁNE — sprawdź ostrzeżenia"
            )
        else:
            export_ok = verified and response_ok
            state_text = "RAW + DANE API ZWERYFIKOWANE — eksport biquadów dozwolony" if export_ok else "TYLKO DIAGNOSTYKA — eksport zablokowany"
        self.status_var.set(f"Migawka gotowa: {path.name}. {state_text}")
        if self.on_snapshot is not None:
            self.on_snapshot(path)
        detail_parts = []
        if not self.time_analysis_mode and not verified:
            detail_parts.append("Braki potwierdzenia RAW: " + ", ".join(missing))
        if not response_ok:
            detail_parts.append("Problemy danych API: " + "; ".join(response_problems))
        if parity_result is not None:
            detail_parts.append("Kontrola API–MDAT: " + str(parity_result.get("status", "—")))
        detail = ("\n" + "\n".join(detail_parts)) if detail_parts else ""
        messagebox.showinfo(
            "Migawka REW API gotowa",
            f"Zapisano:\n{path}\n\n{state_text}{detail}\n\nPlik został ustawiony jako wejście REWDO. Oryginalne pomiary w REW nie zostały zmienione.",
            parent=self,
        )

    def _close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("Operacja trwa", "Pobieranie danych nadal trwa. Zamknąć okno?", parent=self):
                return
        self.destroy()
