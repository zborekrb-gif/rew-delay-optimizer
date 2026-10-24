"""A complete time verdict needs declared conditions and native input integrity."""
import json
from pathlib import Path
import pytest
from rew_time_domain_analyzer import load_time_measurements,write_reports
from test_v20_28_time_domain_repeatability import _full_dataset,_snapshot


def file_for(tmp_path,payload):
    p=tmp_path/'time.rewapi.json';p.write_text(json.dumps(payload));return p


def test_smoothed_response_cannot_bypass_time_loader(tmp_path):
    payload=_snapshot(_full_dataset())
    payload['measurements'][0]['frequencyResponse']['smoothing']='1/6'
    p=file_for(tmp_path,payload)
    with pytest.raises(ValueError):load_time_measurements(p,500.)


def test_missing_native_smoothing_declaration_blocks_loader(tmp_path):
    payload = _snapshot(_full_dataset())
    payload['acquisition'].pop('smoothingRequested')
    with pytest.raises(ValueError, match='smoothingRequested'):
        load_time_measurements(file_for(tmp_path, payload), 500.)


def test_report_rechecks_input_instead_of_trusting_stored_pass(tmp_path):
    p = file_for(tmp_path, _snapshot(_full_dataset()))
    payload, items, warnings = load_time_measurements(p, 500.)
    payload['measurements'][0]['frequencyResponse']['smoothing'] = '1/6'
    out = tmp_path / 'report'
    with pytest.raises(ValueError, match='smoothing'):
        write_reports(out, p, payload, items, 'REF_SLOT2_90BW48_G0', warnings)
    assert not out.exists()


@pytest.mark.parametrize('key',['timingReferenceConsistent','microphoneStationary'])
@pytest.mark.parametrize('value',[False,None])
def test_unconfirmed_session_cannot_publish_positive_verdict(tmp_path,key,value):
    payload=_snapshot(_full_dataset());payload['measurementState'][key]=value
    p=file_for(tmp_path,payload)
    payload,items,warnings=load_time_measurements(p,500.)
    out=tmp_path/'report'
    with pytest.raises(ValueError):write_reports(out,p,payload,items,'REF_SLOT2_90BW48_G0',warnings)
    assert not out.exists()


def test_conflicting_impulse_timing_methods_block_report(tmp_path):
    payload=_snapshot(_full_dataset())
    payload['measurements'][-1]['impulseResponse']['timingReference']='Different reference'
    p=file_for(tmp_path,payload);payload,items,warnings=load_time_measurements(p,500.)
    with pytest.raises(ValueError):write_reports(tmp_path/'report',p,payload,items,'REF_SLOT2_90BW48_G0',warnings)


def test_confirmed_complete_session_retains_existing_verdict(tmp_path):
    p=file_for(tmp_path,_snapshot(_full_dataset()))
    payload,items,warnings=load_time_measurements(p,500.)
    out=tmp_path/'report';write_reports(out,p,payload,items,'REF_SLOT2_90BW48_G0',warnings)
    text=(out/'01_WERDYKT_90BW48_FAST.md').read_text()
    assert 'POTENCJALNIE LEPSZY' in text
    assert (out/'02_POWTARZALNOSC_REFERENCJI.csv').is_file()
