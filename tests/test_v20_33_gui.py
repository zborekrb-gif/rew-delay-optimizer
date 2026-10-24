"""Real Tk widget checks. Run under an X server or on a desktop; skip headless."""
import os
import copy
import tkinter as tk
from tkinter import ttk
import pytest
from rew_project import new_project, put_measurement_preset
from rew_guidance import next_step

pytestmark=pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name!="nt",reason="Requires a real Tk display")


@pytest.fixture
def app(tmp_path,monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    from REWDO_GUI import App
    window=App()
    errors=[]
    window.report_callback_exception=lambda *exc:errors.append(exc)
    yield window
    window.destroy()
    assert not errors


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def assert_buttons_visible(win):
    win.update()
    right,bottom=win.winfo_rootx()+win.winfo_width(),win.winfo_rooty()+win.winfo_height()
    for w in descendants(win):
        if isinstance(w,(ttk.Button,ttk.Menubutton)) and w.winfo_ismapped():
            # Controls inside a scrollable form can legitimately lie outside its viewport.
            if any(isinstance(a,tk.Canvas) for a in ancestors(w)):
                continue
            assert w.winfo_rootx()+w.winfo_width()<=right+1,w.cget("text")
            assert w.winfo_rooty()+w.winfo_height()<=bottom+1,w.cget("text")


def ancestors(widget):
    current=widget.master
    while current is not None:
        yield current
        current=current.master


@pytest.mark.parametrize("page",[0,1,2])
def test_main_actions_visible_in_small_window(app,page):
    app.geometry(f"{int(1024*app.ui_scale)}x{int(650*app.ui_scale)}")
    app.tabs.select(page)
    assert_buttons_visible(app)
    assert app.guide_button.winfo_ismapped()


def test_next_action_opens_exact_required_form(app):
    app.do_next();app.update()
    dialog=next(w for w in app.winfo_children() if isinstance(w,tk.Toplevel))
    notebook=next(w for w in descendants(dialog) if isinstance(w,ttk.Notebook))
    assert notebook.index(notebook.select())==0
    assert_buttons_visible(dialog)


def test_plan_displays_help_and_switches_to_independent_comparison(app):
    app.guide_dialog();app.update()
    dialog=next(w for w in app.winfo_children() if isinstance(w,tk.Toplevel))
    button=next(w for w in descendants(dialog) if isinstance(w,ttk.Radiobutton) and w.cget("value")=="compare")
    button.invoke();app.update()
    assert next_step(app.project).action=="import"
    assert len(app.project["configurations"])==0
    text=next(w for w in descendants(dialog) if isinstance(w,tk.Text))
    assert "1. " in text.get("1.0","end")
    assert_buttons_visible(dialog)


def test_mapping_saves_current_editor_without_extra_apply(app,monkeypatch):
    p=app.project
    cfg=put_measurement_preset(p,"Obecny C268")
    source={"id":"test","name":"Bardzo długa nazwa pomiaru z polskimi znakami ąęść.mdat","stage":"Po Dirac","configuration":cfg["key"],"raw_confirmed":False,"session_confirmed":False,"timing_confirmed":False,"gain_state":"","mapping_confirmed":False,
        "traces":[{"name":"Kanał lewy — suma","role":"L sum","microphone":"","preset":cfg["key"],"stage":"Po Dirac"}]}
    p["sources"].append(source)
    monkeypatch.setattr(app,"start",lambda *args:None)
    app.mapping("test");app.update()
    dialog=next(w for w in app.winfo_children() if isinstance(w,tk.Toplevel))
    label=next(w for w in descendants(dialog) if isinstance(w,ttk.Label) and w.cget("text")=="Gdzie stała kapsuła?")
    mic=next(w for w in label.master.winfo_children() if isinstance(w,ttk.Entry) and not isinstance(w,ttk.Combobox))
    mic.insert(0,"MLP")
    button=next(w for w in descendants(dialog) if isinstance(w,ttk.Button) and w.cget("text")=="Potwierdź przypisanie i sprawdź")
    assert_buttons_visible(dialog)
    button.invoke();app.update()
    assert source["traces"][0]["microphone"]=="MLP"
    assert source["traces"][0]["role"]=="L sum"


def test_check_exposes_entire_problem_without_log(app):
    message="Brak wspólnej referencji czasu. "*20
    app.present({"task":"check","status":"ok","checks":{"sources":[],"levels":{"status":"BLOCKED","message":message}}})
    app.check_table.selection_set("levels")
    app.check_selected();app.update()
    assert app.check_note.get("1.0","end-1c")==message
    assert_buttons_visible(app)


def test_trial_import_moves_to_comparison_without_reusing_design(app,monkeypatch):
    from rew_guidance import Step
    called=[]
    monkeypatch.setattr(app,"import_file",lambda:called.append(True))
    app.act(Step("trial","Próba","review","","trial_import","Wczytaj"))
    assert app.project["ui"]["goal"]=="compare"
    assert called==[True]
    assert app.design_result is None and not app.project["listening"]


def test_detach_removes_only_project_link_and_invalidates_result(app,tmp_path,monkeypatch):
    from rew_project import fingerprint
    data=tmp_path/"measurement.mdat";data.write_bytes(b"source bytes")
    source={"id":"s","name":data.name,"path":str(data),"configuration":"","traces":[{"name":"L","role":"L sum","preset":"","microphone":"MLP","stage":"Po Dirac"}]}
    app.project["sources"].append(source)
    old=fingerprint(app.project)
    monkeypatch.setattr(app,"selected_source",lambda:source)
    monkeypatch.setattr("REWDO_GUI.messagebox.askyesno",lambda *a,**k:True)
    app.detach_source()
    assert data.read_bytes()==b"source bytes"
    assert app.project["sources"]==[] and fingerprint(app.project)!=old
