"""Optional private regression input; never part of the public repository."""
import os
import gc
from pathlib import Path
import pytest


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    if "app" in item.fixturenames:
        # Release destroyed Tk windows on their owning thread, before API tests
        # start background HTTP threads that can otherwise trigger collection.
        gc.collect()
    return result

@pytest.fixture
def private_legacy_mdat():
    value=os.environ.get("REWDO_PRIVATE_LEGACY_MDAT")
    if not value:
        pytest.skip("Private MDAT regression: set REWDO_PRIVATE_LEGACY_MDAT locally; dataset is not published")
    path=Path(value).expanduser().resolve()
    if not path.is_file():
        pytest.fail("Configured private MDAT does not exist")
    return path
