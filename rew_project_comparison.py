"""Explicit post-Dirac A/B bindings and a measured-scope next-action summary.

The historical post-Dirac engine and its formulas are not changed. Its name
heuristics and 90/102 listening profiles are not a project configuration ID.
"""
from __future__ import annotations

import copy
import math
import shutil
import csv

import numpy as np

from rew_common import Measurement
from rew_project import digest, file_path, sha256
from rew_project_time import ROLE_NAMES, SUM_ROLES, compare_time, selection_counts
from rew_post_dirac_ab import PresetGroup, _metrics_for_group

ROLES = ('L sum', 'R sum', 'L+R')
METRICS = (
    ('sum_margin_mean_75_125_db', 'Zapas sumowania L+R 75–125 Hz', 'dB'),
    ('sum_margin_worst_75_125_db', 'Słabsze sumowanie L+R 75–125 Hz', 'dB'),
    ('lr_balance_mean_75_125_db', 'Różnica poziomów L/R 75–125 Hz', 'dB'),
    ('lr_phase_mean_75_125_deg', 'Różnica faz L/R 75–125 Hz', '°'),
    ('gd_excess_75_125_ms', 'Nadwyżka opóźnienia grupowego 75–125 Hz', 'ms'),
    ('dip_88_104_vs_shoulders_db', '88–104 Hz względem sąsiednich pasm', 'dB'),
    ('body_125_220_vs_75_125_db', 'Podparcie 125–220 względem 75–125 Hz', 'dB'),
    ('mid_texture_160_220_vs_125_160_db', '160–220 względem 125–160 Hz', 'dB'),
    ('fullness_120_300_vs_300_1000_db', '120–300 względem 300–1000 Hz', 'dB'),
    ('presence_1_4k_vs_300_1000_db', '1–4 kHz względem 300–1000 Hz', 'dB'),
    ('glass_4_8k_vs_1_4k_db', '4–8 kHz względem 1–4 kHz', 'dB'),
)


def _measurement(record, candidate, lo, hi):
    axis = np.unique(np.r_[lo, record.freq[(record.freq>lo)&(record.freq<hi)], hi])
    phase = np.rad2deg(np.interp(axis,candidate.freq,np.unwrap(np.deg2rad(candidate.phase_deg))))
    return Measurement(record.info.name,axis,np.interp(axis,record.freq,record.spl_db),phase)


def _group_metrics(config, records, chosen, lo, hi, position):
    settings=config.get('settings',{})
    fc=settings.get('fc')
    fc=float(fc) if isinstance(fc,(int,float)) and not isinstance(fc,bool) and math.isfinite(fc) and fc>0 else None
    # The legacy model uses 90 internally for at-XO values without a known fc.
    # Those fields are withheld below when the project has no actual fc.
    group=PresetGroup(config['key'],config['key'],fc,settings.get('filter','') if fc else '',
        mic_position=position,mic_position_explicit=True,
        left_measurement=_measurement(records[0],chosen['main'],lo,hi),
        right_measurement=_measurement(records[1],chosen['sub'],lo,hi),
        sum_measurement=_measurement(records[2],chosen['sum'],lo,hi),
        label_source='explicit_project_ids')
    metrics=_metrics_for_group(group)
    metrics['xo_hz']=fc
    if fc is None or not lo<=fc<=hi:
        for key in ('lr_balance_at_xo_db','lr_phase_at_xo_deg','sum_margin_at_xo_db'):
            metrics[key]=None
    coverage={
        'band_25_60_vs_75_125_db':(25,125), 'band_40_60_vs_75_125_db':(40,125),
        'band_60_90_vs_75_125_db':(60,125), 'band_60_100_vs_75_125_db':(60,125),
        'band_90_120_vs_75_125_db':(75,125),'band_100_160_vs_75_125_db':(75,160),
        'band_125_160_vs_75_125_db':(75,160),'band_160_220_vs_75_125_db':(75,220),
        'body_125_220_vs_75_125_db':(75,220),'mid_texture_160_220_vs_125_160_db':(125,220),
        'fullness_120_300_vs_300_1000_db':(120,1000),
        'presence_1_4k_vs_300_1000_db':(300,4000),'glass_4_8k_vs_1_4k_db':(1000,8000),
    }
    for key,(low,high) in coverage.items():
        if lo>low or hi<high:metrics[key]=None
    if hi<8000 or lo>25:metrics['organic_midrange_index_0_100']=None
    return metrics


def _stereo_phase(records):
    from rew_mdat import _select_phase_for_side
    chosen,diagnostic=_select_phase_for_side('L+R',records[0],records[1],[records[2]])
    if diagnostic['status']=='VERIFIED':
        ordinal=diagnostic['references'][0]['sum_candidate']
        chosen['sum']=next(c for c in records[2].phase_candidates if c.ordinal==ordinal)
    return chosen,diagnostic


def compare_post_dirac(project, *, source_id, config_a, config_b, position, selected, confirmations, include_time=True):
    from rew_workflow import numeric_records, clean_result
    configs={c['key']:c for c in project['configurations']}
    if config_a==config_b or config_a not in configs or config_b not in configs:
        raise ValueError('Wybierz dwie różne konfiguracje A i B.')
    sources=[s for s in project['sources'] if s['id']==source_id]
    if len(sources)!=1:raise ValueError('Wybierz jeden plik zawierający pomiary A i B po Dirac.')
    source=sources[0];path=file_path(source['path'])
    if sha256(path)!=source['sha256']:raise ValueError('Plik źródłowy zmienił zawartość. Wczytaj go ponownie.')
    if source.get('mapping_confirmed') is not True:raise ValueError('Potwierdź przypisania w Pomiary → Opisz pomiary.')
    if not isinstance(position,str) or not position.strip():raise ValueError('Wybierz jedno jawnie opisane miejsce mikrofonu.')
    position=position.strip()
    if not isinstance(confirmations,dict) or confirmations.get('conditions_consistent') is not True:
        raise ValueError('Potwierdź ten sam tor i głośność główną oraz opisane zmiany A/B w tej serii.')
    if not isinstance(include_time,bool):raise ValueError('Wybierz, czy dołączyć analizę czasu.')
    groups=selection_counts(source,'Po Dirac',config_a,config_b,position)
    if not isinstance(selected,dict):raise ValueError('Wybierz pomiary L, R i L+R osobno dla A i B.')
    traces,indices,ids=[],set(),set()
    for cfg in (config_a,config_b):
        for role in ROLES:
            trace_id=selected.get(cfg,{}).get(role)
            matches=[t for t in groups[cfg][SUM_ROLES[role]] if t.get('id')==trace_id]
            if len(matches)!=1:
                raise ValueError(f"{'A' if cfg==config_a else 'B'} / {role}: wybierz dokładnie jeden pomiar dla tej konfiguracji, etapu i miejsca.")
            trace=matches[0];index=trace.get('position')
            if (isinstance(index,bool) or not isinstance(index,int) or index in indices
                    or trace_id in ids or trace_id!=digest([source['sha256'],index])):
                raise ValueError('Powtórzony albo niezgodny identyfikator pomiaru. Sprawdź przypisanie śladów.')
            indices.add(index);ids.add(trace_id);traces.append((cfg,role,trace))
    records={int(r.info.position):r for r in numeric_records(path)}
    if not indices<=set(records):raise ValueError('Wybrany ślad nie występuje w pliku. Wczytaj źródło ponownie.')
    ordered=[records[t['position']] for _,_,t in traces]
    lo=max(float(r.freq[0]) for r in ordered);hi=min(float(r.freq[-1]) for r in ordered)
    if hi<=lo:raise ValueError('Wybrane pomiary nie mają wspólnego zakresu częstotliwości.')
    rows,curves=[],[]
    for i,role in enumerate(ROLES):
        a,b=ordered[i],ordered[i+3]
        axis=np.geomspace(lo,hi,800)
        spl_a=np.interp(axis,a.freq,a.spl_db);spl_b=np.interp(axis,b.freq,b.spl_db)
        differences={name:float(np.mean((spl_b-spl_a)[(axis>=low)&(axis<=high)])) if lo<=low and hi>=high else None
            for name,low,high in (('30–60 Hz',30,60),('80–110 Hz',80,110),('100–220 Hz',100,220))}
        rows.append({'position':position,'role':role,'delta_db':differences,'repeats':[1,1]})
        curves.append({'position':position,'role':role,'frequency':axis.tolist(),'A':spl_a.tolist(),'B':spl_b.tolist()})

    phase={};metrics={};phase_ok=confirmations.get('timing_reference_consistent') is True and confirmations.get('microphone_stationary') is True
    state_issue=None
    from rew_api_client import is_rew_api_snapshot, read_snapshot
    if is_rew_api_snapshot(path):
        payload=read_snapshot(path);state=payload.get('measurementState') or {}
        if not isinstance(state,dict):state_issue='Niepoprawne deklaracje sesji w migawce.'
        elif any(state.get(k) is not None and state.get(k) is not True for k in ('timingReferenceConsistent','microphoneStationary')):
            state_issue='Migawka zawiera sprzeczną deklarację referencji czasu lub położenia mikrofonu.'
        selected_uuids=[]
        for _,_,trace in traces:
            summary=payload['measurements'][trace['position']-1].get('summary') or {}
            value=str(summary.get('uuid') or summary.get('id') or '').strip()
            if value:selected_uuids.append(value)
        if len(selected_uuids)!=len(set(selected_uuids)):raise ValueError('Ten sam pomiar REW przypisano wielokrotnie do A/B. Wybierz osobne pomiary.')
    if state_issue:phase_ok=False
    for i,cfg in enumerate((config_a,config_b)):
        if phase_ok and lo<=35 and hi>=220:
            chosen,diag=_stereo_phase(ordered[i*3:i*3+3])
            phase[cfg]=diag
            if diag['status']=='VERIFIED':metrics[cfg]=_group_metrics(configs[cfg],ordered[i*3:i*3+3],chosen,lo,hi,position)
        else:
            phase[cfg]={'status':'UNAVAILABLE','reason':state_issue or ('Potwierdź wspólną metodę referencji czasu i stały mikrofon.' if not phase_ok else 'Do tych metryk potrzebny zakres obejmujący 35–220 Hz.')}
    phase_verified=all(phase[c]['status']=='VERIFIED' for c in (config_a,config_b))
    metric_rows=[]
    for key,label,unit in METRICS:
        a=metrics.get(config_a,{}).get(key);b=metrics.get(config_b,{}).get(key)
        a=float(a) if isinstance(a,(int,float)) and math.isfinite(a) else None
        b=float(b) if isinstance(b,(int,float)) and math.isfinite(b) else None
        metric_rows.append({'key':key,'label':label,'unit':unit,'A':a,'B':b,'delta':b-a if a is not None and b is not None else None})
    time_result=None;time_issue=None
    if include_time:
        if phase_verified:
            try:
                time_result=compare_time(project,source_id=source_id,stage='Po Dirac',config_a=config_a,config_b=config_b,
                                         position=position,confirmations=confirmations)
            except ValueError as exc:time_issue=str(exc)
        else:time_issue='Najpierw potwierdź zgodność fazy i sumy stereo; wynik czasu nie zastępuje tej kontroli.'
    domains=[{'domain':'Poziomy i pasma','status':'Dostępne SPL','message':f'Rzeczywiste B−A dla wybranych L, R i L+R; wspólny zakres {lo:g}–{hi:g} Hz, bez wyrównania poziomu.'},
             {'domain':'Faza i suma stereo','status':'Sprawdzono' if phase_verified else 'Nie można ocenić',
              'message':'Zespolone L+R odtwarzają zmierzoną sumę w obu ustawieniach.' if phase_verified else 'Nie potwierdzono zgodności w A lub B. Dostępne SPL nie daje zgody na dobór fazy.'},
             {'domain':'Średnica i góra','status':'Zakres dostępny' if lo<=120 and hi>=8000 else 'Ograniczony zakres',
              'message':'Metryki pasmowe nie są pomiarem naturalności ani szklistości brzmienia.' if hi>=8000 else 'Do pełnego porównania pasm 120 Hz–8 kHz potrzebne szersze pomiary; brakujące wartości pozostają puste.'},
             {'domain':'Czas i powtarzalność','status':time_result['time_analysis']['verdict'] if time_result else 'Brak danych' if include_time else 'Nie włączono',
              'message':time_result['message'] if time_result else time_issue or 'Nie oceniono długości wybrzmiewania z samego SPL.'},
             {'domain':'Inne miejsca mikrofonu','status':'Nie oceniono','message':'Ta decyzja dotyczy wyłącznie pozycji '+position+'. Nie potwierdza odporności przestrzennej.'},
             {'domain':'Odsłuch','status':'Decyzja użytkownika','message':'Wokal, fundament, scena i zmęczenie wymagają odsłuchu A/B przy wyrównanej głośności.'}]
    if not phase_verified:
        decision='UZUPEŁNIJ_DANE_FAZY';message='Porównano SPL. Pełna ocena wymaga sprawdzenia fazy i zgodności sumy stereo.'
        next_action='Sprawdź przypisanie L/R/L+R, warunki sesji i referencję czasu. W razie niejednoznacznej fazy MDAT pobierz te pomiary z otwartego REW.'
    elif include_time and not time_result:
        decision='UZUPEŁNIJ_POMIAR_CZASU';message='Porównano pasma i sumę stereo. Do wspólnej oceny brakuje wiarygodnej analizy czasu.'
        next_action=time_issue
    elif time_result and time_result['time_analysis']['verdict']=='DANE CZASOWE NIESPÓJNE':
        decision='SPRAWDZ_SESJE';message='Odniesienie czasu A i B jest niespójne. Nie rozstrzygnięto poprawy.';next_action=time_result['next']
    elif time_result and time_result['time_analysis']['verdict'] in {'ODRZUĆ — UTRATA BODY','ODRZUĆ — NIESPÓJNOŚĆ L/R','GORSZY CZASOWO'}:
        decision='SPRAWDZ_USTAWIENIE_B';message=time_result['message'];next_action=time_result['next']
    else:
        decision='DO_ODSLUCHU_A_B';message='Pomiary pozwalają przejść do odsłuchu A/B w pokazanym zakresie.'
        next_action='Wyrównaj głośność odsłuchu. Porównaj wokal i instrumenty akustyczne, podparcie średnicy, fundament i kontrolę basu oraz zmęczenie. Zapisz własny wybór.'
        if not include_time:next_action+=' Czas wybrzmiewania nie został oceniony.'
    if hi<8000:next_action+=' Do oceny pasm średnicy i góry uzupełnij pomiary co najmniej do 8 kHz.'
    next_short={
        'UZUPEŁNIJ_DANE_FAZY':'Sprawdź przypisania i wspólną referencję czasu. Szczegóły są w pełnym porównaniu.',
        'UZUPEŁNIJ_POMIAR_CZASU':'Uzupełnij impulsy lub powtórzenia A wskazane w pełnym porównaniu.',
        'SPRAWDZ_SESJE':'Powtórz kontrolę odniesienia czasu A/B.',
        'SPRAWDZ_USTAWIENIE_B':'Zachowaj A i sprawdź wskazane pogorszenie B.',
        'DO_ODSLUCHU_A_B':'Wyrównaj głośność odsłuchu i zapisz własny wybór A/B.',
    }[decision]
    bindings=[{'configuration':cfg,'role':role,'trace_id':t['id'],'index':t['position'],'original_name':t['name']} for cfg,role,t in traces]
    if sha256(path)!=source['sha256']:raise ValueError('Źródło zmieniło się podczas porównania. Nie zatwierdzono wyniku.')
    result={'status':'ok','message':message,'next':next_action,'next_short':next_short,'stage':'Po Dirac','config_a':config_a,'config_b':config_b,
            'rows':rows,'curves':curves,'summaries':[d['domain']+': '+d['message'] for d in domains],
            'reasons':[],'post_dirac':{'source_id':source_id,'source_sha256':source['sha256'],'source_name':source['name'],
                'position':position,'range_hz':[lo,hi],'decision':decision,'domains':domains,'metrics':metric_rows,
                'technical_metrics':metrics,'phase_checks':phase,'bindings':bindings,
                'confirmations':copy.deepcopy(confirmations),'time_requested':include_time,'time_issue':time_issue,
                'scope':'Wybrane pojedyncze L/R/L+R; analiza czasu używa wszystkich przypisanych powtórzeń A/B z tego samego pliku, etapu i miejsca. Historyczne profile 90/102 i nazwy pomiarów nie ustalają zwycięzcy.'}}
    if time_result:result['time_analysis']=time_result['time_analysis']
    return clean_result(result)


def report_lines(result):
    p=result['post_dirac']
    def safe(value):return str(value).replace('|','/').replace('\n',' ')
    def fmt(value):return '—' if value is None else f'{value:+.2f}'
    lines=['','## Co zrobić teraz','',result['message'],'',result['next'],'',
           f"Miejsce: {safe(p['position'])}. Plik: {safe(p['source_name'])}. Zakres wspólny: {p['range_hz'][0]:g}–{p['range_hz'][1]:g} Hz.",'',
           '| Obszar | Wynik | Co ustalono |','|---|---|---|']
    for row in p['domains']:lines.append('| '+' | '.join(safe(row[k]) for k in ('domain','status','message'))+' |')
    lines+=['','## Metryki po Dirac','',
            'Wartości pasmowe i różnice B−A. Wskaźniki względne mają pasmo odniesienia podane w nazwie; nie zastępują surowych różnic SPL ani wyrównania głośności odsłuchu.',
            '','| Metryka | Jednostka | A | B | B−A |','|---|---|---:|---:|---:|']
    for row in p['metrics']:lines.append('| '+' | '.join([safe(row['label']),row['unit'],fmt(row['A']),fmt(row['B']),fmt(row['delta'])])+' |')
    lines+=['',p['scope'],'','To ocena techniczna i plan sprawdzenia. Wniosek o naturalności średnicy, scenie i komforcie wymaga odsłuchu.',
            '',f"SHA-256 źródła: `{p['source_sha256']}`"]
    return lines


def write_artifacts(out,project,result):
    from rew_user_report import write_summary
    from rew_project import atomic_json
    pending=out/'pending_full_comparison';target=out/'pelne_porownanie_po_dirac'
    if pending.exists() or target.exists():raise ValueError('Folder pełnego porównania już istnieje. Uruchom nowy przebieg.')
    pending.mkdir()
    try:
        write_summary(pending/'CO_ZROBIC_TERAZ.md',project,result)
        for name,rows in [('metryki_po_dirac.csv',result['post_dirac']['metrics']),('powiazanie_pomiarow.csv',result['post_dirac']['bindings'])]:
            with (pending/name).open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter=';');w.writeheader();w.writerows(rows)
        atomic_json(pending/'DANE_I_KONTROLE.json',{'post_dirac':result['post_dirac'],'time_analysis':result.get('time_analysis')})
        pending.rename(target)
    except Exception:
        shutil.rmtree(pending);raise
    return str(target)
