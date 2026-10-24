# REW Delay Optimizer (REWDO)

Aplikacja Python/Tkinter do pracy z pomiarami REW, integracji kolumn z subwooferami oraz porównań przed i po Dirac Live. Bieżąca baza robocza: **20.34-dev2**.

Projekt jest w trakcie porządkowania. Zachowuje wcześniejsze moduły DSP; aktualne GUI nie udostępnia jeszcze wszystkich historycznych możliwości. Wynik pomiaru, symulacja i wybór odsłuchowy mają odrębne znaczenie.

## Uruchomienie

Rozpakuj lub sklonuj całe repozytorium. Na Windows uruchom `START_REWDO.bat`; wymagany jest Python z Tcl/Tk. Skrypt tworzy środowisko w profilu użytkownika i instaluje zależności przy pierwszym uruchomieniu.

Ręcznie:

```sh
python -m pip install -r requirements-runtime.txt
python REWDO_GUI_SINGLE_BOOT.py
```

Na Linux Tcl/Tk musi być dostępny w użytej instalacji Pythona.

## Przebieg pracy

1. Wczytaj pomiary z MDAT albo lokalnego API REW.
2. Potwierdź role, konfiguracje, etap i pozycję mikrofonu.
3. Sprawdź dane i poziomy przed projektowaniem zwrotnicy.
4. Porównaj A/B. W `Porównanie i odsłuch → Po Dirac → Pełne porównanie…` dostępne są metryki po Dirac, kontrola sumy stereo i opcjonalna analiza czasu.
5. Przeczytaj `Co zrobić teraz`, wykonaj potrzebny pomiar i zapisz własny wybór odsłuchowy.

Pełna analiza czasu wymaga impulsów API i powtórzeń odniesienia. Brak danych nie jest pozytywną oceną. Jedna pozycja mikrofonu nie potwierdza odporności przestrzennej, a metryki nie mierzą naturalności wokalu ani jakości brzmienia.

Instrukcja bieżącej wersji: [INSTRUKCJA_v20_34_dev2.md](INSTRUKCJA_v20_34_dev2.md). Historia importu i zakres danych: [docs/MIGRATION.md](docs/MIGRATION.md). Następne zadania: [docs/ROADMAP.md](docs/ROADMAP.md).

## Testy i budowa Windows

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

Testy okien wymagają ekranu Tk. Trzy regresje na prywatnym MDAT są opcjonalne: można uruchomić je lokalnie, ustawiając `REWDO_PRIVATE_LEGACY_MDAT`. Sam plik nie jest publikowany. Pozostałe dane testowe są syntetyczne. Publiczny model kalibracji jest pusty i wyłączony; test dwóch stanów jednej topologii korzysta z jawnego syntetycznego przykładu.

Budowa na Windows: `BUILD_GUI_ONEDIR_EXE.bat`. Tworzy nowy katalog wynikowy. Import repozytorium nie oznacza odbioru EXE Windows, pomiaru sprzętowego ani odsłuchu. Nie skonfigurowano automatycznych budów ani publikacji artefaktów Actions.

## Dane i status

Pomiary, projekty odsłuchowe, rzeczywiste profile referencji i wyuczone kalibracje pozostają lokalne. Repozytorium zawiera kod i syntetyczne przykłady. Historyczne reguły lub nazwy w starszych silnikach wymagają audytu; nie są uniwersalnymi rekomendacjami nastaw sprzętu.

**Licencja: do ustalenia przez właściciela projektu.** Plik LICENSE nie został jeszcze dodany.
