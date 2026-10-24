"""User guidance from explicit project state. This module grants no DSP permissions."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from rew_project import active_config, common_gains, fingerprint, result_is_current

ROLE_LABELS = {
    "Nieprzypisany": "Nie ustalono",
    "L main": "Lewa kolumna osobno", "R main": "Prawa kolumna osobno",
    "L sub": "Lewy subwoofer osobno", "R sub": "Prawy subwoofer osobno",
    "L sum": "Lewa kolumna + subwoofer", "R sum": "Prawa kolumna + subwoofer",
    "L+R": "Cały system — obie strony",
}


@dataclass
class Step:
    key: str
    title: str
    state: str
    reason: str
    action: str
    button: str
    source_id: str = ""


def current_run(project, *tasks):
    # A later blocked/cancelled attempt must never revive an earlier success.
    return next((r for r in reversed(project["runs"])
                 if r.get("task") in tasks and r.get("fingerprint") == fingerprint(project)), None)


def _assigned(source):
    used = [t for t in source["traces"] if t["role"] != "Nieprzypisany"]
    return bool(source.get("mapping_confirmed") and used and all(
        t["microphone"].strip() and t["stage"] != "Nieustalony" for t in used))


def plan(project):
    """Cheap, IO-free presentation state; the worker rechecks all actual evidence."""
    goal = project["ui"].get("goal", "design")
    sources = project["sources"]
    check = current_run(project, "check")
    checked = check.get("checks", {}) if check and check.get("status") == "ok" else {}
    compare = current_run(project, "compare")
    design = current_run(project, "standard", "custom")
    export = current_run(project, "export")
    steps = []
    if goal == "design":
        cfg = active_config(project)
        ready = bool(cfg and cfg["console_confirmed"] and all(
            cfg["settings"].get(k) is not None for k in ("fc", "l_main_ms", "r_main_ms", "l_sub_ms", "r_sub_ms")))
        steps.append(Step("reference", "1. Zapisz obecne ustawienie", "done" if ready else "todo",
            "Zapisane wartości z konsoli są punktem odniesienia; zgodność z pomiarem sprawdzimy osobno." if ready else
            "Wpisz podział i cztery opóźnienia odczytane w miniDSP. To pozwoli wrócić do znanego brzmienia.",
            "system_reference", "Wpisz obecne ustawienie"))
    if not sources:
        steps.append(Step("sources", "2. Wczytaj pomiary" if goal == "design" else "1. Wczytaj pomiary A i B", "todo",
            "Wybierz istniejący MDAT. Jeśli dopiero mierzysz, otwórz plan — podaje potrzebne ślady i warunki sesji.",
            "import", "Wczytaj plik REW"))
    else:
        unassigned = next((s for s in sources if not _assigned(s)), None)
        steps.append(Step("sources", "2. Opisz wczytane pomiary" if goal == "design" else "1. Opisz pomiary A i B",
            "todo" if unassigned else "done",
            (f"{unassigned['name']}: ustal, co mierzono, gdzie stał mikrofon i czy Dirac był włączony." if unassigned else
             "Role, pozycje mikrofonu i etapy są opisane. Samo przypisanie nie potwierdza jakości danych."),
            "mapping" if unassigned else "measurements", "Opisz ten plik" if unassigned else "Zobacz pomiary",
            unassigned["id"] if unassigned else ""))
    if goal == "design":
        raw = [s for s in sources if s["stage"] == "Bazowe"]
        bridge = [s for s in sources if s["stage"] == "Zmiana poziomu"]
        ref = [s for s in sources if s["stage"] == "Przed Dirac" and s["configuration"] == project["active_configuration"]]
        missing = [label for found, label in ((raw, "pomiary bazowe"), (bridge, "pomiar zmiany poziomu"), (ref, "obecne ustawienie przed Dirac")) if not found]
        steps.append(Step("sessions", "3. Uzupełnij potrzebne sesje", "todo" if missing else "done",
            "Brakuje: " + ", ".join(missing) + ". Plan wyjaśnia każdą sesję." if missing else
            "Sesje są dołączone; teraz sprawdź ich zgodność.", "import", "Wczytaj brakujący pomiar"))
        try:
            common_gains(project)
            levels_ok, reason = True, "Zapisano poziomy dla trzech etapów. Ich rzeczywistą zmianę trzeba potwierdzić pomiarem."
        except ValueError as exc:
            levels_ok, reason = False, str(exc)
        steps.append(Step("levels", "4. Ustal poziomy subwooferów", "done" if levels_ok else "todo", reason,
            "system_levels", "Wpisz poziomy subwooferów"))
        phase = bool(len(raw) == 1 and any(r["source_id"] == raw[0]["id"] and r.get("phase") for r in checked.get("sources", [])))
        bridge_ok = checked.get("levels", {}).get("status") == "VERIFIED"
        check_problems=[r.get("message","Nie potwierdzono fazy i sumowania.") for r in checked.get("sources",[]) if not r.get("phase") and any(s["id"]==r["source_id"] for s in raw)]
        if not bridge_ok and checked.get("levels",{}).get("message"):
            check_problems.append(checked["levels"]["message"])
        steps.append(Step("check", "5. Sprawdź fazę, sumowanie i poziomy", "done" if phase and bridge_ok else "todo",
            "Kontrola fazy obu stron i zmiany poziomu przeszła w bieżącej wersji projektu." if phase and bridge_ok else
            (" ".join(check_problems) or "Sprawdź, czy oddzielne pomiary kolumn i subów odtwarzają zmierzone sumy. Brak sumy nie oznacza poprawnej fazy."),
            "check_help" if checked and not (phase and bridge_ok) else "check",
            "Jak uzupełnić ten brak?" if checked and not (phase and bridge_ok) else "Sprawdź pomiary"))
        verify = current_run(project, "verify")
        verified = bool(verify and verify.get("status") == "ok" and verify.get("evidence"))
        steps.append(Step("verify", "6. Potwierdź punkt odniesienia", "done" if verified else "todo",
            "Obliczenia odtwarzają pomiar odniesienia w sprawdzonym zakresie." if verified else
            verify.get("message","Brakuje dowodu pomiarowego.") if verify else
            "Porównaj ustawienia z konsoli z rzeczywistym pomiarem przed Dirac. To chroni przed projektowaniem na błędnej bazie.",
            "verify_help" if verify and not verified else "verify", "Jak poprawić odniesienie?" if verify and not verified else "Sprawdź obecne ustawienie"))
        prerequisites = all(s.state == "done" for s in steps)
        has_design = bool(design and design.get("status") == "ok" and design.get("candidates"))
        no_candidate = bool(design and design.get("status") == "no_candidate")
        steps.append(Step("design", "7. Wybierz wariant do próby", "done" if has_design else "review" if no_candidate else "todo",
            "Wybierz wiersz wariantu. Porównaj obie strony i dolną średnicę, potem przygotuj pliki do próby." if has_design else
            "Nie znaleziono uzasadnionej poprawy. Pozostanie przy obecnym ustawieniu jest prawidłowym wynikiem." if no_candidate else
            "Najpierw sprawdź standardową zwrotnicę. Własna zwrotnica pozostaje dostępna jako dokładniejsza próba.",
            "design_result" if has_design or no_candidate else "standard" if prerequisites and project["analysis"]["confirmed"] else "system_search" if prerequisites else "design_page",
            "Zobacz warianty" if has_design or no_candidate else "Znajdź warianty" if prerequisites and project["analysis"]["confirmed"] else "Ustal zakres szukania" if prerequisites else "Zobacz dobór ustawień"))
        exported = bool(export and export.get("status") == "ok")
        steps.append(Step("trial", "8. Zmierz wariant i wykonaj Dirac", "review" if exported else "todo",
            "Pliki przygotowano. To jeszcze nie potwierdzenie na sprzęcie. Zachowaj kopię slotu, zmierz przed Dirac, wykonaj nową kalibrację i pomiar po niej." if exported else
            "Zaznacz wariant w tabeli i przygotuj ustawienia do testu. Zmierz go przed Dirac; nowa zwrotnica wymaga nowej kalibracji.",
            "trial_import" if exported else "design_result", "Wczytaj pomiar wariantu" if exported else "Wybierz wariant"))
    else:
        has_two = len(project["configurations"]) >= 2
        steps.append(Step("names", "2. Nazwij porównywane ustawienia", "done" if has_two else "todo",
            "Używaj nazw, które rozpoznasz, np. Obecne C268 i Nowa sesja C268. Przypisz pomiary do tych ustawień." if has_two else
            "Dodaj nazwy A i B. Do porównania SPL nie musisz wpisywać zwrotnicy ani opóźnień.",
            "preset", "Dodaj nazwę ustawienia"))
    compared = bool(compare and compare.get("status") == "ok" and result_is_current(project, compare))
    steps.append(Step("compare", "9. Porównaj pomiar i odsłuch" if goal == "design" else "3. Porównaj i posłuchaj",
        "done" if compared and any(n.get("run_id") == compare.get("run_id") for n in project["listening"]) else "review" if compared else "todo",
        "Pomiar pokazuje różnice. Wyrównaj głośność A/B i zapisz własną ocenę wokalu, basu i sceny." if compared else
        "Porównuj ten sam etap i pozycję mikrofonu. Po nowej kalibracji porównaj dwa pomiary po Dirac.",
        "compare_page", "Przejdź do porównania"))
    return steps


def next_step(project):
    steps = plan(project)
    if project["ui"].get("goal") == "compare" and project["sources"]:
        names=next((s for s in steps if s.key=="names"),None)
        if names and names.state!="done":
            return names
    # Once candidates exist, show their next physical step rather than an already-done search.
    return next((s for s in steps if s.state != "done"), steps[-1])


HELP = {
 "reference": ("Zachowaj znany punkt odniesienia", [
    "Zrób kopię aktywnego slotu w miniDSP. Aplikacja nie odczytuje ani nie zmienia sprzętu.",
    "Otwórz Mój system → Obecne ustawienie. Przepisz podział, rodzinę filtra oraz cztery opóźnienia z konsoli.",
    "Potwierdź wyłącznie sprawdzone wartości. Wcześniejsze 90 Hz BW48 ani historyczne opóźnienia nie są automatycznie bieżącą konfiguracją.",
    "Własna zwrotnica może być wynikiem projektu; obecny model odniesienia obsługuje wskazane standardowe rodziny. Gotowe pomiary możesz porównać niezależnie."]),
 "sources": ("Jak opisać plik pomiarowy", [
    "Wczytaj MDAT, a potem wybierz Opisz pomiary. Oryginalnego pliku nie zmieniamy.",
    "Etap Bazowe oznacza pomiary pojedynczych urządzeń bez zwrotnicy DSP; Przed Dirac to pomiar gotowej zwrotnicy z korekcją wyłączoną; Po Dirac — z korekcją włączoną.",
    "Miejsce mikrofonu opisuje kapsułę, np. MLP — środek między uszami. L/R określa grający kanał, a nie przesunięcie mikrofonu.",
    "Zaznacz ślad i wybierz: lewa/prawa kolumna, subwoofer, para lub cały system. Jeżeli plik ma jedną sesję i pozycję, zastosuj etap, ustawienie i pozycję do wszystkich.",
    "Jeśli nazwy nie wystarczają, sprawdź ślady w REW. Nierozpoznany ślad może pozostać nieprzypisany. Nie potwierdzaj warunków, których nie znasz."]),
 "sessions": ("Trzy sesje potrzebne do projektowania", [
    "Bazowe: Dirac OFF, PEQ OFF, HP/LP w miniDSP OFF, delay 0 ms, polaryzacja OFF. REL 120 Hz. Nie zmieniaj pokręteł, mikrofonu, toru ani głośności w serii. Użyj wspólnej akustycznej referencji czasu w REW.",
    "Zmierz osobno lewą kolumnę, lewy sub, prawą kolumnę i prawy sub. Dodaj rzeczywiste sumy lewa kolumna+sub i prawa kolumna+sub oraz powtórzenia; dodaj sumę stereo. Suma utworzona obliczeniowo nie zastępuje pomiaru.",
    "Zmiana poziomu: wykonaj kontrolne sumy przy drugim, udokumentowanym poziomie cyfrowym subów. Pozostałe warunki muszą odpowiadać RAW. Program porówna rzeczywistą zmianę z wpisem; nie wystarczy samo potwierdzenie pola.",
    "Obecne ustawienie przed Dirac: wgraj znany podział i opóźnienia, pozostaw Dirac OFF. Zmierz oddzielnie MAIN, SUB i ich SUM po lewej i prawej stronie przy tej samej pozycji i opisanym poziomie.",
    "Zapisz każdą sesję do osobnego MDAT. Poziomy mają być bezpieczne dla sprzętu i mikrofonu; przerwij przy przesterowaniu. Nie zdejmuj filtrów ochronnych poza zweryfikowanym protokołem swojego systemu."]),
 "levels": ("Które poziomy wpisać", [
    "Wpisujesz cyfrowy poziom wyjść subwooferowych miniDSP, osobno L i R. To nie głośność główna ani położenie pokrętła REL.",
    "Bazowy — rzeczywisty poziom podczas RAW. Pomiar Dirac — poziom używany do kalibracji. Odsłuch — docelowy poziom cyfrowy po kalibracji.",
    "Dopisz źródło: konsola, sesja i data. Nie uzupełniaj braków wartościami −6/−4/0, jeśli ich nie potwierdziłeś.",
    "Obecny silnik wymaga takich samych poziomów L/R i zweryfikowanego wzrostu między RAW a odsłuchem (do 12 dB). Poziom Dirac musi być pomiędzy nimi. Inny stan nie daje podstaw do eksportu; porównanie SPL pozostaje dostępne."]),
 "check": ("Co sprawdza kontrola pomiarów", [
    "Uruchom Sprawdź pomiary po przypisaniu sesji i poziomów. Odczyt SPL nie jest jeszcze potwierdzeniem fazy.",
    "Program sprawdzi, czy MAIN+SUB odtwarza zmierzone SUM po obu stronach. Bez wspólnej referencji czasu nie wolno dobierać delayu między osobnymi sweepami.",
    "Kontrola zmiany poziomu może ujawnić więcej niż jedno rozwiązanie. Wtedy projektowanie zostaje zatrzymane; potrzebny jest pomiar rozstrzygający, a nie wybór dogodnej liczby.",
    "Jeśli brakuje sumy kontrolnej, zmierz ją w warunkach całej sesji i zapisz kompletny MDAT. Odłącz poprzedni niepełny plik z projektu przed przypisaniem nowego do tej samej sesji. Odłączenie nie usuwa pliku z dysku.",
    "Jeśli komunikat wymaga potwierdzenia warunków lub pozycji, otwórz Opisz pomiary. Dla błędu poziomów sprawdź Poziomy subów i warunki drugiej sesji. Nie potwierdzaj nieznanych danych.",
    "Jeśli MDAT nie da się jednoznacznie odczytać, wczytaj ślady z otwartego REW. Treść blokady znajdziesz przy konkretnym pliku w tabeli wyników kontroli."]),
 "verify": ("Dlaczego trzeba sprawdzić odniesienie", [
    "Wczytaj obecne ustawienie zmierzone przed Dirac i przypisz je do dokładnej wersji ustawienia z konsoli.",
    "Sprawdź zgodność poziomu, pozycji mikrofonu, toru i głośności z serią bazową.",
    "Kliknij Sprawdź obecne ustawienie. Program spróbuje odtworzyć rzeczywisty pomiar z RAW i parametrów. Nie udowadnia w ten sposób każdego absolutnego delayu z osobna.",
    "Konflikt lub brak pomiaru ma pozostać widoczny. Sama nazwa 90 BW48 albo załączony plik nie stanowią dowodu."]),
 "design": ("Jak wybrać próbę, która ma sens", [
    "Zatwierdź zakres częstotliwości, a następnie uruchom Znajdź ustawienia do sprawdzenia. Program bada standardowe rodziny BW48/LR48.",
    "Otrzymasz odniesienie i najwyżej dwa warianty. Zobacz sumowanie obu stron, fundament i dolną średnicę. Większy SPL nie oznacza sam w sobie lepszego dźwięku.",
    "Jeżeli standardowe filtry nie dają wystarczającej poprawy, możesz uruchomić Własną zwrotnicę. Ta opcja projektuje sekcje na pomiarach; obowiązują te same kontrole.",
    "Zaznacz jeden wariant i przygotuj ustawienia do testu. Przed zapisem program ponownie sprawdzi dokładnie ten wariant. Brak kandydata jest poprawnym wynikiem — nie trzeba wymuszać zmiany."]),
 "trial": ("Od obliczeń do rzeczywistego brzmienia", [
    "Zachowaj kopię znanego slotu i targetu Dirac. Wykorzystaj osobny slot do próby; aplikacja niczego nie wgrywa automatycznie.",
    "Wczytaj cztery filtry do bloku Crossover (96 kHz) zgodnie z OUT1=L kolumna, OUT2=R kolumna, OUT3=L sub, OUT4=R sub. Nie dubluj ich w PEQ. Wprowadź dokładne delaye i poziomy z plików próby.",
    "Najpierw zmierz nowy wariant z Dirac OFF: MAIN, SUB i SUM osobno dla L/R oraz sumę stereo. Sprawdź zgodność z symulacją. Duża rozbieżność wymaga wyjaśnienia przed kalibracją.",
    "Dopiero potem wykonaj nową sesję Dirac dla tego wariantu. Zachowaj porównywalny target, kurtyny i układ punktów. Mikrofon UMIK-1 pionowo z właściwym plikiem 90°. Dokładny rozstaw według zatwierdzonej procedury sesji.",
    "Zmierz oba warianty po ich właściwych filtrach Dirac, z kapsułą w tej samej pozycji. Dodaj nazwę nowego ustawienia w Porównaniu, wczytaj pomiary i przypisz je do właściwych wersji.",
    "Porównaj A/B przy wyrównanej głośności. Zachowaj odniesienie, jeśli wynik pomiaru lub odsłuchu nie daje przekonującej poprawy."]),
 "names": ("Dwa ustawienia bez powtarzania projektu", [
    "Kliknij Dodaj nazwę ustawienia. Dodaj dwie czytelne nazwy, np. Obecna sesja C268 i Nowa sesja C268.",
    "Nie musisz dopisywać nieznanych opóźnień do porównania SPL. Taka nazwa nie nadaje statusu zweryfikowanej referencji projektowej.",
    "W Opisie pomiarów przypisz ślady do A albo B, wybierz etap oraz rzeczywistą pozycję kapsuły."]),
 "compare": ("Jak oceniać zmianę brzmienia", [
    "Wybierz A i B oraz wspólny etap: oba przed Dirac albo oba po Dirac. Potrzebne są zmierzone sumy tego samego kanału i położenia mikrofonu.",
    "Różnice w tabeli to B minus A. Plus oznacza więcej poziomu w danym paśmie, a nie automatycznie lepsze brzmienie. Bez impulsu nie ma oceny wygaszania, a sam SPL nie dowodzi poprawnej fazy.",
    "Wyrównaj głośność odsłuchową A/B, użyj tych samych krótkich fragmentów. Zacznij od znanego wokalu i instrumentów akustycznych, następnie sprawdź fundament i kontrolę basu, potem scenę i zmęczenie.",
    "Zapisz A, B lub brak wyraźnej różnicy i własną notatkę. Naturalność średnicy i preferencję potwierdzasz słuchem; program nie przyznaje punktów muzykalności.",
    "Gdy obecne ustawienie już dobrze brzmi, możesz porównać tylko dwie sesje Dirac przy identycznej zwrotnicy. Nie ma obowiązku ponownego projektowania."]),
}


def help_text(key):
    title, paragraphs = HELP.get(key, HELP["sources"])
    return title, "\n\n".join(f"{i}. {text}" for i, text in enumerate(paragraphs, 1))
