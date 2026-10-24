"""Guidance is navigation, never a replacement for scientific verification."""
import copy
import pytest
from rew_project import new_project, fingerprint, put_measurement_preset, changed, active_config
from rew_guidance import plan, next_step, current_run, help_text, ROLE_LABELS
from test_v20_32_workflow import valid_project


def add_run(p, task, **kwargs):
    result={"task":task,"fingerprint":fingerprint(p),"status":"ok","run_id":task,"candidates":[],**kwargs}
    p["runs"].append(result)
    return result


def step(p,key):
    return next(s for s in plan(p) if s.key==key)


def checked(p):
    add_run(p,"check",checks={"sources":[{"source_id":p["sources"][0]["id"],"phase":True}],"levels":{"status":"VERIFIED"}})
    add_run(p,"verify",evidence={"scope":"CONSOLE_AND_RELATIVE_SUM"})


def test_first_start_has_one_action_no_assumed_delays():
    p=new_project()
    before=copy.deepcopy(p)
    assert next_step(p).action=="system_reference"
    assert [s.state for s in plan(p)]==["todo"]*9
    assert p==before
    assert not p["configurations"]


def test_comparison_needs_no_raw_levels_or_reference():
    p=new_project()
    p["ui"]["goal"]="compare"
    assert next_step(p).action=="import"
    a=put_measurement_preset(p,"A")
    b=put_measurement_preset(p,"B")
    assert {s.key for s in plan(p)}=={"sources","names","compare"}
    assert all(p["gains"][k]["L"] is None for k in p["gains"])
    assert not active_config(p)["console_confirmed"]


def test_name_setup_precedes_unassignable_import_mapping():
    p=new_project();p["ui"]["goal"]="compare"
    p["sources"]=[{"id":"s","mapping_confirmed":False,"name":"MDAT","traces":[]}]
    assert next_step(p).action=="preset"


def test_import_routes_to_exact_source_to_describe(valid_project):
    p=valid_project
    p["sources"][1]["mapping_confirmed"]=False
    s=next_step(p)
    assert s.action=="mapping" and s.source_id==p["sources"][1]["id"]


def test_console_entry_alone_never_proves_reference(valid_project):
    assert step(valid_project,"reference").state=="done"
    assert step(valid_project,"verify").state=="todo"
    assert step(valid_project,"check").state=="todo"


def test_spl_only_never_completes_phase_check(valid_project):
    p=valid_project
    add_run(p,"check",checks={"sources":[{"source_id":p["sources"][0]["id"],"spl":True,"phase":False}],"levels":{"status":"VERIFIED"}})
    assert step(p,"check").state=="todo"
    assert step(p,"check").action=="check_help"


def test_current_evidence_unlocks_next_search_action(valid_project):
    p=valid_project;checked(p)
    assert next_step(p).action=="standard"
    p["analysis"]["confirmed"]=False
    checked(p)
    assert next_step(p).action=="system_search"


@pytest.mark.parametrize("status",["needs_data","error","cancelled"])
def test_new_failed_attempt_does_not_revive_old_success(valid_project,status):
    p=valid_project;checked(p)
    add_run(p,"verify",status=status)
    assert step(p,"verify").state=="todo"


def test_changed_parameters_invalidate_guidance_evidence(valid_project):
    p=valid_project;checked(p)
    p["gains"]["dirac"]["L"]=-3
    assert step(p,"check").state=="todo"
    assert step(p,"verify").state=="todo"


def test_goal_change_never_changes_scientific_result(valid_project):
    p=valid_project;original=fingerprint(p)
    p["ui"]["goal"]="compare"
    assert fingerprint(p)==original


def test_prepared_files_are_not_a_completed_hardware_trial(valid_project):
    p=valid_project;checked(p)
    add_run(p,"standard",candidates=[{"id":"candidate"}])
    assert next_step(p).key=="trial"
    add_run(p,"export",export_dir="example")
    assert next_step(p).key=="trial"
    assert next_step(p).state=="review"
    assert next_step(p).action=="trial_import"
    assert not p["listening"]


def test_no_candidate_is_review_not_success(valid_project):
    p=valid_project;checked(p)
    add_run(p,"standard",status="no_candidate")
    assert next_step(p).key=="design" and next_step(p).state=="review"


def test_listening_evidence_must_match_current_comparison(valid_project):
    p=valid_project;p["ui"]["goal"]="compare"
    put_measurement_preset(p,"B")
    add_run(p,"compare",config_a="a",config_b="b")
    p["listening"].append({"run_id":"some_other_run"})
    assert step(p,"compare").state=="review"
    p["listening"].append({"run_id":"compare"})
    assert step(p,"compare").state=="done"


def test_each_plan_step_has_numbered_help_without_setting_defaults():
    for s in plan(new_project()):
        title,body=help_text(s.key)
        assert title and body.startswith("1. ") and "2. " in body
    assert len(set(ROLE_LABELS.values()))==len(ROLE_LABELS)
