import pytest

import tg_garmin_to_osm.runner as runner_mod

from conftest_helpers import _Osm


@pytest.fixture(autouse=True)
def _isolated_dialog_and_osm():
    """Module-level fakes/states must not leak between tests."""
    runner_mod.PENDING.clear()
    _Osm.instances, _Osm.pending, _Osm.next_id = [], {}, 0
    yield
    runner_mod.PENDING.clear()
    _Osm.instances, _Osm.pending, _Osm.next_id = [], {}, 0
