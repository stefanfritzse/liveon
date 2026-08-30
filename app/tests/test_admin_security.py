"""Access-control tests for the admin console.

The console can permanently delete published content. It used to be reachable by
anyone who could load the site — linked from the public navigation, with one-click
delete forms, no authentication and no cross-site protection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import pytest
from fastapi.testclient import TestClient

from app.main import ContentRepository, _paginate_in_memory, app, get_repository
from app.models.content import Article, Tip

ADMIN_USER = "curator"
ADMIN_PASSWORD = "s3cret-pass"
GOOD_CREDENTIALS = (ADMIN_USER, ADMIN_PASSWORD)
SAME_ORIGIN = {"Origin": "http://testserver"}


class RecordingRepository(ContentRepository):
    """Repository stub that records deletions instead of performing them."""

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self._articles = [
            Article(id="article-1", title="Stored Article", content_body="Body", published_date=now)
        ]
        self._tips = [Tip(id="tip-1", title="Stored Tip", content_body="Body", published_date=now)]
        self.deleted_articles: list[str] = []
        self.deleted_tips: list[str] = []

    def get_latest_articles(self, *, limit: int = 5) -> list[Article]:
        return list(self._articles[:limit])

    def get_article(self, article_id: str) -> Article | None:
        return next((a for a in self._articles if a.id == article_id), None)

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        return list(self._tips[:limit])

    def get_latest_tip(self) -> Tip | None:
        return self._tips[0] if self._tips else None

    def delete_article(self, article_id: str) -> bool:
        self.deleted_articles.append(article_id)
        return True

    def delete_tip(self, tip_id: str) -> bool:
        self.deleted_tips.append(tip_id)
        return True

    def browse_articles(self, **kwargs: object):
        return _paginate_in_memory(list(self._articles), **kwargs)

    def browse_tips(self, **kwargs: object):
        return _paginate_in_memory(list(self._tips), **kwargs)


@pytest.fixture()
def repository() -> RecordingRepository:
    return RecordingRepository()


@pytest.fixture()
def client(repository: RecordingRepository) -> Iterable[TestClient]:
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_repository, None)


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the admin console with known credentials."""

    monkeypatch.setenv("LIVEON_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("LIVEON_ADMIN_PASSWORD", ADMIN_PASSWORD)


@pytest.fixture()
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEON_ADMIN_USER", raising=False)
    monkeypatch.delenv("LIVEON_ADMIN_PASSWORD", raising=False)


# ----------------------------------------------------------------------
# Unconfigured: the console is off, not open
# ----------------------------------------------------------------------


def test_console_is_disabled_when_no_password_is_set(client: TestClient, unconfigured: None) -> None:
    response = client.get("/admin")

    assert response.status_code == 503
    # A browser request gets the styled page, with the operator hint intact.
    assert response.headers["content-type"].startswith("text/html")
    assert "LIVEON_ADMIN_PASSWORD" in response.text


def test_delete_is_disabled_when_no_password_is_set(
    client: TestClient, repository: RecordingRepository, unconfigured: None
) -> None:
    response = client.post("/admin/articles/article-1/delete", headers=SAME_ORIGIN)

    assert response.status_code == 503
    assert repository.deleted_articles == []


# ----------------------------------------------------------------------
# Configured: credentials are required
# ----------------------------------------------------------------------


def test_dashboard_requires_credentials(client: TestClient, configured: None) -> None:
    response = client.get("/admin")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_dashboard_rejects_wrong_password(client: TestClient, configured: None) -> None:
    response = client.get("/admin", auth=(ADMIN_USER, "wrong"))

    assert response.status_code == 401


def test_dashboard_rejects_wrong_username(client: TestClient, configured: None) -> None:
    response = client.get("/admin", auth=("intruder", ADMIN_PASSWORD))

    assert response.status_code == 401


def test_dashboard_allows_valid_credentials(client: TestClient, configured: None) -> None:
    response = client.get("/admin", auth=GOOD_CREDENTIALS)

    assert response.status_code == 200
    assert "Stored Article" in response.text


def test_delete_requires_credentials(
    client: TestClient, repository: RecordingRepository, configured: None
) -> None:
    response = client.post("/admin/articles/article-1/delete", headers=SAME_ORIGIN)

    assert response.status_code == 401
    assert repository.deleted_articles == []


def test_delete_rejects_bad_credentials(
    client: TestClient, repository: RecordingRepository, configured: None
) -> None:
    response = client.post(
        "/admin/tips/tip-1/delete", auth=(ADMIN_USER, "wrong"), headers=SAME_ORIGIN
    )

    assert response.status_code == 401
    assert repository.deleted_tips == []


# ----------------------------------------------------------------------
# Cross-site protection
# ----------------------------------------------------------------------


def test_delete_rejects_cross_origin_submission(
    client: TestClient, repository: RecordingRepository, configured: None
) -> None:
    """Basic-auth credentials are cached by the browser, so Origin must be checked."""

    response = client.post(
        "/admin/articles/article-1/delete",
        auth=GOOD_CREDENTIALS,
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert repository.deleted_articles == []


def test_delete_rejects_cross_origin_referer(
    client: TestClient, repository: RecordingRepository, configured: None
) -> None:
    response = client.post(
        "/admin/tips/tip-1/delete",
        auth=GOOD_CREDENTIALS,
        headers={"Referer": "http://evil.example/attack.html"},
    )

    assert response.status_code == 403
    assert repository.deleted_tips == []


def test_delete_rejects_submission_without_origin_or_referer(
    client: TestClient, repository: RecordingRepository, configured: None
) -> None:
    response = client.post("/admin/articles/article-1/delete", auth=GOOD_CREDENTIALS)

    assert response.status_code == 403
    assert repository.deleted_articles == []


def test_delete_accepts_same_origin_referer(
    client: TestClient, repository: RecordingRepository, configured: None
) -> None:
    response = client.post(
        "/admin/tips/tip-1/delete",
        auth=GOOD_CREDENTIALS,
        headers={"Referer": "http://testserver/admin"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert repository.deleted_tips == ["tip-1"]


# ----------------------------------------------------------------------
# Authorised deletion still works
# ----------------------------------------------------------------------


def test_authorised_article_delete_succeeds(
    client: TestClient, repository: RecordingRepository, configured: None
) -> None:
    response = client.post(
        "/admin/articles/article-1/delete",
        auth=GOOD_CREDENTIALS,
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert repository.deleted_articles == ["article-1"]


def test_authorised_tip_delete_succeeds(
    client: TestClient, repository: RecordingRepository, configured: None
) -> None:
    response = client.post(
        "/admin/tips/tip-1/delete",
        auth=GOOD_CREDENTIALS,
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert repository.deleted_tips == ["tip-1"]


def test_default_username_is_admin(
    client: TestClient, repository: RecordingRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LIVEON_ADMIN_USER", raising=False)
    monkeypatch.setenv("LIVEON_ADMIN_PASSWORD", ADMIN_PASSWORD)

    assert client.get("/admin", auth=("admin", ADMIN_PASSWORD)).status_code == 200


# ----------------------------------------------------------------------
# The console is no longer advertised to visitors
# ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/articles", "/tips", "/coach"])
def test_pages_do_not_link_a_console_that_is_switched_off(
    client: TestClient, path: str, unconfigured: None
) -> None:
    """With no password set there is no usable console, so nothing advertises one."""

    response = client.get(path)

    assert response.status_code == 200
    assert '/admin"' not in response.text


@pytest.mark.parametrize("path", ["/", "/articles", "/tips", "/coach"])
def test_pages_link_the_console_once_it_is_enabled(
    client: TestClient, path: str, configured: None
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert '/admin"' in response.text


def test_linking_the_console_does_not_make_it_open(
    client: TestClient, configured: None
) -> None:
    """The link is a convenience; the credentials are still the gate."""

    assert '/admin"' in client.get("/").text
    assert client.get("/admin").status_code == 401


def test_delete_forms_require_confirmation(client: TestClient, configured: None) -> None:
    """Every delete form carries the metadata the confirm handler needs."""

    response = client.get("/admin", auth=GOOD_CREDENTIALS)

    assert response.text.count("admin-delete-form") >= 2
    assert "window.confirm" in response.text
