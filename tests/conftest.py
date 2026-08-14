from __future__ import annotations

import os
import sys

# Ensure QT_QPA_PLATFORM is offscreen for headless CI/CD execution
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402
import pytest  # noqa: E402

from discord_cleanup.api.rate_limiter import RequestCoordinator  # noqa: E402
from discord_cleanup.models.domain import Guild, Relationship, RelationshipType, User  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """Ensure a single persistent QApplication instance is available for all Qt tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def mock_rate_limiter():
    """Fast rate limiter for deterministic tests with zero baseline delay."""
    limiter = RequestCoordinator(min_interval=0.0)
    limiter.reset()
    return limiter


@pytest.fixture
def sample_user():
    return User(
        id="123456789012345678",
        username="testuser",
        discriminator="0",
        global_name="Test User",
        avatar="abcdef1234567890",
    )


@pytest.fixture
def sample_guilds():
    return [
        Guild(id="111111111111111111", name="Community Server A", owner=False, permissions=1024),
        Guild(id="222222222222222222", name="Personal Server B", owner=True, permissions=8),
        Guild(id="333333333333333333", name="Gaming Club C", owner=False, permissions=2048),
    ]


@pytest.fixture
def sample_relationships():
    return [
        Relationship(
            id="444444444444444444",
            user=User(id="444444444444444444", username="alice", global_name="Alice Wonderland"),
            rel_type=RelationshipType.FRIEND,
            since="2023-01-15T12:00:00.000000+00:00",
        ),
        Relationship(
            id="555555555555555555",
            user=User(id="555555555555555555", username="bob", global_name="Bob Builder"),
            rel_type=RelationshipType.FRIEND,
            since="2022-05-10T15:30:00.000000+00:00",
        ),
        Relationship(
            id="666666666666666666",
            user=User(id="666666666666666666", username="charlie_spammer", global_name="Spam Bot"),
            rel_type=RelationshipType.BLOCKED,
        ),
    ]
