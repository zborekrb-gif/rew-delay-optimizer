# REWDO 20.34-dev1 — czas i powtarzalność

To pełna kopia robocza programu z nową drogą do zachowanej analizy czasu. Zawiera poprawki kalibratora i walidacji z etapu 2. Budowę EXE Windows uruchamia `BUILD_GUI_ONEDIR_EXE.bat`; tej budowy nie wykonano w środowisku autora paczki.

## Uruchomienie

Rozpakuj cały ZIP do nowego folderu. Uruchom `START_REWDO.bat` obok tej instrukcji albo `RUN_APP.cmd` w folderze `kod`. Wymagany jest Python z Tcl/Tk; skrypt tworzy środowisko w profilu użytkownika i przy pierwszym uruchomieniu instaluje zależności programu. Nie wymaga zmieniania poprzedniej instalacji. Projekty zapisuj jako osobne pliki.

## Jak otworzyć pełną analizę czasu

1. W zakładce **Porównanie i odsłuch** wybierz etap oraz dwa ustawienia. **A jest odniesieniem**, B sprawdzanym wariantem. Możesz użyć gotowych pomiarów; RAW i projektowanie zwrotnicy nie są warunkiem tego porównania.
2. Kliknij **Czas i powtarzalność…**.
3. Wybierz jeden plik zawierający A i B oraz jedno miejsce mikrofonu. Tabela pokaże liczbę przypisanych pomiarów. Do ustalenia rozrzutu A potrzeba co najmniej trzech osobnych sweepów L, trzech R i trzech L+R. Dla B potrzeba co najmniej po jednym. Kolejne powtórzenia B są agregowane medianą.
4. Potwierdź rzeczywiste warunki tej serii: ten sam tor i głośność główna, ta sama metoda referencji czasowej, nieruchomy mikrofon w wybranym miejscu. Potwierdzenia odnoszą się do wybranego pliku, A/B, etapu i pozycji. Po zmianie pliku lub pozycji trzeba je zaznaczyć ponownie.
5. Kliknij **Oblicz pełną analizę**. Obliczenie działa w osobnym procesie. Ten sam przycisk pozwala przerwać analizę. Zamknięcie okna pozwala procesowi dokończyć pracę; wynik zostanie zapisany w projekcie. Ponowne otwarcie okna wymaga ponownego obliczenia przed pokazaniem bieżącego werdyktu.
6. Przeczytaj kartę **Wynik i powtarzalność**: wyjaśnienie, różnice L/R/L+R, rozrzut odniesienia i progi uznania zmiany za istotną. **Dane i raport** pokazują zanik w poszczególnych pasmach.
7. Użyj **Otwórz raport**, aby zobaczyć podsumowanie i cztery tabele CSV. **Zapisz podsumowanie** tworzy czytelny raport w wybranym miejscu.

## Jeśli masz tylko MDAT

W tej ścieżce programu odczyt MDAT dostarcza danych do porównania SPL. Pełna analiza czasu wymaga migawki REW API z impulsem:

1. Otwórz odpowiedni MDAT w REW i udostępnij lokalne API REW.
2. W REWDO wybierz **Wczytaj pomiary → Z otwartego REW** albo przycisk importu w oknie analizy czasu. Program pobiera natywne SPL i fazę oraz prosi API o group delay i impuls.
3. W **Pomiary → Opisz pomiary** przypisz śladom ustawienie A lub B, rolę sumy L/R/L+R, etap i miejsce kapsuły. Potwierdź przypisanie. Nazwy śladów mogą pozostać takie jak w REW.
4. Wróć do pełnej analizy czasu. Lista pomiarów ma pokazać A: co najmniej 3/3/3 i B: co najmniej 1/1/1.

Nie kopiuj jednego pomiaru, aby uzyskać trzy powtórzenia. Taka kopia nie wyznacza naturalnego rozrzutu sesji. Nie łącz różnych miejsc mikrofonu pod jedną nazwą.

## Co oznacza wynik

- **Późna energia**: udział energii pozostałej po 100 ms, wyznaczany z porównywalnych pasm impulsu. Dodatnia poprawa oznacza niższy udział w B.
- **Spadek o 20 dB**: porównawczy czas zaniku energii o 20 dB. Dodatnia poprawa oznacza krótszy czas w B. To nie jest pełny T20/RT60 ani waterfall z REW.
- **GD**: 90. percentyl względnego opóźnienia grupowego w 45–110 Hz. Program zachowuje dotychczasową metodę odniesienia pasmowego.
- **Dolna średnica**: ochrona podparcia w 100–160 i 125–220 Hz. W tej analizie porównanie kształtu SPL jest wyrównywane w paśmie podanym w szczegółach. Główna tabela SPL A/B nadal pokazuje surowe B−A bez tego wyrównania. Żadna z tych operacji nie wyrównuje fizycznej głośności odsłuchu.
- **Powtarzalność A**: próg wynika z rozrzutu powtórzeń oraz zachowanych progów modułu. Poprawa L+R nie wystarcza, jeżeli jedna ze stron pogarsza się ponad ten rozrzut.

Pomiar daje ocenę techniczną czasu i kształtu odpowiedzi. Nie mierzy naturalności wokalu, barwy ani zmęczenia odsłuchem. Jedno miejsce mikrofonu nie pozwala ocenić odporności przestrzennej.

Jeżeli wynik wskazuje poprawę, wyrównaj głośność odsłuchu A/B i sprawdź wokal, podparcie instrumentów, fundament i kontrolę basu oraz scenę. Własny wybór nadal zapisuje się w głównej części **Odsłuch** po porównaniu SPL A/B; program nie dopisuje wyniku odsłuchowego za użytkownika.

## Kontrole danych

Wynik jest powiązany z SHA-256 pliku i konkretnymi identyfikatorami śladów, wersjami konfiguracji, etapem oraz pozycją. Odczyt sprawdza integralność natywnej odpowiedzi, liczby w impulsach, zakres danych, zgodność skali czasu i próbkowania, referencję czasu oraz stabilność odniesienia A. Tożsamy UUID REW lub powtórzony indeks nie jest liczony jako niezależna próba.

Brakujące deklaracje warunków w migawce można uzupełnić jawnym potwierdzeniem w projekcie dla tego porównania. Zapisana sprzeczna deklaracja w źródle blokuje analizę. Źródłowy JSON nie jest poprawiany automatycznie. Deklaracja operatora pozostaje deklaracją, nie dowodem fizycznych warunków.

Brak danych group delay w API pozwala użyć zachowanej metody obliczenia z fazy; źródło jest zapisane w raporcie danych. Uszkodzone albo wygładzone dane GD nie są po cichu zastępowane. Niepełny impuls lub zbyt krótki ogon zatrzymuje pełny werdykt, ale nie odbiera możliwości oceny SPL.

Nie zmieniono filtrów, wag projektantów, polaryzacji ani nastaw urządzeń. Pełna analiza po Dirac z dawnego modułu pozostaje dostępna dotychczasową drogą i nadal wymaga osobnego adaptera do głównego projektu.

Starsze pliki instrukcji oraz `ETAP2_BUILD.json` są historią. Bieżącą kopię identyfikują wersja **20.34-dev1** i `BUILD_IDENTITY.json`.
