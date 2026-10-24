# REWDO 20.32

Rozpakuj całą paczkę i uruchom `START_REWDO.bat`. Nie uruchamiaj aplikacji wewnątrz ZIP-a.

Ta paczka zawiera aplikację uruchamianą z Pythona, nie gotowy EXE Windows. Wymaga Pythona dla Windows z Tcl/Tk (zalecany 3.12; minimum 3.10). Przy pierwszym starcie launcher instaluje zależności w profilu użytkownika, więc potrzebny jest dostęp do internetu. Nie nadpisuje poprzedniej wersji programu.

## Trzy miejsca pracy

1. **Pomiary:** wczytaj MDAT albo pobierz pomiary z otwartego REW. Sprawdź przypisanie elementu, kanału, presetu, etapu i pozycji mikrofonu. Kanał L/R nie oznacza przesunięcia mikrofonu. Dane i warunki sesji potwierdzasz sam — program nie odczytuje konsoli miniDSP.
2. **Dobór ustawień:** w „Mój system” wpisz odniesienie, poziomy subwooferów podczas pomiarów bez zwrotnicy DSP, kalibracji Dirac i odsłuchu i zakres poszukiwania. Powiąż odniesienie z pomiarem przed Dirac. Główna akcja szuka zwrotnicy i opóźnień; druga uruchamia projektowanie własnej zwrotnicy. Zobaczysz odniesienie i najwyżej dwa warianty. Brak dopuszczonego wariantu jest normalnym wynikiem.
3. **Porównanie i odsłuch:** wybierz dwa presety i etap przed lub po Dirac. Możesz zacząć tutaj bez RAW i bez parametrów zwrotnicy: w „Mój system” dodaj same nazwy presetów, a potem przypisz do nich pomiary. Zapisz własny wybór A/B i informację o wyrównaniu głośności.

„Zapisz projekt” pozwala wybrać lokalizację. Program pamięta ostatni projekt i krok. Projekt wskazuje źródła — nie przenoś ani nie usuwaj pomiarów; po przeniesieniu użyj „Wskaż przeniesiony plik”. Zapis projektu oraz przebiegów jest poza katalogiem aplikacji. Domyślnie są w `%LOCALAPPDATA%\REWDO`.

## Przed próbą na sprzęcie

Przycisk „Przygotuj ustawienia do testu” pojawia się tylko dla aktualnego, dopuszczonego wariantu. Powtarza kontrolę i zapisuje cztery filtry wraz z delayami, poziomami i kopią odniesienia. Samo obliczenie nie tworzy plików do importu.

Zachowaj kopię aktywnego slotu. Najpierw wykonaj pomiar z Dirac OFF, następnie nową kalibrację Dirac i pomiar kontrolny. Nie używaj starego filtra Dirac z nową zwrotnicą. Aplikacja nie wgrywa nic do miniDSP i nie zmienia ustawień sprzętu.

Aktualne projektanty wymagają identycznych poziomów L/R, wspólnego delayu kolumn i zweryfikowanej zmiany poziomu od pomiarów bez zwrotnicy DSP do odsłuchu. Inne wartości są zachowywane, lecz ten rodzaj projektowania zostanie zablokowany; porównanie poprawnych pomiarów nadal działa. Nie wpisuj domyślnych −6/−4/0, jeśli sesja miała inne ustawienia.

Jeśli stary MDAT nie pozwala jednoznacznie odczytać pomiarów, użyj importu z otwartego REW. Bez sum kontrolnych nie uzyskasz weryfikacji nowej konfiguracji. Brak impulsu nie przeszkadza w oglądaniu SPL, ale ogranicza analizę czasu.

Szczegóły techniczne i jeden raport są dostępne z aplikacji. W folderze `deweloper` znajdują się raport testów oraz osobne archiwum kodu, testów i historii. Natywny interfejs Windows, skalowanie DPI i EXE nie przeszły jeszcze odbioru na Windows — raport nie deklaruje takich testów.
