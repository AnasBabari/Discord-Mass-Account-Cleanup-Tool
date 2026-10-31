from __future__ import annotations

import threading
import time
from importlib import import_module
from unittest.mock import MagicMock

import pytest
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QApplication

from discord_cleanup.api.client import DiscordApiClient
from discord_cleanup.cli import usernames as cli
from discord_cleanup.models.domain import User
from discord_cleanup.models.usernames import PUBLIC_USERNAME_SCOPE, UsernameCheckResult, UsernameStatus, utc_now
from discord_cleanup.ui import app as app_module
from discord_cleanup.ui.pages import usernames as page_module
from discord_cleanup.ui.pages.usernames import UsernamesPage
from discord_cleanup.usernames.public_client import PublicUsernameClient
from discord_cleanup.usernames.store import UsernameScanStore
from discord_cleanup.workers.usernames import UsernameScanWorker
from tests.test_api_client import MockResponse


@pytest.fixture
def fake_client():
    client = MagicMock(token="secret-token")
    client.verify_token.return_value = User("account-one", "tester")
    client.check_username_availability.side_effect = lambda name, **_: UsernameCheckResult(
        name, UsernameStatus.AVAILABLE, utc_now()
    )
    return client


def test_worker_completes_and_scrubs(qtbot, tmp_path, fake_client):
    worker = UsernameScanWorker(
        "secret-token", mode="custom", custom_text="ABC, xyz", store_path=tmp_path / "scans.db", client=fake_client
    )
    completed = []
    worker.completed_signal.connect(completed.append)
    with qtbot.waitSignal(worker.finished, timeout=3000):
        worker.start()
    assert completed[0].status == "completed"
    assert completed[0].available == 2
    assert worker.token == ""
    assert worker.custom_text == ""
    fake_client.clear_token.assert_called_once()


def test_worker_rejects_other_accounts_scan(qtbot, tmp_path, fake_client):
    store = UsernameScanStore(tmp_path / "scans.db")
    scan = store.create_scan("account-two", lengths=(3,))
    worker = UsernameScanWorker(
        "secret-token", mode="resume", scan_id=scan.id, store_path=store.path, client=fake_client
    )
    errors = []
    worker.error_signal.connect(errors.append)
    with qtbot.waitSignal(worker.finished, timeout=3000):
        worker.start()
    assert "this account" in errors[0]
