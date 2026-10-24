#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REWDO v20.31 single-EXE boot.

One executable does both jobs:
  - normal GUI
  - CLI helper when called as: REWDO_GUI.exe --rewdo-cli <subcommand> ...

This removes the previous two-EXE dependency (REWDO_GUI.exe + REWDO_CLI.exe).
It also provides very early startup diagnostics.
"""
from __future__ import annotations

import io
import os
import sys
import time
import traceback
from pathlib import Path


VERSION = "20.31"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()


def _repair_text_stream(stream_name: str, fd: int) -> None:
    """Zapewnij działający stdout/stderr w buildzie PyInstaller --noconsole.

    Gdy EXE jest uruchomiony normalnie, brak konsoli jest pożądany. Gdy GUI
    uruchamia ten sam EXE jako wewnętrzne CLI z stdout=PIPE, deskryptory 1/2
    są dostępne i dzięki temu log nadal trafia do okna GUI.
    """
    try:
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "write"):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", line_buffering=True, write_through=True, errors="replace")
            return
    except Exception:
        pass

    try:
        dup_fd = os.dup(fd)
        raw = os.fdopen(dup_fd, "wb", buffering=0)
        text = io.TextIOWrapper(
            raw,
            encoding="utf-8",
            errors="replace",
            line_buffering=True,
            write_through=True,
        )
        setattr(sys, stream_name, text)
        return
    except Exception:
        pass

    try:
        setattr(sys, stream_name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
    except Exception:
        pass


def configure_std_streams() -> None:
    """Make stdout/stderr safe in PyInstaller/GUI pipe runs."""
    _repair_text_stream("stdout", 1)
    _repair_text_stream("stderr", 2)


def write_text_log(name: str, text: str) -> None:
    try:
        p = APP_DIR / name
        with open(p, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


def write_startup_diag(tag: str = "startup") -> None:
    lines = [
        "=" * 78,
        f"REWDO v{VERSION} {tag}",
        time.strftime("%Y-%m-%d %H:%M:%S"),
        f"argv: {sys.argv!r}",
        f"executable: {sys.executable}",
        f"frozen: {getattr(sys, 'frozen', False)}",
        f"cwd: {os.getcwd()}",
        f"APP_DIR: {APP_DIR}",
        f"python: {sys.version}",
        f"sys.path[0:5]: {sys.path[:5]!r}",
        "",
    ]
    write_text_log("REWDO_STARTUP_DIAGNOSTIC.log", "\n".join(lines))


def write_crash_log(exc: BaseException, where: str) -> Path:
    p = APP_DIR / "REWDO_GUI_CRASH.log"
    try:
        with open(p, "a", encoding="utf-8", errors="replace") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"REWDO v{VERSION} crash log — {where}\n")
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.write(f"Executable: {sys.executable}\n")
            f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n")
            f.write(f"CWD: {os.getcwd()}\n")
            f.write(f"APP_DIR: {APP_DIR}\n")
            f.write(f"Args: {sys.argv}\n")
            f.write(f"Python: {sys.version}\n\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            f.write("\n")
    except Exception:
        pass
    return p


def show_native_error(title: str, message: str) -> None:
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
            return
    except Exception:
        pass
    try:
        print(title)
        print(message)
    except Exception:
        pass


def smoke_test() -> int:
    write_startup_diag("smoke-test")
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title("REWDO smoke test")
        root.withdraw()
        root.update()
        root.destroy()
        print("SMOKE TEST OK: tkinter window created and destroyed.")
        write_text_log("REWDO_SMOKE_TEST.log", time.strftime("%Y-%m-%d %H:%M:%S") + " OK")
        return 0
    except Exception as exc:
        p = write_crash_log(exc, "smoke-test")
        print("SMOKE TEST FAILED")
        print(f"Crash log: {p}")
        raise


def run_cli(argv: list[str]) -> int:
    write_startup_diag("cli")
    try:
        from REWDO_CLI import main as cli_main
        return int(cli_main(argv))
    except Exception as exc:
        p = write_crash_log(exc, "cli")
        print(f"REWDO CLI crash. Log: {p}")
        raise


def run_gui() -> int:
    write_startup_diag("gui")
    try:
        from REWDO_GUI import main as gui_main
        return int(gui_main())
    except Exception as exc:
        p = write_crash_log(exc, "gui-startup")
        show_native_error(
            "REWDO GUI — startup error",
            "GUI did not start.\n\n"
            f"Error: {exc}\n\n"
            f"Crash log:\n{p}\n\n"
            "Run START_REWDO_GUI_DIAGNOSTIC.cmd from the ONEDIR folder."
        )
        raise


def main() -> int:
    configure_std_streams()
    argv = list(sys.argv[1:])
    if "--startup-diagnostic" in argv:
        write_startup_diag("startup-diagnostic")
        print("STARTUP DIAGNOSTIC OK")
        print(f"APP_DIR={APP_DIR}")
        print(f"EXE={sys.executable}")
        return 0
    if "--smoke-test" in argv:
        return smoke_test()
    if argv and argv[0] == "--rewdo-cli":
        return run_cli(argv[1:])
    return run_gui()


if __name__ == "__main__":
    # Critical for PyInstaller + multiprocessing on Windows. Without this,
    # spawned worker processes can re-enter the GUI boot code and look like a hang.
    try:
        import multiprocessing as _mp
        _mp.freeze_support()
    except Exception:
        pass
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        p = write_crash_log(exc, "top-level")
        print(f"REWDO top-level crash. Log: {p}")
        raise
