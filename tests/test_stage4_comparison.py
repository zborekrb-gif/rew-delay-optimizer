"""Project post-Dirac comparison: physics parity and honest partial results."""
import copy
import json
from pathlib import Path

import numpy as np
import pytest

from rew_api_client import encode_float32_base64
from rew_project import digest, fingerprint, sha256
from rew_project_comparison import compare_post_dirac, ROLES
from rew_workflow import run_task
from test_stage3_project_time import project_for, update_snapshot


def full_project(tmp_path, *, high=300):
    p,args=project_for(tmp_path)
    def change(payload):
        for i,raw in enumerate(payload['measurements']):
            axis=np.arange(20.,float(high)+1,1.)
            level=80. + (20*np.log10(2) if i%3==2 else 0.)
            raw['frequencyResponse']['magnitudes']=encode_float32_base64(np.full(axis.size,level))
            raw['frequencyResponse']['phases']=encode_float32_base64(np.zeros(axis.size))
    update_snapshot(p,change)
    source=p['sources'][0]
    selected={args['config_a']:{r:source['traces'][i]['id'] for i,r in enumerate(ROLES)},
              args['config_b']:{r:source['traces'][9+i]['id'] for i,r in enumerate(ROLES)}}
    kw={k:args[k] for k in ('source_id','config_a','config_b','position','confirmations')}
    kw.update(selected=selected,include_time=True)
    return p,kw


def refresh_ids(p,kw):
    source=p['sources'][0]
    kw['selected']={kw['config_a']:{r:source['traces'][i]['id'] for i,r in enumerate(ROLES)},
                    kw['config_b']:{r:source['traces'][9+i]['id'] for i,r in enumerate(ROLES)}}


def test_full_comparison_merges_measured_and_time_scope(tmp_path):
    p,kw=full_project(tmp_path)
    before=copy.deepcopy(p);path=Path(p['sources'][0]['path']);raw=path.read_bytes()
    r=compare_post_dirac(p,**kw)
    assert r['post_dirac']['decision']=='DO_ODSLUCHU_A_B'
    assert r['time_analysis']['verdict']=='POTENCJALNIE LEPSZY'
    assert all(row['status']=='VERIFIED' for row in r['post_dirac']['phase_checks'].values())
    assert len(r['rows'])==3 and len(r['post_dirac']['metrics'])==11
    assert p==before and path.read_bytes()==raw
    assert r['post_dirac']['position']==r['time_analysis']['position']=='MLP'
    assert r['post_dirac']['source_sha256']==r['time_analysis']['source_sha256']


def test_preserved_post_dirac_numeric_metrics_match(tmp_path):
    from rew_post_dirac_ab import _metrics_for_group, PresetGroup
    from rew_common import Measurement
    from rew_workflow import numeric_records
    p,kw=full_project(tmp_path)
    for c in p['configurations']:c['settings']['fc']=96
    r=compare_post_dirac(p,**kw)
    records=numeric_records(p['sources'][0]['path'])
    ms=[Measurement(x.info.name,x.freq,x.spl_db,x.phase_candidates[0].phase_deg) for x in records[:3]]
    expected=_metrics_for_group(PresetGroup('x','x',96,'BW48',sum_measurement=ms[2],left_measurement=ms[0],right_measurement=ms[1]))
    actual=r['post_dirac']['technical_metrics'][kw['config_a']]
    for key in ['sum_margin_mean_75_125_db','sum_margin_worst_75_125_db','lr_phase_mean_75_125_deg',
                'body_125_220_vs_75_125_db','gd_excess_75_125_ms','mid_texture_160_220_vs_125_160_db']:
        assert actual[key]==pytest.approx(expected[key],abs=1e-10)


def test_unknown_xo_does_not_present_hidden_90hz_values(tmp_path):
    p,kw=full_project(tmp_path)
    r=compare_post_dirac(p,**kw)
    for metrics in r['post_dirac']['technical_metrics'].values():
        assert metrics['sum_margin_at_xo_db'] is None
        assert metrics['lr_phase_at_xo_deg'] is None
        assert metrics['organic_midrange_index_0_100'] is None


@pytest.mark.parametrize('high,available',[(250,[]),(1000,['fullness_120_300_vs_300_1000_db']),
    (4000,['fullness_120_300_vs_300_1000_db','presence_1_4k_vs_300_1000_db']),
    (8000,['fullness_120_300_vs_300_1000_db','presence_1_4k_vs_300_1000_db','glass_4_8k_vs_1_4k_db'])])
def test_missing_frequency_bands_are_not_extrapolated(tmp_path,high,available):
    p,kw=full_project(tmp_path,high=high);kw['include_time']=False
    r=compare_post_dirac(p,**kw)
    for row in r['post_dirac']['metrics']:
        if row['key'] in ['fullness_120_300_vs_300_1000_db','presence_1_4k_vs_300_1000_db','glass_4_8k_vs_1_4k_db']:
            assert (row['A'] is not None)==(row['key'] in available)


def test_titles_and_historical_profiles_do_not_choose_winner(tmp_path):
    p,kw=full_project(tmp_path)
    for c in p['configurations']:c['label']='REF 102 overall reference candidate'
    for t in p['sources'][0]['traces']:t['name']='90 BW48 REF L+R duplicated name'
    r=compare_post_dirac(p,**kw)
    assert r['post_dirac']['decision']=='DO_ODSLUCHU_A_B'
    assert 'zwycięzcy' in r['post_dirac']['scope']


def test_wrong_phase_sum_cannot_be_overruled_by_good_time(tmp_path):
    p,kw=full_project(tmp_path)
    update_snapshot(p,lambda payload:payload['measurements'][11]['frequencyResponse'].update(phases=encode_float32_base64(np.full(281,90.))))
    refresh_ids(p,kw)
    r=compare_post_dirac(p,**kw)
    assert r['post_dirac']['decision']=='UZUPEŁNIJ_DANE_FAZY'
    assert len(r['rows'])==3 and 'time_analysis' not in r


def test_missing_repeats_keep_post_dirac_results_and_explain_time_gap(tmp_path):
    p,kw=full_project(tmp_path)
    for t in p['sources'][0]['traces'][3:9]:t['stage']='Przed Dirac'
    r=compare_post_dirac(p,**kw)
    assert r['post_dirac']['decision']=='UZUPEŁNIJ_POMIAR_CZASU'
    assert '1 z 3' in r['post_dirac']['time_issue']
    assert r['post_dirac']['technical_metrics'] and len(r['rows'])==3


@pytest.mark.parametrize('field,value',[('microphone','elsewhere'),('stage','Przed Dirac'),('preset','old:1')])
def test_selected_trace_must_belong_to_exact_scope(tmp_path,field,value):
    p,kw=full_project(tmp_path);p['sources'][0]['traces'][0][field]=value
    with pytest.raises(ValueError,match='wybierz dokładnie jeden'):compare_post_dirac(p,**kw)


def test_unconfirmed_timing_keeps_spl_and_no_phase_claim(tmp_path):
    p,kw=full_project(tmp_path);kw['confirmations']['timing_reference_consistent']=False
    r=compare_post_dirac(p,**kw)
    assert r['post_dirac']['decision']=='UZUPEŁNIJ_DANE_FAZY'
    assert all(row['delta'] is None for row in r['post_dirac']['metrics'])
    assert r['rows'] and not r.get('time_analysis')


def test_worker_reports_one_identified_complete_result(tmp_path):
    p,kw=full_project(tmp_path)
    args={**kw,'a':kw['config_a'],'b':kw['config_b'],'stage':'Po Dirac'}
    r=run_task(p,tmp_path/'run','full_compare',args,lambda e:None)
    assert r['status']=='ok',r
    assert r['fingerprint']==fingerprint(p)
    target=tmp_path/'run/pelne_porownanie_po_dirac'
    assert len(list(target.iterdir()))==4
    report=(target/'CO_ZROBIC_TERAZ.md').read_text()
    assert 'Co zrobić teraz' in report and 'Powtarzalność odniesienia A' in report
    assert p['sources'][0]['sha256'] in report
    assert json.loads((target/'DANE_I_KONTROLE.json').read_text())['time_analysis']['verdict']=='POTENCJALNIE LEPSZY'


def test_cancelled_full_job_removes_only_its_reports(tmp_path):
    from rew_process import ProcessJob
    from rew_project import new_project
    out=tmp_path/'cancelled';out.mkdir()
    for name in ('pending_full_comparison','pelne_porownanie_po_dirac'):
        p=out/name;p.mkdir();(p/'x.md').write_text('result')
    untouched=tmp_path/'prior_report';untouched.mkdir();(untouched/'keep.md').write_text('keep')
    job=ProcessJob({'project':new_project(),'out':str(out),'task':'full_compare'},out)
    job.cancelled.set();job._worker()
    assert job.events.get(timeout=1)['result']['status']=='cancelled'
    assert not (out/'pelne_porownanie_po_dirac').exists() and not (out/'pending_full_comparison').exists()
    assert (untouched/'keep.md').read_text()=='keep'


def test_source_change_and_duplicate_measurement_id_are_rejected(tmp_path):
    p,kw=full_project(tmp_path)
    path=Path(p['sources'][0]['path']);path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='zmienił zawartość'):compare_post_dirac(p,**kw)
    update_snapshot(p,lambda payload:payload['measurements'][9]['summary'].update(uuid=payload['measurements'][0]['summary']['uuid']))
    refresh_ids(p,kw)
    with pytest.raises(ValueError,match='wielokrotnie'):compare_post_dirac(p,**kw)


def test_single_position_never_claims_spatial_robustness(tmp_path):
    p,kw=full_project(tmp_path);kw['include_time']=False
    r=compare_post_dirac(p,**kw)
    row=next(row for row in r['post_dirac']['domains'] if row['domain']=='Inne miejsca mikrofonu')
    assert row['status']=='Nie oceniono'
    assert not r.get('time_analysis')
    assert 'Czas wybrzmiewania nie został oceniony' in r['next']
