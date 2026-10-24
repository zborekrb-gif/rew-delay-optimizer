from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import numpy as np
import pytest

from rew_project import *
from rew_workflow import (import_source, numeric_records, compare, verify_reference, check_measurements,
                           _write_anchor, _raw_and_gain, design, export_candidate, run_task)
from rew_source_binding import bound_sources
from rew_api_client import encode_float32_base64
from rew_filter_generator import make_sections, freq_response
from rew_reference_registry import load_reference_record, find_reference_conflicts


def snapshot(path, responses):
    records=[]
    for index,(role,complex_response) in enumerate(responses):
        records.append({"summary":{"title":f"Pomiar_{index}"},"role":role,
            "frequencyResponse":{"smoothing":"None","startFreq":20.,"freqStep":.5,
                "magnitudes":encode_float32_base64(20*np.log10(np.maximum(np.abs(complex_response),1e-15))),
                "phases":encode_float32_base64(np.angle(complex_response,deg=True))},
            "decodedInfo":{"ppoRequested":None}})
    payload={"format":"REWDO_REW_API_SNAPSHOT","formatVersion":3,
        "acquisition":{"nativeAxisRequested":True,"ppoRequested":None,"noSmoothingRequested":True,"smoothingRequested":"None"},"measurements":records}
    atomic_json(path,payload)


def assign(source,stage,config="",level="raw"):
    source.update(stage=stage,configuration=config,mapping_confirmed=True,raw_confirmed=stage=="Bazowe",session_confirmed=True,gain_state=level)
    for trace in source["traces"]:
        trace.update(stage=stage,preset=config,microphone="MLP")
    return source


@pytest.fixture
def valid_project(tmp_path):
    p=new_project()
    cfg=put_config(p,{"fc":90,"filter":"BW48","l_main_ms":15,"r_main_ms":15,"l_sub_ms":15,"r_sub_ms":15},"A",console_confirmed=True)
    p["gains"]={k:{"L":v,"R":v,"origin":"Konsola, syntetyczna sesja testowa"} for k,v in (("raw",-6.),("dirac",-4.),("listening",0.))}
    p["analysis"].update(confirmed=True,fc_low=90,fc_high=90)
    f=np.arange(20,500.1,.5)
    m=np.ones_like(f,dtype=complex)*10**(70/20)
    s=np.ones_like(f,dtype=complex)*10**(64/20)
    responses=[("L main",m),("L sub",s),("R main",m),("R sub",s)]
    responses += [(role,m+s) for role in ("L sum","L sum","R sum","R sum")]
    snapshot(tmp_path/"raw.json",responses)
    snapshot(tmp_path/"bridge.json",[("L sum",m+s*10**(.3)),("R sum",m+s*10**(.3))])
    hm=freq_response(make_sections("HP",96000,90,"BW48"),96000,f)
    hs=freq_response(make_sections("LP",96000,90,"BW48"),96000,f)
    snapshot(tmp_path/"reference.json",[("L main",m*hm),("R main",m*hm),("L sub",s*hs),("R sub",s*hs),("L sum",m*hm+s*hs),("R sum",m*hm+s*hs)])
    p["sources"]=[assign(import_source(tmp_path/"raw.json"),"Bazowe"),assign(import_source(tmp_path/"bridge.json"),"Zmiana poziomu",level="listening"),assign(import_source(tmp_path/"reference.json"),"Przed Dirac",cfg["key"])]
    return p


@pytest.mark.parametrize("value",["",None,"NaN","inf","-Infinity"])
def test_number_rejects_missing_and_nonfinite(value):
    with pytest.raises(ValueError):
        number(value)


def test_comma_and_decimal_are_equivalent():
    assert number("20,43")==number("20.43")


@pytest.mark.parametrize("value",["",None,"."])
def test_file_path_rejects_empty_and_directories(value):
    with pytest.raises(ValueError):
        file_path(value)


def test_gains_are_not_defaulted():
    with pytest.raises(ValueError):
        common_gains(new_project())


def test_gains_other_than_historical_are_used(valid_project):
    for k,v in (("raw",-9),("dirac",-7),("listening",-3)):
        valid_project["gains"][k].update(L=v,R=v)
    with bound_sources(valid_project["sources"]):
        _,_,g,state,_,_=_raw_and_gain(valid_project)
    assert g=={"raw":-9,"dirac":-7,"listening":-3}
    assert state.raw_db==-9 and state.dirac_db==-7 and state.listening_db==-3
    assert state.side_delta_db("dirac","L")==pytest.approx(2,abs=.01)


def test_different_sides_are_preserved_not_averaged(valid_project):
    valid_project["gains"]["raw"]["R"]=-5
    with pytest.raises(ValueError,match="różne poziomy"):
        common_gains(valid_project)
    assert valid_project["gains"]["raw"]["R"]==-5


def test_unchanged_gain_cannot_bypass_bridge(valid_project):
    valid_project["gains"]["listening"].update(L=-6,R=-6)
    with pytest.raises(ValueError,match="Niezmienione"):
        common_gains(valid_project)


def test_name_invariance_and_explicit_listening(valid_project):
    p=valid_project
    token=fingerprint(p)
    for suffix in ("_82","_96","_102"):
        q=copy.deepcopy(p)
        q["configurations"][0]["label"]="A"+suffix
        for s in q["sources"]:
            s["name"]="A"+suffix
            for t in s["traces"]:
                t["name"]="A"+suffix
        assert fingerprint(q)==token
        assert not q["listening"]


def test_legacy_name_penalties_removed():
    from rew_post_dirac_ab import _apply_listening_profile,_load_listening_profile_entries
    for label in ("A","A_82","A_96","A_102"):
        row={"label":label,"source_names":label}
        assert _apply_listening_profile(row,_load_listening_profile_entries(),None)==0
        assert row["listening_profile_status"]==""


def test_reference_evidence_is_relative_and_recomputed(valid_project,tmp_path):
    e=verify_reference(valid_project)
    assert e["scope"]=="CONSOLE_AND_RELATIVE_SUM"
    path=tmp_path/"anchor.yaml"
    _write_anchor(valid_project,e,path)
    record=load_reference_record(path)
    assert record.anchor_allowed
    assert record.relative_delays_ms==(0,0)
    valid_project["configurations"][0]["settings"]["l_sub_ms"]-=3
    with pytest.raises(ValueError,match="nie odtwarza"):
        verify_reference(valid_project)


def test_txt_never_verifies_reference(tmp_path):
    from rew_verify_user_slot import _write_user_reference_yaml
    path=tmp_path/"anything.txt"
    path.write_text("not a measurement")
    with pytest.raises(ValueError):
        _write_user_reference_yaml(tmp_path/"reference.yaml",l_main=30,r_main=30,l_sub=20.4,r_sub=19.1,l_pol="OFF",r_pol="OFF",xo=90,filt="BW48",verification_source=path)


def test_valid_attachment_alone_is_not_verified(valid_project,tmp_path):
    from rew_verify_user_slot import _write_user_reference_yaml
    path=_write_user_reference_yaml(tmp_path/"reference.yaml",l_main=30,r_main=30,l_sub=20.4,r_sub=19.1,l_pol="OFF",r_pol="OFF",xo=90,filt="BW48",verification_source=valid_project["sources"][2]["path"])
    r=load_reference_record(path)
    assert not r.anchor_allowed and not r.verified
    assert "wokale" not in path.read_text()


def test_missing_sum_blocks_only_reference(valid_project):
    ref=valid_project["sources"][2]
    ref["traces"]=[dict(t,role="Nieprzypisany") if t["role"]=="R sum" else t for t in ref["traces"]]
    with pytest.raises(ValueError,match="sum"):
        verify_reference(valid_project)
    assert numeric_records(ref["path"])


def test_config_versions_do_not_conflict(valid_project,tmp_path):
    cfg=active_config(valid_project)
    evidence=verify_reference(valid_project)
    a=tmp_path/"a.yaml"
    _write_anchor(valid_project,evidence,a)
    values=copy.deepcopy(cfg["settings"])
    values["r_sub_ms"]=19.1
    second=put_config(valid_project,values,"Druga",parent=cfg["key"],console_confirmed=True)
    b=tmp_path/"b.yaml"
    with pytest.raises(ValueError,match="innego stanu"):
        _write_anchor(valid_project,evidence,b)
    # Registry fixture: a different version is not a global frequency conflict.
    b.write_text(a.read_text().replace("configuration_version: 1", "configuration_version: 2").replace("r_sub_ms: 15.0", "r_sub_ms: 19.1"))
    assert len(valid_project["configurations"])==2 and second["version"]==2
    assert not find_reference_conflicts([a,b])


def test_source_hash_change_and_relocation(valid_project,tmp_path):
    source=valid_project["sources"][0]
    moved=tmp_path/"moved.json"
    moved.write_bytes(Path(source["path"]).read_bytes())
    relocate(source,moved)
    assert source["path"]==str(moved)
    wrong=tmp_path/"wrong.json"
    wrong.write_text("{}")
    with pytest.raises(ValueError):
        relocate(source,wrong)
    moved.write_text("{}")
    with pytest.raises(ValueError):
        verify_sources(valid_project)


def test_atomic_save_restore_outside_installation(valid_project,tmp_path,monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path/"userdata"))
    valid_project["ui"]["step"]=2
    path=tmp_path/"user_project"/"project.json"
    save_project(path,valid_project)
    restored=load_project(path)
    assert restored==valid_project and restored["ui"]["step"]==2
    assert not list(path.parent.glob("*.tmp"))


def test_state_changes_invalidate_but_note_does_not(valid_project):
    r={"fingerprint":fingerprint(valid_project),"status":"ok"}
    valid_project["listening"].append({"choice":"B"})
    assert result_is_current(valid_project,r)
    valid_project["gains"]["raw"]["L"]=-9
    assert not result_is_current(valid_project,r)


def test_compare_without_raw_anchor_or_impulse(tmp_path):
    p=new_project()
    configs=[put_config(p,{"fc":90,"filter":"BW48","l_main_ms":30,"r_main_ms":30,"l_sub_ms":20,"r_sub_ms":19},name) for name in ("A","B")]
    for n,c in enumerate(configs):
        path=tmp_path/f"{n}.json"
        snapshot(path,[("L sum",np.full(961,1000*10**(n/20),complex)),("R sum",np.full(961,1000*10**(n/20),complex))])
        p["sources"].append(assign(import_source(path),"Po Dirac",c["key"]))
    r=compare(p,"Po Dirac",configs[0]["key"],configs[1]["key"])
    assert r["rows"][0]["delta_db"]["100–220 Hz"]==pytest.approx(1,abs=.0001)
    assert "Jedna pozycja" in " ".join(r["reasons"])
    assert not r["candidates"]
    for source in p["sources"]:
        for trace in source["traces"]:
            trace["name"]="A_96_102_82"
    renamed=compare(p,"Po Dirac",configs[0]["key"],configs[1]["key"])
    assert renamed["rows"]==r["rows"]


def test_wrong_stage_does_not_compare(valid_project):
    with pytest.raises(ValueError,match="Brak par"):
        compare(valid_project,"Po Dirac","a","b")


def test_structured_missing_data_and_no_stale_artifacts(tmp_path):
    r=run_task(new_project(),tmp_path/"new_run","standard",emit=lambda _:None)
    assert r["status"]=="needs_data"
    assert not r["candidates"]
    assert not list((tmp_path/"new_run").rglob("*BIQUADS.txt"))


def test_no_candidate_not_error(monkeypatch,valid_project,tmp_path):
    from rew_candidate_safety import NoSafeCandidateError
    import rew_acoustic_crossover_designer as mod
    def no_candidate(*args,**kwargs):
        out=kwargs["out_dir"]
        atomic_json(out/"QUARANTINE_DIAGNOSTIC.json",{"candidates":[{"reasons":["suma L+R poniżej progu"]}]})
        raise NoSafeCandidateError("no")
    monkeypatch.setattr(mod,"design_acoustic_crossovers",no_candidate)
    r=run_task(valid_project,tmp_path/"result","standard",emit=lambda _:None)
    assert r["status"]=="no_candidate" and "suma L+R" in r["reasons"][0]


def test_export_rejects_stale_candidate(valid_project,tmp_path):
    original={"fingerprint":"old","status":"ok","candidates":[]}
    with pytest.raises(ValueError,match="nieaktualny"):
        export_candidate(valid_project,original,"x",tmp_path)


def test_process_worker_contains_no_tk_calls():
    import inspect,rew_process
    source=inspect.getsource(rew_process)
    assert "import tkinter" not in source and ".after(" not in source


@pytest.mark.skipif(os.name=="nt",reason="Linux process-group test; Windows requires native taskkill check")
def test_cancellation_stops_process_group(tmp_path):
    from rew_process import stop_tree
    proc=subprocess.Popen([sys.executable,"-c","import signal,time,subprocess,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"],start_new_session=True,stdout=subprocess.PIPE,text=True)
    child=int(proc.stdout.readline())
    stop_tree(proc)
    assert proc.poll() is not None
    state=Path(f"/proc/{child}/stat")
    if state.exists():
        assert state.read_text().split()[2]=="Z"  # exited child waiting for init reaping


def test_job_snapshot_and_cancellation(tmp_path):
    from rew_process import ProcessJob
    p=new_project()
    out=tmp_path/"job"
    request={"project":p,"out":str(out),"task":"standard"}
    job=ProcessJob(request,out)
    p["name"]="changed after click"
    assert job.request["project"]["name"]!="changed after click"
    job.cancelled.set()
    job.start()
    job.thread.join(timeout=10)
    event=job.events.get(timeout=2)
    assert event["result"]["status"]=="cancelled"
    assert json.loads((out/"result.json").read_text())["candidates"]==[]


@pytest.mark.parametrize("kind",["standard","custom"])
def test_complete_design_then_exact_export(valid_project,tmp_path,kind):
    result=run_task(valid_project,tmp_path/kind,kind,emit=lambda _:None)
    assert result["status"]=="ok",result
    assert 1<=len(result["candidates"])<=2
    assert not list((tmp_path/kind).rglob("*MINIDSP_BIQUADS.txt"))
    selected=result["candidates"][0]
    exp=run_task(valid_project,tmp_path/(kind+"_export"),"export",{"result":result,"candidate_id":selected["id"]},emit=lambda _:None)
    assert exp["status"]=="ok",exp
    files=list(Path(exp["export_dir"]).glob("OUT*.txt"))
    assert len(files)==4
    for f in files:
        assert "biquad8," in f.read_text()
    assert not (tmp_path/(kind+"_export")/"pending_export").exists()


def test_compare_presets_do_not_require_dsp_parameters():
    p=new_project()
    a=put_measurement_preset(p,"A")
    b=put_measurement_preset(p,"B")
    assert a["settings"]["l_sub_ms"] is None
    assert a["key"]!=b["key"]
    with pytest.raises(ValueError,match="konsoli"):
        verify_reference(p)


def test_real_mdat_partial_spl_is_available(private_legacy_mdat):
    source=import_source(private_legacy_mdat)
    assert source["checks"]["spl"]
    assert len(source["traces"])==4
    assert not source["mapping_confirmed"]


def test_phase_comparison_requires_explicit_timing(valid_project):
    from rew_workflow import measured_details
    cfg=active_config(valid_project)["key"]
    result=measured_details(valid_project,"Przed Dirac",cfg,"other")
    assert result["integration"]==[]
    valid_project["sources"][2]["timing_confirmed"]=True
    result=measured_details(valid_project,"Przed Dirac",cfg,"other")
    assert len(result["integration"])==2
    assert result["time"] and all(not r.get("impulse_available",False) for r in result["time"])
    assert all(r.get("late100_db") is None for r in result["time"])


def test_result_serialisation_represents_unavailable_values():
    from rew_workflow import clean_result
    assert clean_result({"x":float("inf"),"arr":np.array([1,np.nan])})=={"x":None,"arr":[1.,None]}


def test_positive_relative_delay_cannot_silently_shift_main_anchor(valid_project):
    from rew_workflow import anchored_delays
    cfg=active_config(valid_project)["settings"]
    cfg.update(l_main_ms=30,r_main_ms=30)
    with pytest.raises(ValueError,match="0–30"):
        anchored_delays(valid_project,{"l_rel_ms":1,"r_rel_ms":-2})
    assert cfg["l_main_ms"]==30


def test_measurement_check_does_not_confuse_native_api_phase_with_sum_proof(valid_project):
    for t in valid_project["sources"][0]["traces"]:
        if t["role"]=="R sum":
            t["role"]="Nieprzypisany"
    result=check_measurements(valid_project)
    assert result["sources"][0]["spl"]
    assert not result["sources"][0]["phase"]


def test_reference_rejects_mirrored_delay_despite_same_spl_sum(valid_project):
    p=valid_project
    f=np.arange(20,500.1,.5)
    m=np.full(len(f),10**(70/20),complex)*freq_response(make_sections("HP",96000,90,"BW48"),96000,f)
    s=np.full(len(f),10**(64/20),complex)*freq_response(make_sections("LP",96000,90,"BW48"),96000,f)*np.exp(-2j*np.pi*f*.001)
    path=Path(p["sources"][2]["path"])
    snapshot(path,[("L main",m),("R main",m),("L sub",s),("R sub",s),("L sum",m+s),("R sum",m+s)])
    p["sources"][2]["sha256"]=sha256(path)
    active_config(p)["settings"].update(l_sub_ms=14,r_sub_ms=14)
    with pytest.raises(ValueError,match="względnej fazy"):
        verify_reference(p)


@pytest.mark.parametrize("kind",["standard","custom"])
def test_real_worker_protocol_and_external_working_directory(valid_project,tmp_path,kind):
    from rew_process import ProcessJob
    out=tmp_path/(kind+"_worker")
    job=ProcessJob({"project":valid_project,"out":str(out),"task":kind},out)
    job.start()
    job.thread.join(timeout=40)
    if job.thread.is_alive():
        job.cancel()
        job.thread.join(timeout=10)
        pytest.fail("Worker did not complete")
    events=[]
    while not job.events.empty():
        events.append(job.events.get_nowait())
    result=next(e["result"] for e in events if e["type"]=="done")
    assert result["status"]=="ok",result
    assert any(e["type"]=="stage" for e in events)
    assert 1<=len(result["candidates"])<=2
    assert not list(out.rglob("*MINIDSP_BIQUADS.txt"))


def test_gui_no_candidate_replaces_old_results_without_error_dialog():
    from types import SimpleNamespace
    from REWDO_GUI import App
    rows={"reference":{},"old_candidate":{}}
    text={}
    hidden=[]
    fake=SimpleNamespace(design_result={"status":"ok"},
        design_summary=SimpleNamespace(configure=lambda **kw:text.update(kw)),
        candidates=SimpleNamespace(get_children=lambda:list(rows),delete=lambda key:rows.pop(key)),
        design_plot=None,plot=lambda *args:None,
        export_button=SimpleNamespace(pack_forget=lambda:hidden.append(True)))
    result={"task":"standard","status":"no_candidate","message":"Nie znaleziono ustawienia do testu.","next":"Pozostaw odniesienie.","candidates":[]}
    App.present(fake,result)
    assert fake.design_result is result and list(rows)==["reference"]
    assert "Nie znaleziono" in text["text"] and hidden


def test_branching_from_historical_configuration_uses_fresh_version(valid_project):
    first=active_config(valid_project)
    second=put_config(valid_project,first["settings"],"B",parent=first["key"])
    third=put_config(valid_project,first["settings"],"C",parent=first["key"])
    assert second["version"]==2 and third["version"]==3
    assert len({c["key"] for c in valid_project["configurations"]})==3
    validate_project(valid_project)
