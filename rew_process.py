"""GUI-independent process lifecycle. Workers communicate only through Queue."""
import json
import os
import queue
import signal
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from rew_project import atomic_json


def stop_tree(process):
    if os.name == "nt":
        # taskkill /T terminates descendants as well, including multiprocessing workers.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # The group can outlive its leader; kill the group even if the leader exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


class ProcessJob:
    def __init__(self, request, directory):
        self.directory = Path(directory)
        self.request = json.loads(json.dumps(request, allow_nan=False))
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.process = None
        self._lock = threading.Lock()
        self.thread = None

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        atomic_json(self.directory / "request.json", self.request)
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def cancel(self):
        self.cancelled.set()
        threading.Thread(target=self._terminate, daemon=True).start()

    def _terminate(self):
        with self._lock:
            process = self.process
        if process is not None:
            try:
                stop_tree(process)
            except Exception as exc:
                self.events.put({"type":"log", "message":f"Zatrzymywanie: {exc}"})

    def _worker(self):
        request_path = self.directory / "request.json"
        command = ([sys.executable, "--rewdo-task"] if getattr(sys, "frozen", False) else
                   [sys.executable, "-u", str(Path(__file__).with_name("rew_task_runner.py"))]) + [str(request_path)]
        options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session":True}
        result = None
        try:
            with self._lock:
                if self.cancelled.is_set():
                    return
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", cwd=self.directory, **options)
            with (self.directory / "process.log").open("w", encoding="utf-8") as log:
                for line in self.process.stdout:
                    log.write(line)
                    log.flush()
                    if line.startswith("REWDO_EVENT:"):
                        try:
                            self.events.put(json.loads(line[len("REWDO_EVENT:"):]))
                        except ValueError:
                            self.events.put({"type":"log", "message":line})
                    else:
                        self.events.put({"type":"log", "message":line})
            code = self.process.wait()
            if not self.cancelled.is_set():
                result = json.loads((self.directory / "result.json").read_text(encoding="utf-8"))
                if result.get("schema") != 1 or code not in (0,2,3):
                    result = {"schema":1, "status":"error", "message":result.get("message", f"Proces zakończył się kodem {code}."), "candidates":[]}
        except Exception as exc:
            result = {"schema":1, "status":"error", "message":str(exc), "candidates":[]}
        finally:
            if self.cancelled.is_set():
                # An interrupted result, even if the engine wrote it during cancellation, is never consumable.
                result = {"schema":1, "run_id":self.directory.name, "status":"cancelled", "message":"Przerwano na życzenie użytkownika.", "candidates":[], "next":"Możesz rozpocząć nowy przebieg."}
                for name in ("do_proby_pomiarowej", "pending_export", "pending_time_analysis", "pelna_analiza_czasu", "pending_full_comparison", "pelne_porownanie_po_dirac"):
                    temporary = self.directory / name
                    if temporary.is_dir() and not temporary.is_symlink():
                        shutil.rmtree(temporary)  # only this run's unpublished export
                atomic_json(self.directory / "result.json", result)
            self.events.put({"type":"done", "result":result})
