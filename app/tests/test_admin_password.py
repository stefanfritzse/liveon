"""Tests for changing the admin console password from the console itself.

The password used to come only from the environment, so changing it meant editing a
Kubernetes secret and restarting the pod.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest
from fastapi.testclient import TestClient

from app.main import ContentRepository, _paginate_in_memory, app, get_repository
from app.models.content import Article, Tip
from app.services.admin_credentials import (
    MIN_PASSWORD_LENGTH,
    AdminCredentialStore,
)

ENV_PASSWORD = "bootstrap-password-1"
NEW_PASSWORD = "BlueButterfly456!"
SAME_ORIGIN = {"Origin": "http://testserver"}


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> AdminCredentialStore:
    return AdminCredentialStore(tmp_path / "content.db")


def test_a_fresh_store_holds_no_password(store: AdminCredentialStore) -> None:
    assert store.is_configured() is False
    assert store.verify("anything") is False


def test_a_password_round_trips(store: AdminCredentialStore) -> None:
    store.set_password(NEW_PASSWORD)

    assert store.is_configured() is True
    assert store.verify(NEW_PASSWORD) is True
    assert store.verify("wrong-password-here") is False


def test_the_password_is_not_stored_in_plain_text(store: AdminCredentialStore, tmp_path: Path) -> None:
    store.set_password(NEW_PASSWORD)

    raw = (tmp_path / "content.db").read_bytes()

    assert NEW_PASSWORD.encode() not in raw


def test_each_password_gets_its_own_salt(tmp_path: Path) -> None:
    """Two stores with the same password must not produce the same hash."""

    first = AdminCredentialStore(tmp_path / "a.db")
    second = AdminCredentialStore(tmp_path / "b.db")
    first.set_password(NEW_PASSWORD)
    second.set_password(NEW_PASSWORD)

    def digest(path: Path) -> bytes:
        import sqlite3

        con = sqlite3.connect(path)
        try:
            return con.execute("SELECT password_hash FROM admin_credential").fetchone()[0]
        finally:
            con.close()

    assert digest(tmp_path / "a.db") != digest(tmp_path / "b.db")


def test_changing_the_password_replaces_the_old_one(store: AdminCredentialStore) -> None:
    store.set_password("first-password-value")
    store.set_password("second-password-value")

    assert store.verify("first-password-value") is False
    assert store.verify("second-password-value") is True


def test_a_short_password_is_refused(store: AdminCredentialStore) -> None:
    with pytest.raises(ValueError, match=str(MIN_PASSWORD_LENGTH)):
        store.set_password("short")

    assert store.is_configured() is False


def test_clearing_restores_the_environment_password(store: AdminCredentialStore) -> None:
    """The recovery path for a forgotten console password."""

    store.set_password(NEW_PASSWORD)
    store.clear()

    assert store.is_configured() is False
    assert store.verify(NEW_PASSWORD) is False


def test_a_stored_password_survives_a_restart(tmp_path: Path) -> None:
    AdminCredentialStore(tmp_path / "content.db").set_password(NEW_PASSWORD)

    assert AdminCredentialStore(tmp_path / "content.db").verify(NEW_PASSWORD) is True


# ----------------------------------------------------------------------
# The console
# ----------------------------------------------------------------------


class _Repo(ContentRepository):
    def __init__(self) -> None:
        self._articles = [Article(id="a", title="A", content_body="b")]
        self._tips = [Tip(id="t", title="T", content_body="b")]

    def get_latest_articles(self, *, limit: int = 5): return self._articles[:limit]
    def get_article(self, article_id: str): return None
    def get_latest_tips(self, *, limit: int = 5): return self._tips[:limit]
    def get_latest_tip(self): return self._tips[0]
    def delete_article(self, article_id: str) -> bool: return False
    def delete_tip(self, tip_id: str) -> bool: return False
    def browse_articles(self, **kw): return _paginate_in_memory(list(self._articles), **kw)
    def browse_tips(self, **kw): return _paginate_in_memory(list(self._tips), **kw)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterable[TestClient]:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    monkeypatch.setenv("LIVEON_ADMIN_PASSWORD", ENV_PASSWORD)
    monkeypatch.delenv("LIVEON_ADMIN_USER", raising=False)

    app.state.admin_credential_store = None
    app.dependency_overrides[get_repository] = lambda: _Repo()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_repository, None)
    app.state.admin_credential_store = None


def _change(client: TestClient, auth, current: str, new: str, confirm: str | None = None):
    return client.post(
        "/admin/password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirm is None else confirm,
        },
        auth=auth,
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


def test_the_console_offers_a_password_form(client: TestClient) -> None:
    page = client.get("/admin", auth=("admin", ENV_PASSWORD)).text

    assert "/admin/password" in page
    assert 'name="current_password"' in page
    assert 'name="new_password"' in page
    assert 'name="confirm_password"' in page
    # Password inputs, not plain text.
    assert page.count('type="password"') == 3


def test_the_environment_password_works_before_any_change(client: TestClient) -> None:
    assert client.get("/admin", auth=("admin", ENV_PASSWORD)).status_code == 200


def test_changing_the_password_takes_effect_immediately(client: TestClient) -> None:
    response = _change(client, ("admin", ENV_PASSWORD), ENV_PASSWORD, NEW_PASSWORD)

    assert response.status_code == 303
    assert "notice=password-changed" in response.headers["location"]
    # The new one works and the old one no longer does — no restart involved.
    assert client.get("/admin", auth=("admin", NEW_PASSWORD)).status_code == 200
    assert client.get("/admin", auth=("admin", ENV_PASSWORD)).status_code == 401


def test_the_stored_password_beats_the_environment(client: TestClient, tmp_path: Path) -> None:
    _change(client, ("admin", ENV_PASSWORD), ENV_PASSWORD, NEW_PASSWORD)

    # Even though the environment still holds the old value.
    import os

    assert os.environ["LIVEON_ADMIN_PASSWORD"] == ENV_PASSWORD
    assert client.get("/admin", auth=("admin", NEW_PASSWORD)).status_code == 200


def test_the_wrong_current_password_is_refused(client: TestClient) -> None:
    response = _change(client, ("admin", ENV_PASSWORD), "not-the-password", NEW_PASSWORD)

    assert "notice=password-wrong" in response.headers["location"]
    assert client.get("/admin", auth=("admin", ENV_PASSWORD)).status_code == 200


def test_a_mismatched_confirmation_is_refused(client: TestClient) -> None:
    response = _change(
        client, ("admin", ENV_PASSWORD), ENV_PASSWORD, NEW_PASSWORD, confirm="Different456!"
    )

    assert "notice=password-mismatch" in response.headers["location"]
    assert client.get("/admin", auth=("admin", ENV_PASSWORD)).status_code == 200


def test_a_short_new_password_is_refused(client: TestClient) -> None:
    response = _change(client, ("admin", ENV_PASSWORD), ENV_PASSWORD, "short")

    assert "notice=password-short" in response.headers["location"]
    assert client.get("/admin", auth=("admin", ENV_PASSWORD)).status_code == 200


def test_reusing_the_current_password_is_refused(client: TestClient) -> None:
    response = _change(client, ("admin", ENV_PASSWORD), ENV_PASSWORD, ENV_PASSWORD)

    assert "notice=password-unchanged" in response.headers["location"]


def test_changing_the_password_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/admin/password",
        data={
            "current_password": ENV_PASSWORD,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 401
    assert client.get("/admin", auth=("admin", ENV_PASSWORD)).status_code == 200


def test_changing_the_password_rejects_cross_origin_submissions(client: TestClient) -> None:
    """Browsers cache Basic credentials, so this needs the same guard as deletion."""

    response = client.post(
        "/admin/password",
        data={
            "current_password": ENV_PASSWORD,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        auth=("admin", ENV_PASSWORD),
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert client.get("/admin", auth=("admin", ENV_PASSWORD)).status_code == 200


def test_an_authenticated_session_alone_cannot_change_the_password(client: TestClient) -> None:
    """The current password is required even though the request is authenticated.

    A browser holding cached credentials should not be enough to lock the owner out.
    """

    response = _change(client, ("admin", ENV_PASSWORD), "", NEW_PASSWORD)

    assert "notice=password-wrong" in response.headers["location"]


def test_the_outcome_is_reported_on_the_page(client: TestClient) -> None:
    page = client.get("/admin?notice=password-changed", auth=("admin", ENV_PASSWORD)).text

    assert "Password updated" in page


def test_an_unknown_notice_code_shows_nothing(client: TestClient) -> None:
    """Nothing user-supplied is echoed into the page."""

    page = client.get("/admin?notice=<script>alert(1)</script>", auth=("admin", ENV_PASSWORD)).text

    assert "<script>alert(1)</script>" not in page
    assert "alert(1)" not in page
    # No banner element is rendered (the class name still appears in the stylesheet).
    assert 'class="admin-notice' not in page


def test_the_console_says_where_the_password_comes_from(client: TestClient) -> None:
    before = client.get("/admin", auth=("admin", ENV_PASSWORD)).text
    assert "LIVEON_ADMIN_PASSWORD" in before

    _change(client, ("admin", ENV_PASSWORD), ENV_PASSWORD, NEW_PASSWORD)

    after = client.get("/admin", auth=("admin", NEW_PASSWORD)).text
    assert "Set from this console" in after


def test_the_console_stays_off_with_no_password_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.delenv("LIVEON_ADMIN_PASSWORD", raising=False)
    app.state.admin_credential_store = None

    with TestClient(app) as fresh:
        assert fresh.get("/admin").status_code == 503

    app.state.admin_credential_store = None


def test_a_stored_password_alone_enables_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once set from the console, the environment variable is no longer needed."""

    AdminCredentialStore(tmp_path / "content.db").set_password(NEW_PASSWORD)
    monkeypatch.setenv("LIVEON_DB_PATH", str(tmp_path / "content.db"))
    monkeypatch.delenv("LIVEON_ADMIN_PASSWORD", raising=False)
    app.state.admin_credential_store = None
    app.dependency_overrides[get_repository] = lambda: _Repo()

    try:
        with TestClient(app) as fresh:
            assert fresh.get("/admin").status_code == 401
            assert fresh.get("/admin", auth=("admin", NEW_PASSWORD)).status_code == 200
    finally:
        app.dependency_overrides.pop(get_repository, None)
        app.state.admin_credential_store = None
