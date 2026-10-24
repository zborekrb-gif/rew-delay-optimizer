"""Native Tk integration of the common post-Dirac/time report."""
import os
import time

import pytest

from rew_project import changed, fingerprint
from rew_workflow import run_task
from test_stage4_comparison import full_project
from test_v20_33_gui import assert_buttons_visible

pytestmark=pytest.mark.skipif(not os.environ.get('DISPLAY') and os.name!='nt',reason='Requires a Tk display')


@pytest.fixture
def app(tmp_path,monkeypatch,request):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'userdata'))
    import tkinter as tk
    original_init=tk.Tk.__init__;scale=getattr(request,'param',1.)
    def scaled(self,*args,**kwargs):
        original_init(self,*args,**kwargs);self.tk.call('tk','scaling',96/72*scale)
    monkeypatch.setattr(tk.Tk,'__init__',scaled)
    from REWDO_GUI import App
    host=App();errors=[];host.report_callback_exception=lambda *error:errors.append(error)
    p,kw=full_project(tmp_path);host.project=p;host.refresh();host.tabs.select(2);host.full_dialog();host.update()
    win=host.full_window
    yield host,win,kw
    if host.job:
        host.cancel();until=time.monotonic()+10
        while host.job and time.monotonic()<until:host.update();time.sleep(.02)
    host.destroy()
    assert not errors


def choose(win,kw):
    for (cfg,role),var in win.selection_vars.items():
        var.set(next(label for label,identity in win.trace_choices[cfg,role].items() if identity==kw['selected'][cfg][role]))
    for var in win.confirmations.values():var.set(True)
    win.input_changed()


def test_multiple_repeats_require_explicit_selection(app):
    host,win,kw=app
    assert all(not var.get() for (cfg,role),var in win.selection_vars.items() if cfg==kw['config_a'])
    assert all(var.get() for (cfg,role),var in win.selection_vars.items() if cfg==kw['config_b'])
    assert str(win.run_button['state'])=='disabled'
    choose(win,kw)
    assert str(win.run_button['state'])=='normal'
    assert win.request()['selected']==kw['selected']


@pytest.mark.parametrize('app',[1.,1.5],indirect=True)
@pytest.mark.parametrize('page',[0,1,2])
def test_full_window_actions_fit_two_scales(app,page):
    host,win,kw=app;scale=host.ui_scale
    win.minsize(0,0);win.geometry(f'{int(1024*scale)}x{int(700*scale)}')
    win.tabs.select(page)
    assert_buttons_visible(win)
    assert win.run_button.winfo_ismapped()


def test_real_worker_presents_common_result_and_main_spl(app):
    host,win,kw=app;choose(win,kw);win.run()
    assert host.job
    until=time.monotonic()+25
    while host.job and time.monotonic()<until:host.update();time.sleep(.02)
    assert host.job is None
    assert win.result['status']=='ok',win.result
    assert len(win.roles.get_children())==6
    assert len(win.repeatability.get_children())==11
    assert len(win.bands.get_children())==60
    assert host.compare_result is win.result
    assert len(host.comparison.get_children())==3
    assert 'Co zrobić teraz' in host.compare_summary['text']
    assert str(win.report_button['state'])=='normal'


def test_changed_trace_choice_discards_previous_result(app,tmp_path):
    host,win,kw=app;choose(win,kw)
    result=run_task(host.project,tmp_path/'run','full_compare',win.request(),lambda e:None)
    win.present(result)
    first=win.selection_vars[kw['config_a'],'L sum']
    first.set(next(n for n in win.trace_choices[kw['config_a'],'L sum'] if n!=first.get()))
    win.input_changed()
    assert win.result is None and not win.roles.get_children()
    assert str(win.report_button['state'])=='disabled'


def test_wrong_context_or_project_cannot_restore_positive_result(app,tmp_path):
    host,win,kw=app;choose(win,kw)
    result=run_task(host.project,tmp_path/'run','full_compare',win.request(),lambda e:None)
    host.project['sources'][0]['traces'][0]['microphone']='elsewhere';changed(host.project)
    win.present(result)
    assert win.result is None and not win.roles.get_children()


def test_full_domain_description_can_be_read_without_truncation(app,tmp_path):
    host,win,kw=app;choose(win,kw)
    result=run_task(host.project,tmp_path/'run','full_compare',win.request(),lambda e:None)
    win.present(result);win.roles.selection_set('3');win.domain_selected()
    assert result['post_dirac']['domains'][3]['message'] in win.explanation.get('1.0','end')


def test_legacy_time_window_remains_accessible(app):
    host,win,kw=app;win.open_time();host.update()
    assert host.full_window is None and host.time_window is not None
