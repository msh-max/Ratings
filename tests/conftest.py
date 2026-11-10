from pathlib import Path

import pytest

from ranking.scoring import Registry

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo"


@pytest.fixture(scope="session")
def registry() -> Registry:
    return Registry.load()


@pytest.fixture
def fixture_root() -> Path:
    return FIXTURE_ROOT
