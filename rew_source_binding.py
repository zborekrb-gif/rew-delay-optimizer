"""Scoped, explicit trace assignments. Does not modify source bytes or phase."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

_sources = ContextVar("rewdo_sources", default={})


@contextmanager
def bound_sources(sources):
    token = _sources.set({str(Path(s["path"]).resolve()): s for s in sources})
    try:
        yield
    finally:
        _sources.reset(token)


def binding(path):
    return _sources.get().get(str(Path(path).resolve()))


def map_infos(path, infos):
    source = binding(path)
    if source is None:
        return infos
    traces = {int(t["position"]): t for t in source["traces"]}
    return [replace(info, name=f"trace_{info.position}", base_delay_ms=None,
                    role=traces.get(int(info.position), {}).get("role", "Nieprzypisany")) for info in infos]


def raw_contract(path):
    source = binding(path)
    if source is None:
        return None
    if source["stage"] != "Bazowe" or not source.get("raw_confirmed") or not source.get("mapping_confirmed"):
        return ("RAW_NIEPOTWIERDZONY", "EKSPORT ZABLOKOWANY", "Potwierdź przypisanie pomiarów i warunki pomiarów bez zwrotnicy DSP w Pomiary.")
    return ("RAW_VERIFIED_BY_NAMES", "EXPORT DOZWOLONY", "Stan RAW zadeklarowano jawnie w projekcie; niezależna kontrola fazy i sumowania nadal obowiązuje.")
