"""A guided Tk window for the full, explicitly scoped time comparison."""
from __future__ import annotations

import copy
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from rew_project import fingerprint, result_is_current
from rew_project_time import CONFIRMATIONS, ROLE_NAMES, ROLE_ORDER, result_matches, selection_counts


def readonly_text(parent, *, height=6):
    frame = ttk.Frame(parent)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    text = tk.Text(frame, wrap='word', height=height, width=50, relief='flat', padx=10, pady=8,
                   background='white', font=('Segoe UI' if __import__('os').name == 'nt' else 'DejaVu Sans', 10))
    text.grid(row=0, column=0, sticky='nsew')
    scroll = ttk.Scrollbar(frame, command=text.yview)
    scroll.grid(row=0, column=1, sticky='ns')
    text.configure(yscrollcommand=scroll.set, state='disabled')
    return frame, text


def set_text(widget, value):
    widget.configure(state='normal')
    widget.delete('1.0', 'end')
    widget.insert('1.0', value)
    widget.configure(state='disabled')
    widget.yview_moveto(0)


class TimeAnalysisWindow(tk.Toplevel):
    def __init__(self, host, config_a, config_b, stage):
        super().__init__(host)
        from REWDO_GUI import table
        self.host, self.a, self.b, self.stage = host, config_a, config_b, stage
        self.result = None
        self.loading = False
        self.title('Czas i powtarzalność — porównanie A i B')
        scale = host.ui_scale
        self.geometry(f'{min(int(1000*scale), self.winfo_screenwidth()-50)}x{min(int(700*scale), self.winfo_screenheight()-70)}')
        self.minsize(min(int(790*scale), self.winfo_screenwidth()-50), min(int(560*scale), self.winfo_screenheight()-70))
        self.transient(host)
        self.grab_set()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.wrap_labels = []
        heading = ttk.Frame(self, padding=(12,10,12,4))
        heading.grid(row=0, column=0, sticky='ew')
        heading.columnconfigure(0, weight=1)
        label = ttk.Label(heading, text=f'A — odniesienie: {host.config_label(self.a)}\nB — do sprawdzenia: {host.config_label(self.b)} · {self.stage}',
                          wraplength=850, justify='left', font=(host.font_family,10,'bold'))
        label.grid(row=0,column=0,sticky='ew')
        self.wrap_labels.append(label)
        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=1,column=0,sticky='nsew',padx=12,pady=6)
        pages = []
        for title in ('1. Wybór pomiarów','2. Wynik i powtarzalność','3. Dane i raport'):
            page = ttk.Frame(self.tabs,padding=10)
            page.columnconfigure(0,weight=1)
            self.tabs.add(page,text=title)
            pages.append(page)

        first, result_page, data_page = pages
        first.rowconfigure(4,weight=1)
        selectors=ttk.Frame(first)
        selectors.grid(row=0,column=0,sticky='ew')
        selectors.columnconfigure(1,weight=1)
        self.source_choices={f'{i+1}. {s["name"]}':s for i,s in enumerate(host.project['sources'])}
        self.source_var=tk.StringVar()
        self.position_var=tk.StringVar()
        ttk.Label(selectors,text='Plik z obiema konfiguracjami').grid(row=0,column=0,sticky='w',padx=(0,10),pady=4)
        self.source_combo=ttk.Combobox(selectors,textvariable=self.source_var,values=list(self.source_choices),state='readonly',width=1)
        self.source_combo.grid(row=0,column=1,sticky='ew',pady=4)
        ttk.Label(selectors,text='Miejsce mikrofonu').grid(row=1,column=0,sticky='w',padx=(0,10),pady=4)
        self.position_combo=ttk.Combobox(selectors,textvariable=self.position_var,state='readonly',width=1)
        self.position_combo.grid(row=1,column=1,sticky='ew',pady=4)
        self.counts=table(first,[('role','Co grało',240),('a','Powtórzenia A (min. 3)',220),('b','Pomiary B (min. 1)',220)],3)
        self.counts.grid(row=1,column=0,sticky='ew',pady=8)
        checks=ttk.Frame(first)
        checks.grid(row=2,column=0,sticky='ew')
        checks.columnconfigure(1,weight=1)
        self.confirmations={key:tk.BooleanVar(value=False) for key in CONFIRMATIONS}
        self.checks=[]
        captions=(
            'Ten sam tor i głośność główna w A i B; zmieniono tylko opisane ustawienie.',
            'Ta sama metoda referencji czasu w całej wybranej serii.',
            'Mikrofon pozostawał nieruchomy w wybranym miejscu podczas wszystkich powtórzeń A i B.',
        )
        for row,(key,caption) in enumerate(zip(CONFIRMATIONS,captions)):
            check=ttk.Checkbutton(checks,variable=self.confirmations[key],command=self.input_changed)
            check.grid(row=row,column=0,sticky='nw',pady=4)
            self.checks.append(check)
            label=ttk.Label(checks,text=caption,wraplength=800,justify='left')
            label.grid(row=row,column=1,sticky='ew',pady=4)
            self.wrap_labels.append(label)
            label.bind('<Button-1>',lambda event,k=key:self.toggle_confirmation(k))
        self.input_note=ttk.Label(first,wraplength=850,justify='left')
        self.input_note.grid(row=3,column=0,sticky='ew',pady=(8,4))
        self.wrap_labels.append(self.input_note)
        help_frame, help_widget=readonly_text(first,height=6)
        help_frame.grid(row=4,column=0,sticky='nsew',pady=(4,6))
        set_text(help_widget,'Jak przygotować porównanie\n\n'
            '1. W REW otwórz pomiary A i B z jednej serii, dla tego samego miejsca mikrofonu. '
            'Dla A potrzeba co najmniej trzech osobnych pomiarów lewej strony, prawej strony i obu razem. '
            'Dla B wystarczy po jednym; kolejne pomiary B będą agregowane medianą.\n\n'
            '2. Wczytaj te pomiary przez „Z otwartego REW”. Program pobierze SPL, fazę, group delay i impuls. '
            'MDAT nadal służy do porównania SPL; ta ścieżka pełnej analizy czasu wymaga migawki API.\n\n'
            '3. W „Opisz pomiary” przypisz każdemu śladowi A albo B, właściwą sumę L/R/L+R, etap i miejsce kapsuły. '
            'Nazwy pomiarów nie muszą mieć specjalnego formatu.\n\n'
            '4. Wybierz plik i miejsce powyżej, potwierdź warunki i oblicz analizę. '
            'Różne miejsca mikrofonu sprawdzaj osobno. Wynik nie zmienia ustawień sprzętu ani nie zastępuje odsłuchu.')
        self.import_button=ttk.Button(first,text='Wczytaj pomiary z otwartego REW',command=self.import_from_rew)
        self.import_button.grid(row=5,column=0,sticky='w')

        result_page.rowconfigure(4,weight=1)
        self.verdict=ttk.Label(result_page,text='Najpierw wybierz pomiary i uruchom analizę.',wraplength=850,
                               justify='left',font=(host.font_family,11,'bold'))
        self.verdict.grid(row=0,column=0,sticky='ew',pady=(0,8))
        self.wrap_labels.append(self.verdict)
        self.roles=table(result_page,[('role','Co grało',155),('late','Późna energia [dB]',185),
                                     ('t20','Spadek o 20 dB [ms]',210),('gd','GD [ms]',120),
                                     ('body','125–220 Hz [dB]',170)],3)
        self.roles.grid(row=1,column=0,sticky='ew')
        label=ttk.Label(result_page,text='Powtarzalność A — w tabeli: rozrzut / próg. Dodatnia poprawa powyżej oznacza mniej późnej energii albo krótszy czas w B; 125–220 Hz to zmiana podparcia średnicy.',wraplength=850,justify='left')
        label.grid(row=2,column=0,sticky='ew',pady=8)
        self.wrap_labels.append(label)
        self.repeatability=table(result_page,[('role','Co grało',155),('count','Liczba prób',105),
            ('late','Energia [dB]',180),('t20','Spadek 20 dB [ms]',210),('gd','GD [ms]',180)],3)
        self.repeatability.grid(row=3,column=0,sticky='ew')
        details_frame,self.explanation=readonly_text(result_page,height=7)
        details_frame.grid(row=4,column=0,sticky='nsew',pady=(8,0))
        set_text(self.explanation,'Tu pojawi się przyczyna wyniku, następny krok i zakres oceny.')

        data_page.rowconfigure(1,weight=1)
        label=ttk.Label(data_page,text='Wartości policzone z poszczególnych impulsów. Pełny raport zachowuje także identyfikatory źródła i przypisania.',wraplength=850,justify='left')
        label.grid(row=0,column=0,sticky='ew',pady=(0,8))
        self.wrap_labels.append(label)
        self.bands=table(data_page,[('name','Pomiar w REW',230),('config','A / B',70),('role','Co grało',140),('band','Pasmo [Hz]',110),
            ('late','Energia po 100 ms [dB]',180),('t20','Spadek o 20 dB [ms]',170),('duration','Dostępny ogon [ms]',160)],8)
        self.bands.grid(row=1,column=0,sticky='nsew')
        self.data_note=ttk.Label(data_page,wraplength=850,justify='left',text='Wskaźniki zaniku są porównawcze. Nie są pełnym pomiarem waterfall ani T20/RT60 z REW.')
        self.data_note.grid(row=2,column=0,sticky='ew',pady=8)
        self.wrap_labels.append(self.data_note)

        footer=ttk.Frame(self,padding=(12,4,12,10))
        footer.grid(row=2,column=0,sticky='ew')
        footer.columnconfigure(0,weight=1)
        self.status=ttk.Label(footer,text='',wraplength=850,justify='left')
        self.status.grid(row=0,column=0,columnspan=4,sticky='ew',pady=(0,6))
        self.wrap_labels.append(self.status)
        self.run_button=ttk.Button(footer,text='Oblicz pełną analizę',style='Primary.TButton',command=self.run)
        self.run_button.grid(row=1,column=0,sticky='w')
        self.report_button=ttk.Button(footer,text='Otwórz raport',command=self.open_report,state='disabled')
        self.report_button.grid(row=1,column=1,padx=5)
        self.save_button=ttk.Button(footer,text='Zapisz podsumowanie',command=self.save_summary,state='disabled')
        self.save_button.grid(row=1,column=2,padx=5)
        ttk.Button(footer,text='Zamknij',command=self.close).grid(row=1,column=3,padx=(5,0))
        self.source_combo.bind('<<ComboboxSelected>>',self.source_changed)
        self.position_combo.bind('<<ComboboxSelected>>',self.position_changed)
        self.bind('<Configure>',self.resize,add='+')
        self.protocol('WM_DELETE_WINDOW',self.close)
        saved=host.project['ui'].get('time_comparison',{})
        same_pair=(saved.get('a')==self.a and saved.get('b')==self.b and saved.get('stage')==self.stage
                   and saved.get('assignment_fingerprint')==fingerprint(host.project))
        remembered=next((label for label,s in self.source_choices.items() if same_pair and s['id']==saved.get('source_id') and s['sha256']==saved.get('source_sha256')),'')
        self.source_var.set(remembered or (next(iter(self.source_choices)) if len(self.source_choices)==1 else ''))
        self.source_changed()
        if remembered and saved.get('position') in self.position_combo['values']:
            self.position_var.set(saved['position'])
            for key,var in self.confirmations.items():
                var.set(saved.get('confirmations',{}).get(key) is True)
            self.input_changed()
        # An earlier result is not silently shown as a freshly checked source.
        self.status.configure(text='Obliczenie sprawdzi zawartość pliku i zgodność wybranych danych.')

    def resize(self,event=None):
        if event is not None and event.widget is not self:
            return
        for label in self.wrap_labels:
            label.configure(wraplength=max(250,self.winfo_width()-85))

    def source(self):
        return self.source_choices.get(self.source_var.get())

    def source_changed(self,event=None):
        source=self.source()
        positions=sorted({str(t.get('microphone','')).strip() for t in (source or {}).get('traces',[])
            if t.get('stage')==self.stage and (t.get('preset') or source.get('configuration')) in {self.a,self.b}
            and t.get('role') in {'L sum','R sum','L+R'} and str(t.get('microphone','')).strip()})
        self.position_combo['values']=positions
        self.position_var.set(positions[0] if len(positions)==1 else '')
        for var in self.confirmations.values():
            var.set(False)
        self.input_changed()

    def input_changed(self,event=None):
        self.result=None
        self.report_button.configure(state='disabled')
        self.save_button.configure(state='disabled')
        self.verdict.configure(text='Wybór zmieniony — uruchom analizę dla bieżących danych.')
        for tree in (self.roles,self.repeatability,self.bands,self.counts):
            tree.delete(*tree.get_children())
        set_text(self.explanation,'Wynik poprzedniego wyboru nie dotyczy bieżących pomiarów.')
        source=self.source()
        position=self.position_var.get()
        complete=False
        if source:
            groups=selection_counts(source,self.stage,self.a,self.b,position)
            for role in ROLE_ORDER:
                self.counts.insert('','end',values=(ROLE_NAMES[role],len(groups[self.a][role]),len(groups[self.b][role])))
            complete=all(len(groups[self.a][role])>=3 and groups[self.b][role] for role in ROLE_ORDER)
        if not source:
            note='Wybierz jeden plik z A i B. Jeśli masz tylko MDAT, użyj przycisku wczytania z REW poniżej.'
        elif not str(source['path']).lower().endswith('.json'):
            note='Ten plik MDAT pozostaje dostępny do SPL. Do czasu pobierz wybrane pomiary z otwartego REW.'
        elif not source.get('mapping_confirmed'):
            note='Najpierw potwierdź przypisania przez Pomiary → Opisz pomiary.'
        elif not position:
            note='Wybierz miejsce mikrofonu. Jeśli lista jest pusta, przypisz je w Pomiary → Opisz pomiary.'
        elif not complete:
            note='Brakuje pełnego zestawu: dla A co najmniej 3 × L, R i L+R, dla B co najmniej po jednym. Sprawdź przypisania albo wykonaj brakujące pomiary.'
        elif not all(v.get() for v in self.confirmations.values()):
            note='Liczba przypisanych pomiarów jest wystarczająca. Potwierdź powyższe warunki; obliczenie sprawdzi także rzeczywiste dane.'
        else:
            note='Możesz obliczyć analizę. Program sprawdzi impuls, referencję czasu, źródło i powtarzalność A.'
        self.input_note.configure(text=note)
        ready=bool(source and str(source['path']).lower().endswith('.json') and source.get('mapping_confirmed')
                   and position and complete and all(v.get() for v in self.confirmations.values()))
        self.run_button.configure(state='normal' if ready and not self.loading else 'disabled')

    def position_changed(self,event=None):
        for var in self.confirmations.values():
            var.set(False)
        self.input_changed()

    def toggle_confirmation(self,key):
        if not self.loading:
            var=self.confirmations[key]
            var.set(not var.get())
            self.input_changed()

    def request(self):
        source=self.source()
        return {'source_id':source['id'] if source else None,'stage':self.stage,'a':self.a,'b':self.b,
                'position':self.position_var.get(),'confirmations':{key:v.get() for key,v in self.confirmations.items()}}

    def run(self):
        if self.host.job:
            return
        args=self.request()
        source=self.source()
        self.host.project['ui']['time_comparison']={**copy.deepcopy(args),'source_sha256':source['sha256'] if source else None,
                                                  'assignment_fingerprint':fingerprint(self.host.project)}
        self.host.time_result=None
        self.input_changed()
        self.host.start('time_compare',args)
        if self.host.job:
            self.loading=True
            self.status.configure(text='Trwa analiza. Możesz ją przerwać przyciskiem „Przerwij analizę”.')
            self.run_button.configure(text='Przerwij analizę',command=self.host.cancel,state='normal')
            for widget in (self.source_combo,self.position_combo,self.import_button,*self.checks):
                widget.configure(state='disabled')

    def present(self,result):
        self.loading=False
        for widget in (self.source_combo,self.position_combo):
            widget.configure(state='readonly')
        for widget in (self.import_button,*self.checks):
            widget.configure(state='normal')
        self.run_button.configure(text='Oblicz ponownie',command=self.run)
        self.input_changed()
        if result.get('status')=='ok' and not result_is_current(self.host.project,result):
            self.status.configure(text='Wynik dotyczy wcześniejszych przypisań lub ustawień projektu. Uruchom analizę ponownie.')
            return
        args=self.request()
        if not result_matches(result,source_id=args['source_id'],stage=self.stage,config_a=self.a,config_b=self.b,position=args['position']):
            self.status.configure(text='Zakończono wcześniejszy wybór pomiarów. Oblicz analizę dla bieżącego wyboru.')
            return
        self.result=result
        self.status.configure(text='Analiza zakończona.' if result.get('status')=='ok' else 'Analiza wymaga uzupełnienia danych lub została przerwana.')
        self.verdict.configure(text=result.get('message','Brak wyniku.'))
        analysis=result.get('time_analysis')
        text=result.get('message','')+'\n\nNastępny krok: '+result.get('next','')
        if analysis:
            text+='\n\nPrzyczyny:\n'+'\n'.join('• '+x for x in result.get('reasons',[]))
            text+='\n\n'+analysis['scope']
            text+='\n\nPóźna energia: udział po 100 ms. Spadek o 20 dB: porównawczy zanik z impulsu. GD: względne opóźnienie grupowe w 45–110 Hz. '
            text+='W ocenie podparcia średnicy SPL wyrównano w pasmach L / R / L+R: '+' / '.join(str(row['spl_anchor_hz']) for row in analysis['roles'])+'.'
            if analysis['warnings']:
                text+='\n\nOgraniczenia:\n'+'\n'.join('• '+x for x in analysis['warnings'])
            for row in analysis['roles']:
                self.roles.insert('','end',values=(ROLE_NAMES[row['role']],*[self.host.fmt(row[k]) for k in ('late_improvement_db','t20_improvement_ms','gd_improvement_ms','body_125_220_db')]))
            for row in analysis['repeatability']:
                vals=[f"{row[r]:.2f} / {row[t]:.2f}" for r,t in (('late_range_db','late_threshold_db'),('t20_range_ms','t20_threshold_ms'),('gd_range_ms','gd_threshold_ms'))]
                self.repeatability.insert('','end',values=(ROLE_NAMES[row['role']],row['count'],*vals))
            names={r['trace_id']:r['original_name'] for r in analysis['bindings']}
            for row in analysis['bands']:
                self.bands.insert('','end',values=(names[row['trace_id']],'A' if row['configuration']==self.a else 'B',ROLE_NAMES[row['role']],row['band'],
                    *[self.host.fmt(row[k]) for k in ('late100_db','t20_ms','duration_ms')]))
            self.report_button.configure(state='normal' if result.get('time_report_dir') else 'disabled')
            self.save_button.configure(state='normal')
        set_text(self.explanation,text)
        self.tabs.select(1)

    def open_report(self):
        if self.result and self.result.get('time_report_dir') and result_is_current(self.host.project,self.result):
            from REWDO_GUI import folder
            folder(self.result['time_report_dir'])

    def save_summary(self):
        if self.result:
            from rew_user_report import write_summary
            path=filedialog.asksaveasfilename(parent=self,initialfile='REWDO_czas_i_powtarzalnosc.md',defaultextension='.md',filetypes=[('Raport','*.md')])
            if path:
                write_summary(Path(path),self.host.project,self.result)

    def import_from_rew(self):
        if not self.loading:
            self.close()
            self.host.api_dialog()

    def close(self):
        if self.host.time_window is self:
            self.host.time_window=None
        self.destroy()
