# REWDO 20.34-dev2 — pełne porównanie po Dirac

## Uruchomienie

Rozpakuj całą paczkę do nowego folderu i uruchom `START_REWDO.bat`. Skrypt korzysta z Pythona z Tcl/Tk, tworzy środowisko w profilu użytkownika i przy pierwszym uruchomieniu instaluje zależności. `BUDUJ_EXE_WINDOWS.bat` w głównym folderze uruchamia budowę EXE na Windows. Budowy EXE i pomiaru sprzętowego nie wykonano w środowisku przygotowania tej paczki.

## Jedno porównanie A/B

1. W **Porównanie i odsłuch** wybierz etap **Po Dirac**, A jako odniesienie i B do sprawdzenia. Nazwy ustawień dodasz przyciskiem **Dodaj nazwę ustawienia**.
2. Kliknij **Pełne porównanie…**.
3. Wybierz jeden plik z pomiarami obu ustawień oraz jedno miejsce kapsuły. Jeżeli w projekcie jest tylko jedna możliwość, program ją uzupełni.
4. Wskaż konkretne L, R i L+R osobno dla A i B. Przy jednym dostępnym śladzie wybór jest automatyczny. Przy kilku pomiarach trzeba wskazać właściwy; program nie wybiera ostatniego na podstawie nazwy lub kolejności.
5. Potwierdź rzeczywiste warunki serii: ten sam tor i głośność główna, opisana zmiana A/B, wspólna metoda referencji czasu i nieruchomy mikrofon w tym miejscu. Niezaznaczenie referencji lub nieruchomego mikrofonu pozwala odczytać SPL, ale blokuje pełną ocenę fazy i czasu.
6. Pozostaw **Dołącz czas i powtarzalność**, jeśli chcesz wspólną ocenę. Czas wymaga migawki API REW z impulsami, co najmniej trzech osobnych pomiarów A dla każdej roli L/R/L+R i co najmniej po jednym dla B. Te same powtórzenia muszą mieć w projekcie właściwy etap, konfigurację i pozycję.
7. Uruchom **Porównaj po Dirac**. Na karcie **Co zrobić teraz** zobaczysz wynik, zalecane działanie, dostępność poszczególnych obszarów i metryki. Zaznacz obszar w tabeli, aby przeczytać cały opis.
8. **Otwórz raport** prowadzi do podsumowania Markdown, metryk CSV, przypisań CSV i danych kontroli JSON. **Zapisz podsumowanie** zapisuje czytelny raport w wybranym miejscu.

Sześć wskazanych śladów służy do porównania SPL i sumowania stereo. Analiza czasu korzysta ze wszystkich przypisanych powtórzeń A/B z tego samego pliku, etapu i miejsca, dzięki czemu może wyznaczyć rozrzut A. W raporcie oba zakresy są rozróżnione.

## Przygotowanie danych

W **Pomiary → Opisz pomiary** przypisz każdemu śladowi rzeczywistą konfigurację, etap, rolę i pozycję mikrofonu. Kolumna L/R oznacza stronę, a pozycja — miejsce kapsuły. Nazwy śladów nie muszą mieć specjalnego formatu.

Pełne porównanie fazy wymaga zmierzonych L, R i L+R. Program niezależnie sprawdza, czy zespolone L+R odtwarzają zmierzoną sumę, używając zachowanej kontroli fazy w jej paśmie kontrolnym. Nie zastępuje brakującego pomiaru L+R sumą obliczoną.

MDAT może udostępniać kilka kandydatur fazy; zgodność sumy służy do ich kontroli. Jeżeli wynik pozostaje niejednoznaczny, odczyt SPL jest zachowany, a do dalszej kontroli trzeba pobrać właściwe pomiary z otwartego REW.

Przycisk **Wczytaj pomiary z otwartego REW** otwiera istniejący import API. Program prosi o SPL, fazę, group delay i impuls. Po imporcie potwierdź przypisania, a następnie wróć do pełnego porównania.

Przycisk **Tylko czas i powtarzalność…** zachowuje dostęp do osobnego okna z dev1.

## Jak czytać wynik

- Surowa tabela i wykres głównego okna pokazują B−A bez wyrównania poziomu, dla konkretnych wybranych pomiarów.
- Metryki po Dirac zachowują wzory dawnego modułu. Wskaźniki względne mają pasmo odniesienia podane w nazwie. Wzrost liczby nie jest automatycznie poprawą brzmienia.
- Puste pole oznacza brak wiarygodnych danych albo całego wymaganego pasma. Nie przedłużamy pomiaru kończącego się na 250 Hz do średnicy i góry.
- Gdy nie wpisano rzeczywistej częstotliwości podziału w konfiguracji, metryki „w punkcie XO” pozostają niedostępne. Program nie przedstawia historycznego domyślnego 90 Hz jako nastawy sprzętu.
- Czas i powtarzalność zachowują ocenę całego zestawu L/R/L+R oraz ochronę podparcia dolnej średnicy z dev1. Spadek o 20 dB jest porównawczym zanikiem z impulsu, nie pełnym T20/RT60 ani waterfall REW.
- Pozytywny wynik czasu nie przykrywa niepotwierdzonej fazy ani braków danych. „Zakończono porównanie” oznacza wykonanie operacji; ocena każdego obszaru jest podana osobno.
- Ta decyzja dotyczy jednej wskazanej pozycji. Innych miejsc mikrofonu nie uznaje się za jej powtórzenia ani za dowód odporności przestrzennej.

Historyczne profile odsłuchowe 90/102 i nazwy plików nie ustalają zwycięzcy w tym oknie. Zachowany moduł i jego dotychczasowe narzędzia pozostają w źródłach. Nowe podsumowanie służy do ustalenia kolejnego działania na podstawie danych projektu; nie zmienia wag projektantów ani ustawień DSP.

## Odsłuch i zachowanie projektu

Jeżeli dane pozwalają przejść do odsłuchu, fizycznie wyrównaj głośność A/B. Porównaj wokal i instrumenty akustyczne, podparcie średnicy, fundament i kontrolę basu, scenę oraz zmęczenie. Własny wybór zapisz w głównym polu **Odsłuch**. Program nie dopisuje go za użytkownika i nie wyprowadza naturalności czy sceny z jednego wskaźnika.

Obliczenie działa w osobnym procesie. Przycisk zmienia się na **Przerwij porównanie**. Anulowany przebieg nie publikuje raportu jako ukończonego. Zamknięcie okna pozwala procesowi dokończyć pracę; wynik pozostaje w projekcie. Zmiana wybranych śladów lub przypisań unieważnia prezentowany wynik.

Bieżąca wersja: **20.34-dev2**, identyfikator w `BUILD_IDENTITY.json`. Starsze instrukcje i metadane pozostają dokumentacją historyczną. Nie wykonano automatycznego wgrania ustawień do sprzętu ani publikacji na GitHub.
