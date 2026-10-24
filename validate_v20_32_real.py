"""Read-only source regression. Session declarations below are test inputs, NOT user evidence."""
import argparse
from pathlib import Path
from rew_project import new_project, atomic_json, put_config
from rew_workflow import import_source, check_measurements, clean_result, run_task


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--sources",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    report={"scope":"Import rzeczywistych plików. Przypisanie RAW/bridge i poziomów to wyłącznie warunki testu regresji, nie potwierdzenie aktualnego sprzętu.","files":[]}
    project=new_project()
    project["gains"]={k:{"L":v,"R":v,"origin":"scenariusz regresji, nie dowód użytkownika"} for k,v in (("raw",-6),("dirac",-4),("listening",0))}
    for path in sorted(args.sources.glob("*.mdat")):
        try:
            source=import_source(path)
            report["files"].append({"name":path.name,"sha256":source["sha256"],"traces":len(source["traces"]),"roles":[t["role"] for t in source["traces"]],"status":"READ"})
            if path.name.startswith("07-") or path.name.startswith("06-"):
                stage="Bazowe" if path.name.startswith("07-") else "Zmiana poziomu"
                source.update(stage=stage,mapping_confirmed=True,raw_confirmed=stage=="Bazowe",session_confirmed=True,gain_state="raw" if stage=="Bazowe" else "listening")
                for trace in source["traces"]:
                    trace.update(stage=stage,microphone="test_MLP")
                project["sources"].append(source)
        except Exception as exc:
            report["files"].append({"name":path.name,"status":"BLOCKED","message":str(exc)})
    report["checks"]=check_measurements(project)
    put_config(project,{"fc":90,"filter":"BW48","l_main_ms":30,"r_main_ms":30,"l_sub_ms":30,"r_sub_ms":30},"Warunek testu blokady, NIE stan sprzętu",console_confirmed=True)
    report["design_without_reference"]={}
    for task in ("standard","custom"):
        result=run_task(project,args.out.parent/("real_blocked_"+task),task,emit=lambda _:None)
        report["design_without_reference"][task]={"status":result["status"],"message":result["message"],"candidates":len(result["candidates"])}
        assert result["status"]=="needs_data" and "odniesienia" in result["message"]
        assert not list((args.out.parent/("real_blocked_"+task)).rglob("*MINIDSP_BIQUADS.txt"))
    atomic_json(args.out,clean_result(report))
    print(args.out)
    for row in report["files"]:
        print(row["name"],row["status"],row.get("traces"),row.get("message",""))
    print("LEVELS",report["checks"]["levels"])


if __name__=="__main__":
    main()
