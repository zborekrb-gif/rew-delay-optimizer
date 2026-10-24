from __future__ import annotations
import csv
import hashlib
import json
import math
from pathlib import Path
import pytest
import rew_prediction_measurement_calibrator as cal


def prediction(path,rows=None,fields=None):
    fields=fields or ['custom_code','side_l_base','side_r_base','technical_base_simulation']
    rows=rows or [{'custom_code':'K1','side_l_base':'70','side_r_base':'80','technical_base_simulation':'75'}]
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    return path


@pytest.fixture
def files(tmp_path,monkeypatch):
    p=prediction(tmp_path/'prediction.csv')
    m=tmp_path/'measured.mdat';m.write_bytes(b'controlled input; measurement function replaced only in write-safety tests')
    j=tmp_path/'calibration.json';j.write_text(json.dumps({'schema_version':2,'enabled':False,'custom_biquad_records':[]}))
    monkeypatch.setattr(cal,'_measured_metrics',lambda *a:{'side_l':68.,'side_r':77.,'technical':73.,'lr_sum':74.,'phase_l':75.,'phase_r':76.})
    return dict(prediction_csv=p,candidate_code='K1',measured_mdat=m,xo_hz=90.,topology_id='T1',state_id='S1',calibration_json=j,out_json=tmp_path/'new.json')


def test_wrong_single_candidate_is_rejected(tmp_path):
    p=prediction(tmp_path/'p.csv')
    with pytest.raises(ValueError):cal._read_prediction(p,'OTHER')


@pytest.mark.parametrize('field',['side_l_base','side_r_base','technical_base_simulation'])
@pytest.mark.parametrize('value',['NaN','Inf','-Infinity'])
def test_nonfinite_prediction_is_rejected(tmp_path,field,value):
    row={'custom_code':'K1','side_l_base':'70','side_r_base':'80','technical_base_simulation':'75'};row[field]=value
    p=prediction(tmp_path/'p.csv',[row])
    with pytest.raises(ValueError):cal._read_prediction(p,'K1')


def test_missing_candidate_code_is_not_inferred(tmp_path):
    p=prediction(tmp_path/'p.csv',[{'side_l_base':'70','side_r_base':'80','technical_base_simulation':'75'}],['side_l_base','side_r_base','technical_base_simulation'])
    with pytest.raises(ValueError):cal._read_prediction(p,'K1')


def test_duplicate_candidate_is_rejected(tmp_path):
    row={'custom_code':'K1','side_l_base':'70','side_r_base':'80','technical_base_simulation':'75'}
    p=prediction(tmp_path/'p.csv',[row,row])
    with pytest.raises(ValueError):cal._read_prediction(p,'K1')


def test_supported_column_aliases_preserve_values(tmp_path):
    p=prediction(tmp_path/'p.csv',[{'custom_code':'K1','side_l_base_simulation_0_100':'70','side_r_base_simulation_0_100':'80','technical_base_simulation_0_100':'75'}],['custom_code','side_l_base_simulation_0_100','side_r_base_simulation_0_100','technical_base_simulation_0_100'])
    assert cal._read_prediction(p,'K1')=={'side_l':70.,'side_r':80.,'technical':75.}


def test_conflicting_alias_values_are_rejected(tmp_path):
    row={'custom_code':'K1','side_l_base':'70','side_l_base_simulation_0_100':'71','side_r_base':'80','technical_base_simulation':'75'}
    p=prediction(tmp_path/'p.csv',[row],list(row))
    with pytest.raises(ValueError):cal._read_prediction(p,'K1')


def test_valid_update_has_correct_bias_provenance_and_keeps_sources(files):
    before={k:v.read_bytes() for k,v in files.items() if isinstance(v,Path) and k!='out_json'}
    result=cal.update_calibration(**files)
    d=json.loads(result.read_text());row=d['custom_biquad_records'][0]
    assert row['bias_side_l']==-2 and row['bias_side_r']==-3 and row['bias_technical']==-2
    assert d['custom_biquad']['independent_topology_count']==1
    assert d['enabled'] is False
    assert row['prediction_csv_sha256']==hashlib.sha256(before['prediction_csv']).hexdigest()
    assert row['measured_mdat_sha256']==hashlib.sha256(before['measured_mdat']).hexdigest()
    assert row['calibration_input_sha256']==hashlib.sha256(before['calibration_json']).hexdigest()
    assert result.with_suffix('.md').is_file()
    for key,data in before.items():assert files[key].read_bytes()==data


@pytest.mark.parametrize('key',['candidate_code','topology_id','state_id'])
def test_blank_identity_never_writes(files,key):
    files[key]='  '
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert not files['out_json'].exists()


@pytest.mark.parametrize('value',[float('nan'),float('inf'),0,-1])
def test_invalid_frequency_never_writes(files,value):
    files['xo_hz']=value
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert not files['out_json'].exists()


def test_input_cannot_be_overwritten(files):
    before=files['calibration_json'].read_bytes();files['out_json']=files['calibration_json']
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert files['calibration_json'].read_bytes()==before


@pytest.mark.parametrize('suffix',['.json','.md'])
def test_existing_result_is_preserved(files,suffix):
    target=files['out_json'].with_suffix(suffix);target.write_bytes(b'existing result')
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert target.read_bytes()==b'existing result'


@pytest.mark.parametrize('where',['prediction','measurement','history'])
def test_invalid_numeric_data_does_not_publish_partial_result(files,monkeypatch,where):
    if where=='prediction':
        prediction(files['prediction_csv'],[{'custom_code':'K1','side_l_base':'NaN','side_r_base':'80','technical_base_simulation':'75'}])
    elif where=='measurement':
        monkeypatch.setattr(cal,'_measured_metrics',lambda *a:{'side_l':float('nan'),'side_r':77.,'technical':73.})
    else:
        files['calibration_json'].write_text('{"enabled":false,"value":1e999}')
    before=files['calibration_json'].read_bytes()
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert not files['out_json'].exists() and not files['out_json'].with_suffix('.md').exists()
    assert files['calibration_json'].read_bytes()==before


def test_nonfinite_existing_bias_is_rejected(files):
    files['calibration_json'].write_text(json.dumps({'custom_biquad_records':[{'topology_id':'T0','state_id':'S0','candidate_code':'OLD','bias_side_l':float('nan'),'bias_side_r':0,'bias_technical':0}]}))
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert not files['out_json'].exists()


def test_source_changed_during_measurement_is_rejected(files,monkeypatch):
    def measured(*args):
        files['prediction_csv'].write_text('changed source')
        return {'side_l':68.,'side_r':77.,'technical':73.}
    monkeypatch.setattr(cal,'_measured_metrics',measured)
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert not files['out_json'].exists()


def test_same_state_cannot_silently_move_to_another_topology(files):
    files['calibration_json'].write_text(json.dumps({'custom_biquad_records':[{'topology_id':'OTHER','state_id':'S1','candidate_code':'K1','bias_side_l':1,'bias_side_r':2,'bias_technical':3}]}))
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert not files['out_json'].exists()


def test_same_configuration_state_is_updated_once(files):
    files['calibration_json'].write_text(json.dumps({'enabled':False,'custom_biquad_records':[{'topology_id':'T1','state_id':'S1','candidate_code':'K1','bias_side_l':1,'bias_side_r':2,'bias_technical':3}]}))
    d=json.loads(cal.update_calibration(**files).read_text())
    assert len(d['custom_biquad_records'])==1
    assert d['custom_biquad']['validation_state_count']==1
    assert d['custom_biquad_records'][0]['bias_side_l']==-2


def test_publish_failure_rolls_back_new_outputs(files,monkeypatch):
    real_link=cal.os.link
    def fail_json(source,target,*args,**kwargs):
        if str(target).endswith('.json'):raise OSError('simulated publication failure')
        return real_link(source,target,*args,**kwargs)
    monkeypatch.setattr(cal.os,'link',fail_json)
    before=files['calibration_json'].read_bytes()
    with pytest.raises(ValueError):cal.update_calibration(**files)
    assert not files['out_json'].exists() and not files['out_json'].with_suffix('.md').exists()
    assert files['calibration_json'].read_bytes()==before
    assert not list(files['out_json'].parent.glob('*.tmp'))


def test_complete_api_measurement_updates_calibration_without_mocks(tmp_path):
    import numpy as np
    from test_v20_32_workflow import snapshot
    f=np.arange(20,500.1,.5)
    main=np.ones_like(f,dtype=complex)*1000
    sub=np.ones_like(f,dtype=complex)*500
    m=tmp_path/'measurement.rewapi.json'
    snapshot(m,[('L main',main),('L sub',sub),('L strona',main+sub),
                ('R main',main),('R sub',sub),('R strona',main+sub),('L+R',2*(main+sub))])
    old=tmp_path/'old.json';old.write_text('{"schema_version":2,"enabled":false,"custom_biquad_records":[]}')
    p=prediction(tmp_path/'prediction.csv')
    target=tmp_path/'new.json'
    result=cal.update_calibration(prediction_csv=p,candidate_code='K1',measured_mdat=m,xo_hz=90.,topology_id='CONTROLLED_BW48',state_id='S1',calibration_json=old,out_json=target)
    d=json.loads(result.read_text());r=d['custom_biquad_records'][0]
    assert r['measured_side_l']==pytest.approx(100.) and r['measured_side_r']==pytest.approx(100.)
    assert r['bias_side_l']==pytest.approx(30.) and r['bias_side_r']==pytest.approx(20.)
    assert d['enabled'] is False
