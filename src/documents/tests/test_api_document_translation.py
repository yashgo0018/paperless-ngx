"""The document translation endpoint: reading, requesting and deleting AI
translations, with document permissions and AI enabled."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rest_framework import status

from documents.models import Document
from documents.models import DocumentTranslation
from paperless_ai.translation import content_checksum
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global
from paperless_testing.permissions import grant_object

if TYPE_CHECKING:
    import pytest_mock
    from django.contrib.auth.models import User
    from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks
    from pytest_django.fixtures import Settings
    from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

CONTENT = "Sehr geehrte Damen und Herren, hiermit bestätigen wir Ihre Anmeldung."


@pytest.fixture
def ai_enabled(settings: Settings) -> None:
    settings.AI_ENABLED = True


@pytest.fixture
def document() -> Document:
    return DocumentFactory.create(content=CONTENT, owner=UserFactory(username="o"))


@pytest.fixture
def translate_task(mocker: pytest_mock.MockerFixture):
    return mocker.patch("documents.tasks.translate_document.apply_async")


def _url(document: Document) -> str:
    return f"/api/documents/{document.pk}/translation/"


def _done(document: Document, **kwargs) -> DocumentTranslation:
    return DocumentTranslation.objects.create(
        document=document,
        language="en-us",
        source_language="de",
        status=DocumentTranslation.Status.DONE,
        content="Dear Sir or Madam, we hereby confirm your registration.",
        content_checksum=content_checksum(CONTENT),
        model="qwen3.5:4b",
        **kwargs,
    )


class TestGetTranslation:
    def test_not_translated_yet(
        self,
        admin_client: APIClient,
        ai_enabled: None,
        document: Document,
    ) -> None:
        response = admin_client.get(_url(document))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "language": "en-us",
            "language_name": "English (US)",
            "status": None,
        }

    def test_translated(
        self,
        admin_client: APIClient,
        ai_enabled: None,
        document: Document,
    ) -> None:
        _done(document)

        response = admin_client.get(_url(document))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == "done"
        assert response.data["content"].startswith("Dear Sir or Madam")
        assert response.data["source_language_name"] == "German"
        assert response.data["outdated"] is False

    def test_outdated_once_the_text_changes(
        self,
        admin_client: APIClient,
        ai_enabled: None,
        document: Document,
    ) -> None:
        _done(document)
        document.content = "Neuer Inhalt nach erneuter Texterkennung."
        document.save()

        response = admin_client.get(_url(document))

        assert response.data["outdated"] is True

    def test_other_language(
        self,
        admin_client: APIClient,
        ai_enabled: None,
        document: Document,
    ) -> None:
        _done(document)

        response = admin_client.get(f"{_url(document)}?language=fr-fr")

        assert response.data["status"] is None
        assert response.data["language"] == "fr-fr"

    def test_unknown_language(
        self,
        admin_client: APIClient,
        ai_enabled: None,
        document: Document,
    ) -> None:
        response = admin_client.get(f"{_url(document)}?language=klingon")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_requires_view_permission(
        self,
        user_client: APIClient,
        regular_user: User,
        ai_enabled: None,
        document: Document,
    ) -> None:
        grant_global(regular_user, "view_document")

        response = user_client.get(_url(document))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_requires_ai(self, admin_client: APIClient, document: Document) -> None:
        response = admin_client.get(_url(document))

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_versions_share_the_translation_of_their_root(
        self,
        admin_client: APIClient,
        ai_enabled: None,
        document: Document,
    ) -> None:
        _done(document)
        version = DocumentFactory.create(root_document=document, content=CONTENT)

        response = admin_client.get(_url(version))

        assert response.data["status"] == "done"


class TestRequestTranslation:
    def test_queues_translation(
        self,
        admin_client: APIClient,
        ai_enabled: None,
        document: Document,
        translate_task,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            response = admin_client.post(
                _url(document),
                {"language": "de-de"},
                format="json",
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.data["status"] == "pending"
        assert response.data["language"] == "de-de"
        translation = DocumentTranslation.objects.get(document=document)
        translate_task.assert_called_once()
        assert translate_task.call_args.kwargs["kwargs"] == {
            "translation_id": translation.pk,
            "document_id": document.pk,
        }

    def test_requires_change_permission(
        self,
        user_client: APIClient,
        regular_user: User,
        ai_enabled: None,
        document: Document,
        translate_task,
    ) -> None:
        grant_global(regular_user, "view_document")
        grant_object(regular_user, document, "view_document")

        response = user_client.post(_url(document), {}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        translate_task.assert_not_called()

    def test_viewer_can_read(
        self,
        user_client: APIClient,
        regular_user: User,
        ai_enabled: None,
        document: Document,
    ) -> None:
        grant_global(regular_user, "view_document")
        grant_object(regular_user, document, "view_document")
        _done(document)

        response = user_client.get(_url(document))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == "done"


class TestDeleteTranslation:
    def test_deletes(
        self,
        admin_client: APIClient,
        ai_enabled: None,
        document: Document,
    ) -> None:
        _done(document)

        response = admin_client.delete(_url(document))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not DocumentTranslation.objects.exists()

    def test_deleted_with_the_document(
        self,
        ai_enabled: None,
        document: Document,
    ) -> None:
        _done(document)

        document.hard_delete()

        assert not DocumentTranslation.objects.exists()
