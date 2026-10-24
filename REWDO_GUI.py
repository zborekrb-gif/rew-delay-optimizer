"""REWDO 20.33. Guided measurement, design and listening workflow."""
from __future__ import annotations
import copy
import json
import os
import queue
import subprocess
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from rew_project import (STAGES, STEPS, ROLES, active_config, changed, fingerprint, load_project,
    new_project, number, put_config, put_measurement_preset, relocate, result_is_current, save_project, uid, user_dir, utc_now)
from rew_process import ProcessJob
from rew_guidance import ROLE_LABELS, plan, next_step, help_text, current_run

from rew_app_metadata import APP_VERSION


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
    scale=max(1.0,float(parent.tk.call("tk","scaling"))/(96/72))
    for key, label, width in columns:
        t.heading(key, text=label)
        t.column(key, width=int(width*scale), minwidth=int(60*scale))
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
        self.title(f"REWDO {APP_VERSION} — od pomiaru do odsłuchu")
        self.ui_scale=max(1.0,float(self.tk.call("tk","scaling"))/(96/72))
        self.geometry(f"{min(int(1120*self.ui_scale),self.winfo_screenwidth()-60)}x{min(int(730*self.ui_scale),self.winfo_screenheight()-90)}")
        self.minsize(min(int(840*self.ui_scale),self.winfo_screenwidth()-60),min(int(560*self.ui_scale),self.winfo_screenheight()-90))
        self.project, self.path = new_project(), None
        self.job, self.latest, self.design_result, self.compare_result = None, None, None, None
        self.time_result, self.time_window = None, None
        self.full_window=None
        self.log, self.stage, self.api = [], "Projekt gotowy. Zacznij od zalecanego kroku u góry.", None
        try:
            prefs = user_dir()/"preferences.json"
            if prefs.is_file():
                self.path = Path(json.loads(prefs.read_text(encoding="utf-8"))["last_project"])
                self.project = load_project(self.path)
        except Exception as exc:
            self.path = None
            self.stage = f"Nie przywrócono projektu: {exc}. Wskaż go przez Projekt → Otwórz."
        style=ttk.Style(self)
        self.font_family="Segoe UI" if os.name=="nt" else "DejaVu Sans"
        for name in ("TkDefaultFont","TkTextFont","TkMenuFont","TkHeadingFont"):
            tkfont.nametofont(name).configure(family=self.font_family,size=10)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.configure(background="#f3f5f7")
        style.configure("TFrame",background="#f3f5f7")
        style.configure("TLabel",background="#f3f5f7",foreground="#233746")
        style.configure("TLabelframe",background="#f3f5f7")
        style.configure("TLabelframe.Label",background="#f3f5f7",foreground="#233746")
        style.configure("TNotebook",background="#f3f5f7",borderwidth=0)
        style.configure("TNotebook.Tab",padding=(12,6))
        style.configure("Treeview",rowheight=int(25*self.ui_scale),background="white",fieldbackground="white")
        style.configure("Treeview.Heading",font=(self.font_family,10,"bold"))
        style.configure("TButton",padding=(8,4))
        style.configure("Guide.TFrame",background="#edf3f6")
        style.configure("Guide.TLabel",background="#edf3f6",foreground="#183847")
        style.configure("GuideTitle.TLabel",background="#edf3f6",foreground="#183847",font=(self.font_family,11,"bold"))
        style.configure("Primary.TButton",padding=(12,7),font=(self.font_family,10,"bold"))
        self.columnconfigure(0,weight=1)
        self.rowconfigure(2,weight=1)
        self.busy_widgets=[]
        top=ttk.Frame(self,padding=10)
        top.grid(row=0,column=0,sticky="ew")
        top.columnconfigure(0,weight=1)
        self.header=ttk.Label(top,font=(self.font_family,12,"bold"))
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
        guide=ttk.Frame(self,padding=(12,8),style="Guide.TFrame")
        guide.grid(row=1,column=0,sticky="ew",padx=10,pady=(0,7))
        guide.columnconfigure(0,weight=1)
        self.guide_title=ttk.Label(guide,style="GuideTitle.TLabel")
        self.guide_title.grid(row=0,column=0,sticky="w")
        self.guide_reason=ttk.Label(guide,style="Guide.TLabel",wraplength=640,justify="left")
        self.guide_reason.grid(row=1,column=0,sticky="ew",pady=(3,0))
        controls=ttk.Frame(guide,style="Guide.TFrame")
        controls.grid(row=0,column=1,rowspan=2,padx=(14,0),sticky="ne")
        self.guide_button=self.button(controls,"Zacznij",self.do_next)
        self.guide_button.configure(style="Primary.TButton")
        self.guide_button.pack(fill="x")
        ttk.Button(controls,text="Plan i instrukcje krok po kroku",command=self.guide_dialog).pack(fill="x",pady=(4,0))
        self.tabs=ttk.Notebook(self)
        self.tabs.grid(row=2,column=0,sticky="nsew",padx=10)
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
        bottom.grid(row=3,column=0,sticky="ew")
        bottom.columnconfigure(0,weight=1)
        self.status=ttk.Label(bottom,text=self.stage,wraplength=700)
        self.status.grid(row=0,column=0,sticky="w")
        self.stop=ttk.Button(bottom,text="Przerwij",command=self.cancel,state="disabled")
        self.stop.grid(row=0,column=1,padx=4)
        ttk.Button(bottom,text="Szczegóły techniczne",command=self.details).grid(row=0,column=2,padx=4)
        self.refresh()
        self.tabs.select(min(2,max(0,self.project["ui"]["step"])))
        self.tabs.bind("<<NotebookTabChanged>>",self.step_changed)
        self.bind("<F1>",lambda event:self.guide_dialog())
        self.bind("<Control-s>",lambda event:self.save())
        self.bind("<Configure>",self.resize_help,add="+")
        self.protocol("WM_DELETE_WINDOW",self.close)
        self.after(100,self.poll)

    def button(self,parent,title,fn):
        b=ttk.Button(parent,text=title,command=fn)
        self.busy_widgets.append(b)
        return b

    def description(self,page,text):
        label=ttk.Label(page,text=text,wraplength=950)
        label.grid(row=0,column=0,sticky="w",pady=(0,8))
        page.bind("<Configure>",lambda event:label.configure(wraplength=max(300,event.width-25)),add="+")

    def measurements_page(self):
        p=self.pages[0]
        self.description(p,"1. Wczytaj plik.  2. Opisz, co mierzono.  3. Sprawdź dane. Instrukcję danej sesji znajdziesz w planie u góry.")
        row=ttk.Frame(p)
        row.grid(row=1,column=0,sticky="ew",pady=(0,8))
        load=ttk.Menubutton(row,text="Wczytaj pomiary")
        menu=tk.Menu(load,tearoff=False)
        menu.add_command(label="Z pliku REW",command=self.import_file)
        menu.add_command(label="Z otwartego REW",command=self.api_dialog)
        load["menu"]=menu
        load.pack(side="left")
        self.busy_widgets.append(load)
        for name,fn in (("Opisz pomiary",self.mapping),("Wskaż przeniesiony plik",self.locate),("Sprawdź pomiary",lambda:self.start("check"))):
            self.button(row,name,fn).pack(side="left",padx=5)
        self.button(row,"Odłącz plik",self.detach_source).pack(side="left",padx=5)
        self.traces=table(p,[("source","Plik",145),("name","Nazwa w REW",180),("config","Ustawienie",125),("element","Co grało",170),("channel","Kanał",60),("mic","Miejsce mikrofonu",115),("stage","Etap",105)],5)
        self.traces.grid(row=2,column=0,sticky="nsew")
        p.rowconfigure(2,weight=2)
        self.traces.bind("<Double-1>",lambda e:self.mapping())
        self.measure_summary=ttk.Label(p,text="",wraplength=950,justify="left")
        self.measure_summary.grid(row=3,column=0,sticky="ew",pady=8)
        self.gain_label=ttk.Label(p,wraplength=950)
        self.check_table=table(p,[("item","Co sprawdzono",240),("state","Wynik",130),("message","Co ustalono / czego brakuje",560)],3)
        self.check_table.grid(row=4,column=0,sticky="nsew")
        p.rowconfigure(4,weight=1)
        self.check_table.bind("<<TreeviewSelect>>",self.check_selected)
        note_frame=ttk.Frame(p)
        note_frame.grid(row=5,column=0,sticky="ew",pady=(4,0))
        note_frame.columnconfigure(0,weight=1)
        self.check_note=tk.Text(note_frame,wrap="word",font=(self.font_family,10),height=2,width=60,relief="flat",state="disabled",background="#f3f5f7")
        self.check_note.grid(row=0,column=0,sticky="ew")
        note_scroll=ttk.Scrollbar(note_frame,command=self.check_note.yview)
        note_scroll.grid(row=0,column=1,sticky="ns")
        self.check_note.configure(yscrollcommand=note_scroll.set)
        actions=ttk.Frame(p)
        actions.grid(row=6,column=0,sticky="ew",pady=(6,0))
        self.button(actions,"Sprawdź obecne ustawienie",lambda:self.start("verify")).pack(side="left")
        ttk.Button(actions,text="Jak wykonać brakujący pomiar?",command=lambda:self.guide_dialog("sessions")).pack(side="left",padx=8)

    def design_page(self):
        p=self.pages[1]
        self.description(p,"Najpierw sprawdź pomiary i obecne ustawienie. Potem oblicz warianty, zaznacz jeden w tabeli i przygotuj próbę na sprzęcie.")
        row=ttk.Frame(p)
        row.grid(row=1,column=0,sticky="ew")
        self.button(row,"Znajdź ustawienia do sprawdzenia",lambda:self.start("standard")).pack(side="left")
        self.button(row,"Szukaj dokładniej — własna zwrotnica",lambda:self.start("custom")).pack(side="left",padx=8)
        ttk.Button(row,text="Jak wybrać?",command=lambda:self.guide_dialog("design")).pack(side="left")
        self.design_summary=ttk.Label(p,text="Najpierw potwierdź pomiary, poziomy i odniesienie. OFF/OFF, all-pass wyłączony.",wraplength=950)
        self.design_summary.grid(row=2,column=0,sticky="ew",pady=10)
        self.candidates=table(p,[("setting","Ustawienie",245),("sum","Sumowanie L/R: zmiana [dB]",230),("body","100–220 Hz: zmiana [dB]",170),("evidence","Na jakiej podstawie",240)],3)
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
        self.description(p,"Wybierz dwa ustawienia z tego samego etapu i miejsca mikrofonu. Różnice w tabeli to B − A; więcej poziomu nie musi oznaczać poprawy.")
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
        actions=ttk.Frame(p)
        actions.grid(row=2,column=0,sticky="ew",pady=6)
        self.button(actions,"Porównaj A i B",self.compare).pack(side="left")
        self.button(actions,"Dodaj nazwę ustawienia",self.add_preset).pack(side="left",padx=6)
        self.button(actions,"Pełne porównanie…",self.full_dialog).pack(side="left",padx=6)
        ttk.Button(actions,text="Jak oceniać odsłuch?",command=lambda:self.guide_dialog("compare")).pack(side="left")
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

    def resize_help(self,event=None):
        if event is not None and event.widget is not self:
            return
        width=max(300,self.winfo_width()-365)
        self.guide_reason.configure(wraplength=width)
        for widget in (self.measure_summary,self.design_summary,self.candidate_note,self.compare_summary):
            widget.configure(wraplength=max(350,self.winfo_width()-65))
        self.ref_label.configure(wraplength=max(280,self.winfo_width()-475))

    def refresh_guide(self):
        step=next_step(self.project)
        self.guide_title.configure(text="Co teraz: "+step.title.split(". ",1)[-1])
        self.guide_reason.configure(text=step.reason if len(step.reason)<260 else step.reason[:240]+"… Pełny opis w planie.")
        self.guide_button.configure(text=step.button)

    def do_next(self):
        self.act(next_step(self.project))

    def act(self,step):
        if self.job:
            return
        action=step.action
        if action.startswith("system_"):
            self.system({"system_reference":0,"system_levels":1,"system_search":2}[action])
        elif action=="import":
            self.import_file()
        elif action=="trial_import":
            # Navigation only: importing a trial is not proof of success on hardware.
            self.project["ui"]["goal"]="compare"
            self.autosave()
            self.refresh_guide()
            self.tabs.select(0)
            self.import_file()
        elif action=="preset":
            self.add_preset()
        elif action=="mapping":
            self.tabs.select(0)
            self.mapping(step.source_id)
        elif action in ("check","verify","standard"):
            self.tabs.select(1 if action=="standard" else 0)
            self.start(action)
        elif action=="trial_help":
            self.guide_dialog("trial")
        elif action in ("check_help","verify_help"):
            self.guide_dialog(action.removesuffix("_help"))
        else:
            self.tabs.select({"measurements":0,"design_page":1,"design_result":1,"compare_page":2}.get(action,0))
            if action=="design_result":
                self.candidates.focus_set()

    def guide_dialog(self,key=None):
        win=self.dialog("Plan dojścia do dobrego brzmienia","1000x650")
        win.minsize(760,480)
        win.columnconfigure(1,weight=1)
        win.rowconfigure(1,weight=1)
        goals=ttk.Frame(win,padding=10)
        goals.grid(row=0,column=0,columnspan=2,sticky="ew")
        ttk.Label(goals,text="Cel pracy:",font=(self.font_family,10,"bold")).pack(side="left")
        goal=tk.StringVar(value=self.project["ui"].get("goal","design"))
        steps=[]
        listing=table(win,[("step","Twój plan",285),("state","Stan",130)],9)
        listing.grid(row=1,column=0,sticky="nsew",padx=(10,5))
        right=ttk.Frame(win,padding=10)
        right.grid(row=1,column=1,sticky="nsew")
        right.columnconfigure(0,weight=1)
        right.rowconfigure(2,weight=1)
        title=ttk.Label(right,font=(self.font_family,12,"bold"),wraplength=490)
        title.grid(row=0,column=0,sticky="ew")
        reason=ttk.Label(right,wraplength=490,justify="left")
        reason.grid(row=1,column=0,sticky="ew",pady=10)
        body_frame=ttk.Frame(right)
        body_frame.grid(row=2,column=0,sticky="nsew")
        body_frame.columnconfigure(0,weight=1)
        body_frame.rowconfigure(0,weight=1)
        body=tk.Text(body_frame,wrap="word",font=(self.font_family,10),padx=10,pady=10,relief="flat",width=45,height=14)
        body.grid(row=0,column=0,sticky="nsew")
        scroll=ttk.Scrollbar(body_frame,command=body.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        body.configure(yscrollcommand=scroll.set)
        execute=ttk.Button(right,style="Primary.TButton")
        execute.grid(row=3,column=0,sticky="e",pady=(10,0))
        def show(event=None):
            ids=listing.selection()
            if not ids:
                return
            step=next(s for s in steps if s.key==ids[0])
            if step.action in ("check_help","verify_help"):
                step=copy.copy(step)
                step.action=step.action.removesuffix("_help")
                step.button="Sprawdź po poprawkach"
            heading,text=help_text(step.key)
            title.configure(text=heading)
            reason.configure(text=step.reason if len(step.reason)<260 else step.reason[:240]+"… Pełny opis poniżej.")
            body.configure(state="normal")
            body.delete("1.0","end")
            body.insert("1.0","Bieżący stan:\n"+step.reason+"\n\nInstrukcja:\n\n"+text)
            body.configure(state="disabled")
            body.yview_moveto(0)
            def proceed():
                win.destroy()
                self.act(step)
            execute.configure(text=step.button,command=proceed,state="disabled" if self.job else "normal")
        def fill(target=None):
            nonlocal steps
            steps=plan(self.project)
            if target and target not in [s.key for s in steps]:
                from rew_guidance import Step
                steps.append(Step(target,"Instrukcja próby" if target=="trial" else "Instrukcja pomiarów","review","Instrukcja jest planem; nie potwierdza wykonania pomiaru.","measurements","Przejdź do pomiarów"))
            listing.delete(*listing.get_children())
            for s in steps:
                listing.insert("","end",iid=s.key,values=(s.title,{"done":"Zapisane / sprawdzone","todo":"Do zrobienia","review":"Do oceny"}[s.state]))
            selected=target or next_step(self.project).key
            listing.selection_set(selected)
            listing.see(selected)
            show()
        def set_goal():
            self.project["ui"]["goal"]=goal.get()
            self.autosave()
            self.refresh_guide()
            fill()
        for text,value in (("Dobrać połączenie kolumn i subów","design"),("Porównać gotowe pomiary / sesje Dirac","compare")):
            ttk.Radiobutton(goals,text=text,value=value,variable=goal,command=set_goal,state="disabled" if self.job else "normal").pack(side="left",padx=8)
        listing.bind("<<TreeviewSelect>>",show)
        right.bind("<Configure>",lambda e:(title.configure(wraplength=max(250,e.width-25)),reason.configure(wraplength=max(250,e.width-25))))
        ttk.Button(win,text="Zamknij plan",command=win.destroy).grid(row=2,column=0,columnspan=2,sticky="e",padx=10,pady=8)
        fill(key)

    def add_preset(self):
        if self.job:
            return
        win=self.dialog("Nazwij ustawienie do porównania","570x235")
        panel=ttk.Frame(win,padding=14)
        panel.pack(fill="both",expand=True)
        panel.columnconfigure(1,weight=1)
        ttk.Label(panel,text="Dodaj nazwę, np. Obecna sesja C268 lub Nowa sesja C268.\nNazwa służy do przypisania pomiarów; nie potwierdza ustawień DSP.",wraplength=520).grid(row=0,column=0,columnspan=2,sticky="w",pady=(0,8))
        name=field(panel,1,"Nazwa ustawienia")
        def commit():
            if not name.get().strip():
                messagebox.showinfo("Nazwa","Wpisz nazwę, którą rozpoznasz podczas porównania.",parent=win)
                return
            put_measurement_preset(self.project,name.get())
            self.autosave()
            self.refresh()
            win.destroy()
        ttk.Button(panel,text="Dodaj ustawienie",command=commit).grid(row=2,column=1,sticky="e",pady=12)

    def check_selected(self,event=None):
        ids=self.check_table.selection()
        self.set_check_note(self.check_messages.get(ids[0],"") if ids else "")

    def set_check_note(self,text):
        self.check_note.configure(state="normal")
        self.check_note.delete("1.0","end")
        self.check_note.insert("1.0",text)
        self.check_note.configure(state="disabled")
        self.check_note.yview_moveto(0)

    def step_changed(self,event=None):
        self.project["ui"]["step"]=self.tabs.index(self.tabs.select())
        if self.project["ui"]["step"] in (1,2):
            self.project["ui"]["goal"]="compare" if self.project["ui"]["step"]==2 else "design"
        self.refresh_guide()
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
        self.time_result=None
        if self.time_window and self.time_window.winfo_exists():
            self.time_window.close()
        if self.full_window and self.full_window.winfo_exists():
            self.full_window.close()
        self.comparison.delete(*self.comparison.get_children())
        self.design_summary.configure(text="Wyniki zależą od aktualnej wersji projektu. Potwierdź dane i uruchom dobór.")
        self.candidate_note.configure(text="")
        for frame in (self.design_plot,self.compare_plot):
            self.plot(frame,[],"")
        for tasks in (("standard","custom"),("compare","full_compare"),("check",)):
            result=next((r for r in reversed(self.project["runs"]) if r.get("task") in tasks),None)
            if result and result_is_current(self.project,result):
                self.present(result)
        if not current_run(self.project,"check"):
            self.check_table.delete(*self.check_table.get_children())
            self.measure_summary.configure(text="Kontrola czeka na uruchomienie. Dołączone pliki nie mają jeszcze potwierdzonej zgodności.")
            self.set_check_note("")
        self.refresh_guide()

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

    def detach_source(self):
        if self.job:
            return
        source=self.selected_source()
        if source and messagebox.askyesno("Odłącz plik",f"Odłączyć {source['name']} z tego projektu?\n\nPlik pozostanie na dysku. Zależne wyniki wymagają ponownego sprawdzenia.",parent=self):
            self.project["sources"]=[s for s in self.project["sources"] if s["id"]!=source["id"]]
            changed(self.project)
            self.autosave()
            self.refresh()

    def dialog(self,title,size):
        win=tk.Toplevel(self)
        win.title(title)
        width,height=map(int,size.split("x"))
        win.geometry(f"{min(int(width*self.ui_scale),self.winfo_screenwidth()-50)}x{min(int(height*self.ui_scale),self.winfo_screenheight()-70)}")
        win.transient(self)
        win.grab_set()
        return win

    def system(self,section=0):
        if self.job:
            return
        win=self.dialog("Mój system","800x620")
        win.columnconfigure(0,weight=1)
        win.rowconfigure(0,weight=1)
        book=ttk.Notebook(win)
        book.grid(row=0,column=0,sticky="nsew",padx=12,pady=12)
        panels=[]
        canvases=[]
        for name in ("Obecne ustawienie","Poziomy subów","Zakres szukania","Opis systemu"):
            holder=ttk.Frame(book)
            holder.columnconfigure(0,weight=1)
            holder.rowconfigure(0,weight=1)
            canvas=tk.Canvas(holder,highlightthickness=0,background="#f3f5f7")
            canvas.grid(row=0,column=0,sticky="nsew")
            scrollbar=ttk.Scrollbar(holder,command=canvas.yview)
            scrollbar.grid(row=0,column=1,sticky="ns")
            canvas.configure(yscrollcommand=scrollbar.set)
            frame=ttk.Frame(canvas,padding=12)
            item=canvas.create_window((0,0),window=frame,anchor="nw")
            frame.bind("<Configure>",lambda e,cv=canvas:cv.configure(scrollregion=cv.bbox("all")))
            canvas.bind("<Configure>",lambda e,cv=canvas,it=item:cv.itemconfigure(it,width=e.width))
            frame.columnconfigure(1,weight=1)
            book.add(holder,text=name)
            panels.append(frame)
            canvases.append(canvas)
        book.select(section)
        def wheel(event):
            direction=(-1 if event.num==4 else 1) if getattr(event,"num",None) in (4,5) else -int(event.delta/120)
            canvases[book.index(book.select())].yview_scroll(direction,"units")
        win.bind("<MouseWheel>",wheel)
        win.bind("<Button-4>",wheel)
        win.bind("<Button-5>",wheel)
        c=active_config(self.project)
        ref=panels[0]
        choices={"Nie wybrano":"",**self.choices()}
        history=field(ref,0,"Zapisane ustawienia",next((n for n,k in choices.items() if c and k==c["key"]),"Nie wybrano"),choices)
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
        ttk.Label(ref,text="Przepisz wartości z konsoli miniDSP. Każda zmiana parametrów tworzy nową wersję.\nWpis potwierdza ustawienia z konsoli; zgodność z pomiarem sprawdzimy osobno.\nDo samego porównania SPL wystarczy nazwa ustawienia.",wraplength=700).grid(row=10,column=0,columnspan=2,sticky="w")
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

    def mapping(self,source_id=None):
        if self.job:
            return
        source=next((s for s in self.project["sources"] if s["id"]==source_id),None) if source_id else self.selected_source()
        if not source:
            return
        win=self.dialog("Opisz pomiary — "+source["name"],"1020x650")
        win.columnconfigure(0,weight=1)
        win.rowconfigure(1,weight=1)
        top=ttk.Frame(win,padding=8)
        top.grid(row=0,column=0,sticky="ew")
        top.columnconfigure(1,weight=1)
        choices={"Nieprzypisane":"",**self.choices()}
        stage=field(top,0,"Etap sesji",source["stage"],STAGES)
        preset=field(top,1,"Konfiguracja sesji",next((n for n,k in choices.items() if k==source["configuration"]),"Nieprzypisane"),choices)
        ttk.Label(top,text="Wybierz etap i ustawienie całej sesji. Niżej wybierz ślad i opisz, co grało.\nMiejsce mikrofonu, np. MLP, opisuje położenie kapsuły; kanał L/R — grającą stronę.",wraplength=750).grid(row=2,column=0,columnspan=2,sticky="w",pady=4)
        t=table(win,[("name","Nazwa źródłowa",220),("role","Co grało",225),("mic","Miejsce mikrofonu",130),("preset","Ustawienie",190),("stage","Etap",125)],6)
        t.grid(row=1,column=0,sticky="nsew",padx=8)
        drafts=copy.deepcopy(source["traces"])
        def fill():
            selection=t.selection()
            t.delete(*t.get_children())
            for i,r in enumerate(drafts):
                t.insert("","end",iid=str(i),values=(r["name"],ROLE_LABELS.get(r["role"],r["role"]),r["microphone"],self.config_label(r["preset"]),r["stage"]))
            if selection:
                t.selection_set(selection)
        fill()
        edit=ttk.Frame(win,padding=8)
        edit.grid(row=2,column=0,sticky="ew")
        role,mic,trace_preset,trace_stage=tk.StringVar(value=ROLE_LABELS["Nieprzypisany"]),tk.StringVar(),tk.StringVar(value="Nieprzypisane"),tk.StringVar(value=source["stage"])
        for col,caption in enumerate(("Co grało w tym śladzie?","Gdzie stała kapsuła?","Które ustawienie?","Kiedy zmierzono?")):
            ttk.Label(edit,text=caption).grid(row=0,column=col,sticky="w",padx=3)
        for col,var,vals,width in ((0,role,ROLE_LABELS.values(),26),(1,mic,None,17),(2,trace_preset,choices,26),(3,trace_stage,STAGES,16)):
            w=ttk.Entry(edit,textvariable=var,width=width) if vals is None else ttk.Combobox(edit,textvariable=var,values=list(vals),state="readonly",width=width)
            w.grid(row=1,column=col,padx=3)
        def selected(event=None):
            ids=t.selection()
            if ids:
                r=drafts[int(ids[0])]
                role.set(ROLE_LABELS.get(r["role"],ROLE_LABELS["Nieprzypisany"]))
                mic.set(r["microphone"])
                trace_stage.set(r["stage"])
                trace_preset.set(next((n for n,k in choices.items() if k==r["preset"]),"Nieprzypisane"))
        t.bind("<<TreeviewSelect>>",selected)
        def apply_one():
            for i in t.selection():
                drafts[int(i)].update(role=next(k for k,v in ROLE_LABELS.items() if v==role.get()),microphone=mic.get().strip(),preset=choices[trace_preset.get()],stage=trace_stage.get())
            fill()
        def apply_all():
            for r in drafts:
                r.update(microphone=mic.get().strip(),preset=choices[preset.get()],stage=stage.get())
            fill()
            selected()
        ttk.Button(edit,text="Zastosuj do śladu",command=apply_one).grid(row=1,column=4,padx=3)
        ttk.Button(edit,text="Etap sesji, ustawienie i wpisane miejsce dla wszystkich",command=apply_all).grid(row=2,column=0,columnspan=4,sticky="w",pady=6)
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
            apply_one()
            source.update(traces=drafts,stage=stage.get(),configuration=choices[preset.get()],raw_confirmed=raw.get(),session_confirmed=same.get(),timing_confirmed=timing.get(),gain_state=levels[level.get()],mapping_confirmed=True)
            changed(self.project)
            self.autosave()
            self.refresh()
            win.destroy()
            self.start("check")
        ttk.Button(checks,text="Potwierdź przypisanie i sprawdź",command=commit).grid(row=4,column=0,columnspan=3,sticky="e",pady=6)
        if drafts:
            t.selection_set("0")
            selected()

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
                    if result["task"]=="time_compare":
                        result.setdefault("time_request",copy.deepcopy(self.job.request.get("args",{})))
                    if result["task"]=="full_compare":
                        result.setdefault("full_request",copy.deepcopy(self.job.request.get("args",{})))
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
                    self.refresh_guide()
                    break
            self.status.configure(text=(self.stage[:145]+f" · {int(time.monotonic()-self.started)} s") if self.job else self.stage[:240])
        self.after(100,self.poll)

    def present(self,result):
        task=result.get("task")
        if task=="check":
            checks=result.get("checks",{})
            names={s["id"]:s["name"] for s in self.project["sources"]}
            self.check_table.delete(*self.check_table.get_children())
            self.check_messages={}
            for i,r in enumerate(checks.get("sources",[])):
                state="Faza i suma sprawdzone" if r.get("phase") else "SPL dostępny" if r.get("spl") else "Nie można odczytać"
                self.check_table.insert("","end",iid=str(i),values=(names.get(r["source_id"],"Źródło"),state,r["message"]))
                self.check_messages[str(i)]=r["message"]
            level=checks.get("levels",{})
            msg=level.get("message","Nie sprawdzono poziomów.")
            self.check_table.insert("","end",iid="levels",values=("Zmiana poziomu subwooferów","Sprawdzona" if level.get("status")=="VERIFIED" else "Wymaga danych / poprawy",msg))
            self.check_messages["levels"]=msg
            self.measure_summary.configure(text="Wyniki kontroli — zaznacz wiersz, aby przeczytać całą przyczynę. Odczyt SPL nie potwierdza jeszcze fazy ani odniesienia.")
            self.set_check_note("")
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
        elif task in {"compare","full_compare"}:
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
            self.compare_summary.configure(text=("Pomiar: pokazano B − A, bez wyrównania poziomu. Zaznacz wiersz, aby obejrzeć wykres. Odsłuch: wyrównaj głośność i zapisz własny wybór." if result.get("status")=="ok" else result["message"]+"\n"+result.get("next","")))
            if task=='full_compare':
                self.compare_summary.configure(text=result['message']+'\nCo zrobić teraz: '+result.get('next_short',result.get('next','')))
                if self.full_window and self.full_window.winfo_exists():self.full_window.present(result)
            self.comparison.delete(*self.comparison.get_children())
            for i,r in enumerate(result.get("rows",[])):
                self.comparison.insert("","end",iid=str(i),values=(r["position"],r["role"],*[self.fmt(r["delta_db"][k]) for k in ("30–60 Hz","80–110 Hz","100–220 Hz")]))
            if result.get("rows"):
                self.comparison.selection_set("0")
                self.compare_curve()
            else:
                self.plot(self.compare_plot,[],"")
        elif task=="time_compare":
            self.time_result=result
            if self.time_window and self.time_window.winfo_exists():
                self.time_window.present(result)
            self.status.configure(text=result["message"])
        elif task=="export" and result["status"]=="ok":
            messagebox.showinfo("Ustawienia do testu",result["message"]+"\n\n"+result["export_dir"],parent=self)
            folder(result["export_dir"])
            self.guide_dialog("trial")
        elif task=="verify":
            self.measure_summary.configure(text=result["message"]+"\n"+result.get("next",""))
            self.status.configure(text=result["message"])
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
        self.time_result=None
        self.comparison.delete(*self.comparison.get_children())
        self.plot(self.compare_plot,[],"")
        self.compare_summary.configure(text="Zmieniono wybór presetów lub etap. Uruchom porównanie przed zapisaniem odsłuchu.")

    def time_dialog(self):
        if self.job:
            return
        choices=self.choices()
        a,b=choices.get(self.a_var.get()),choices.get(self.b_var.get())
        if not a or not b or a==b:
            messagebox.showinfo("Czas i powtarzalność","Wybierz dwa różne ustawienia A i B nad tabelą. A będzie odniesieniem. Nazwy dodasz przyciskiem „Dodaj nazwę ustawienia”, a ślady przypiszesz przez Pomiary → Opisz pomiary.",parent=self)
            return
        from rew_time_gui import TimeAnalysisWindow
        if self.time_window and self.time_window.winfo_exists():
            self.time_window.lift()
            return
        self.project["ui"].update(compare_stage=self.stage_var.get(),compare_a=a,compare_b=b)
        self.time_window=TimeAnalysisWindow(self,a,b,self.stage_var.get())

    def full_dialog(self):
        if self.job:return
        choices=self.choices();a=choices.get(self.a_var.get());b=choices.get(self.b_var.get())
        if not a or not b or a==b:
            messagebox.showinfo('Pełne porównanie','Wybierz dwa różne ustawienia A i B nad tabelą. A będzie odniesieniem.',parent=self);return
        if self.stage_var.get()!='Po Dirac':
            messagebox.showinfo('Pełne porównanie','Ta analiza dotyczy pomiarów po Dirac. Wybierz etap Po Dirac; zwykłe porównanie SPL pozostaje dostępne dla obu etapów.',parent=self);return
        from rew_comparison_gui import ComparisonWindow
        if self.full_window and self.full_window.winfo_exists():self.full_window.lift();return
        self.project['ui'].update(compare_stage='Po Dirac',compare_a=a,compare_b=b)
        self.full_window=ComparisonWindow(self,a,b)

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
        self.refresh_guide()
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

    def destroy(self):
        if getattr(self,'_rewdo_destroying',False):return
        self._rewdo_destroying=True
        for name in ('full_window','time_window'):
            window=getattr(self,name,None)
            if window is not None and window.winfo_exists():window.close()
        super().destroy()
        for key,value in list(self.__dict__.items()):
            if isinstance(value,tk.Variable):setattr(self,key,None)

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
