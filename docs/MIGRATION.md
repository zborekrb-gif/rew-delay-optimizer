# Import źródeł do GitHub

Główna baza: 20.34-dev2, z archiwum `REWDO_20_34_DEV2_PELNE_POROWNANIE.zip`, SHA-256 `4879ba02615c4f602c147c0ef53d5e84c21216f102fcd457a4a5e9908ac5f226`.

Historia Git zachowuje kolejne migawki źródeł 20.31, 20.32, 20.33, 20.34-dev1 i 20.34-dev2. Daty commitów są datami importu, nie deklaracją dat powstania wersji. Na GitHub migawki są dostępne w historii commitów i na gałęziach `archive/v*-source`. Nie są odbiorem wydania Windows. Lokalne tagi kopii importowej nie są tagami repozytorium GitHub.

W każdej archiwalnej migawce importu zachowano bajtowo wszystkie pliki Python z głównego katalogu źródeł, w tym silniki, starsze interfejsy i narzędzia. Historyczne ustawienia i opisy systemu zapisane bezpośrednio w kodzie są publikowane za zgodą właściciela. Nie opublikowano prywatnego MDAT, profili YAML, danych wyuczonej kalibracji, wyników przebiegów ani prywatnych raportów. Oryginalne paczki pozostają zachowane osobno. Scenariusze BAT z konkretnymi historycznymi nastawami nie stanowią ogólnego sposobu uruchomienia repozytorium; główne launchery i kompletny BAT budowy są zachowane.

Zmiany dystrybucyjne:

- `USER_EMPIRICAL_CALIBRATION.json` jest pustym, wyłączonym modelem bez źródeł pomiarowych.
- Test kalibracji dwóch stanów jednej topologii korzysta z `tests/fixtures/calibration_two_states.synthetic.json` zamiast danych użytkownika.
- Test obecności dawnego prywatnego raportu zastąpiono kontrolą znaczenia jednej topologii dla poziomu pewności.
- Test odtwarzający metryki z rzeczywistych raportów zastąpiono niezależnym syntetycznym przypadkiem: zgodne powtórzenia, przesunięty anchor oraz samo obniżenie poziomu basu. Oryginał pozostaje w prywatnej kopii. Pozostały test powtarzalności generuje sinusoidalne impulsy i tablice analitycznie; dopisano jawne pochodzenie syntetyczne.
- Dwa testy zabezpieczeń mają jawne pochodzenie syntetyczne. Test po Dirac używa nieistniejącej ścieżki w katalogu tymczasowym zamiast lokalnego profilu. Test konfliktu referencji używa niezależnych przykładowych nastaw 12/4/5/6 ms zamiast historycznych nastaw użytkownika.
- Trzy historyczne regresje MDAT mogą korzystać z lokalnej zmiennej `REWDO_PRIVATE_LEGACY_MDAT`. Ich pominięcie jest jawne.
- Nie zmieniono wzorów, progów ani wag silników DSP. Bieżący model dystrybucji nie jest kalibracją systemu użytkownika.

`BUILD_IDENTITY.json` zawiera wersję migawki i sumy kontrolne zachowanych plików Python. Bieżący README i dokumenty migracji opisują docelowy import; przy przeglądaniu historycznej migawki rozstrzygająca jest jej wersja w BUILD_IDENTITY oraz kod.

Licencja pozostaje do wyboru właściciela projektu. Nie dodano LICENSE ani automatycznego procesu publikacji.

## Poprawki po imporcie

Pole `upstream_runtime_python_sha256` w BUILD_IDENTITY opisuje źródła użyte do pierwotnego importu. Pole `runtime_python_sha256`, gdy występuje, opisuje bieżące pliki. `source_base_commit` i `source_change` wskazują podstawę i rodzaj zmiany. Poprawka `plain-polish-ui` zmienia opisy i sposób wyświetlania etapu pomiaru; zapisane identyfikatory etapów i algorytmy DSP pozostają zgodne.
