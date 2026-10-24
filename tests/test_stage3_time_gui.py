"""Real Tk checks of the full-analysis workflow, including a real worker."""
import copy
import os
import time

import pytest

from rew_project import changed
from rew_project_time import CONFIRMATIONS
from rew_workflow import run_task
from test_stage3_project_time import project_for
from test_v20_33_gui import assert_buttons_visible

pytestmark = pytest.mark.skipif(not os.environ.get('DISPLAY') and os.name != 'nt', reason='Requires real Tk display')


@pytest.fixture
def app(tmp_path, monkeypatch, request):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'userdata'))
    import tkinter as tk
    scale=getattr(request,'param',1.0)
    original_init=tk.Tk.__init__
    def init_at_scale(self,*args,**kwargs):
        original_init(self,*args,**kwargs)
        self.tk.call('tk','scaling',96/72*scale)
    monkeypatch.setattr(tk.Tk,'__init__',init_at_scale)
    from REWDO_GUI import App
    window = App()
    errors = []
    window.report_callback_exception = lambda *exc: errors.append(exc)
    p, args = project_for(tmp_path)
    window.project = p
    window.refresh()
    window.tabs.select(2)
    window.time_dialog()
    window.update()
    yield window, window.time_window, args
    if window.job:
        window.cancel()
        until = time.monotonic()+10
        while window.job and time.monotonic()<until:
            window.update()
            time.sleep(.02)
    window.destroy()
    assert not errors


def confirmed(win):
    for var in win.confirmations.values():
        var.set(True)
    win.input_changed()


def worker_result(app, args, tmp_path):
    return run_task(app.project,tmp_path/'time-run','time_compare',
        {'source_id':args['source_id'],'stage':args['stage'],'a':args['config_a'],'b':args['config_b'],
         'position':args['position'],'confirmations':args['confirmations']},lambda e:None)


def test_dialog_counts_and_scoped_confirmations(app):
    host, win, args = app
    assert win.position_var.get() == 'MLP'
    assert [win.counts.item(i,'values')[1:] for i in win.counts.get_children()] == [('3','1')]*3
    assert str(win.run_button['state']) == 'disabled'
    confirmed(win)
    assert str(win.run_button['state']) == 'normal'
    assert win.request()['a'] == args['config_a']
    assert win.request()['b'] == args['config_b']


@pytest.mark.parametrize('app',[1.0,1.5],indirect=True)
@pytest.mark.parametrize('page',[0,1,2])
def test_all_main_actions_fit_at_two_scales(app,page):
    host,win,args=app
    scale=host.ui_scale
    win.minsize(0,0)
    win.geometry(f'{int(1024*scale)}x{int(650*scale)}')
    win.tabs.select(page)
    assert_buttons_visible(win)
    assert win.run_button.winfo_ismapped()
    assert win.report_button.winfo_ismapped()
    from tkinter import font
    heading_font=font.Font(font='TkHeadingFont')
    for tree in (win.roles,win.repeatability):
        for column in tree['columns']:
            assert tree.column(column,'width') >= heading_font.measure(tree.heading(column,'text'))+10


def test_real_worker_displays_full_result_without_replacing_spl_comparison(app):
    host,win,args=app
    prior={'task':'compare','status':'ok','sentinel':'previous SPL comparison'}
    host.compare_result=prior
    confirmed(win)
    win.run()
    assert host.job and win.loading
    until=time.monotonic()+25
    while host.job and time.monotonic()<until:
        host.update()
        time.sleep(.02)
    assert host.job is None
    assert win.result['status']=='ok',win.result
    assert 'ponad rozrzut A' in win.verdict['text']
    assert len(win.roles.get_children())==3
    assert len(win.repeatability.get_children())==3
    assert len(win.bands.get_children())==60
    assert str(win.report_button['state'])=='normal'
    assert host.compare_result is prior
    assert host.time_result is win.result
    assert_buttons_visible(win)


def test_changing_scope_clears_verdict_and_confirmations(app,tmp_path):
    host,win,args=app
    confirmed(win)
    result=worker_result(host,args,tmp_path)
    win.present(result)
    win.position_var.set('Different position')
    win.position_changed()
    assert win.result is None
    assert not win.roles.get_children()
    assert not any(v.get() for v in win.confirmations.values())
    assert str(win.report_button['state'])=='disabled'


def test_changed_project_cannot_restore_positive_result(app,tmp_path):
    host,win,args=app
    confirmed(win)
    result=worker_result(host,args,tmp_path)
    host.project['sources'][0]['traces'][0]['microphone']='new'
    changed(host.project)
    win.present(result)
    assert win.result is None
    assert 'wcześniejszych' in win.status['text']
    assert str(win.report_button['state'])=='disabled'


def test_data_failure_explains_next_action_without_stale_verdict(app,tmp_path):
    host,win,args=app
    confirmed(win)
    host.project['sources'][0]['traces'][-1]['role']='Nieprzypisany'
    result=worker_result(host,args,tmp_path)
    win.present(result)
    assert result['status']=='needs_data'
    assert 'Brakuje pomiarów' in win.verdict['text']
    assert not win.roles.get_children()
    assert str(win.save_button['state'])=='disabled'


def test_import_action_returns_to_existing_rew_import(app,monkeypatch):
    host,win,args=app
    calls=[]
    monkeypatch.setattr(host,'api_dialog',lambda:calls.append('import'))
    win.import_from_rew()
    assert host.time_window is None
    assert calls==['import']


def test_closing_dialog_keeps_worker_result_and_main_window_usable(app):
    host,win,args=app
    confirmed(win)
    win.run()
    win.close()
    until=time.monotonic()+25
    while host.job and time.monotonic()<until:
        host.update()
        time.sleep(.02)
    assert host.job is None
    assert host.time_window is None
    assert host.time_result['status']=='ok'
    host.set_busy(False)
