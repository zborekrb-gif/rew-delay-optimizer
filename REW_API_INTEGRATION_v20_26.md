# REWDO v20.28 — opcjonalne połączenie z API REW 5.40

## Co zostało dodane

REWDO ma teraz dwa równoległe źródła danych:

1. **MDAT offline** — dotychczasowy importer plików `.mdat` pozostaje bez zmian w codziennym użyciu;
2. **REW 5.40 API** — aplikacja może połączyć się z uruchomionym REW, wyświetlić aktualną listę pomiarów i pobrać wybrane dane do lokalnej migawki `*.rewapi.json`.

Migawka API jest następnie używana przez te same tryby REWDO co plik MDAT. Nie trzeba eksportować pomiarów do TXT.

## Uruchomienie

1. Uruchom REW 5.40.
2. W REW otwórz ustawienia API i uruchom serwer API.
3. W REWDO naciśnij **REW API…**.
4. Domyślny adres to `http://127.0.0.1:4735`.
5. Naciśnij **Połącz / odśwież**.
6. Zaznacz pomiary. Dla podstawowego RAW można użyć przycisku **Zaznacz podstawowe RAW**.
7. Naciśnij **Utwórz migawkę i użyj w REWDO…**.
8. Wybierz tryb analizy i uruchom go tak samo jak dla MDAT.

## Nazwy pomiarów

Automatyczne mapowanie działa na tych samych zasadach co importer MDAT. Zalecane nazwy:

```text
01_L_MAIN_D30.00
02_L_SUB_D20.40
04_R_MAIN_D30.00
05_R_SUB_D19.10
```

Dla pomiarów pełnych stron i po Dirac można używać m.in.:

```text
L STRONA
R STRONA
L+R
L Dirac ON
R Dirac ON
L+R Dirac ON
```

Jeżeli dwóm zaznaczonym pomiarom zostanie przypisana ta sama wymagana rola, REWDO zatrzyma analizę i poprosi o jednoznaczny wybór.

Jeżeli ten sam pomiar został w REW załadowany więcej niż raz i pozycje mają wspólny UUID, REWDO podczas tworzenia migawki używa ich bieżących indeksów listy.

## Jakie dane są pobierane

Obowiązkowo:

- SPL;
- faza;
- metadane pomiaru i grupy;
- zakres częstotliwości;
- wersja REW podana w podsumowaniu pomiaru.

Opcjonalnie:

- group delay;
- odpowiedź impulsowa.

Obecne silniki zwrotnic wykorzystują bezpośrednio SPL i fazę. Group delay oraz impuls są zachowywane w migawce jako materiał do kolejnego etapu rozwoju analizy czasu, decay i waterfall.

## Dlaczego migawka zamiast pracy wyłącznie „na żywo”

Migawka daje powtarzalność:

- wynik można później odtworzyć bez uruchomionego REW;
- wiadomo dokładnie, które pomiary były podstawą rankingu;
- kolejne zmiany nazw lub usunięcie pomiarów w REW nie zmieniają starej analizy;
- plik można dołączyć do paczki walidacyjnej.

Migawka nie zastępuje oryginalnego projektu `.mdat`. MDAT nadal powinien być głównym archiwum pomiarów REW.

## Bezpieczeństwo

Integracja v20.28 jest celowo **odczytowa**:

- nie usuwa pomiarów;
- nie zmienia ich nazw;
- nie uruchamia automatycznie sweepów;
- nie zmienia ustawień generatora, EQ ani Alignment Tool;
- nie wymaga REW Pro do pobierania istniejących danych.

REWDO domyślnie odrzuca adresy inne niż `localhost` i `127.0.0.1`. Dzięki temu nie wysyła pomiarów poza komputer.

## Ograniczenia obecnej wersji

- Automatyczne wykonywanie sekwencji sweepów nie zostało wdrożone.
- REWDO nie steruje miniDSP SHD Studio przez API REW.
- Dane impulsowe są przechowywane, ale nie są jeszcze automatycznie uwzględniane w rankingu „szybkości basu”.
- API REW 5.40 jest częścią wersji beta, dlatego klient ma tolerancyjny parser nazw pól, ale po dużej zmianie API może wymagać aktualizacji.


## Gdy lista pomiarów jest pusta

Wersja v20.28 naprawia dwa problemy okna API na Windows: odczyt zmiennych Tkinter z wątku roboczego oraz znikający komunikat wyjątku. Rozszerza też obsługę odpowiedzi `/measurements` z różnych wydań beta.

Po naciśnięciu `Połącz / odśwież` aplikacja powinna teraz pokazać jeden z dwóch jednoznacznych rezultatów:

- `Odczytano N pomiarów` — tabela zostanie wypełniona;
- `REW zwrócił 0 pomiarów` — API działa, ale w tej instancji REW nie ma otwartych pomiarów.

Jeżeli pojawi się błąd HTTP lub formatu JSON, komunikat jest wyświetlany w oknie zamiast pozostawienia pustej tabeli.

## Potwierdzenie RAW i eksport biquadów w v20.28

API REW nie udostępnia ustawień wyjść miniDSP SHD. W oknie **REW API** trzeba więc przed utworzeniem migawki potwierdzić stan obowiązujący podczas wszystkich zaznaczonych sweepów:

- Dirac OFF;
- crossover SHD OFF;
- PEQ OFF;
- delaye wszystkich wyjść 0,00 ms;
- polaryzacja normalna;
- filtr analogowy REL 120 Hz;
- ta sama metoda timing reference;
- mikrofon nieruchomo;
- rzeczywiste gainy main i sub.

Przycisk **Wypełnij typowy RAW 0 / −6 dB** uzupełnia typowe wartości, ale trzeba je sprawdzić przed zatwierdzeniem. Kompletne potwierdzenie zapisuje w migawce sekcję `measurementState` i nadaje status `RAW_VERIFIED_BY_API_CONFIRMATION`. Dopiero ten status odblokowuje eksport plików biquadów. Niepełne potwierdzenie nadal pozwala utworzyć migawkę diagnostyczną, ale eksport pozostaje zablokowany.

Po aktualizacji starszą migawkę v20.26/v20.26a należy utworzyć ponownie, ponieważ nie zawiera ona sekcji `measurementState`.


## v20.28 — dane projektowe bez smoothingu

Tryb projektowy nie wysyła parametru `ppo`. REW zwraca natywną oś pomiaru, a REWDO jawnie pobiera listę `smoothing-choices` i wybiera opcję oznaczającą brak wygładzania. Migawka zapisuje żądane i zwrócone ustawienia. Jeśli REW nie potwierdzi braku wygładzania, migawka nie zostanie użyta do eksportu biquadów.

Opcjonalnie można wskazać MDAT zawierający ten sam komplet pomiarów. Kontrola porównuje SPL i fazę w rolach L main, L sub, R main i R sub. Wynik FAIL jest zapisywany w migawce i blokuje eksport.


## v20.28 — dane czasowe

Migawka formatu 3 może zawierać `groupDelayInfo` i `impulseInfo`. Dla trybu 10 klient pobiera impuls bez normalizacji i bez okna (`normalised=false`, `windowed=false`). W oknie API dostępny jest przycisk **Zaznacz sumy do analizy czasowej**.
