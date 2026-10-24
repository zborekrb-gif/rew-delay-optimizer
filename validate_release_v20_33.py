"""Read-only archive verification plus a sandboxed source-runner smoke test."""
import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile


def check_manifest(z,root):
    entries=z.read(root+"/SHA256SUMS.txt").decode().splitlines()
    for line in entries:
        expected,path=line.split("  ",1)
        assert hashlib.sha256(z.read(root+"/"+path)).hexdigest()==expected,path
    assert len(entries)==len([n for n in z.namelist() if not n.endswith("/") and n!=root+"/SHA256SUMS.txt"])
    return len(entries)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("archive",type=Path)
    args=parser.parse_args()
    report={"archive":args.archive.name,"sha256":hashlib.sha256(args.archive.read_bytes()).hexdigest()}
    base=Path(tempfile.mkdtemp(prefix="rewdo_2033_verify_"))
    with zipfile.ZipFile(args.archive) as z:
        assert z.testzip() is None
        for name in z.namelist():
            p=PurePosixPath(name)
            assert not p.is_absolute() and ".." not in p.parts
            assert not any(part in {"__pycache__",".venv",".pytest_cache"} for part in p.parts)
        root="REWDO_v20_33_PROWADZENIE"
        report["user_manifest_files"]=check_manifest(z,root)
        with zipfile.ZipFile(io.BytesIO(z.read(root+"/deweloper/REWDO_v20_33_DEV.zip"))) as nested:
            assert nested.testzip() is None
            report["developer_manifest_files"]=check_manifest(nested,"REWDO_v20_33_DEV")
            original=nested.read("REWDO_v20_33_DEV/historia/REWDO_v20_32_UPROSZCZENIE.zip")
            assert hashlib.sha256(original).hexdigest()=="3e16950bebc0a7269452d488dd68126e4826c1d988bff7a9c3777d244196041c"
        z.extractall(base)
    app=base/root/"aplikacja"
    code=list(app.glob("*.py"))
    for file in code:
        ast.parse(file.read_text(encoding="utf-8-sig"),filename=file.name)
        file.chmod(0o444)
    app.chmod(0o555)
    report["runtime_syntax_files"]=len(code)
    environment={**os.environ,"PYTHONDONTWRITEBYTECODE":"1","LOCALAPPDATA":str(base/"userdata")}
    check="import importlib; from pathlib import Path; modules=[p.stem for p in Path.cwd().glob('*.py') if p.stem.startswith('rew_') or p.stem in ('REWDO_GUI','REWDO_GUI_SINGLE_BOOT')]; [importlib.import_module(m) for m in modules]; print(len(modules))"
    result=subprocess.run([sys.executable,"-c",check],cwd=app,env=environment,capture_output=True,text=True,timeout=40)
    assert result.returncode==0,result.stderr
    report["runtime_imports"]=int(result.stdout.strip())
    # Use the actual packaged source entrypoint, writing only to a separate user directory.
    project_code="import json; from rew_project import new_project; print(json.dumps(new_project()))"
    project=json.loads(subprocess.check_output([sys.executable,"-c",project_code],cwd=app,env=environment,text=True))
    out=base/"userdata"/"run"
    request=base/"request.json"
    request.write_text(json.dumps({"project":project,"out":str(out),"task":"standard"}),encoding="utf-8")
    completed=subprocess.run([sys.executable,str(app/"REWDO_GUI_SINGLE_BOOT.py"),"--rewdo-task",str(request)],cwd=base,env=environment,capture_output=True,text=True,timeout=40)
    data=json.loads((out/"result.json").read_text(encoding="utf-8"))
    assert completed.returncode==2 and data["status"]=="needs_data" and not data["candidates"],completed.stderr
    assert not list(app.rglob("*.log")) and not list(app.rglob("__pycache__"))
    report.update(zip="PASS",manifests="PASS",read_only_source_runner="PASS",native_windows="NOT_RUN",visual_gui="NOT_RUN",hardware="NOT_RUN")
    print(json.dumps(report,indent=2))


if __name__=="__main__":
    main()
