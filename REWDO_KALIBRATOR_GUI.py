#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from rew_prediction_measurement_calibrator import update_calibration

APP_DIR = Path(__file__).resolve().parent


class CalibratorGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("REWDO v20.31 — Kalibrator predykcja–pomiar")
        self.geometry("900x470")
        self.resizable(True, False)
        self.vars = {
            "prediction": tk.StringVar(),
            "candidate": tk.StringVar(value="ABD_01_REFERENCE_SAFE_92HZ"),
            "mdat": tk.StringVar(),
            "xo": tk.StringVar(value="92.0"),
            "topology": tk.StringVar(value="ABD92_REFERENCE_SAFE_V1"),
            "state": tk.StringVar(value="ABD92_RSUB17.52"),
            "calibration": tk.StringVar(value=str(APP_DIR / "USER_EMPIRICAL_CALIBRATION.json")),
            "out": tk.StringVar(value=str(APP_DIR / "USER_EMPIRICAL_CALIBRATION_UPDATED.json")),
        }
        self._build()

    def _row(self, parent, row, label, key, kind="file"):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        if kind:
            ttk.Button(parent, text="Wybierz…", command=lambda: self._browse(key, kind)).grid(row=row, column=2, padx=8, pady=6)

    def _build(self):
        f=ttk.Frame(self,padding=10); f.pack(fill="both",expand=True); f.columnconfigure(1,weight=1)
        self._row(f,0,"CSV predykcji (04_POROWNANIE…):","prediction","csv")
        self._row(f,1,"Kod kandydata:","candidate",None)
        self._row(f,2,"Pełny pomiar PRE-DIRAC .mdat:","mdat","mdat")
        self._row(f,3,"Częstotliwość XO [Hz]:","xo",None)
        self._row(f,4,"Identyfikator topologii:","topology",None)
        self._row(f,5,"Identyfikator stanu:","state",None)
        self._row(f,6,"Bieżący plik kalibracji:","calibration","json")
        self._row(f,7,"Nowy plik kalibracji:","out","savejson")
        ttk.Label(f,text="Ten sam kształt filtru musi zawsze mieć ten sam identyfikator topologii. Zmiana delayu tworzy nowy stan, ale nie nową topologię.",wraplength=820).grid(row=8,column=0,columnspan=3,sticky="w",padx=8,pady=8)
        ttk.Button(f,text="Przelicz i zapisz nową kalibrację",command=self._run).grid(row=9,column=0,columnspan=3,pady=14)

    def _browse(self,key,kind):
        if kind=="savejson":
            p=filedialog.asksaveasfilename(defaultextension=".json",filetypes=[("JSON","*.json")])
        else:
            patterns={"csv":"*.csv","mdat":"*.mdat","json":"*.json"}
            p=filedialog.askopenfilename(filetypes=[("Plik",patterns[kind]),("Wszystkie","*.*")])
        if p:self.vars[key].set(p)

    def _run(self):
        try:
            out=update_calibration(
                prediction_csv=Path(self.vars["prediction"].get()), candidate_code=self.vars["candidate"].get().strip(),
                measured_mdat=Path(self.vars["mdat"].get()), xo_hz=float(self.vars["xo"].get()),
                topology_id=self.vars["topology"].get().strip(), state_id=self.vars["state"].get().strip(),
                calibration_json=Path(self.vars["calibration"].get()), out_json=Path(self.vars["out"].get()),
            )
        except Exception as exc:
            messagebox.showerror("Błąd kalibratora",str(exc)); return
        messagebox.showinfo("Gotowe",f"Zapisano:\n{out}\n\nRaport:\n{out.with_suffix('.md')}")

if __name__ == "__main__":
    CalibratorGUI().mainloop()
