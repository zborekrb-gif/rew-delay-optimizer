# Rozwój projektu

Zachowuj istniejące moduły DSP i jawnie wskazuj zmianę wzorów, wag albo progów. Oddzielaj poprawność danych i bezpieczeństwo od preferencji oraz rankingu.

Pracuj na gałęzi zadania i przedstaw zmianę przez pull request. Opis powinien zawierać problem, nowe zachowanie, zakres testów i ograniczenia. Audyt strategii optymalizacji zaczyna się na `audit/optimization-strategy`.

Do testów używaj danych syntetycznych. Pomiary, projekty odsłuchowe i kalibracje konkretnego systemu zachowuj lokalnie. Trzy opcjonalne regresje MDAT korzystają ze zmiennej `REWDO_PRIVATE_LEGACY_MDAT`. Nie zastępuj pustego modelu dystrybucyjnego wyuczoną kalibracją w publikowanych zmianach.

Uruchom `python -m pytest -q tests`. Testów pominiętych z powodu ekranu albo prywatnego pliku nie przedstawiaj jako wykonanych. Odbiór Windows oraz rzeczywista weryfikacja pomiarowa i odsłuchowa są osobnymi etapami.
