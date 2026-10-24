# REWDO v20.28 — Analiza czasowa / 90 BW48 Fast

## Najważniejsza zasada

Tryb 10 wymaga co najmniej trzech powtórzeń referencji dla każdej roli: L, R i L+R. Naturalny rozrzut tych powtórzeń wyznacza osobne progi istotności. Pojedynczy pomiar nie może już zostać uznany za lepszy tylko dlatego, że przypadkowo ma krótszy T20.

## Nazwy referencji

Użyj wspólnego rdzenia konfiguracji, np.:

- `REF_SLOT2_90BW48_G0_DIRAC_ON_L_STRONA`
- `REF_SLOT2_90BW48_G0_DIRAC_ON_R_STRONA`
- `REF_SLOT2_90BW48_G0_DIRAC_ON_L+R`
- `REF_REPEAT2_90BW48_G0_DIRAC_ON_...`
- `REF_REPEAT3_90BW48_G0_DIRAC_ON_...`

W polu referencji wpisz `REF_SLOT2_90BW48_G0`.

## Werdykt zbiorczy

Każdy kandydat musi mieć komplet L, R i L+R. Status **POTENCJALNIE LEPSZY** wymaga jednocześnie:

1. poprawy większej niż rozrzut referencji w L+R;
2. potwierdzenia poprawy co najmniej po jednej stronie;
3. braku istotnego pogorszenia drugiej strony;
4. zachowania body 100–220 Hz;
5. zgodnego anchoru group delay.

Tryb zgłasza błąd lub jawne ostrzeżenie, gdy podany fragment referencji nie pasuje do nazw. Nie wybiera już po cichu pierwszego pomiaru.
