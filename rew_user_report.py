"""One user-facing summary. No generated listening verdicts."""
from rew_project import active_config, result_is_current
from rew_app_metadata import APP_VERSION


def write_summary(path,project,result):
    def safe(v):
        return str(v).replace("|","/").replace("\n"," ")
    c=active_config(project)
    if result.get("time_analysis"):
        c=next((item for item in project["configurations"] if item["key"]==result["config_a"]),None)
    lines=[f"# REWDO {APP_VERSION} — podsumowanie","",f"Projekt: {safe(project['name'])}",
           f"Odniesienie: {safe(c['label']) if c else 'nieustalone'}","",result.get("message","Brak decyzji."),"",
           "Następny krok: "+result.get("next","—"),""]
    if not result_is_current(project,result):
        lines += ["**Wynik historyczny lub nieukończony. Nie służy do eksportu.**",""]
    for reason in result.get("reasons",[]):
        lines.append("- "+safe(reason))
    if result.get("time_analysis"):
        from rew_project_time import report_lines
        labels={c["key"]:c["label"]+f" (v{c['version']})" for c in project["configurations"]}
        lines += ["", "A — odniesienie tego porównania: "+safe(labels.get(result["config_a"],result["config_a"])),
                  "B — sprawdzane ustawienie: "+safe(labels.get(result["config_b"],result["config_b"]))]
        lines += report_lines(result)
    if result.get("candidates"):
        lines += ["","## Z obliczeń — do próby pomiarowej","","| Wariant | Sumowanie Δ L/R [dB] | 100–220 Hz Δ [dB] |","|---|---:|---:|"]
        for candidate in result["candidates"][:2]:
            m=candidate["metrics"]
            lines.append(f"| {safe(candidate['label'])} | {m['L_sum_gain_delta_db']:+.2f} / {m['R_sum_gain_delta_db']:+.2f} | {m['body_delta_db'] if m['body_delta_db'] is not None else 'brak zakresu'} |")
        lines += ["","Nie jest to pomiar naturalności wokalu ani gwarancja lepszego brzmienia."]
    if result.get("rows"):
        labels={c["key"]:c["label"]+f" (v{c['version']})" for c in project["configurations"]}
        lines += ["", "Etap: "+safe(result["stage"]),"A: "+safe(labels.get(result["config_a"],result["config_a"])),"B: "+safe(labels.get(result["config_b"],result["config_b"]))]
        lines += ["", *result.get("summaries",[])]
        lines += ["","## Rzeczywiste SPL — B minus A","","| Pozycja | Suma | 30–60 Hz [dB] | 80–110 Hz [dB] | 100–220 Hz [dB] |","|---|---|---:|---:|---:|"]
        for row in result["rows"]:
            vals=["—" if v is None else f"{v:+.2f}" for v in row["delta_db"].values()]
            lines.append("| "+" | ".join([safe(row["position"]),safe(row["role"]),*vals])+" |")
    lines += ["","## Odsłuch",""]
    notes=[n for n in project["listening"] if n.get("run_id")==result.get("run_id")]
    if not notes:
        lines.append("Użytkownik nie zapisał jeszcze wyboru.")
    for n in notes:
        lines.append(f"{n['created']} — {safe(n['choice'])}; głośność {'wyrównana' if n['level_matched'] else 'niewyrównana — ograniczona miarodajność'}. {safe(n['note'])}")
    lines += ["", "Identyfikator przebiegu: "+result.get("run_id","—"),"Dane źródłowe i szczegóły metody znajdują się w projekcie i szczegółach technicznych."]
    path.write_text("\n".join(lines)+"\n",encoding="utf-8")
