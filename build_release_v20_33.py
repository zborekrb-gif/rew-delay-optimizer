"""Allowlisted source release, with a separate nested developer archive."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

VERSION="REWDO_v20_33_PROWADZENIE"
SOURCE_SHA="3e16950bebc0a7269452d488dd68126e4826c1d988bff7a9c3777d244196041c"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive(folder,target):
    with zipfile.ZipFile(target,"x",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for file in sorted(folder.rglob("*")):
            if file.is_file():
                z.write(file,file.relative_to(folder.parent))
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None


def manifest(folder):
    files=sorted(p for p in folder.rglob("*") if p.is_file() and p.name!="SHA256SUMS.txt")
    (folder/"SHA256SUMS.txt").write_text("".join(f"{sha(p)}  {p.relative_to(folder).as_posix()}\n" for p in files),encoding="utf-8")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--source-zip",type=Path,required=True)
    parser.add_argument("--specification",type=Path,required=True)
    parser.add_argument("--destination",type=Path,required=True)
    args=parser.parse_args()
    assert sha(args.source_zip)==SOURCE_SHA,"Wrong original archive"
    root=Path(__file__).resolve().parent
    args.destination.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix="rewdo_2033_release_",dir=args.destination))
    release=stage/VERSION
    app=release/"aplikacja"
    dev=stage/"REWDO_v20_33_DEV"
    for folder in (app,release/"deweloper",dev/"kod",dev/"historia"):
        folder.mkdir(parents=True,exist_ok=False)
    shutil.copy2(root/"START_REWDO.bat",release/"START_REWDO.bat")
    shutil.copy2(root/"INSTRUKCJA_v20_33.md",release/"INSTRUKCJA.md")
    runtime=[p for p in root.glob("*.py") if p.name.startswith("rew_") or p.name in {"REWDO_GUI.py","REWDO_GUI_SINGLE_BOOT.py","REWDO_CLI.py","REWDO_KALIBRATOR_GUI.py"}]
    runtime += [root/n for n in ("RUN_APP.cmd","requirements-runtime.txt","METRYKI_I_SKALY.md","METRYKI_I_SKALY.csv","USER_EMPIRICAL_CALIBRATION.json")]
    for source in runtime:
        shutil.copy2(source,app/source.name)
    allowed_suffixes={".py",".md",".txt",".csv",".json",".yaml",".bat",".cmd",".vbs",".spec",".mdat"}
    source_files=[p for p in root.iterdir() if p.is_file() and p.suffix in allowed_suffixes and p.name!="SHA256SUMS.txt"]
    source_files += list((root/"tests").glob("*.py"))
    source_files += [p for p in (root/"validation_logs").rglob("*") if p.is_file() and p.suffix in {".log",".txt",".json",".xml"}]
    for source in source_files:
        target=dev/"kod"/source.relative_to(root)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
    for source in (root/"gui_validation").glob("*"):
        if source.is_file() and source.suffix in {".png",".json",".log"}:
            target=dev/"sprawdzenia_interfejsu"/source.name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
    shutil.copy2(args.specification,dev/"WYTYCZNE_v20_33.md")
    shutil.copy2(args.source_zip,dev/"historia"/"REWDO_v20_32_UPROSZCZENIE.zip")
    for name in ("RAPORT_WDROZENIA_v20_33.md","RAPORT_TESTOW_v20_33.md"):
        shutil.copy2(root/name,release/"deweloper"/name)
    (dev/"README_DEWELOPER.md").write_text(
        "# REWDO 20.33 — deweloper\n\nAktualny kod i testy: `kod/`. Uruchom `python -m pytest -q` z tego katalogu po zainstalowaniu `requirements-dev.txt`. Raporty v20.33 opisują bieżące prace. Starsze README, audyty, profile i launchery w `kod/` są materiałem historycznym/deweloperskim, nie instrukcją użytkową ani aktywną konfiguracją. Nie uruchamia ich nowy interfejs.\n\n`historia/` zachowuje niezmieniony ZIP 20.32; niczego z niego nie importuje się automatycznie do projektu. `WYTYCZNE_v20_33.md` to zlecenie. Build Windows: `kod/BUILD_GUI_ONEDIR_EXE.bat`; nie zamyka działających aplikacji i tworzy nowy katalog roboczy. Nie został wykonany na Windows przez autora tej paczki.\n",encoding="utf-8")
    manifest(dev)
    archive(dev,release/"deweloper"/"REWDO_v20_33_DEV.zip")
    (release/"deweloper"/"RELEASE_ALLOWLIST.json").write_text(json.dumps({"runtime":[p.name for p in runtime],"source_suffixes":sorted(allowed_suffixes),"source_sha256":SOURCE_SHA,"excludes":["venv","cache","coverage database","user projects","unselected inputs"]},indent=2)+"\n",encoding="utf-8")
    manifest(release)
    target=args.destination/(VERSION+".zip")
    archive(release,target)
    print(json.dumps({"archive":str(target),"sha256":sha(target),"size_bytes":target.stat().st_size,"staging":str(release),"developer_source":str(dev/"kod")},indent=2))


if __name__=="__main__":
    main()
