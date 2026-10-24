"""Native Windows builder. Creates a new isolated directory; never closes other apps."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import venv


def main():
    if os.name != "nt":
        raise SystemExit("Build wymaga Windows; nie jest to cross-kompilacja.")
    root=Path(__file__).resolve().parent
    base=Path(tempfile.mkdtemp(prefix="rewdo_20_33_build_"))
    environment=base/"venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python=environment/"Scripts"/"python.exe"
    log=base/"build.log"
    with log.open("w",encoding="utf-8") as out:
        for command in ([str(python),"-m","pip","install","-r",str(root/"requirements.txt")],
                        [str(python),"-m","PyInstaller","--noconfirm","--clean","--distpath",str(base/"dist"),"--workpath",str(base/"work"),str(root/"REWDO_onedir.spec")]):
            print("Uruchamianie:",command[2:5],"Log:",log,flush=True)
            subprocess.run(command,cwd=root,stdout=out,stderr=subprocess.STDOUT,check=True)
    target=base/"dist"/"REWDO_GUI_ONEDIR"
    executable=target/"REWDO_GUI.exe"
    subprocess.run([str(executable),"--startup-diagnostic"],check=True)
    subprocess.run([str(executable),"--smoke-test"],check=True)
    shutil.copy2(root/"INSTRUKCJA_v20_33.md",target/"INSTRUKCJA.md")
    print("Nowy folder aplikacji:",target)
    print("Sprawdz recznie 1366x768 / 100% oraz 1920x1080 / 150%. Nie wykonano testu sprzetowego.")


if __name__=="__main__":
    main()
