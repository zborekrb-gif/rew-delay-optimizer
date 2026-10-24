# REWDO 20.33 — od pomiarów do odsłuchu

Rozpakuj całą paczkę i uruchom **START_REWDO.bat**. To aplikacja z Pythona, nie gotowy EXE. Wymaga Pythona Windows z Tcl/Tk (minimum 3.10, zalecany 3.12). Pierwszy start instaluje zależności w profilu użytkownika i wymaga internetu.

## Zacznij od „Co teraz”

U góry okna widzisz jeden zalecany krok, jego cel i przycisk prowadzący do właściwego miejsca. Po zapisaniu danych lub wykonaniu kontroli wskazówka się zmienia. **F1** otwiera plan, **Ctrl+S** zapisuje projekt.

Przycisk **Plan i instrukcje krok po kroku** pokazuje całą drogę i dokładne instrukcje pomiarowe. Zaznacz etap po lewej; po prawej przeczytasz bieżący stan i kolejne czynności. Nie trzeba znać numerów silników ani skryptów.

## Wybierz cel

- **Dobrać połączenie kolumn i subów:** zapisz obecne ustawienie, dołącz pomiary bez zwrotnicy DSP, kontrolę poziomu i odniesienie przed Dirac. Wpisz rzeczywiste poziomy i wykonaj kontrole. Potem porównaj najwyżej dwa warianty, przygotuj jeden do próby, zmierz go i wykonaj właściwy dla niego Dirac.
- **Porównać gotowe pomiary / sesje Dirac:** dodaj dwie nazwy ustawień, wczytaj pomiary i opisz ich etap oraz miejsce mikrofonu. RAW ani znajomość delayów nie są potrzebne do samego porównania SPL. Ten cel wybierzesz w planie lub przechodząc do zakładki Porównanie i odsłuch.

Jeśli obecne C268 dobrze brzmi i chcesz sprawdzić inną sesję Dirac przy tej samej zwrotnicy, skorzystaj z porównania gotowych pomiarów. Nie trzeba projektować zwrotnicy od nowa.

## Jak opisać plik

1. Wczytaj MDAT lub pomiary z otwartego REW.
2. Zaznacz wiersz i kliknij **Opisz pomiary**. Jeśli korzystasz z „Co teraz”, aplikacja wskaże właściwy plik.
3. Wybierz etap sesji i ustawienie. Dla każdego pomiaru sprawdź, co grało: kolumna, subwoofer, ich para lub cały system.
4. Wpisz rzeczywiste miejsce mikrofonu, np. MLP. Kanał L/R oznacza grającą stronę, a nie położenie mikrofonu.
5. Dla jednolitej sesji możesz zastosować etap, ustawienie i wpisane miejsce do wszystkich pomiarów. Rolę każdego pomiaru zachowujemy osobno.
6. Potwierdź znane warunki sesji i uruchom kontrolę. Bieżąca edycja wybranego pomiaru zapisuje się także po kliknięciu końcowego przycisku.

Wyniki kontroli zobaczysz przy konkretnym pliku. Zaznaczenie wiersza pokazuje całą przyczynę poniżej; długą treść można przewijać. Nie trzeba szukać jej w logu. Nieznanych warunków nie potwierdzaj na podstawie samej nazwy pliku.

Gdy zastępujesz niepełny pomiar kompletnym, użyj **Odłącz plik**, a potem wczytaj nowy. Odłączenie usuwa powiązanie z projektu i unieważnia zależne wyniki; oryginalny plik pozostaje na dysku. Do projektowania wybierz jeden kompletny zestaw pomiarów bez zwrotnicy DSP. Gdy zastępujesz go nowym zestawem, odłącz poprzedni plik od projektu.

## Od wariantu do decyzji

Po obliczeniu wariantów zaznacz jeden w tabeli. Przycisk przygotowania plików pojawia się tylko dla aktualnego, dopuszczonego wyniku i ponownie sprawdza dokładnie ten wariant. Zapis nie oznacza poprawy na sprzęcie. Aplikacja nie wgrywa filtrów ani nie zmienia miniDSP.

Po eksporcie otworzy się instrukcja próby: kopia znanego slotu → ustawienia wariantu → pomiar z Dirac OFF → sprawdzenie wyniku → nowa kalibracja Dirac → pomiar po Dirac → odsłuch A/B. Przyciskiem **Wczytaj pomiar wariantu** przejdziesz do dalszego porównania, bez ponownego projektowania. Nowe dane unieważniają wcześniejsze obliczenia; historyczne pliki próby nie stają się przez to bieżącym wynikiem analizy.

Porównuj ten sam etap i miejsce mikrofonu. W tabeli plus oznacza większy poziom B względem A, nie automatycznie lepszy dźwięk. W odsłuchu wyrównaj głośność, użyj tych samych fragmentów i oceń wokal, fundament oraz kontrolę basu, scenę i zmęczenie. Zapisz A, B lub brak wyraźnej różnicy. Pomiar nie rozstrzyga naturalności ani preferencji.

## Projekt i ograniczenia

Program pamięta ostatni projekt i etap. Zapis działa poza katalogiem aplikacji, domyślnie w `%LOCALAPPDATA%\REWDO`. Pliki pomiarowe pozostają w swoich miejscach; po przeniesieniu wskaż ich nową lokalizację. Projekty v20.32 są obsługiwane bez zmiany formatu.

Nie wpisuj historycznych opóźnień ani −6/−4/0 dB jako bieżących bez sprawdzenia. Zabezpieczenia fazy, poziomów, odniesienia i eksportu nadal obowiązują. Bieżący silnik ma ograniczenia dotyczące wspólnego poziomu L/R, delayu kolumn i potwierdzonej zmiany poziomu — plan je opisuje; nie omijamy ich dla wygody interfejsu.

Wersja do odbioru użytkowego. Okna Tk sprawdzono na Linuxie przy dwóch skalach; nie wykonano natywnego testu Windows, budowy EXE ani próby na sprzęcie. Kod, testy, raport i skrypt budowy Windows są w folderze deweloperskim.
