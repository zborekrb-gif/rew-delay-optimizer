# REWDO v20.29 — MDAT Phase Truth Guard

## Problem

Plik REW `.mdat` jest serializacją Javy, a nie publicznym, stabilnym formatem tabel SPL/fazy. W jednym rekordzie pomiarowym mogą wystąpić co najmniej dwie równodługie tablice spełniające prosty test „wygląda jak faza”. v20.28 przyjmował pierwszą z nich. Sam poprawny zakres wartości, brak `NaN` i zgodna liczba punktów nie dowodzą, że jest to faza właściwa do zespolonego sumowania.

## Zasada v20.29

REWDO najpierw zachowuje wszystkie kandydatury. Dla każdej strony sprawdza kombinacje:

`H_pred(f) = H_main(f) + H_sub(f)`

i porównuje je z rzeczywiście zmierzonym `H_sum(f)`.

Ocena odbywa się w akustycznym paśmie współpracy źródeł, z pominięciem fragmentów, w których jedna składowa jest za słaba, aby wiarygodnie weryfikować relację fazową. Dla każdego powtórzenia liczone są:

- błąd SPL RMS;
- mediana bezwzględnego błędu SPL;
- błąd fazy RMS po zawinięciu do ±180°;
- mediana bezwzględnego błędu fazy;
- liczba punktów i rzeczywiste pasmo walidacji.

Przy kilku powtórzeniach wybór opiera się na medianie, ale kontrolowane jest również najgorsze powtórzenie.

## Progi bramki

Kandydatura przechodzi, jeżeli jednocześnie:

- mediana błędu SPL RMS ≤ 0,90 dB;
- mediana błędu fazy RMS ≤ 12°;
- najgorszy błąd SPL RMS ≤ 1,50 dB;
- najgorszy błąd fazy RMS ≤ 25°;
- przewaga nad drugą kombinacją wynosi co najmniej 0,35 w skali `SPL_RMS + faza_RMS/20`.

Progi są celowo znacznie szersze od wyniku prawidłowej tablicy zaobserwowanego w serii Slotu 2, a jednocześnie wyraźnie oddzielają błędną tablicę dającą około 3–3,7 dB i 65–71° rozbieżności.

## Statusy

| Status | Znaczenie | Działanie |
|---|---|---|
| `VERIFIED` | Obie strony odtworzyły zmierzone SUM | faza i ranking delayu dozwolone |
| `VERIFIED_PARTIAL` | Jedna strona zweryfikowana SUM, pozostałe role jednoznaczne strukturalnie | dozwolone z jawnym źródłem |
| `STRUCTURAL_ONLY` | Brak SUM, ale każda wymagana rola ma jedną kandydaturę | dozwolone z ostrzeżeniem |
| `BLOCKED` | kandydatury są niejednoznaczne albo rekonstrukcja nie przechodzi progów | ranking fazy/delayu/polaryzacji zatrzymany |
| `API_INTEGRITY` | faza pochodzi z jawnej odpowiedzi `frequencyResponse` zweryfikowanej migawki REW API | dozwolone |

## Nazwy pomiarów

Minimalna seria kontrolna:

- `L_MAIN`, `L_SUB`, `L_SUM1`;
- `R_MAIN`, `R_SUB`, `R_SUM1`.

Seria zalecana:

- `L_SUM1`, `L_SUM2`, `L_SUM3`;
- `R_SUM1`, `R_SUM2`, `R_SUM3`;
- opcjonalnie `LR_SUM1`, `LR_SUM2`, `LR_SUM3` do dalszej kontroli stereo.

Wszystkie pomiary muszą używać tej samej acoustic timing reference, tej samej pozycji mikrofonu, poziomu i częstotliwości próbkowania.

## Raporty

- `MDAT_PHASE_TRUTH_GUARD.md` — decyzja o źródle fazy, kandydatury i błędy rekonstrukcji;
- `MDAT_HEALTH_CHECK.md` — bramka całego importu;
- `MDAT_NUMERIC_IMPORT.md` — role i źródło fazy użytej do obliczeń.

Status `BLOCKED` nie oznacza, że SPL jest bezużyteczny. Oznacza, że nie wolno na jego podstawie wykonywać zespolonego rankingu delayu, fazy ani polaryzacji.

