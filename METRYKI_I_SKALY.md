# Rejestr metryk i skal REWDO v20.31

## Zasada porównywania

Wyniki `/100` wolno porównywać bezpośrednio tylko wtedy, gdy w kolumnie `Grupa porównawcza` mają ten sam identyfikator. Dopisek `PREDYKCJA RAW`, `SYMULACJA PROJEKTU` albo `POMIAR POST-DIRAC` określa poziom dowodu. Identyczna matematyka nie zamienia symulacji w rzeczywisty pomiar.

| Identyfikator | Nazwa w raporcie | Etap | Skala | Kierunek | Tryby | Grupa porównawcza | Porównywalność |
|---|---|---|---|---|---|---|---|
| `side_integration_raw` | Sklejenie stron | PREDYKCJA RAW | 0–100 | więcej = lepiej | 1, 3, 5, 6 | `SIDE_INTEGRATION_V1` | Skala jest matematycznie zgodna z trybami 8–9, lecz RAW i projekt pozostają innymi etapami analizy. |
| `side_integration_project` | Sklejenie stron | SYMULACJA PROJEKTU | 0–100 | więcej = lepiej | 8, 9 | `SIDE_INTEGRATION_V1` | Liczba ma tę samą skalę co w trybach 1/3/5/6; dopisek etapu informuje, że jest to symulacja, a nie pomiar po Dirac. |
| `lr_coherence_raw` | Spójność sum L/R przy XO | PREDYKCJA RAW | 0–100 | więcej = lepiej | 1, 3, 5, 6 | `LR_COHERENCE_RAW_V1` | Nie jest zamiennikiem sklejenia main–sub jednej strony i nie jest porównywalna z bezpośrednią fazą main–sub trybu 9. |
| `lr_sum_project` | Jakość sumy stereo L+P | SYMULACJA PROJEKTU | 0–100 | więcej = lepiej | 8, 9 | `LR_SUM_PROJECT_V1` | Porównywalna między trybem 8 i 9; nie jest tą samą metryką co Spójność sum L/R przy XO w rankingu RAW. |
| `main_sub_phase_project` | Zgodność fazowa main–sub | SYMULACJA PROJEKTU | 0–100 | więcej = lepiej | 9 | `MAIN_SUB_PHASE_PROJECT_V1` | Metryka specyficzna dla projektanta biquadów; nie porównuj jej z fazą L/R sumy stereo. |
| `body_support_raw` | Podparcie dolnej średnicy | PREDYKCJA RAW | 0–100 | więcej = lepiej | 1, 3, 5, 6 | `BODY_SUPPORT_RAW_V1` | Nie porównuj liczbowo z ciągłością 100–220 Hz trybu 8/9 ani z teksturą po Dirac. |
| `body_continuity_project` | Ciągłość korpusu 100–220 Hz | SYMULACJA PROJEKTU | 0–100 | więcej = lepiej | 8, 9 | `BODY_CONTINUITY_PROJECT_V1` | Porównywalna między trybem 8 i 9; nie jest skalą Podparcia dolnej średnicy rankingu RAW. |
| `mid_texture_post` | Zachowanie tekstury dolnej średnicy | POMIAR POST-DIRAC | 0–100 / różnica dB | więcej = lepiej dla wyniku; 0 dB = referencja | 2 | `MID_TEXTURE_POST_V1` | Jest końcową oceną pomiarową; nie porównuj jej 1:1 z predykcją RAW ani symulacją projektu. |
| `spatial_robustness_raw` | Przewidywana odporność przestrzenna | PREDYKCJA RAW | 0–100 | więcej = lepiej | 1, 3, 5, 6 | `SPATIAL_ROBUSTNESS_RAW_V1` | Nie jest rzeczywistym pomiarem kilku pozycji. |
| `parameter_robustness_project` | Odporność na błąd opóźnienia i poziomu | SYMULACJA PROJEKTU | 0–100 (20. percentyl) | więcej = lepiej | 9 | `PARAMETER_ROBUSTNESS_PROJECT_V1` | Nie porównuj z odpornością przestrzenną z kilku pozycji mikrofonu. |
| `spatial_robustness_post` | Zmierzona odporność przestrzenna L/MLP/P | POMIAR POST-DIRAC | 0–100 | więcej = lepiej | 2 | `SPATIAL_ROBUSTNESS_POST_V1` | Najwyższy poziom wiarygodności przestrzennej; nie porównuj bezpośrednio z zmianami opóźnienia/poziomu. |
| `dirac_burden_raw` | Przewidywane obciążenie korekcji Dirac | PREDYKCJA RAW | 0–100 | mniej = lepiej | 1, 3, 5, 6 | `DIRAC_BURDEN_RAW_V1` | Nie porównuj liczbowo z lokalnym wskaźnikiem projektu ani z wynikiem po Dirac. |
| `dirac_burden_project` | Symulowane obciążenie korekcji Dirac | SYMULACJA PROJEKTU | 0–100 | mniej = lepiej | 8, 9 | `DIRAC_BURDEN_PROJECT_V1` | Porównywalny między trybem 8 i 9; nie oznacza liczby dB korekcji. |
| `dirac_result_post` | Jakość wyniku po korekcji Dirac | POMIAR POST-DIRAC | wiele wartości fizycznych / lokalne wyniki 0–100 | zależnie od pola | 2 | `DIRAC_RESULT_POST_V1` | Nie jest prognozą obciążenia Dirac i nie powinna używać tej samej krótkiej nazwy. |
| `lr_null_risk_raw` | Przewidywane ryzyko dziury L+P po Dirac | PREDYKCJA RAW | 0–100 | mniej = lepiej | 1, 3, 5, 6 | `LR_NULL_RISK_RAW_V1` | Nie porównuj liczbowo z symulacją zwrotnicy ani z rzeczywistym pomiarem po Dirac. |
| `lr_null_risk_project` | Symulowane ryzyko dziury L+P | SYMULACJA PROJEKTU | 0–100 | mniej = lepiej | 8, 9 | `LR_NULL_RISK_PROJECT_V1` | Porównywalne między trybem 8 i 9. |
| `lr_null_post` | Zmierzona dziura L+P po Dirac | POMIAR POST-DIRAC | dB / pomocnicze 0–100 | mniejsza dziura = lepiej | 2 | `LR_NULL_POST_V1` | To pomiar wyniku, nie ryzyko prognostyczne. |
| `music_score_raw` | Wynik muzyczny rankingu RAW | PREDYKCJA RAW | 0–100 | więcej = lepiej | 1, 3, 5, 6 | `MUSIC_SCORE_RAW_V1` | Nie jest absolutną oceną systemu i nie porównuj go z wynikiem selektora lub projektanta. |
| `selector_score_project` | Wynik selektora standardowych filtrów | SYMULACJA PROJEKTU | 0–100 | więcej = lepiej | 8 | `SELECTOR_SCORE_PROJECT_V1` | Porównuj tylko wewnątrz trybu 8 dla tego samego RAW. |
| `biquad_score_project` | Wynik projektanta biquadów | SYMULACJA PROJEKTU | 0–100 | więcej = lepiej | 9 | `BIQUAD_SCORE_PROJECT_V1` | Porównuj tylko między kandydatami tego samego profilu i tego samego RAW. |
| `replacement_score_post` | Wynik zastąpienia referencji 90 BW48 | POMIAR POST-DIRAC | 0–100 | więcej = lepiej | 2 | `REPLACEMENT_SCORE_POST_V1` | Nie porównuj z wynikiem muzycznym RAW ani wynikami projektantów. |

## Znaczenie metryk

### Sklejenie stron — PREDYKCJA RAW

Osobno ocenia L main+L sub i R main+R sub z konstruktywności oraz głębokości dołku; wynik ogólny mocniej waży słabszą stronę.

**Porównywalność:** Skala jest matematycznie zgodna z trybami 8–9, lecz RAW i projekt pozostają innymi etapami analizy.

### Sklejenie stron — SYMULACJA PROJEKTU

Ten sam wzór co w rankingu RAW, liczony po zasymulowaniu projektowanego HPF/LPF, delayu i polaryzacji.

**Porównywalność:** Liczba ma tę samą skalę co w trybach 1/3/5/6; dopisek etapu informuje, że jest to symulacja, a nie pomiar po Dirac.

### Spójność sum L/R przy XO — PREDYKCJA RAW

Kontroluje wzajemną fazę, balans i sumowanie gotowych stron L i R przy crossoverze.

**Porównywalność:** Nie jest zamiennikiem sklejenia main–sub jednej strony i nie jest porównywalna z bezpośrednią fazą main–sub trybu 9.

### Jakość sumy stereo L+P — SYMULACJA PROJEKTU

Ocena konstruktywności, dołku i asymetrii sumy dwóch gotowych stron po zasymulowaniu zwrotnicy.

**Porównywalność:** Porównywalna między trybem 8 i 9; nie jest tą samą metryką co Spójność sum L/R przy XO w rankingu RAW.

### Zgodność fazowa main–sub — SYMULACJA PROJEKTU

Bezpośrednio porównuje fazę maina i subwoofera każdej strony w paśmie, w którym obie gałęzie mają istotny udział.

**Porównywalność:** Metryka specyficzna dla projektanta biquadów; nie porównuj jej z fazą L/R sumy stereo.

### Podparcie dolnej średnicy — PREDYKCJA RAW

Wieloskładnikowa predykcja masy 100–220 Hz względem basu, z ochroną przed odchudzeniem i maskowaniem.

**Porównywalność:** Nie porównuj liczbowo z ciągłością 100–220 Hz trybu 8/9 ani z teksturą po Dirac.

### Ciągłość korpusu 100–220 Hz — SYMULACJA PROJEKTU

Ocena ciągłości pasm 100–160 i 160–220 Hz oraz lokalnej nierówności po zasymulowaniu filtru.

**Porównywalność:** Porównywalna między trybem 8 i 9; nie jest skalą Podparcia dolnej średnicy rankingu RAW.

### Zachowanie tekstury dolnej średnicy — POMIAR POST-DIRAC

Ocena rzeczywistego pomiaru po Dirac względem 90 BW48, przede wszystkim w 125–220 Hz.

**Porównywalność:** Jest końcową oceną pomiarową; nie porównuj jej 1:1 z predykcją RAW ani symulacją projektu.

### Przewidywana odporność przestrzenna — PREDYKCJA RAW

Predykcja z jednego RAW na podstawie asymetrii, sumowania i lokalnej stabilności.

**Porównywalność:** Nie jest rzeczywistym pomiarem kilku pozycji.

### Odporność na błąd opóźnienia i poziomu — SYMULACJA PROJEKTU

Stabilność wyniku po perturbacji delayu ±0,2 ms i poziomu subów ±0,5 dB.

**Porównywalność:** Nie porównuj z odpornością przestrzenną z kilku pozycji mikrofonu.

### Zmierzona odporność przestrzenna L/MLP/P — POMIAR POST-DIRAC

Ocena rzeczywistych pomiarów L+P w pozycjach lewa/MLP/prawa; przy braku kompletu raport wyraźnie oznacza predykcję.

**Porównywalność:** Najwyższy poziom wiarygodności przestrzennej; nie porównuj bezpośrednio z zmianami opóźnienia/poziomu.

### Przewidywane obciążenie korekcji Dirac — PREDYKCJA RAW

Predykcja ryzyka, że Dirac będzie musiał ratować słaby punkt startowy.

**Porównywalność:** Nie porównuj liczbowo z lokalnym wskaźnikiem projektu ani z wynikiem po Dirac.

### Symulowane obciążenie korekcji Dirac — SYMULACJA PROJEKTU

Wskaźnik lokalnych reszt, dołków i nierówności po usunięciu łagodnego trendu tonalnego.

**Porównywalność:** Porównywalny między trybem 8 i 9; nie oznacza liczby dB korekcji.

### Jakość wyniku po korekcji Dirac — POMIAR POST-DIRAC

Rzeczywiste SPL, balans L/R, faza, opóźnienie grupowe, dołki oraz zgodność przestrzenna po korekcji.

**Porównywalność:** Nie jest prognozą obciążenia Dirac i nie powinna używać tej samej krótkiej nazwy.

### Przewidywane ryzyko dziury L+P po Dirac — PREDYKCJA RAW

Predyktor kasowania w sumie stereo po korekcji, liczony z RAW.

**Porównywalność:** Nie porównuj liczbowo z symulacją zwrotnicy ani z rzeczywistym pomiarem po Dirac.

### Symulowane ryzyko dziury L+P — SYMULACJA PROJEKTU

Ryzyko wynikające z dołku, konstruktywności i asymetrii zasymulowanej sumy L+P.

**Porównywalność:** Porównywalne między trybem 8 i 9.

### Zmierzona dziura L+P po Dirac — POMIAR POST-DIRAC

Ocena rzeczywistej sumy L+P po korekcji, również w kilku pozycjach, jeżeli są dostępne.

**Porównywalność:** To pomiar wyniku, nie ryzyko prognostyczne.

### Wynik muzyczny rankingu RAW — PREDYKCJA RAW

Kolejność kandydatów w obrębie tego samego pliku RAW i profilu.

**Porównywalność:** Nie jest absolutną oceną systemu i nie porównuj go z wynikiem selektora lub projektanta.

### Wynik selektora standardowych filtrów — SYMULACJA PROJEKTU

Ogólny wynik lokalnej symulacji standardowych BW/LR.

**Porównywalność:** Porównuj tylko wewnątrz trybu 8 dla tego samego RAW.

### Wynik projektanta biquadów — SYMULACJA PROJEKTU

Połączenie integracji technicznej, profilu tonalnego i odporności na perturbacje.

**Porównywalność:** Porównuj tylko między kandydatami tego samego profilu i tego samego RAW.

### Wynik zastąpienia referencji 90 BW48 — POMIAR POST-DIRAC

Końcowa pomocnicza ocena rzeczywistego slotu względem referencji po Dirac.

**Porównywalność:** Nie porównuj z wynikiem muzycznym RAW ani wynikami projektantów.
