"""Full project integration: explicit identities, unchanged physics, missing data."""
import copy
import json
from pathlib import Path

import numpy as np
import pytest

from rew_api_client import encode_float32_base64
from rew_project import fingerprint, new_project, put_measurement_preset, sha256
from rew_project_time import CONFIRMATIONS, compare_time
from rew_time_domain_analyzer import analyse_variants, load_time_measurements
from rew_workflow import import_source, run_task
from test_v20_28_time_domain_repeatability import _full_dataset, _snapshot


def project_for(tmp_path, *, records=None, state=True):
    payload = _snapshot(records if records is not None else _full_dataset())
    if not state:
        payload.pop('measurementState')
    path = tmp_path / 'session.rewapi.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    p = new_project()
    a = put_measurement_preset(p, 'Odniesienie')
    b = put_measurement_preset(p, 'Do sprawdzenia')
    source = import_source(path)
    source.update(mapping_confirmed=True, stage='Po Dirac')
    for i, trace in enumerate(source['traces']):
        trace.update(preset=a['key'] if i < 9 else b['key'], stage='Po Dirac', microphone='MLP')
    p['sources'].append(source)
    args = dict(source_id=source['id'], stage='Po Dirac', config_a=a['key'], config_b=b['key'],
                position='MLP', confirmations={key: True for key in CONFIRMATIONS})
    return p, args


def update_snapshot(p, change):
    source = p['sources'][0]
    path = Path(source['path'])
    payload = json.loads(path.read_text())
    change(payload)
    path.write_text(json.dumps(payload))
    source['sha256'] = sha256(path)
    from rew_project import digest
    for trace in source['traces']:
        trace['id'] = digest([source['sha256'], trace['position']])


def test_project_comparison_matches_preserved_complete_engine(tmp_path):
    p, args = project_for(tmp_path)
    original = copy.deepcopy(p)
    before = Path(p['sources'][0]['path']).read_bytes()
    result = compare_time(p, **args)
    _, items, _ = load_time_measurements(Path(p['sources'][0]['path']), 500.)
    profiles, variants, *_ = analyse_variants(items, 'REF_SLOT2_90BW48_G0')
    assert result['time_analysis']['verdict'] == variants[0].verdict == 'POTENCJALNIE LEPSZY'
    for row in result['time_analysis']['roles']:
        cmp = variants[0].roles[row['role']]
        assert row['late_improvement_db'] == cmp.late_improvement_db
        assert row['t20_improvement_ms'] == cmp.t20_improvement_ms
        assert row['gd_improvement_ms'] == cmp.gd_improvement_ms
        assert row['significant_good'] == cmp.significant_good
    assert p == original
    assert Path(p['sources'][0]['path']).read_bytes() == before
    assert len(result['time_analysis']['bindings']) == 12
    assert len(result['time_analysis']['bands']) == 60


@pytest.mark.parametrize('options,verdict', [({'side_bad': True}, 'ODRZUĆ — NIESPÓJNOŚĆ L/R'),
                                          ({'thin': True}, 'ODRZUĆ — UTRATA BODY')])
def test_existing_side_and_midrange_protections_remain(tmp_path, options, verdict):
    p, args = project_for(tmp_path, records=_full_dataset(**options))
    assert compare_time(p, **args)['time_analysis']['verdict'] == verdict


def test_original_titles_roles_and_display_labels_do_not_drive_grouping(tmp_path):
    records = _full_dataset()
    for record in records:
        record['summary']['title'] = 'REF 102Hz LR R L+R duplicate misleading name'
        record['role'] = 'R Dirac OFF'
    p, args = project_for(tmp_path, records=records)
    # Actual assignments in the project are authoritative.
    for i, trace in enumerate(p['sources'][0]['traces']):
        trace['role'] = ('L sum', 'R sum', 'L+R')[i % 3]
    for c in p['configurations']:
        c['label'] = 'The same REF_102 name'
    assert compare_time(p, **args)['time_analysis']['verdict'] == 'POTENCJALNIE LEPSZY'


def test_repeats_in_another_position_are_not_merged(tmp_path):
    p, args = project_for(tmp_path)
    p['sources'][0]['traces'][6]['microphone'] = '20 cm left'
    with pytest.raises(ValueError, match='2 z 3'):
        compare_time(p, **args)


def test_stages_and_configuration_versions_are_not_merged(tmp_path):
    p, args = project_for(tmp_path)
    p['sources'][0]['traces'][6]['stage'] = 'Przed Dirac'
    with pytest.raises(ValueError, match='2 z 3'):
        compare_time(p, **args)


def test_incomplete_candidate_has_no_positive_verdict(tmp_path):
    p, args = project_for(tmp_path)
    p['sources'][0]['traces'][-1]['role'] = 'Nieprzypisany'
    with pytest.raises(ValueError, match='B / Obie strony razem: 0 z 1'):
        compare_time(p, **args)


@pytest.mark.parametrize('key', CONFIRMATIONS)
def test_explicit_conditions_are_required(tmp_path, key):
    p, args = project_for(tmp_path)
    args['confirmations'][key] = False
    with pytest.raises(ValueError, match='Potwierdź warunki'):
        compare_time(p, **args)


def test_missing_snapshot_declarations_use_scoped_project_evidence(tmp_path):
    p, args = project_for(tmp_path, state=False)
    result = compare_time(p, **args)
    assert result['time_analysis']['confirmation_scope'].startswith('USER_DECLARATION')
    assert any('Warunki sesji' in x for x in result['time_analysis']['warnings'])
    assert 'measurementState' not in json.loads(Path(p['sources'][0]['path']).read_text())


@pytest.mark.parametrize('key', ['timingReferenceConsistent', 'microphoneStationary'])
def test_false_snapshot_declaration_cannot_be_overridden(tmp_path, key):
    p, args = project_for(tmp_path)
    update_snapshot(p, lambda payload: payload['measurementState'].update({key: False}))
    with pytest.raises(ValueError, match='sprzeczną deklarację'):
        compare_time(p, **args)


def test_same_trace_cannot_be_counted_twice(tmp_path):
    p, args = project_for(tmp_path)
    p['sources'][0]['traces'][6] = copy.deepcopy(p['sources'][0]['traces'][0])
    with pytest.raises(ValueError, match='powtórzony'):
        compare_time(p, **args)


def test_same_rew_measurement_uuid_is_not_an_independent_repeat(tmp_path):
    p, args = project_for(tmp_path)
    update_snapshot(p, lambda payload: payload['measurements'][6]['summary'].update(uuid=payload['measurements'][0]['summary']['uuid']))
    with pytest.raises(ValueError, match='Ponowny import'):
        compare_time(p, **args)


@pytest.mark.parametrize('damage,expected', [
    ('missing_ir', 'brak poprawnego impulsu'), ('zero_ir', 'zerowy'),
    ('nan_ir', 'niepoprawne liczby'), ('short_ir', 'zbyt krótki'),
    ('smooth_gd', 'group delay'), ('short_fr', '25–220'),
    ('different_reference', 'sprzeczne oznaczenia'), ('different_fs', 'częstotliwości próbkowania'),
])
def test_bad_time_inputs_block_verdict(tmp_path, damage, expected):
    p, args = project_for(tmp_path)
    def change(payload):
        raw = payload['measurements'][-1]
        if damage == 'missing_ir': raw.pop('impulseResponse')
        elif damage == 'zero_ir': raw['impulseResponse']['data'] = encode_float32_base64(np.zeros(8000))
        elif damage == 'nan_ir': raw['impulseResponse']['data'] = encode_float32_base64([0., float('nan')] * 4000)
        elif damage == 'short_ir': raw['impulseResponse']['data'] = encode_float32_base64(np.ones(400))
        elif damage == 'smooth_gd': raw['groupDelay']['smoothing'] = '1/6'
        elif damage == 'short_fr': raw['frequencyResponse']['freqStep'] = .5
        elif damage == 'different_reference': raw['impulseResponse']['timingReference'] = 'Other method'
        elif damage == 'different_fs':
            raw['impulseResponse']['sampleRate'] = 16000
            raw['impulseResponse']['sampleInterval'] = 1 / 16000
    update_snapshot(p, change)
    with pytest.raises(ValueError, match=expected):
        compare_time(p, **args)


def test_unstable_reference_repeats_block_report(tmp_path):
    p, args = project_for(tmp_path)
    def change(payload):
        payload['measurements'][6]['groupDelay']['magnitudes'] = encode_float32_base64(np.full(281, .015))
    update_snapshot(p, change)
    with pytest.raises(ValueError, match='Niestabilne odniesienie'):
        compare_time(p, **args)


def test_unrelated_source_is_not_silently_merged(tmp_path):
    p, args = project_for(tmp_path)
    extra = copy.deepcopy(p['sources'][0]); extra['id'] = 'unselected'; extra['path'] = '/missing/unrelated.json'
    p['sources'].append(extra)
    assert compare_time(p, **args)['status'] == 'ok'


def test_worker_writes_identified_result_and_data_failure(tmp_path):
    p, args = project_for(tmp_path)
    worker_args = {**args, 'a': args['config_a'], 'b': args['config_b']}
    result = run_task(p, tmp_path/'run', 'time_compare', worker_args, lambda e: None)
    assert result['status'] == 'ok'
    assert result['fingerprint'] == fingerprint(p)
    assert json.loads((tmp_path/'run/result.json').read_text())['time_analysis']['verdict'] == 'POTENCJALNIE LEPSZY'
    report = tmp_path/'run/pelna_analiza_czasu'
    assert len(list(report.iterdir())) == 5
    text = (report/'WYNIK_I_NASTEPNY_KROK.md').read_text()
    from rew_app_metadata import APP_VERSION
    assert APP_VERSION in text and 'Powtarzalność odniesienia A' in text
    assert '125–220 Hz' in text and 'T20/RT60' in text
    assert p['sources'][0]['sha256'] in text
    worker_args['position'] = 'other'
    blocked = run_task(p, tmp_path/'blocked', 'time_compare', worker_args, lambda e: None)
    assert blocked['status'] == 'needs_data' and 'time_analysis' not in blocked


def test_source_change_requires_reimport(tmp_path):
    p, args = project_for(tmp_path)
    path = Path(p['sources'][0]['path'])
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError, match='zmienił zawartość'):
        compare_time(p, **args)


def test_inconsistent_impulse_clock_is_rejected(tmp_path):
    p, args = project_for(tmp_path)
    update_snapshot(p, lambda payload: payload['measurements'][-1]['impulseResponse'].update(sampleInterval=1/16000))
    with pytest.raises(ValueError, match='różne skale czasu'):
        compare_time(p, **args)


def test_corrupt_gd_does_not_silently_become_phase_derivative(tmp_path):
    p, args = project_for(tmp_path)
    update_snapshot(p, lambda payload: payload['measurements'][-1]['groupDelay'].update(magnitudes='bad-base64'))
    with pytest.raises(ValueError, match='błędne dane group delay'):
        compare_time(p, **args)


def test_absent_gd_uses_preserved_phase_derivative_with_explicit_origin(tmp_path):
    p, args = project_for(tmp_path)
    def change(payload):
        for raw in payload['measurements']:
            raw.pop('groupDelay')
    update_snapshot(p, change)
    result = compare_time(p, **args)
    assert result['status'] == 'ok'
    assert all('pochodna fazy' in row['gd_source_a'] for row in result['time_analysis']['roles'])


def test_report_publish_failure_leaves_no_positive_report(tmp_path, monkeypatch):
    from rew_project_time import write_artifacts
    p, args = project_for(tmp_path)
    result = compare_time(p, **args)
    result.update(task='time_compare', fingerprint=fingerprint(p))
    out = tmp_path/'out'; out.mkdir()
    import rew_user_report
    def fail(*args): raise OSError('simulated write failure')
    monkeypatch.setattr(rew_user_report,'write_summary',fail)
    with pytest.raises(OSError): write_artifacts(out,p,result)
    assert not list(out.iterdir())


def test_cancelled_job_removes_its_time_reports(tmp_path):
    from rew_process import ProcessJob
    out = tmp_path/'cancelled'
    out.mkdir()
    for name in ['pending_time_analysis','pelna_analiza_czasu']:
        folder = out/name; folder.mkdir(); (folder/'report.md').write_text('partial or completed result')
    job = ProcessJob({'project':new_project(),'out':str(out),'task':'time_compare'},out)
    job.cancelled.set()
    job._worker()
    assert job.events.get(timeout=1)['result']['status']=='cancelled'
    assert not (out/'pelna_analiza_czasu').exists() and not (out/'pending_time_analysis').exists()
