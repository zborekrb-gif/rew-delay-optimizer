"""One guided post-Dirac comparison, with optional full time evidence."""
import copy
import tkinter as tk
from tkinter import ttk, filedialog
from pathlib import Path

from rew_project import fingerprint, result_is_current
from rew_project_comparison import ROLES
from rew_project_time import ROLE_NAMES, SUM_ROLES, selection_counts
from rew_time_gui import TimeAnalysisWindow, set_text


class ComparisonWindow(TimeAnalysisWindow):
    def __init__(self,host,a,b):
        self.selection_vars={}
        self.trace_choices={}
        self.selection_controls=[]
        self.include_time=tk.BooleanVar(master=host,value=True)
        super().__init__(host,a,b,'Po Dirac')
        self.title('Pełne porównanie po Dirac — co zrobić teraz')
        self.tabs.tab(1,text='2. Co zrobić teraz')
        self.tabs.tab(2,text='3. Czas i szczegóły')
        first=self.counts.container.master
        for child in first.winfo_children():
            info=child.grid_info()
            if info and int(info.get('row',0))>=2:child.grid_configure(row=int(info['row'])+1)
        first.rowconfigure(4,weight=0);first.rowconfigure(5,weight=1)
        self.counts.container.grid_remove()
        self.count_note=ttk.Label(first,text='',wraplength=850,justify='left')
        self.count_note.grid(row=1,column=0,sticky='ew',pady=5)
        self.wrap_labels.append(self.count_note)
        frame=ttk.Frame(first);frame.grid(row=2,column=0,sticky='ew',pady=5)
        frame.columnconfigure(1,weight=1);frame.columnconfigure(2,weight=1)
        ttk.Label(frame,text='Konkretny pomiar').grid(row=0,column=0,sticky='w')
        ttk.Label(frame,text='A — odniesienie').grid(row=0,column=1,sticky='w',padx=5)
        ttk.Label(frame,text='B — do sprawdzenia').grid(row=0,column=2,sticky='w',padx=5)
        for i,role in enumerate(ROLES,1):
            ttk.Label(frame,text=ROLE_NAMES[SUM_ROLES[role]]).grid(row=i,column=0,sticky='w',padx=(0,8))
            for col,cfg in ((1,a),(2,b)):
                var=tk.StringVar();self.selection_vars[cfg,role]=var
                combo=ttk.Combobox(frame,textvariable=var,state='readonly',width=1)
                combo.grid(row=i,column=col,sticky='ew',padx=5,pady=2)
                combo.bind('<<ComboboxSelected>>',self.input_changed)
                self.selection_controls.append((cfg,role,combo))
        self.time_check=ttk.Checkbutton(frame,text='Dołącz czas i powtarzalność z tego samego pliku i miejsca (A: min. 3 × L/R/L+R)',
            variable=self.include_time,command=self.input_changed)
        self.time_check.grid(row=4,column=0,columnspan=3,sticky='w',pady=(6,0))
        self.counts.heading('a',text='Dostępne pomiary A');self.counts.heading('b',text='Dostępne pomiary B')
        self.only_time=ttk.Button(first,text='Tylko czas i powtarzalność…',command=self.open_time)
        self.only_time.grid(row=6,column=0,sticky='e')
        help_frame=next(c for c in first.winfo_children() if c.grid_info().get('row')==5)
        help_widget=next(c for c in help_frame.winfo_children() if isinstance(c,tk.Text))
        set_text(help_widget,'1. Wybierz plik i miejsce kapsuły. Przypisania konfiguracji A/B, etapu i ról pochodzą z projektu.\n\n'
            '2. Wskaż konkretne L, R i L+R dla A i B. Pojedynczy dostępny ślad jest wybierany automatycznie; przy kilku wybierz właściwy. '
            'Te sześć pomiarów tworzy porównanie pasm i zgodności sumy stereo.\n\n'
            '3. Potwierdź rzeczywiste warunki serii. Bez potwierdzonej referencji czasowej program pokaże tylko dostępne SPL i wskaże brak.\n\n'
            '4. Analiza czasu jest opcjonalna i korzysta ze wszystkich przypisanych powtórzeń A/B w tym samym pliku i miejscu. '
            'Wymaga impulsów z API REW. Brak czasu jest pokazany osobno; nie staje się pozytywnym werdyktem.\n\n'
            '5. Na karcie „Co zrobić teraz” przeczytaj zalecenie i jego zakres. Własny wybór odsłuchowy zapiszesz w głównym oknie.')
        self.roles.configure(columns=('domain','status','message'),height=4)
        for key,label,width in [('domain','Obszar',175),('status','Wynik',190),('message','Co ustalono',560)]:
            self.roles.heading(key,text=label);self.roles.column(key,width=int(width*host.ui_scale),minwidth=70)
        self.roles.bind('<<TreeviewSelect>>',self.domain_selected)
        self.repeatability.configure(columns=('metric','a','b','delta','unit'),height=3)
        for key,label,width in [('metric','Metryka',510),('a','A',90),('b','B',90),('delta','B−A',100),('unit','Jednostka',90)]:
            self.repeatability.heading(key,text=label);self.repeatability.column(key,width=int(width*host.ui_scale),minwidth=60)
        result_page=self.verdict.master
        for child in result_page.winfo_children():
            info=child.grid_info()
            if info and int(info.get('row',0))>=1:child.grid_configure(row=int(info['row'])+1)
        result_page.rowconfigure(4,weight=0);result_page.rowconfigure(5,weight=1)
        self.next_action=ttk.Label(result_page,text='',wraplength=850,justify='left')
        self.next_action.grid(row=1,column=0,sticky='ew',pady=(0,7))
        self.wrap_labels.append(self.next_action)
        for child in result_page.winfo_children():
            if isinstance(child,ttk.Label) and child not in (self.verdict,self.next_action):
                child.configure(text='Metryki po Dirac. Puste pole oznacza brak wiarygodnych danych lub wymaganego zakresu; B−A nie jest oceną jakości brzmienia.')
        self.run_button.configure(text='Porównaj po Dirac')
        self.source_changed()
        saved=host.project['ui'].get('full_comparison',{})
        if saved.get('a')==a and saved.get('b')==b and saved.get('assignment_fingerprint')==fingerprint(host.project):
            label=next((n for n,s in self.source_choices.items() if s['id']==saved.get('source_id') and s['sha256']==saved.get('source_sha256')),'')
            if label:
                self.source_var.set(label);self.source_changed()
                if saved.get('position') in self.position_combo['values']:
                    self.position_var.set(saved['position']);self.fill_selections()
                    for key,var in self.selection_vars.items():
                        cfg,role=key;identity=saved.get('selected',{}).get(cfg,{}).get(role)
                        var.set(next((n for n,value in self.trace_choices[key].items() if value==identity),var.get()))
                    for key,var in self.confirmations.items():var.set(saved.get('confirmations',{}).get(key) is True)
                    self.include_time.set(saved.get('include_time') is True)
        self.input_changed()

    def source_changed(self,event=None):
        super().source_changed(event)
        if self.selection_vars:self.fill_selections()

    def position_changed(self,event=None):
        super().position_changed(event)
        self.fill_selections()

    def fill_selections(self):
        source=self.source()
        if not source:return
        groups=selection_counts(source,'Po Dirac',self.a,self.b,self.position_var.get())
        for cfg,role,combo in self.selection_controls:
            choices={f"{t['position']}. {t['name']}":t['id'] for t in groups[cfg][SUM_ROLES[role]]}
            self.trace_choices[cfg,role]=choices;combo['values']=list(choices)
            self.selection_vars[cfg,role].set(next(iter(choices)) if len(choices)==1 else '')
        self.input_changed()

    def input_changed(self,event=None):
        if not self.selection_vars:return super().input_changed(event)
        self.result=None
        for tree in (self.roles,self.repeatability,self.bands,self.counts):tree.delete(*tree.get_children())
        self.report_button.configure(state='disabled');self.save_button.configure(state='disabled')
        source=self.source();position=self.position_var.get()
        if source:
            groups=selection_counts(source,'Po Dirac',self.a,self.b,position)
            for role in ('L','R','L+R'):
                self.counts.insert('','end',values=(ROLE_NAMES[role],len(groups[self.a][role]),len(groups[self.b][role])))
            self.count_note.configure(text='Dostępne pomiary — '+ ' | '.join(side+': '+', '.join(role+' '+str(len(groups[cfg][role])) for role in ('L','R','L+R')) for side,cfg in (('A',self.a),('B',self.b))))
        else:self.count_note.configure(text='Brak wybranego źródła.')
        complete=all(var.get() in self.trace_choices.get(key,{}) for key,var in self.selection_vars.items())
        ready=bool(source and position and source.get('mapping_confirmed') and complete and self.confirmations['conditions_consistent'].get())
        if not source:note='Wybierz jeden plik z pomiarami A i B po Dirac.'
        elif not source.get('mapping_confirmed'):note='Najpierw potwierdź przypisania w Pomiary → Opisz pomiary.'
        elif not position:note='Wybierz jedno miejsce mikrofonu. Różnych pozycji nie łączymy w powtórzenia.'
        elif not complete:note='Wybierz sześć konkretnych pomiarów L/R/L+R. Przy kilku śladach w jednej roli program czeka na Twój wybór.'
        elif not self.confirmations['conditions_consistent'].get():note='Potwierdź wspólny tor i głośność główną oraz opisane zmiany A/B.'
        else:note='Możesz uruchomić porównanie. Program oddzieli dostępne pomiary od obszarów, których nie da się ocenić.'
        self.input_note.configure(text=note);self.verdict.configure(text='Wybierz pomiary — wynik poprzedniego wyboru jest nieaktualny.')
        self.next_action.configure(text='')
        set_text(self.explanation,'Po obliczeniu zobaczysz zalecenie, jego przyczynę i zakres. Zaznaczenie obszaru pokaże jego pełny opis.')
        self.run_button.configure(state='normal' if ready and not self.loading else 'disabled')

    def request(self):
        source=self.source()
        selected={cfg:{role:self.trace_choices.get((cfg,role),{}).get(self.selection_vars[cfg,role].get()) for role in ROLES} for cfg in (self.a,self.b)}
        return {'source_id':source['id'] if source else None,'stage':'Po Dirac','a':self.a,'b':self.b,'position':self.position_var.get(),
                'selected':selected,'confirmations':{key:v.get() for key,v in self.confirmations.items()},'include_time':self.include_time.get()}

    def busy(self,value):
        self.loading=value
        for widget in (self.source_combo,self.position_combo,*[c for _,_,c in self.selection_controls]):widget.configure(state='disabled' if value else 'readonly')
        for widget in (self.import_button,self.only_time,self.time_check,*self.checks):widget.configure(state='disabled' if value else 'normal')

    def run(self):
        if self.host.job:return
        args=self.request();source=self.source()
        self.host.project['ui']['full_comparison']={**copy.deepcopy(args),'source_sha256':source['sha256'] if source else None,
                                                   'assignment_fingerprint':fingerprint(self.host.project)}
        self.input_changed();self.host.start('full_compare',args)
        if self.host.job:
            self.busy(True);self.run_button.configure(text='Przerwij porównanie',command=self.host.cancel,state='normal')
            self.status.configure(text='Trwa kontrola danych, porównanie po Dirac i ewentualna analiza czasu.')

    def present(self,result):
        self.busy(False);self.run_button.configure(text='Porównaj ponownie',command=self.run)
        self.input_changed()
        if result.get('full_request')!=self.request():
            self.status.configure(text='Wynik dotyczy innego wyboru pomiarów. Uruchom bieżące porównanie.');return
        if result.get('status')=='ok' and not result_is_current(self.host.project,result):
            self.status.configure(text='Przypisania projektu zmieniły się. Uruchom porównanie ponownie.');return
        self.result=result
        self.verdict.configure(text=result.get('message','Brak wyniku.'))
        self.next_action.configure(text='Co zrobić teraz: '+result.get('next',''))
        p=result.get('post_dirac')
        text='Co zrobić teraz:\n'+result.get('next','')
        if p:
            self.status.configure(text='Porównano wybrane pomiary; zakres i ograniczenia są podane w tabeli.')
            self.domain_details={}
            for i,row in enumerate(p['domains']):
                self.roles.insert('','end',iid=str(i),values=(row['domain'],row['status'],row['message']));self.domain_details[str(i)]=row['message']
            for row in p['metrics']:
                self.repeatability.insert('','end',values=(row['label'],self.host.fmt(row['A']),self.host.fmt(row['B']),self.host.fmt(row['delta']),row['unit']))
            text+='\n\n'+p['scope']+'\n\nZaznacz obszar, aby przeczytać pełny opis. Naturalność, scena i komfort wymagają odsłuchu.'
            time=result.get('time_analysis')
            if time:
                names={b['trace_id']:b['original_name'] for b in time['bindings']}
                for row in time['bands']:
                    self.bands.insert('','end',values=(names[row['trace_id']],'A' if row['configuration']==self.a else 'B',ROLE_NAMES[row['role']],row['band'],
                        *[self.host.fmt(row[k]) for k in ('late100_db','t20_ms','duration_ms')]))
            self.data_note.configure(text=p['time_issue'] or ('Analiza czasu: '+time['verdict'] if time else 'Nie włączono analizy czasu. Dostępne SPL nie określa długości wybrzmiewania.'))
            self.report_button.configure(state='normal' if result.get('full_report_dir') else 'disabled');self.save_button.configure(state='normal')
        else:self.status.configure(text='Uzupełnij wskazany brak; nie utworzono pełnego wyniku.')
        set_text(self.explanation,text);self.tabs.select(1)

    def domain_selected(self,event=None):
        selection=self.roles.selection()
        if selection and self.result:
            set_text(self.explanation,'Co zrobić teraz:\n'+self.result.get('next','')+'\n\nWybrany obszar:\n'+self.domain_details.get(selection[0],''))

    def open_report(self):
        if self.result and self.result.get('full_report_dir') and result_is_current(self.host.project,self.result):
            from REWDO_GUI import folder
            folder(self.result['full_report_dir'])

    def open_time(self):
        if not self.loading:self.close();self.host.time_dialog()

    def close(self):
        if self.host.full_window is self:self.host.full_window=None
        self.destroy()
        self.selection_vars.clear();self.confirmations.clear()
        self.source_var=None;self.position_var=None;self.include_time=None
