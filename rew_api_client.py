# -*- coding: utf-8 -*-
"""Klient lokalnego API Room EQ Wizard 5.40 dla REWDO.

Moduł korzysta wyłącznie z biblioteki standardowej Pythona. Domyślnie łączy
się z REW na localhost:4735 i działa w trybie odczytu. Dane wybranych pomiarów
mogą zostać zapisane jako migawka ``*.rewapi.json``. Migawka jest wejściem
równorzędnym z plikiem MDAT dla silników REWDO, ale nie zastępuje oryginalnego
projektu REW i nie modyfikuje listy pomiarów w REW.
"""
from __future__ import annotations

import base64
import json
import math
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np

from rew_measurement_validation import validate_numeric_response_arrays


SNAPSHOT_FORMAT = "REWDO_REW_API_SNAPSHOT"
SNAPSHOT_VERSION = 3
SUPPORTED_SNAPSHOT_VERSIONS = {1, 2, 3}

RAW_STATE_REQUIRED_FLAGS = (
    "userConfirmed",
    "raw",
    "diracOff",
    "crossoverOff",
    "peqOff",
    "delaysZero",
    "polarityNormal",
    "relFilter120",
    "timingReferenceConsistent",
    "microphoneStationary",
)


def verify_measurement_state(state: Any) -> tuple[bool, list[str]]:
    """Sprawdź jawne potwierdzenie warunków RAW zapisane w migawce API.

    API REW nie zna ustawień wyjść miniDSP, dlatego są one świadomie
    potwierdzane przez użytkownika podczas tworzenia migawki.
    """
    if not isinstance(state, Mapping):
        return False, list(RAW_STATE_REQUIRED_FLAGS)
    missing = [name for name in RAW_STATE_REQUIRED_FLAGS if state.get(name) is not True]
    for gain_name in ("mainGainDb", "subGainDb"):
        if _as_float(state.get(gain_name)) is None:
            missing.append(gain_name)
    return not missing, missing
DEFAULT_REW_API_URL = "http://127.0.0.1:4735"


class REWApiError(RuntimeError):
    """Czytelny błąd połączenia lub odpowiedzi API REW."""


@dataclass(frozen=True)
class REWApiMeasurement:
    uuid: str
    title: str
    notes: str = ""
    date: str = ""
    group_name: str = ""
    group_id: str = ""
    start_freq: Optional[float] = None
    end_freq: Optional[float] = None
    sample_rate: Optional[float] = None
    rew_version: str = ""
    api_reference: str = ""
    list_index: int = 0
    duplicate_uuid: bool = False
    raw_summary: Optional[Dict[str, Any]] = None


def _first_value(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """Zwróć pierwsze pole, ignorując różnice w wielkości liter."""
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for name in names:
        if name in mapping:
            return mapping[name]
        key = name.lower()
        if key in lowered:
            return lowered[key]
    return default


def _as_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except Exception:
        return None


def normalise_api_url(url: str, *, allow_remote: bool = False) -> str:
    """Znormalizuj adres API i domyślnie ogranicz go do komputera lokalnego."""
    raw = str(url or "").strip() or DEFAULT_REW_API_URL
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() != "http":
        raise ValueError("API REW używa lokalnego HTTP. Dozwolony schemat: http://")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Brak hosta w adresie API REW.")
    if not allow_remote and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "Dla bezpieczeństwa REWDO domyślnie łączy się tylko z lokalnym API REW "
            "(127.0.0.1 lub localhost)."
        )
    port = parsed.port or 4735
    if port < 1024 or port > 65535:
        raise ValueError("Port API REW musi mieścić się w zakresie 1024–65535.")
    host_for_url = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{host_for_url}:{port}"


def decode_float32_base64(value: str) -> np.ndarray:
    """Dekoduj tablicę 32-bit float zapisaną przez REW w big-endian Base64."""
    if not isinstance(value, str) or not value.strip():
        return np.asarray([], dtype=float)
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise REWApiError(f"Nieprawidłowa tablica Base64 w odpowiedzi REW: {exc}") from exc
    if len(raw) % 4:
        raise REWApiError("Długość tablicy REW nie jest wielokrotnością 4 bajtów.")
    return np.frombuffer(raw, dtype=">f4").astype(float)


def encode_float32_base64(values: Iterable[float]) -> str:
    """Pomocnicze kodowanie używane w testach i narzędziach diagnostycznych."""
    arr = np.asarray(list(values), dtype=">f4")
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_frequency_response(payload: Mapping[str, Any]) -> Dict[str, np.ndarray | float | str]:
    """Zamień obiekt FrequencyResponse API REW na jawne osie SPL i fazy.

    REW zwraca logarytmiczne dane przez ``startFreq`` + ``ppo`` albo dane
    liniowe przez ``startFreq`` + ``freqStep``. Nazwy pól są obsługiwane
    tolerancyjnie, aby migawki pozostawały czytelne przy drobnych zmianach bety.
    """
    magnitude_b64 = _first_value(
        payload,
        "magnitudes", "magnitude", "magnitudeData", "magnitudeValues",
    )
    phase_b64 = _first_value(payload, "phases", "phase", "phaseData", "phaseValues")
    if not isinstance(magnitude_b64, str):
        raise REWApiError("Odpowiedź frequency-response nie zawiera tablicy magnitudes.")
    if not isinstance(phase_b64, str):
        raise REWApiError("Odpowiedź frequency-response nie zawiera tablicy phases.")

    magnitudes = decode_float32_base64(magnitude_b64)
    phases = decode_float32_base64(phase_b64)
    if magnitudes.size == 0:
        raise REWApiError("Odpowiedź frequency-response ma pustą tablicę SPL.")
    if phases.size != magnitudes.size:
        raise REWApiError(
            f"API REW zwróciło różne długości SPL i fazy: {magnitudes.size} / {phases.size}."
        )

    start = _as_float(_first_value(payload, "startFreq", "startFrequency", "startFrequencyHz"))
    if start is None or start <= 0:
        raise REWApiError("Odpowiedź frequency-response nie zawiera poprawnego startFreq.")

    ppo = _as_float(_first_value(payload, "ppo", "pointsPerOctave", "points_per_octave"))
    step = _as_float(_first_value(payload, "freqStep", "frequencyStep", "frequencyStepHz"))
    if ppo is not None and ppo > 0:
        freq = start * np.power(2.0, np.arange(magnitudes.size, dtype=float) / ppo)
        axis_kind = "log"
        nominal_step = float(np.nanmedian(np.diff(freq))) if freq.size > 1 else 0.0
    elif step is not None and step > 0:
        freq = start + np.arange(magnitudes.size, dtype=float) * step
        axis_kind = "linear"
        nominal_step = step
    else:
        end = _as_float(_first_value(payload, "endFreq", "endFrequency", "endFrequencyHz"))
        if end is None or end <= start or magnitudes.size < 2:
            raise REWApiError("Brak ppo/freqStep i brak użytecznego endFreq w odpowiedzi REW.")
        freq = np.linspace(start, end, magnitudes.size, dtype=float)
        axis_kind = "linear-derived"
        nominal_step = float((end - start) / (magnitudes.size - 1))

    return {
        "frequency_hz": freq,
        "spl_db": magnitudes,
        "phase_deg": phases,
        "axis_kind": axis_kind,
        "start_freq_hz": float(freq[0]),
        "end_freq_hz": float(freq[-1]),
        "freq_step_hz": float(nominal_step),
        "smoothing": str(_first_value(payload, "smoothing", default="") or ""),
    }


def decode_group_delay(payload: Mapping[str, Any]) -> Dict[str, np.ndarray | float | str]:
    """Zdekoduj Group Delay API REW.

    REW zwraca te dane w obiekcie zgodnym z FrequencyResponse, lecz wartości
    opóźnienia znajdują się w polu ``magnitudes`` i mają jednostkę sekund.
    Funkcja zachowuje natywną oś częstotliwości i zwraca również milisekundy.
    """
    magnitude_b64 = _first_value(
        payload,
        "magnitudes", "magnitude", "magnitudeData", "magnitudeValues",
    )
    if not isinstance(magnitude_b64, str):
        raise REWApiError("Odpowiedź group-delay nie zawiera tablicy magnitudes.")
    values_s = decode_float32_base64(magnitude_b64)
    if values_s.size == 0:
        raise REWApiError("Odpowiedź group-delay ma pustą tablicę danych.")

    start = _as_float(_first_value(payload, "startFreq", "startFrequency", "startFrequencyHz"))
    if start is None or start <= 0:
        raise REWApiError("Odpowiedź group-delay nie zawiera poprawnego startFreq.")
    ppo = _as_float(_first_value(payload, "ppo", "pointsPerOctave", "points_per_octave"))
    step = _as_float(_first_value(payload, "freqStep", "frequencyStep", "frequencyStepHz"))
    if ppo is not None and ppo > 0:
        freq = start * np.power(2.0, np.arange(values_s.size, dtype=float) / ppo)
        axis_kind = "log"
        nominal_step = float(np.nanmedian(np.diff(freq))) if freq.size > 1 else 0.0
    elif step is not None and step > 0:
        freq = start + np.arange(values_s.size, dtype=float) * step
        axis_kind = "linear"
        nominal_step = float(step)
    else:
        end = _as_float(_first_value(payload, "endFreq", "endFrequency", "endFrequencyHz"))
        if end is None or end <= start or values_s.size < 2:
            raise REWApiError("Brak ppo/freqStep i brak użytecznego endFreq w group-delay.")
        freq = np.linspace(start, end, values_s.size, dtype=float)
        axis_kind = "linear-derived"
        nominal_step = float((end - start) / (values_s.size - 1))

    return {
        "frequency_hz": freq,
        "group_delay_s": values_s,
        "group_delay_ms": values_s * 1000.0,
        "axis_kind": axis_kind,
        "start_freq_hz": float(freq[0]),
        "end_freq_hz": float(freq[-1]),
        "freq_step_hz": float(nominal_step),
        "smoothing": str(_first_value(payload, "smoothing", default="") or ""),
        "unit": "s",
    }


def decode_impulse_response(payload: Mapping[str, Any]) -> Dict[str, np.ndarray | float | str]:
    """Zdekoduj ImpulseResponse API REW na oś czasu i próbki.

    Obsługiwane są drobne różnice nazw pól pomiędzy wydaniami beta 5.40.
    Dane są pobierane przez klienta bez normalizacji, dlatego zachowują relacje
    poziomów między pomiarami. Analiza czasowa normalizuje energię lokalnie.
    """
    samples_b64 = _first_value(
        payload,
        "data", "responseData", "samples", "values", "impulseResponse", "impulseData",
    )
    if not isinstance(samples_b64, str):
        raise REWApiError("Odpowiedź impulse-response nie zawiera tablicy data.")
    samples = decode_float32_base64(samples_b64)
    if samples.size == 0:
        raise REWApiError("Odpowiedź impulse-response ma pustą tablicę danych.")

    sample_interval = _as_float(_first_value(
        payload,
        "sampleInterval", "sampleIntervalSeconds", "samplePeriod", "samplePeriodSeconds",
    ))
    sample_rate = _as_float(_first_value(payload, "sampleRate", "sampleRateHz", "samplerate"))
    if (sample_interval is None or sample_interval <= 0) and sample_rate is not None and sample_rate > 0:
        sample_interval = 1.0 / sample_rate
    if (sample_rate is None or sample_rate <= 0) and sample_interval is not None and sample_interval > 0:
        sample_rate = 1.0 / sample_interval
    if sample_interval is None or sample_interval <= 0 or sample_rate is None or sample_rate <= 0:
        raise REWApiError("Impulse-response nie zawiera poprawnego sampleInterval/sampleRate.")

    start_time = _as_float(_first_value(
        payload,
        "startTime", "startTimeSeconds", "startTimeSec", "startSeconds",
        default=0.0,
    ))
    if start_time is None:
        start_time = 0.0
    time_s = float(start_time) + np.arange(samples.size, dtype=float) * float(sample_interval)
    timing_reference = str(_first_value(
        payload,
        "timingReference", "timingReferenceDescription", "timingRef", "reference",
        default="",
    ) or "")
    unit = str(_first_value(payload, "unit", "units", default="") or "")

    return {
        "time_s": time_s,
        "samples": samples,
        "start_time_s": float(time_s[0]),
        "end_time_s": float(time_s[-1]),
        "sample_interval_s": float(sample_interval),
        "sample_rate_hz": float(sample_rate),
        "timing_reference": timing_reference,
        "unit": unit,
    }


_NO_SMOOTHING_TOKENS = {
    "none", "no smoothing", "unsmoothed", "off", "brak", "bez wygladzania",
    "bez wygładzania", "aucun", "keine", "ninguno", "nessuna",
}


def _normalise_choice_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("ł", "l").replace("ą", "a").replace("ę", "e").replace("ó", "o")
    text = text.replace("ś", "s").replace("ż", "z").replace("ź", "z").replace("ć", "c").replace("ń", "n")
    return " ".join(text.replace("_", " ").replace("-", " ").split())


def is_no_smoothing_label(value: Any) -> bool:
    """Czy etykieta REW jednoznacznie oznacza brak wygładzania."""
    text = _normalise_choice_text(value)
    if not text:
        return False
    tokens = {_normalise_choice_text(x) for x in _NO_SMOOTHING_TOKENS}
    return text in tokens or text.startswith("none ") or text.startswith("no smoothing")


def _string_choices(value: Any) -> List[str]:
    """Wyodrębnij listę napisów z prostych i opakowanych odpowiedzi choices."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        out: List[str] = []
        for item in value:
            out.extend(_string_choices(item))
        return out
    if isinstance(value, Mapping):
        for key in ("choices", "values", "smoothings", "items", "data"):
            nested = _first_value(value, key, default=None)
            if nested is not None:
                choices = _string_choices(nested)
                if choices:
                    return choices
        out: List[str] = []
        for item in value.values():
            out.extend(_string_choices(item))
        return out
    return []


def verify_snapshot_response_integrity(payload: Any) -> tuple[bool, list[str]]:
    """Sprawdź metadane akwizycji oraz rzeczywiste wartości numeryczne.

    Zwracany przez REW obiekt może mieć ``ppo`` także wtedy, gdy natywny pomiar
    sam jest logarytmiczny. Krytyczne jest, aby REWDO nie wysłał parametru
    wymuszającego ``ppo`` i aby odpowiedź potwierdzała brak wygładzania.
    """
    problems: list[str] = []
    if not isinstance(payload, Mapping):
        return False, ["brak obiektu migawki"]
    acquisition = payload.get("acquisition")
    if not isinstance(acquisition, Mapping):
        return False, ["brak metadanych acquisition (stara migawka)"]
    if acquisition.get("nativeAxisRequested") is not True:
        problems.append("oś natywna nie została zażądana")
    if acquisition.get("ppoRequested") not in (None, "", 0):
        problems.append("wymuszono PPO")
    if acquisition.get("noSmoothingRequested") is not True:
        problems.append("brak jawnego żądania bez wygładzania")
    requested = acquisition.get("smoothingRequested")
    if not is_no_smoothing_label(requested):
        problems.append(f"niejednoznaczne smoothingRequested={requested!r}")
    records = payload.get("measurements")
    if not isinstance(records, list) or not records:
        problems.append("brak rekordów pomiarów")
    else:
        for index, record in enumerate(records, start=1):
            if not isinstance(record, Mapping):
                problems.append(f"pomiar #{index}: uszkodzony rekord")
                continue
            response = record.get("frequencyResponse")
            if not isinstance(response, Mapping):
                problems.append(f"pomiar #{index}: brak frequencyResponse")
                continue
            returned = _first_value(response, "smoothing", default="")
            if not is_no_smoothing_label(returned):
                problems.append(f"pomiar #{index}: REW zwrócił smoothing={returned!r}")
            info = record.get("decodedInfo")
            if isinstance(info, Mapping) and info.get("ppoRequested") not in (None, "", 0):
                problems.append(f"pomiar #{index}: zapisano wymuszone PPO")
            try:
                decoded = decode_frequency_response(response)
                numeric = validate_numeric_response_arrays(
                    decoded["frequency_hz"],
                    decoded["spl_db"],
                    decoded["phase_deg"],
                    name=f"pomiar #{index}",
                )
                problems.extend(numeric.messages)
            except (REWApiError, ValueError, TypeError) as exc:
                problems.append(f"pomiar #{index}: uszkodzone dane/Base64: {exc}")
    parity = payload.get("parityCheck")
    if isinstance(parity, Mapping) and str(parity.get("status", "")).upper() == "FAIL":
        problems.append("kontrola API–MDAT zakończona FAIL")
    return not problems, problems


class REWApiClient:
    """Mały klient HTTP dla lokalnego API REW 5.40."""

    def __init__(self, base_url: str = DEFAULT_REW_API_URL, *, timeout: float = 8.0) -> None:
        self.base_url = normalise_api_url(base_url)
        self.timeout = max(1.0, float(timeout))

    def _url(self, path: str, query: Optional[Mapping[str, Any]] = None) -> str:
        clean = "/" + str(path or "").lstrip("/")
        url = self.base_url + clean
        if query:
            values = {k: v for k, v in query.items() if v is not None and v != ""}
            if values:
                url += "?" + urllib.parse.urlencode(values)
        return url

    def get_json(self, path: str, query: Optional[Mapping[str, Any]] = None) -> Any:
        request = urllib.request.Request(
            self._url(path, query),
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "REWDO/20.31"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise REWApiError(f"REW API zwróciło HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise REWApiError(
                "Nie można połączyć się z REW API. Uruchom REW 5.40, włącz API "
                f"i sprawdź adres {self.base_url}. Szczegóły: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise REWApiError(f"Przekroczono czas oczekiwania na REW API ({self.timeout:.0f} s).") from exc
        try:
            return json.loads(data.decode("utf-8"))
        except Exception as exc:
            raise REWApiError("REW API zwróciło odpowiedź, która nie jest poprawnym JSON.") from exc

    @staticmethod
    def _looks_like_measurement_summary(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        keys = {str(key).lower() for key in value.keys()}
        return bool(keys & {"uuid", "title", "name", "startfreq", "endfreq", "samplerate"})

    def _measurement_entries(self, raw: Any) -> List[tuple[int, Any]]:
        """Wyodrębnij pozycje listy z wariantów JSON spotykanych w betach REW.

        Oficjalny endpoint zwraca listę MeasurementSummary, ale starsze lub
        pośrednie implementacje potrafią opakować ją w pole albo zwrócić mapę.
        Zachowujemy bieżący indeks listy, ponieważ endpointy szczegółowe
        przyjmują indeks od 1 także wtedy, gdy wpis nie zawiera UUID.
        """
        value = raw
        seen_ids = set()
        while isinstance(value, Mapping) and not self._looks_like_measurement_summary(value):
            marker = id(value)
            if marker in seen_ids:
                break
            seen_ids.add(marker)
            nested = _first_value(
                value,
                "measurements", "measurementSummaries", "summaries", "items",
                "results", "result", "data", "value", "list",
                default=None,
            )
            if nested is None or nested is value:
                break
            value = nested

        if isinstance(value, list):
            return [(index, item) for index, item in enumerate(value, start=1)]
        if self._looks_like_measurement_summary(value):
            return [(1, value)]
        if isinstance(value, Mapping):
            entries: List[tuple[int, Any]] = []
            fallback_index = 1
            for key, item in value.items():
                try:
                    list_index = int(str(key))
                    if list_index < 1:
                        raise ValueError
                except Exception:
                    list_index = fallback_index
                entries.append((list_index, item))
                fallback_index += 1
            entries.sort(key=lambda pair: pair[0])
            return entries
        raise REWApiError(
            "Endpoint /measurements zwrócił nieobsługiwany format odpowiedzi: "
            f"{type(raw).__name__}."
        )

    def _normalise_measurement_summary(self, item: Any, list_index: int) -> Dict[str, Any]:
        """Zwróć pełne podsumowanie; w razie potrzeby dopytaj endpoint indeksowy."""
        if isinstance(item, Mapping):
            # Czasem pojedyncza pozycja jest opakowana np. {"measurement": {...}}.
            if not self._looks_like_measurement_summary(item):
                nested = _first_value(item, "measurement", "summary", "data", "value", default=None)
                if self._looks_like_measurement_summary(nested):
                    item = nested
            if self._looks_like_measurement_summary(item):
                summary = dict(item)
                # Brak UUID nie może powodować niewidoczności pomiaru. Spróbuj
                # pobrać pełny summary po indeksie, ale zachowaj dane listy przy błędzie.
                if not str(_first_value(summary, "uuid", "id", default="") or "").strip():
                    try:
                        detailed = self.measurement_summary(str(list_index))
                        if detailed:
                            summary.update(detailed)
                    except REWApiError:
                        pass
                return summary
        # Lista może zawierać same indeksy, UUID albo nazwy. Najpewniejsze jest
        # odwołanie po bieżącym indeksie listy.
        return self.measurement_summary(str(list_index))

    def list_measurements(self) -> List[REWApiMeasurement]:
        raw = self.get_json("/measurements")
        entries = self._measurement_entries(raw)
        parsed: List[tuple[int, Dict[str, Any], str, str, bool]] = []
        detail_errors: List[str] = []
        for list_index, item in entries:
            try:
                summary = self._normalise_measurement_summary(item, list_index)
            except REWApiError as exc:
                detail_errors.append(f"#{list_index}: {exc}")
                continue
            uuid = str(_first_value(summary, "uuid", "id", default="") or "").strip()
            title = str(_first_value(summary, "title", "name", default="") or "").strip()
            synthetic_uuid = not bool(uuid)
            if synthetic_uuid:
                uuid = f"rew-index-{list_index}"
            if not title:
                title = f"Pomiar {list_index}"
            parsed.append((list_index, summary, uuid, title, synthetic_uuid))

        if entries and not parsed:
            detail = "; ".join(detail_errors[:3])
            raise REWApiError(
                "REW zwrócił pozycje pomiarów, ale nie udało się odczytać ich podsumowań."
                + (f" Szczegóły: {detail}" if detail else "")
            )

        uuid_counts: Dict[str, int] = {}
        for _index, _item, uuid, _title, synthetic_uuid in parsed:
            if not synthetic_uuid:
                uuid_counts[uuid] = uuid_counts.get(uuid, 0) + 1

        result: List[REWApiMeasurement] = []
        for list_index, item, uuid, title, synthetic_uuid in parsed:
            duplicate = (not synthetic_uuid) and uuid_counts.get(uuid, 0) > 1
            # Użyj indeksu przy braku prawdziwego UUID albo gdy UUID się powtarza.
            api_reference = str(list_index) if duplicate or synthetic_uuid else uuid
            summary = dict(item)
            summary["_rewdoApiReference"] = api_reference
            summary["_rewdoListIndex"] = list_index
            summary["_rewdoSyntheticUUID"] = synthetic_uuid
            result.append(
                REWApiMeasurement(
                    uuid=uuid,
                    title=title,
                    notes=str(_first_value(item, "notes", default="") or ""),
                    date=str(_first_value(item, "date", default="") or ""),
                    group_name=str(_first_value(item, "groupName", "group", default="") or ""),
                    group_id=str(_first_value(item, "groupID", "groupId", default="") or ""),
                    start_freq=_as_float(_first_value(item, "startFreq", "startFrequency")),
                    end_freq=_as_float(_first_value(item, "endFreq", "endFrequency")),
                    sample_rate=_as_float(_first_value(item, "sampleRate")),
                    rew_version=str(_first_value(item, "rewVersion", "version", default="") or ""),
                    api_reference=api_reference,
                    list_index=list_index,
                    duplicate_uuid=duplicate,
                    raw_summary=summary,
                )
            )
        return result

    def measurement_summary(self, measurement_id: str) -> Dict[str, Any]:
        quoted = urllib.parse.quote(str(measurement_id), safe="")
        result = self.get_json(f"/measurements/{quoted}")
        if not isinstance(result, Mapping):
            raise REWApiError("Podsumowanie pomiaru nie jest obiektem JSON.")
        return dict(result)

    def smoothing_choices(self) -> List[str]:
        raw = self.get_json("/measurements/frequency-response/smoothing-choices")
        choices = [str(x).strip() for x in _string_choices(raw) if str(x).strip()]
        if not choices:
            raise REWApiError("REW nie zwrócił listy smoothing-choices.")
        return choices

    def resolve_no_smoothing_choice(self) -> str:
        """Pobierz od REW dokładną etykietę opcji bez wygładzania."""
        try:
            choices = self.smoothing_choices()
        except REWApiError:
            # Starsze bety mogą nie udostępniać choices poprawnie. Sama odpowiedź
            # frequency-response zostanie później rygorystycznie zweryfikowana.
            return "None"
        for choice in choices:
            if is_no_smoothing_label(choice):
                return choice
        raise REWApiError(
            "Lista smoothing-choices nie zawiera jednoznacznej opcji bez wygładzania: "
            + ", ".join(choices)
        )

    def frequency_response(
        self,
        measurement_uuid: str,
        *,
        ppo: Optional[int] = None,
        smoothing: Optional[str] = None,
    ) -> Dict[str, Any]:
        quoted = urllib.parse.quote(str(measurement_uuid), safe="")
        query: Dict[str, Any] = {}
        if ppo is not None:
            query["ppo"] = int(ppo)
        if smoothing:
            query["smoothing"] = str(smoothing)
        result = self.get_json(f"/measurements/{quoted}/frequency-response", query)
        if not isinstance(result, Mapping):
            raise REWApiError("Frequency response nie jest obiektem JSON.")
        return dict(result)

    def group_delay(
        self,
        measurement_uuid: str,
        *,
        ppo: Optional[int] = None,
        smoothing: Optional[str] = None,
    ) -> Dict[str, Any]:
        quoted = urllib.parse.quote(str(measurement_uuid), safe="")
        query: Dict[str, Any] = {}
        if ppo is not None:
            query["ppo"] = int(ppo)
        if smoothing:
            query["smoothing"] = str(smoothing)
        result = self.get_json(f"/measurements/{quoted}/group-delay", query)
        if not isinstance(result, Mapping):
            raise REWApiError("Group delay nie jest obiektem JSON.")
        return dict(result)

    def impulse_response(self, measurement_uuid: str, *, windowed: bool = False) -> Dict[str, Any]:
        quoted = urllib.parse.quote(str(measurement_uuid), safe="")
        result = self.get_json(
            f"/measurements/{quoted}/impulse-response",
            {"windowed": str(bool(windowed)).lower(), "normalised": "false"},
        )
        if not isinstance(result, Mapping):
            raise REWApiError("Impulse response nie jest obiektem JSON.")
        return dict(result)

    def create_snapshot(
        self,
        measurements: Sequence[REWApiMeasurement],
        output_path: str | Path,
        *,
        ppo: Optional[int] = None,
        force_no_smoothing: bool = True,
        include_group_delay: bool = False,
        include_impulse: bool = False,
        measurement_state: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        """Utwórz migawkę. Domyślnie pobiera natywną oś bez wygładzania.

        ``ppo`` jest pozostawione wyłącznie jako jawny tryb diagnostyczny.
        Projektowanie biquadów wymaga ``ppo=None`` i ``force_no_smoothing=True``.
        """
        if not measurements:
            raise ValueError("Nie wybrano żadnych pomiarów REW do migawki.")
        from rew_mdat import infer_mdat_measurement_role  # import lokalny unika cyklu modułów

        smoothing_choice = self.resolve_no_smoothing_choice() if force_no_smoothing else None
        records: List[Dict[str, Any]] = []
        for measurement in measurements:
            reference = measurement.api_reference or measurement.uuid
            if measurement.duplicate_uuid:
                current = self.measurement_summary(reference)
                current_uuid = str(_first_value(current, "uuid", "id", default="") or "")
                current_title = str(_first_value(current, "title", "name", default="") or "")
                if current_uuid != measurement.uuid or current_title != measurement.title:
                    raise REWApiError(
                        "Lista pomiarów REW zmieniła się po odświeżeniu, a wybrana pozycja ma "
                        "powtórzony UUID. Naciśnij Połącz / odśwież i wybierz pomiary ponownie."
                    )
            response = self.frequency_response(reference, ppo=ppo, smoothing=smoothing_choice)
            decoded = decode_frequency_response(response)
            returned_smoothing = str(decoded.get("smoothing", "") or "")
            if force_no_smoothing and not is_no_smoothing_label(returned_smoothing):
                raise REWApiError(
                    f"REW nie potwierdził braku wygładzania dla '{measurement.title}'. "
                    f"Żądano {smoothing_choice!r}, zwrócono {returned_smoothing!r}. "
                    "Eksport został zatrzymany, aby nie projektować filtrów na wygładzonych danych."
                )
            response_ppo = _as_float(_first_value(response, "ppo", "pointsPerOctave"))
            response_step = _as_float(_first_value(response, "freqStep", "frequencyStep"))
            record: Dict[str, Any] = {
                "summary": dict(measurement.raw_summary or {
                    "uuid": measurement.uuid,
                    "title": measurement.title,
                    "notes": measurement.notes,
                    "date": measurement.date,
                    "groupName": measurement.group_name,
                    "groupID": measurement.group_id,
                    "startFreq": measurement.start_freq,
                    "endFreq": measurement.end_freq,
                    "sampleRate": measurement.sample_rate,
                    "rewVersion": measurement.rew_version,
                }),
                "role": infer_mdat_measurement_role(measurement.title),
                "frequencyResponse": response,
                "decodedInfo": {
                    "points": int(np.asarray(decoded["frequency_hz"]).size),
                    "startFreqHz": float(decoded["start_freq_hz"]),
                    "endFreqHz": float(decoded["end_freq_hz"]),
                    "axisKind": str(decoded["axis_kind"]),
                    "smoothingRequested": smoothing_choice,
                    "smoothingReturned": returned_smoothing,
                    "ppoRequested": None if ppo is None else int(ppo),
                    "ppoReturned": response_ppo,
                    "freqStepReturnedHz": response_step,
                },
            }
            if include_group_delay:
                try:
                    gd_payload = self.group_delay(reference, ppo=ppo, smoothing=smoothing_choice)
                    record["groupDelay"] = gd_payload
                    gd_decoded = decode_group_delay(gd_payload)
                    record["groupDelayInfo"] = {
                        "points": int(np.asarray(gd_decoded["frequency_hz"]).size),
                        "startFreqHz": float(gd_decoded["start_freq_hz"]),
                        "endFreqHz": float(gd_decoded["end_freq_hz"]),
                        "axisKind": str(gd_decoded["axis_kind"]),
                        "smoothingReturned": str(gd_decoded["smoothing"]),
                        "unit": "seconds",
                    }
                except (REWApiError, ValueError) as exc:
                    record["groupDelayError"] = str(exc)
            if include_impulse:
                try:
                    ir_payload = self.impulse_response(reference)
                    record["impulseResponse"] = ir_payload
                    ir_decoded = decode_impulse_response(ir_payload)
                    record["impulseInfo"] = {
                        "points": int(np.asarray(ir_decoded["samples"]).size),
                        "startTimeSeconds": float(ir_decoded["start_time_s"]),
                        "endTimeSeconds": float(ir_decoded["end_time_s"]),
                        "sampleIntervalSeconds": float(ir_decoded["sample_interval_s"]),
                        "sampleRateHz": float(ir_decoded["sample_rate_hz"]),
                        "timingReference": str(ir_decoded["timing_reference"]),
                        "unit": str(ir_decoded["unit"]),
                        "normalisedRequested": False,
                        "windowedRequested": False,
                    }
                except (REWApiError, ValueError) as exc:
                    record["impulseResponseError"] = str(exc)
            records.append(record)

        acquisition = {
            "mode": "native_unsmoothed" if ppo is None and force_no_smoothing else "diagnostic_custom",
            "nativeAxisRequested": ppo is None,
            "noSmoothingRequested": bool(force_no_smoothing),
            "smoothingRequested": smoothing_choice,
            "ppoRequested": None if ppo is None else int(ppo),
            "documentationNote": "REW frequency-response bez parametru ppo; smoothing jawnie ustawiony na brak wygładzania.",
            "groupDelayRequested": bool(include_group_delay),
            "impulseRequested": bool(include_impulse),
            "impulseWindowedRequested": False,
            "impulseNormalisedRequested": False,
        }
        snapshot = {
            "format": SNAPSHOT_FORMAT,
            "formatVersion": SNAPSHOT_VERSION,
            "createdUtc": datetime.now(timezone.utc).isoformat(),
            "apiBaseUrl": self.base_url,
            "acquisition": acquisition,
            "readOnlySnapshot": True,
            "measurementState": dict(measurement_state or {}),
            "measurements": records,
        }
        integrity_ok, integrity_problems = verify_snapshot_response_integrity(snapshot)
        snapshot["responseIntegrity"] = {
            "status": "PASS" if integrity_ok else "FAIL",
            "problems": integrity_problems,
        }
        if not integrity_ok and force_no_smoothing:
            raise REWApiError("Kontrola integralności danych API nie przeszła: " + "; ".join(integrity_problems))

        out = Path(output_path).expanduser().resolve()
        if not out.name.lower().endswith(".rewapi.json"):
            out = out.with_name(out.name + ".rewapi.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(out.suffix + ".tmp")
        temp.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temp.replace(out)
        return out



def read_snapshot(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Nie można odczytać migawki REW API: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("Plik nie jest migawką REW API utworzoną przez REWDO.")
    version = int(payload.get("formatVersion", 0) or 0)
    if version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise ValueError(f"Nieobsługiwana wersja migawki REW API: {version}.")
    if not isinstance(payload.get("measurements"), list):
        raise ValueError("Migawka REW API nie zawiera listy measurements.")
    return payload


def is_rew_api_snapshot(path: str | Path) -> bool:
    p = Path(path)
    if not p.is_file() or p.suffix.lower() != ".json":
        return False
    try:
        read_snapshot(p)
        return True
    except Exception:
        return False
