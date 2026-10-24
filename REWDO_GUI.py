"""REWDO 20.32. One application, three task-oriented workspaces."""
from __future__ import annotations
import copy
import json
import os
import queue
import subprocess
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from rew_project import (STAGES, STEPS, ROLES, active_config, changed, fingerprint, load_project,
    new_project, number, put_config, put_measurement_preset, relocate, result_is_current, save_project, uid, user_dir, utc_now)
from rew_process import ProcessJob

APP_VERSION = "20.32"


def __getattr__(name):
    # Legacy developer API remains testable, but is not the normal application.
    if name in {"MODES", "build_command", "_repair_log_text", "_decode_process_line"}:
        import rew_legacy_launcher
        return getattr(rew_legacy_launcher, name)
    raise AttributeError(name)


def folder(path):
    if os.name == "nt":
        os.startfile(str(Path(path).resolve()))
    else:
        subprocess.Popen(["xdg-open", str(Path(path).resolve())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ScrolledTable(ttk.Treeview):
    def __init__(self, parent, **kwargs):
        self.container = ttk.Frame(parent)
        super().__init__(self.container, **kwargs)
        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(0, weight=1)
        super().grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(self.container, orient="vertical", command=self.yview)
        horizontal = ttk.Scrollbar(self.container, orient="horizontal", command=self.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

    def grid(self, *args, **kwargs):
        return self.container.grid(*args, **kwargs)

    def pack(self, *args, **kwargs):
        return self.container.pack(*args, **kwargs)


def table(parent, columns, height=5):
    t = ScrolledTable(parent, columns=[c[0] for c in columns], show="headings", height=height, selectmode="browse")
    for key, label, width in columns:
        t.heading(key, text=label)
        t.column(key, width=width, minwidth=60)
    return t


def field(parent, row, label, value="", choices=None, width=28):
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=4)
    var = tk.StringVar(value="" if value is None else value)
    widget = ttk.Entry(parent, textvariable=var, width=width) if choices is None else ttk.Combobox(parent, textvariable=var, values=list(choices), state="readonly", width=width)
    widget.grid(row=row, column=1, sticky="ew", padx=5, pady=4)
    return var


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("REWDO 20.32")
        self.geometry(f"{min(1120,self.winfo_screenwidth()-60)}x{min(730,self.winfo_screenheight()-90)}")
        self.minsize(840, 560)
        self.project, self.path = new_project(), None
        self.job, self.latest, self.design_result, self.compare_result = None, None, None, None
        self.log, self.stage, self.api = [], "Wczytaj pomiary, aby rozpocząć.", None
        try:
            prefs = user_dir()/"preferences.json"
            if prefs.is_file():
                self.path = Path(json.loads(prefs.read_text(encoding="utf-8"))["last_project"])
                self.project = load_project(self.path)
        except Exception as exc:
            self.path = None
            self.stage = f"Nie przywrócono projektu: {exc}. Wskaż go przez Projekt → Otwórz."
        style=ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TButton",padding=(8,4))
        self.columnconfigure(0,weight=1)
        self.rowconfigure(1,weight=1)
        self.busy_widgets=[]
        top=ttk.Frame(self,padding=10)
        top.grid(row=0,column=0,sticky="ew")
        top.columnconfigure(0,weight=1)
        self.header=ttk.Label(top,font=("Segoe UI",12,"bold"))
        self.header.grid(row=0,column=0,sticky="w")
        self.ref_label=ttk.Label(top)
        self.ref_label.grid(row=1,column=0,sticky="w")
        menu=ttk.Menubutton(top,text="Projekt")
        items=tk.Menu(menu,tearoff=False)
        for title,fn in (("Nowy",self.new),("Otwórz",self.open),("Zapisz jako…",lambda:self.save(True))):
            items.add_command(label=title,command=fn)
        menu["menu"]=items
        menu.grid(row=0,column=1,rowspan=2,padx=4)
        self.busy_widgets.append(menu)
        ttk.Button(top,text="Zapisz projekt",command=self.save).grid(row=0,column=2,rowspan=2,padx=4)
        self.button(top,"Mój system",self.system).grid(row=0,column=3,rowspan=2,padx=4)
        self.tabs=ttk.Notebook(self)
        self.tabs.grid(row=1,column=0,sticky="nsew",padx=10)
        self.pages=[]
        for name in STEPS:
            page=ttk.Frame(self.tabs,padding=10)
            page.columnconfigure(0,weight=1)
            self.tabs.add(page,text=name)
            self.pages.append(page)
        self.measurements_page()
        self.design_page()
        self.compare_page()
        bottom=ttk.Frame(self,padding=10)
        bottom.grid(row=2,column=0,sticky="ew")
        bottom.columnconfigure(0,weight=1)
        self.status=ttk.Label(bottom,text=self.stage,wraplength=700)
        self.status.grid(row=0,column=0,sticky="w")
        self.stop=ttk.Button(bottom,text="Przerwij",command=self.cancel,state="disabled")
        self.stop.grid(row=0,column=1,padx=4)
        ttk.Button(bottom,text="Szczegóły techniczne",command=self.details).grid(row=0,column=2,padx=4)
        self.refresh()
        self.tabs.select(min(2,max(0,self.project["ui"]["step"])))
        self.tabs.bind("<<NotebookTabChanged>>",self.step_changed)
        self.protocol("WM_DELETE_WINDOW",self.close)
        self.after(100,self.poll)

    def button(self,parent,title,fn):
        b=ttk.Button(parent,text=title,command=fn)
        self.busy_widgets.append(b)
        return b

    def description(self,page,text):
        ttk.Label(page,text=text,wraplength=950).grid(row=0,column=0,sticky="w",pady=(0,8))

    def measurements_page(self):
        p=self.pages[0]
        self.description(p,"Wczytaj pomiary. Sprawdzę, do czego można ich użyć i czego ewentualnie brakuje.")
        row=ttk.Frame(p)
        row.grid(row=1,column=0,sticky="ew",pady=(0,8))
        load=ttk.Menubutton(row,text="Wczytaj pomiary")
        menu=tk.Menu(load,tearoff=False)
        menu.add_command(label="Z pliku REW",command=self.import_file)
        menu.add_command(label="Z otwartego REW",command=self.api_dialog)
        load["menu"]=menu
        load.pack(side="left")
        self.busy_widgets.append(load)
        for name,fn in (("Przypisz / popraw ślady",self.mapping),("Wskaż przeniesiony plik",self.locate),("Sprawdź ponownie",lambda:self.start("check"))):
            self.button(row,name,fn).pack(side="left",padx=5)
        self.traces=table(p,[("source","Źródło",135),("name","Nazwa w REW",175),("config","Ustawienie",110),("element","Mierzony element",115),("channel","Kanał",70),("mic","Pozycja mikrofonu",125),("stage","Etap",105)],8)
        self.traces.grid(row=2,column=0,sticky="nsew")
        p.rowconfigure(2,weight=1)
        self.traces.bind("<Double-1>",lambda e:self.mapping())
        self.measure_summary=ttk.Label(p,text="",wraplength=950,justify="left")
        self.measure_summary.grid(row=3,column=0,sticky="ew",pady=8)
        self.gain_label=ttk.Label(p,wraplength=950)
        self.gain_label.grid(row=4,column=0,sticky="w")
        self.button(p,"Sprawdź powiązanie odniesienia",lambda:self.start("verify")).grid(row=5,column=0,sticky="w",pady=(8,0))

    def design_page(self):
        p=self.pages[1]
        self.description(p,"Poszukam lepszego połączenia kolumn z subwooferami. Wynik sprawdzisz pomiarem i odsłuchem.")
        row=ttk.Frame(p)
        row.grid(row=1,column=0,sticky="ew")
        self.button(row,"Znajdź ustawienia do sprawdzenia",lambda:self.start("standard")).pack(side="left")
        self.button(row,"Szukaj dokładniej — własna zwrotnica",lambda:self.start("custom")).pack(side="left",padx=8)
        self.design_summary=ttk.Label(p,text="Najpierw potwierdź pomiary, poziomy i odniesienie. OFF/OFF, all-pass wyłączony.",wraplength=950)
        self.design_summary.grid(row=2,column=0,sticky="ew",pady=10)
        self.candidates=table(p,[("setting","Ustawienie",245),("sum","Zmiana sumowania L/R [dB]",230),("body","100–220 Hz [dB]",145),("evidence","Dowód",240)],3)
        self.candidates.grid(row=3,column=0,sticky="ew")
        self.candidates.bind("<<TreeviewSelect>>",self.select_candidate)
        self.candidate_note=ttk.Label(p,wraplength=950,justify="left")
        self.candidate_note.grid(row=4,column=0,sticky="ew",pady=8)
        self.design_plot=ttk.Frame(p)
        self.design_plot.grid(row=5,column=0,sticky="nsew")
        p.rowconfigure(5,weight=1)
        row=ttk.Frame(p)
        row.grid(row=6,column=0,sticky="ew",pady=(8,0))
        ttk.Button(row,text="Zapisz podsumowanie",command=self.summary).pack(side="left")
        self.export_button=self.button(row,"Przygotuj ustawienia do testu",self.export)

    def compare_page(self):
        p=self.pages[2]
        self.description(p,"Porównaj rzeczywiste pomiary i zapisz, które ustawienie brzmi lepiej.")
        row=ttk.Frame(p)
        row.grid(row=1,column=0,sticky="ew")
        self.stage_var=tk.StringVar(value=self.project["ui"].get("compare_stage","Po Dirac"))
        stage_combo=ttk.Combobox(row,textvariable=self.stage_var,values=("Przed Dirac","Po Dirac"),state="readonly",width=14)
        stage_combo.pack(side="left")
        stage_combo.bind("<<ComboboxSelected>>",self.invalidate_comparison)
        self.busy_widgets.append(stage_combo)
        self.a_var,self.b_var=tk.StringVar(),tk.StringVar()
        self.compare_combos=[]
        for title,var in ((" A:",self.a_var),(" B:",self.b_var)):
            ttk.Label(row,text=title).pack(side="left")
            combo=ttk.Combobox(row,textvariable=var,state="readonly",width=28)
            combo.pack(side="left",padx=4)
            combo.bind("<<ComboboxSelected>>",self.invalidate_comparison)
            self.busy_widgets.append(combo)
            self.compare_combos.append(combo)
        self.button(p,"Porównaj z moim ustawieniem",self.compare).grid(row=2,column=0,sticky="w",pady=8)
        self.compare_summary=ttk.Label(p,text="Możesz zacząć od gotowych pomiarów; RAW i referencja do projektowania nie są wymagane.",wraplength=950)
        self.compare_summary.grid(row=3,column=0,sticky="ew",pady=(0,6))
        self.comparison=table(p,[("mic","Pozycja mikrofonu",180),("role","Suma",100),("bass","30–60 Hz [dB]",170),("xo","80–110 Hz [dB]",170),("body","100–220 Hz [dB]",170)],3)
        self.comparison.grid(row=4,column=0,sticky="ew")
        self.comparison.bind("<<TreeviewSelect>>",self.compare_curve)
        self.compare_plot=ttk.Frame(p)
        self.compare_plot.grid(row=5,column=0,sticky="nsew")
        p.rowconfigure(5,weight=1)
        row=ttk.LabelFrame(p,text="Odsłuch — wybór użytkownika",padding=6)
        row.grid(row=6,column=0,sticky="ew",pady=(6,0))
        row.columnconfigure(3,weight=1)
        self.choice=tk.StringVar(value="brak wyraźnej różnicy")
        ttk.Combobox(row,textvariable=self.choice,values=("A","B","brak wyraźnej różnicy"),state="readonly",width=23).grid(row=0,column=0,padx=4)
        self.matched=tk.BooleanVar(value=False)
        ttk.Checkbutton(row,text="Wyrównano głośność",variable=self.matched).grid(row=0,column=1,padx=4)
        self.button(row,"Zapisz odsłuch",self.listening).grid(row=0,column=2,padx=4)
        ttk.Button(row,text="Zapisz podsumowanie",command=self.summary).grid(row=0,column=3,padx=4)
        self.note=tk.StringVar()
        ttk.Entry(row,textvariable=self.note).grid(row=1,column=0,columnspan=4,sticky="ew",pady=4)
        ttk.Label(row,text="Notatka: wokal, fundament i kontrola basu, scena, zmęczenie.").grid(row=2,column=0,columnspan=4,sticky="w")

    def autosave(self):
        if self.path is None:
            self.path=user_dir()/"projekty"/self.project["id"]/"projekt.rewdo.json"
        save_project(self.path,self.project)

    def step_changed(self,event=None):
        self.project["ui"]["step"]=self.tabs.index(self.tabs.select())
        if self.path:
            self.autosave()

    def save(self,as_new=False):
        if as_new or self.path is None:
            path=filedialog.asksaveasfilename(parent=self,initialfile="projekt.rewdo.json",defaultextension=".json",filetypes=[("Projekt REWDO","*.json")])
            if not path:
                return
            self.path=Path(path)
        try:
            self.autosave()
            self.status.configure(text="Zapisano projekt. Pliki źródłowe pozostają w swoich lokalizacjach.")
        except Exception as exc:
            messagebox.showerror("Zapis projektu",str(exc),parent=self)

    def new(self):
        if self.job:
            return
        self.autosave()
        self.project,self.path,self.latest=new_project(),None,None
        self.refresh()
        self.tabs.select(0)

    def open(self):
        if self.job:
            return
        path=filedialog.askopenfilename(parent=self,filetypes=[("Projekt REWDO","*.json")])
        if path:
            try:
                loaded=load_project(path)
                self.autosave()
                self.project,self.path,self.latest=loaded,Path(path),None
                self.refresh()
                self.tabs.select(loaded["ui"]["step"])
            except Exception as exc:
                messagebox.showerror("Projekt",str(exc),parent=self)

    def choices(self):
        return {c["label"]+f" · v{c['version']} [{c['id'][:6]}]":c["key"] for c in self.project["configurations"]}

    def config_label(self,key):
        return next((c["label"]+f" · v{c['version']}" for c in self.project["configurations"] if c["key"]==key),"nieprzypisane")

    @staticmethod
    def fmt(value):
        return "—" if value is None else f"{value:+.2f}"

    def refresh(self):
        c=active_config(self.project)
        self.header.configure(text=self.project["name"])
        self.ref_label.configure(text="Odniesienie: "+(self.config_label(c["key"]) if c else "wybierz w Mój system"))
        self.traces.delete(*self.traces.get_children())
        for s in self.project["sources"]:
            for i,t in enumerate(s["traces"]):
                parts=t["role"].split()
                channel=parts[0] if parts[0] in ("L","R","L+R") else "nieustalony"
                element={"main":"Kolumna","sub":"Subwoofer","sum":"Suma"}.get(parts[-1],"Suma stereo" if channel=="L+R" else "nieustalony")
                self.traces.insert("","end",iid=f"{s['id']}:{i}",values=(s["name"][:30],t["name"],self.config_label(t["preset"] or s["configuration"]),element,channel,t["microphone"] or "nieustalona",t["stage"]))
        choices=self.choices()
        for combo in self.compare_combos:
            combo["values"]=list(choices)
        for field_name,var in (("compare_a",self.a_var),("compare_b",self.b_var)):
            saved=self.project["ui"].get(field_name)
            if saved:
                var.set(next((label for label,key in choices.items() if key==saved),""))
        self.stage_var.set(self.project["ui"].get("compare_stage","Po Dirac"))
        if self.a_var.get() not in choices:
            self.a_var.set(next((n for n,k in choices.items() if k==self.project["active_configuration"]),next(iter(choices),"")))
        if self.b_var.get() not in choices:
            self.b_var.set(next((n for n in choices if n!=self.a_var.get()),""))
        self.gain_label.configure(text="Poziom subów [dB L/R]: "+" · ".join(k+" "+"/".join(self.fmt(v[s]) for s in ("L","R")) for k,v in self.project["gains"].items())+" — edycja: Mój system")
        self.candidates.delete(*self.candidates.get_children())
        self.candidates.insert("","end",iid="reference",values=(c["label"] if c else "Odniesienie","0 / 0","0","Wpis z konsoli"))
        self.export_button.pack_forget()
        self.design_result,self.compare_result=None,None
        self.comparison.delete(*self.comparison.get_children())
        self.design_summary.configure(text="Wyniki zależą od aktualnej wersji projektu. Potwierdź dane i uruchom dobór.")
        self.candidate_note.configure(text="")
        for frame in (self.design_plot,self.compare_plot):
            self.plot(frame,[],"")
        for tasks in (("standard","custom"),("compare",),("check",)):
            result=next((r for r in reversed(self.project["runs"]) if r.get("task") in tasks),None)
            if result and result_is_current(self.project,result):
                self.present(result)

    def selected_source(self):
        ids=self.traces.selection()
        if not ids:
            messagebox.showinfo("Pomiary","Zaznacz ślad źródła.",parent=self)
            return None
        return next(s for s in self.project["sources"] if s["id"]==ids[0].split(":")[0])

    def import_file(self):
        path=filedialog.askopenfilename(parent=self,title="Wczytaj pomiary",filetypes=[("MDAT / REW API JSON","*.mdat *.json")])
        if path:
            self.start("import",{"path":path})

    def locate(self):
        s=self.selected_source()
        if s and not self.job:
            path=filedialog.askopenfilename(parent=self,title="Wskaż ten sam plik w nowej lokalizacji")
            if path:
                try:
                    relocate(s,path)
                    self.autosave()
                    self.refresh()
                except ValueError as exc:
                    messagebox.showerror("Tożsamość pliku",str(exc),parent=self)

    def dialog(self,title,size):
        win=tk.Toplevel(self)
        win.title(title)
        win.geometry(size)
        win.transient(self)
        win.grab_set()
        return win

    def system(self):
        if self.job:
            return
        win=self.dialog("Mój system","800x620")
        win.columnconfigure(0,weight=1)
        win.rowconfigure(0,weight=1)
        book=ttk.Notebook(win)
        book.grid(row=0,column=0,sticky="nsew",padx=12,pady=12)
        panels=[]
        for name in ("Odniesienie","Poziomy","Zakres szukania","System"):
            frame=ttk.Frame(book,padding=12)
            frame.columnconfigure(1,weight=1)
            book.add(frame,text=name)
            panels.append(frame)
        c=active_config(self.project)
        ref=panels[0]
        choices={"Nie wybrano":"",**self.choices()}
        history=field(ref,0,"Historia",next((n for n,k in choices.items() if c and k==c["key"]),"Nie wybrano"),choices)
        def activate():
            key=choices.get(history.get())
            if key:
                self.project["active_configuration"]=key
                changed(self.project)
                self.autosave()
                self.refresh()
                win.destroy()
        ttk.Button(ref,text="Użyj wybranej konfiguracji",command=activate).grid(row=1,column=1,sticky="w",pady=5)
        label=field(ref,2,"Nazwa",c["label"] if c else "Moje ustawienie")
        variables={}
        for row,(key,title) in enumerate((("fc","Podział [Hz]"),("filter","Rodzina"),("l_main_ms","L kolumna [ms]"),("r_main_ms","R kolumna [ms]"),("l_sub_ms","L subwoofer [ms]"),("r_sub_ms","R subwoofer [ms]")),3):
            variables[key]=field(ref,row,title,c["settings"][key] if c else "BW48" if key=="filter" else "",("BW48","LR48","BW24","LR24") if key=="filter" else None)
        confirmed=tk.BooleanVar(value=c["console_confirmed"] if c else False)
        ttk.Checkbutton(ref,text="Sprawdziłem wartości w konsoli; polaryzacja OFF/OFF",variable=confirmed).grid(row=9,column=0,columnspan=2,sticky="w",pady=8)
        ttk.Label(ref,text="Zmiana parametrów tworzy nową wersję. Wpis nie jest dowodem pomiarowym.\nDo samego porównania możesz dodać tylko nazwę presetu poniżej.\nW Historii wybierzesz konfigurację odniesienia.").grid(row=10,column=0,columnspan=2,sticky="w")
        def measurement_preset():
            put_measurement_preset(self.project,label.get())
            self.autosave()
            self.refresh()
            win.destroy()
        ttk.Button(ref,text="Dodaj tylko nazwę presetu do porównania (bez delayów)",command=measurement_preset).grid(row=11,column=0,columnspan=2,sticky="w",pady=8)
        gains={}
        panel=panels[1]
        ttk.Label(panel,text="Cyfrowy poziom subów w miniDSP — nie pokrętło REL ani głośność główna.",wraplength=690).grid(row=0,column=0,columnspan=4,pady=(0,10),sticky="w")
        for col,title in enumerate(("Kiedy","L [dB]","R [dB]","Źródło: konsola, data, sesja")):
            ttk.Label(panel,text=title).grid(row=1,column=col,padx=5,sticky="w")
        for row,(key,title) in enumerate((("raw","Pomiar bazowy"),("dirac","Pomiar Dirac"),("listening","Odsłuch")),2):
            ttk.Label(panel,text=title).grid(row=row,column=0,padx=5,pady=8,sticky="w")
            gains[key]={}
            for col,f in enumerate(("L","R","origin"),1):
                value=self.project["gains"][key][f]
                var=tk.StringVar(value="" if value is None else value)
                gains[key][f]=var
                ttk.Entry(panel,textvariable=var,width=10 if f!="origin" else 30).grid(row=row,column=col,padx=5,sticky="ew")
        ttk.Label(panel,text="Brak wartości nie uruchamia domyślnych −6/−4/0. Różne L/R są zachowane, lecz obecne projektanty wymagają wspólnego poziomu. Zmiana poziomów wymaga sprawdzenia pomiarem.",wraplength=680).grid(row=6,column=0,columnspan=4,pady=20,sticky="w")
        search={key:field(panels[2],row,title,self.project["analysis"][key]) for row,(key,title) in enumerate((("fc_low","Od [Hz]"),("fc_high","Do [Hz]"),("fc_step","Krok [Hz]")))}
        search_ok=tk.BooleanVar(value=self.project["analysis"]["confirmed"])
        ttk.Checkbutton(panels[2],text="Akceptuję zakres poszukiwania BW48/LR48",variable=search_ok).grid(row=3,column=0,columnspan=2,pady=10)
        ttk.Label(panels[2],text="Własna zwrotnica rozwija sekcje fc/Q wokół baz z tego zakresu.\nAll-pass i korekcja empiryczna pozostają wyłączone.").grid(row=4,column=0,columnspan=2,sticky="w",pady=8)
        system={key:field(panels[3],row,title,self.project["system"].get(key)) for row,(key,title) in enumerate((("speakers","Kolumny"),("subs","Subwoofery"),("dsp","DSP"),("dirac_low","Dolna kurtyna [Hz]"),("dirac_high","Górna kurtyna [Hz]")))}
        project_name=field(panels[3],5,"Nazwa projektu",self.project["name"])
        def commit():
            try:
                p=copy.deepcopy(self.project)
                for state,fields in gains.items():
                    p["gains"][state]={f:(v.get().strip() if f=="origin" else number(v.get(),state+"/"+f,low=-60,high=12) if v.get().strip() else None) for f,v in fields.items()}
                for k,v in search.items():
                    p["analysis"][k]=number(v.get(),k,low=.5 if k=="fc_step" else 20,high=20 if k=="fc_step" else 200)
                if p["analysis"]["fc_low"]>p["analysis"]["fc_high"]:
                    raise ValueError("Koniec zakresu jest niższy niż początek.")
                p["analysis"]["confirmed"]=search_ok.get()
                for k,v in system.items():
                    p["system"][k]=(number(v.get(),k,low=0,high=24000) if v.get().strip() else None) if k.startswith("dirac_") else v.get().strip()
                p["name"]=project_name.get().strip() or "Mój system"
                values={k:v.get() for k,v in variables.items()}
                if any(v.strip() for k,v in values.items() if k!="filter"):
                    parsed={k:v if k=="filter" else number(v,k) for k,v in values.items()}
                    if c and all(parsed[k]==c["settings"][k] for k in parsed) and c["console_confirmed"]==confirmed.get():
                        next(x for x in p["configurations"] if x["key"]==c["key"])["label"]=label.get().strip() or c["label"]
                    else:
                        put_config(p,values,label.get(),console_confirmed=confirmed.get(),parent=c["key"] if c else None)
                if fingerprint(p)!=fingerprint(self.project):
                    changed(p)
                self.project=p
                self.autosave()
                self.refresh()
                win.destroy()
            except ValueError as exc:
                messagebox.showerror("Ustawienia",str(exc),parent=win)
        ttk.Button(win,text="Zapisz ustawienia",command=commit).grid(row=1,column=0,sticky="e",padx=12,pady=(0,12))

    def mapping(self):
        if self.job:
            return
        source=self.selected_source()
        if not source:
            return
        win=self.dialog("Przypisanie — "+source["name"],"1020x620")
        win.columnconfigure(0,weight=1)
        win.rowconfigure(1,weight=1)
        top=ttk.Frame(win,padding=8)
        top.grid(row=0,column=0,sticky="ew")
        top.columnconfigure(1,weight=1)
        choices={"Nieprzypisane":"",**self.choices()}
        stage=field(top,0,"Etap sesji",source["stage"],STAGES)
        preset=field(top,1,"Konfiguracja sesji",next((n for n,k in choices.items() if k==source["configuration"]),"Nieprzypisane"),choices)
        ttk.Label(top,text="Konfiguracje dodasz w Mój system. Kanał L/R i pozycja mikrofonu to osobne informacje.").grid(row=2,column=0,columnspan=2,sticky="w",pady=4)
        t=table(win,[("name","Nazwa źródłowa",210),("role","Rola / kanał",115),("mic","Pozycja mikrofonu",150),("preset","Preset",175),("stage","Etap",120)],7)
        t.grid(row=1,column=0,sticky="nsew",padx=8)
        drafts=copy.deepcopy(source["traces"])
        def fill():
            t.delete(*t.get_children())
            for i,r in enumerate(drafts):
                t.insert("","end",iid=str(i),values=(r["name"],r["role"],r["microphone"],self.config_label(r["preset"]),r["stage"]))
        fill()
        edit=ttk.Frame(win,padding=8)
        edit.grid(row=2,column=0,sticky="ew")
        role,mic,trace_preset,trace_stage=tk.StringVar(value="Nieprzypisany"),tk.StringVar(),tk.StringVar(value="Nieprzypisane"),tk.StringVar(value=source["stage"])
        for col,var,vals,width in ((0,role,ROLES,17),(1,mic,None,18),(2,trace_preset,choices,29),(3,trace_stage,STAGES,18)):
            w=ttk.Entry(edit,textvariable=var,width=width) if vals is None else ttk.Combobox(edit,textvariable=var,values=list(vals),state="readonly",width=width)
            w.grid(row=0,column=col,padx=3)
        def selected(event=None):
            ids=t.selection()
            if ids:
                r=drafts[int(ids[0])]
                role.set(r["role"] if r["role"] in ROLES else "Nieprzypisany")
                mic.set(r["microphone"])
                trace_stage.set(r["stage"])
                trace_preset.set(next((n for n,k in choices.items() if k==r["preset"]),"Nieprzypisane"))
        t.bind("<<TreeviewSelect>>",selected)
        def apply_one():
            for i in t.selection():
                drafts[int(i)].update(role=role.get(),microphone=mic.get().strip(),preset=choices[trace_preset.get()],stage=trace_stage.get())
            fill()
        def apply_all():
            for r in drafts:
                r.update(microphone=mic.get().strip(),preset=choices[preset.get()],stage=stage.get())
            fill()
        ttk.Button(edit,text="Zastosuj",command=apply_one).grid(row=0,column=4,padx=3)
        ttk.Button(edit,text="Etap sesji, konfiguracja i wpisana pozycja dla wszystkich śladów",command=apply_all).grid(row=1,column=0,columnspan=4,sticky="w",pady=6)
        checks=ttk.Frame(win,padding=8)
        checks.grid(row=3,column=0,sticky="ew")
        raw,same=tk.BooleanVar(value=source["raw_confirmed"]),tk.BooleanVar(value=source["session_confirmed"])
        ttk.Checkbutton(checks,text="RAW: Dirac / PEQ / zwrotnica OFF, delay 0, polaryzacja OFF, REL 120 Hz, wspólna referencja czasu",variable=raw).grid(row=0,column=0,columnspan=3,sticky="w")
        ttk.Checkbutton(checks,text="Ten sam tor, głośność główna i pozycja mikrofonu co RAW; zmieniono tylko opisane ustawienia",variable=same).grid(row=1,column=0,columnspan=3,sticky="w")
        timing=tk.BooleanVar(value=source.get("timing_confirmed",False))
        ttk.Checkbutton(checks,text="Wspólna metoda timing reference, stały mikrofon w powtórzeniach (faza i czas)",variable=timing).grid(row=2,column=0,columnspan=3,sticky="w")
        levels={"Nieustalony":"","Bazowy":"raw","Pomiar Dirac":"dirac","Odsłuch":"listening"}
        level=field(checks,3,"Poziom subów w tej sesji",next(k for k,v in levels.items() if v==source["gain_state"]),levels)
        def commit():
            source.update(traces=drafts,stage=stage.get(),configuration=choices[preset.get()],raw_confirmed=raw.get(),session_confirmed=same.get(),timing_confirmed=timing.get(),gain_state=levels[level.get()],mapping_confirmed=True)
            changed(self.project)
            self.autosave()
            self.refresh()
            win.destroy()
            self.start("check")
        ttk.Button(checks,text="Potwierdź przypisanie i sprawdź",command=commit).grid(row=4,column=0,columnspan=3,sticky="e",pady=6)

    def api_dialog(self):
        if self.job:
            return
        win=self.dialog("Wczytaj z otwartego REW","850x510")
        win.columnconfigure(0,weight=1)
        win.rowconfigure(1,weight=1)
        row=ttk.Frame(win,padding=10)
        row.grid(row=0,column=0,sticky="ew")
        url=field(row,0,"REW → Preferences → API → Start", "http://127.0.0.1:4735")
        ttk.Button(row,text="Połącz",command=lambda:self.start("api_list",{"url":url.get()})).grid(row=0,column=2,padx=5)
        t=table(win,[("title","Zaznacz pomiary do pobrania",750)],12)
        t.configure(selectmode="extended")
        t.grid(row=1,column=0,sticky="nsew",padx=10)
        self.api={"window":win,"table":t,"measurements":[]}
        def fetch():
            data=self.api
            if data:
                selected=[data["measurements"][int(i)] for i in t.selection()]
                if selected:
                    self.start("api_import",{"url":url.get(),"measurements":selected})
        ttk.Button(win,text="Wczytaj zaznaczone",command=fetch).grid(row=2,column=0,sticky="e",padx=10,pady=10)
        def close():
            self.api=None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW",close)

    def set_busy(self,busy):
        for widget in self.busy_widgets:
            widget.configure(state="disabled" if busy else "readonly" if isinstance(widget,ttk.Combobox) else "normal")
        self.stop.configure(state="normal" if busy else "disabled")

    def start(self,task,args=None):
        if self.job:
            return
        try:
            self.autosave()
            directory=self.path.parent/"przebiegi"/(task+"_"+time.strftime("%Y%m%d_%H%M%S")+"_"+uid()[:6])
            request={"project":copy.deepcopy(self.project),"out":str(directory),"task":task,"args":args or {}}
            self.log,self.started,self.stage=[],time.monotonic(),"Rozpoczynanie…"
            self.set_busy(True)
            self.export_button.pack_forget()
            if task in ("standard","custom"):
                self.design_result=None
                for row in self.candidates.get_children():
                    if row!="reference":
                        self.candidates.delete(row)
                self.plot(self.design_plot,[],"")
            self.job=ProcessJob(request,directory)
            self.job.start()
        except Exception as exc:
            self.job=None
            self.set_busy(False)
            messagebox.showerror("Nie uruchomiono",str(exc),parent=self)

    def poll(self):
        if self.job:
            while True:
                try:
                    event=self.job.events.get_nowait()
                except queue.Empty:
                    break
                if event["type"]=="stage":
                    self.stage=event["message"]
                elif event["type"]=="log":
                    self.log=(self.log+[event["message"]])[-1500:]
                elif event["type"]=="done":
                    result=event["result"] or {"status":"error","message":"Proces nie zwrócił wyniku.","candidates":[]}
                    result.setdefault("task",self.job.request["task"])
                    result["directory"]=str(self.job.directory)
                    self.job=None
                    self.set_busy(False)
                    if result["status"]=="ok" and "source" in result:
                        if not any(s["sha256"]==result["source"]["sha256"] for s in self.project["sources"]):
                            self.project["sources"].append(result["source"])
                            changed(self.project)
                        self.refresh()
                        self.tabs.select(0)
                        if self.api:
                            self.api["window"].destroy()
                            self.api=None
                        self.autosave()
                        self.start("check")
                        break
                    if result["status"]=="ok" and "measurements" in result and self.api:
                        self.api["measurements"]=result["measurements"]
                        t=self.api["table"]
                        t.delete(*t.get_children())
                        for i,m in enumerate(result["measurements"]):
                            t.insert("","end",iid=str(i),values=(m["title"],))
                    else:
                        self.project["runs"].append(result)
                        if "evidence" in result:
                            self.project["reference_evidence"]=result["evidence"]
                        self.latest=result
                        self.present(result)
                        self.autosave()
                    self.stage=result["message"]
                    break
            self.status.configure(text=(self.stage[:145]+f" · {int(time.monotonic()-self.started)} s") if self.job else self.stage[:240])
        self.after(100,self.poll)

    def present(self,result):
        task=result.get("task")
        if task=="check":
            checks=result.get("checks",{})
            names={s["id"]:s["name"] for s in self.project["sources"]}
            text="\n".join(names.get(r["source_id"],"Źródło")+": "+r["message"] for r in checks.get("sources",[]))
            text+="\nPoziomy: "+checks.get("levels",{}).get("message","Nie sprawdzono.")
            self.measure_summary.configure(text=text[:920])
        elif task in ("standard","custom"):
            self.design_result=result
            self.design_summary.configure(text=result["message"]+"\n"+result.get("next",""))
            for row in self.candidates.get_children():
                if row!="reference":
                    self.candidates.delete(row)
            curves=[]
            ref=result.get("reference_curve")
            if ref:
                curves.append(("Odniesienie",ref["frequency"],ref["spl"]))
            for c in result.get("candidates",[])[:2]:
                m=c["metrics"]
                self.candidates.insert("","end",iid=c["id"],values=(c["label"],self.fmt(m["L_sum_gain_delta_db"])+" / "+self.fmt(m["R_sum_gain_delta_db"]),self.fmt(m["body_delta_db"]),c["evidence"]))
                curves.append((c["label"],c["curve"]["frequency"],c["curve"]["spl"]))
            self.plot(self.design_plot,curves,"Suma stereo — symulacja w stanie pomiaru Dirac")
            self.export_button.pack_forget()
        elif task=="compare":
            self.compare_result=result
            if result.get("status")=="ok":
                labels={key:label for label,key in self.choices().items()}
                self.a_var.set(labels.get(result["config_a"],""))
                self.b_var.set(labels.get(result["config_b"],""))
                self.stage_var.set(result["stage"])
                note=next((n for n in reversed(self.project["listening"]) if n.get("run_id")==result["run_id"]),None)
                self.choice.set(note["choice"] if note else "brak wyraźnej różnicy")
                self.matched.set(note["level_matched"] if note else False)
                self.note.set(note["note"] if note else "")
            summaries=result.get("summaries",[])
            self.compare_summary.configure(text="\n".join(summaries) if summaries else result["message"]+"\n"+result.get("next",""))
            self.comparison.delete(*self.comparison.get_children())
            for i,r in enumerate(result.get("rows",[])):
                self.comparison.insert("","end",iid=str(i),values=(r["position"],r["role"],*[self.fmt(r["delta_db"][k]) for k in ("30–60 Hz","80–110 Hz","100–220 Hz")]))
            if result.get("rows"):
                self.comparison.selection_set("0")
                self.compare_curve()
            else:
                self.plot(self.compare_plot,[],"")
        elif task=="export" and result["status"]=="ok":
            messagebox.showinfo("Ustawienia do testu",result["message"]+"\n\n"+result["export_dir"],parent=self)
            folder(result["export_dir"])
        else:
            self.status.configure(text=result["message"])

    def select_candidate(self,event=None):
        self.export_button.pack_forget()
        ids=self.candidates.selection()
        if not ids or not self.design_result:
            return
        c=next((c for c in self.design_result.get("candidates",[]) if c["id"]==ids[0]),None)
        if c:
            m,s=c["metrics"],c["settings"]
            self.candidate_note.configure(text=f"Relacja sub−kolumna L/R: {s['l_rel_ms']:.2f} / {s['r_rel_ms']:.2f} ms. Fundament 30–60 Hz Δ {self.fmt(m['foundation_delta_db'])} dB; suma 80–110 Hz Δ {self.fmt(m['stereo_delta_db'])} dB.\nOceń kompromis między stronami i dolną średnicą. Obliczenia nie rozstrzygają naturalności wokalu.")
            if c["safe"] and result_is_current(self.project,self.design_result) and not self.job:
                self.export_button.pack(side="left",padx=8)
        else:
            self.candidate_note.configure(text="Obecny slot pozostaje odniesieniem. Aplikacja nie zmienia ustawień sprzętu.")

    def compare(self):
        choices=self.choices()
        if self.a_var.get() not in choices or self.b_var.get() not in choices:
            messagebox.showinfo("Porównanie","Dodaj konfiguracje w Mój system i przypisz do nich ślady w Pomiary.",parent=self)
            return
        self.project["ui"]["compare_stage"]=self.stage_var.get()
        self.project["ui"].update(compare_a=choices[self.a_var.get()],compare_b=choices[self.b_var.get()])
        self.start("compare",{"stage":self.stage_var.get(),"a":choices[self.a_var.get()],"b":choices[self.b_var.get()]})

    def invalidate_comparison(self,event=None):
        self.compare_result=None
        self.comparison.delete(*self.comparison.get_children())
        self.plot(self.compare_plot,[],"")
        self.compare_summary.configure(text="Zmieniono wybór presetów lub etap. Uruchom porównanie przed zapisaniem odsłuchu.")

    def compare_curve(self,event=None):
        ids=self.comparison.selection()
        if ids and self.compare_result and self.compare_result.get("curves"):
            c=self.compare_result["curves"][int(ids[0])]
            self.plot(self.compare_plot,[("A",c["frequency"],c["A"]),("B",c["frequency"],c["B"])],f"SPL · {c['role']} · {c['position']} · wspólne osie, bez wyrównania")

    def plot(self,frame,curves,title):
        for child in frame.winfo_children():
            child.destroy()
        if not curves:
            return
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        fig=Figure(figsize=(8,2.2),dpi=90,layout="constrained")
        ax=fig.add_subplot(111)
        for label,freq,spl in curves:
            ax.semilogx(freq,spl,label=label,linewidth=1.2)
        ax.set(xlabel="Częstotliwość [Hz]",ylabel="SPL [dB]",title=title)
        ax.grid(True,alpha=.25)
        ax.legend(fontsize=8)
        canvas=FigureCanvasTkAgg(fig,master=frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both",expand=True)

    def export(self):
        ids=self.candidates.selection()
        if ids and self.design_result:
            self.start("export",{"result":self.design_result,"candidate_id":ids[0]})

    def listening(self):
        r=self.compare_result
        if not r or r["status"]!="ok" or not result_is_current(self.project,r):
            messagebox.showinfo("Odsłuch","Najpierw porównaj te konfiguracje; notatka musi mieć ID i etap.",parent=self)
            return
        self.project["listening"].append({"a":r["config_a"],"b":r["config_b"],"stage":r["stage"],"choice":self.choice.get(),"level_matched":self.matched.get(),"note":self.note.get().strip(),"author":"użytkownik","created":utc_now(),"run_id":r["run_id"]})
        self.autosave()
        self.status.configure(text="Zapisano Twój wybór."+(" Głośność niewyrównana — ograniczona miarodajność." if not self.matched.get() else ""))

    def shown_result(self):
        return (self.compare_result if self.tabs.index(self.tabs.select())==2 else self.design_result) or self.latest

    def summary(self):
        from rew_user_report import write_summary
        r=self.shown_result()
        if not r:
            messagebox.showinfo("Podsumowanie","Brak zakończonego wyniku.",parent=self)
            return
        path=filedialog.asksaveasfilename(parent=self,initialfile="REWDO_podsumowanie.md",defaultextension=".md",filetypes=[("Raport","*.md")])
        if path:
            write_summary(Path(path),self.project,r)

    def details(self):
        win=tk.Toplevel(self)
        win.title("Szczegóły techniczne")
        win.geometry("1000x650")
        text=tk.Text(win,wrap="word",font=("Consolas",10))
        text.pack(fill="both",expand=True,padx=10,pady=10)
        r=copy.deepcopy(self.shown_result() or {})
        for k in ("curves","reference_curve"):
            r.pop(k,None)
        for c in r.get("candidates",[]):
            c.pop("curve",None)
        text.insert("end",json.dumps(r,ensure_ascii=False,indent=2)+"\n\n"+"".join(self.log))
        text.configure(state="disabled")
        ttk.Button(win,text="Kopiuj szczegóły",command=lambda:(self.clipboard_clear(),self.clipboard_append(text.get("1.0","end")))).pack(side="left",padx=10,pady=6)
        if r.get("directory"):
            ttk.Button(win,text="Otwórz folder techniczny",command=lambda:folder(r["directory"])).pack(side="left",padx=4)

    def cancel(self):
        if self.job:
            self.stage="Zatrzymywanie obliczeń i procesów potomnych…"
            self.job.cancel()

    def close(self):
        if self.job:
            if messagebox.askyesno("Zamknij","Przerwać obliczenia i zamknąć po zatrzymaniu procesów?",parent=self):
                self.cancel()
                def finish():
                    if self.job:
                        self.after(150,finish)
                    else:
                        self.autosave()
                        self.destroy()
                self.after(150,finish)
            return
        self.autosave()
        self.destroy()


def main():
    App().mainloop()
    return 0


if __name__=="__main__":
    raise SystemExit(main())
