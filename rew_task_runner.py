"""Internal process entry point, also used by the single EXE."""
import json
import sys
from pathlib import Path

from rew_workflow import run_task


def main(argv=None):
    request_path = Path((argv or sys.argv[1:])[0])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    def emit(event):
        print("REWDO_EVENT:" + json.dumps(event, ensure_ascii=False), flush=True)
    result = run_task(request["project"], request["out"], request["task"], request.get("args"), emit)
    emit({"type": "completed", "status": result["status"]})
    return {"ok":0, "no_candidate":3, "needs_data":2, "error":1}.get(result["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
