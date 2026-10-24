#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wspólny adapter wszystkich śladów dla MDAT i migawek REW API."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from rew_api_client import (
    decode_frequency_response,
    is_rew_api_snapshot,
    read_snapshot,
    verify_snapshot_response_integrity,
)
from rew_common import Measurement
from rew_mdat import (
    _decode_mdat_numeric_records,
    _scan_java_serialized_float_arrays,
    infer_mdat_measurement_role,
    scan_mdat_project,
)
from rew_measurement_validation import (
    MeasurementValidationError,
    MeasurementValidationIssue,
    MeasurementValidationResult,
    validate_measurement_project,
    validate_numeric_response_arrays,
)


@dataclass(frozen=True)
class SourceMeasurement:
    name: str
    position: int
    role: str
    measurement: Measurement
    source_type: str


class MeasurementSource(Protocol):
    def load_all(self) -> list[SourceMeasurement]: ...


@dataclass(frozen=True)
class RewApiMeasurementSource:
    path: Path

    def load_all(self) -> list[SourceMeasurement]:
        payload = read_snapshot(self.path)
        integrity_ok, problems = verify_snapshot_response_integrity(payload)
        if not integrity_ok:
            issues = tuple(
                MeasurementValidationIssue("API_INTEGRITY", problem, self.path.name)
                for problem in problems
            )
            raise MeasurementValidationError(MeasurementValidationResult(False, issues))

        output: list[SourceMeasurement] = []
        for index, raw_record in enumerate(payload.get("measurements", []), start=1):
            if not isinstance(raw_record, dict):
                issue = MeasurementValidationIssue("INVALID_API_RECORD", "uszkodzony rekord API", f"#{index}")
                raise MeasurementValidationError(MeasurementValidationResult(False, (issue,)))
            summary = raw_record.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            name = str(summary.get("title") or summary.get("name") or f"Pomiar API {index}").strip()
            response = raw_record.get("frequencyResponse")
            if not isinstance(response, dict):
                issue = MeasurementValidationIssue("MISSING_FREQUENCY_RESPONSE", "brak frequencyResponse", name)
                raise MeasurementValidationError(MeasurementValidationResult(False, (issue,)))
            decoded = decode_frequency_response(response)
            freq = np.asarray(decoded["frequency_hz"], dtype=float)
            spl = np.asarray(decoded["spl_db"], dtype=float)
            phase = np.asarray(decoded["phase_deg"], dtype=float)
            numeric = validate_numeric_response_arrays(freq, spl, phase, name=name)
            if not numeric.ok:
                raise MeasurementValidationError(numeric)
            role = str(raw_record.get("role") or infer_mdat_measurement_role(name)).strip() or "Nieprzypisany"
            output.append(SourceMeasurement(
                name=name,
                position=index,
                role=role,
                measurement=Measurement(name, freq.copy(), spl.copy(), phase.copy()),
                source_type="REW_API",
            ))
        validate_measurement_project(output)
        return output


@dataclass(frozen=True)
class MdatMeasurementSource:
    path: Path

    def load_all(self) -> list[SourceMeasurement]:
        data = self.path.read_bytes()
        scan = scan_mdat_project(self.path)
        infos = list(scan.get("measurements", []))
        if not infos:
            raise ValueError("MDAT nie zawiera rozpoznanych pomiarów.")
        records, warnings = _decode_mdat_numeric_records(
            data,
            infos,
            _scan_java_serialized_float_arrays(data),
        )
        record_names = {record.info.name for record in records}
        relevant_warnings = [warning for warning in warnings if any(warning.startswith(name + ":") for name in record_names)]
        if relevant_warnings:
            raise ValueError("; ".join(relevant_warnings[:6]))

        output: list[SourceMeasurement] = []
        for record in records:
            if len(record.phase_candidates) != 1:
                raise ValueError(
                    f"MDAT Phase Truth Guard: pomiar {record.info.name!r} ma "
                    f"{len(record.phase_candidates)} kandydatury fazy. POST-DIRAC nie wybiera gałęzi strukturalnie; "
                    "użyj migawki REW API albo MDAT z jednoznaczną fazą."
                )
            phase = record.phase_candidates[0].phase_deg
            measurement = Measurement(
                record.info.name,
                np.asarray(record.freq, dtype=float).copy(),
                np.asarray(record.spl_db, dtype=float).copy(),
                np.asarray(phase, dtype=float).copy(),
            )
            output.append(SourceMeasurement(
                name=record.info.name,
                position=int(record.info.position),
                role=str(record.info.role),
                measurement=measurement,
                source_type="MDAT",
            ))
        validate_measurement_project(output)
        return output


def measurement_source_for(path: str | Path) -> MeasurementSource:
    source_path = Path(path)
    if is_rew_api_snapshot(source_path):
        return RewApiMeasurementSource(source_path)
    return MdatMeasurementSource(source_path)


def load_all_measurements(path: str | Path) -> list[SourceMeasurement]:
    return measurement_source_for(path).load_all()

