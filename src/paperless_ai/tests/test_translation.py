from datetime import timedelta

import pytest
import pytest_mock
from django.utils import timezone
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks
from pytest_django.fixtures import Settings

from documents.models import Document
from documents.models import DocumentTranslation
from paperless.config import AIConfig
from paperless_ai import translation
from paperless_ai.exceptions import LLMTimeoutError
from paperless_testing.factories import DocumentFactory

GERMAN = (
    "Sehr geehrte Damen und Herren,\n\n"
    "hiermit bestätigen wir Ihre Anmeldung in der Wohnung. "
    "Diese Bescheinigung bitte sorgfältig aufbewahren.\n\n"
    "Mit freundlichen Grüßen"
)
ENGLISH = "Dear Sir or Madam, we hereby confirm your registration at the flat."


class FakeClient:
    """Translates by prefixing, recording the prompts it was sent."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run_text_query(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"translated #{len(self.prompts)}"


@pytest.fixture
def ai_enabled(settings: Settings) -> None:
    settings.AI_ENABLED = True
    settings.LLM_BACKEND = "ollama"
    settings.LLM_MODEL = "qwen3.5:4b"


@pytest.fixture
def fake_client(mocker: pytest_mock.MockerFixture) -> FakeClient:
    client = FakeClient()
    mocker.patch("paperless_ai.translation.AIClient", return_value=client)
    return client


class TestTranslationLanguage:
    @pytest.mark.django_db
    def test_requested_language(self) -> None:
        assert translation.translation_language(None, "DE-DE") == "de-de"

    @pytest.mark.django_db
    def test_rejects_unknown_language(self) -> None:
        with pytest.raises(ValueError, match="Unsupported language"):
            translation.translation_language(None, "klingon")

    @pytest.mark.django_db
    def test_defaults_to_output_language_then_english(
        self,
        settings: Settings,
    ) -> None:
        assert translation.translation_language(None) == "en-us"

        settings.LLM_OUTPUT_LANGUAGE = "fr-fr"
        assert translation.translation_language(None) == "fr-fr"


@pytest.mark.parametrize(
    ("code", "name"),
    [("en-us", "English (US)"), ("de", "German"), ("xx", "xx")],
)
def test_language_display_name(code: str, name: str) -> None:
    assert translation.language_display_name(code) == name


def test_detect_language() -> None:
    assert translation.detect_language(GERMAN) == "de"
    assert translation.detect_language(ENGLISH) == "en"
    assert translation.detect_language("1234 5678") == ""


@pytest.mark.parametrize(
    ("source", "target", "same"),
    [
        ("de", "de-de", True),
        ("en", "en-gb", True),
        ("de", "en-us", False),
        ("", "en-us", False),
    ],
)
def test_is_same_language(
    source: str,
    target: str,
    same: bool,  # noqa: FBT001
) -> None:
    assert translation.is_same_language(source, target) is same


class TestSplitText:
    def test_short_text_is_one_chunk(self) -> None:
        assert translation.split_text(GERMAN, 1500) == [GERMAN]

    def test_long_text_splits_between_sentences(self) -> None:
        sentence = "Ein vollständiger Satz über die Wohnung. "
        text = sentence * 400

        chunks = translation.split_text(text, 256)

        assert len(chunks) > 1
        assert all(chunk.rstrip().endswith(".") for chunk in chunks)
        assert "".join(chunks).replace(" ", "") == text.replace(" ", "")

    def test_budget_fits_the_context_window(self) -> None:
        config = AIConfig.__new__(AIConfig)
        config.llm_context_size = 8192
        assert translation.chunk_token_budget(config) == translation.MAX_CHUNK_TOKENS
        config.llm_context_size = 2048
        assert translation.chunk_token_budget(config) == (2048 - 512) // 2
        config.llm_context_size = 512
        assert translation.chunk_token_budget(config) == translation.MIN_CHUNK_TOKENS


@pytest.mark.django_db
def test_translate_text_translates_each_chunk(ai_enabled: None) -> None:
    client = FakeClient()
    text = "Ein Satz über die Wohnung. " * 400

    result = translation.translate_text(
        text,
        "en-us",
        config=AIConfig(),
        client=client,
    )

    assert len(client.prompts) > 1
    assert result == "\n\n".join(
        f"translated #{i}" for i in range(1, len(client.prompts) + 1)
    )
    assert "into English (US)" in client.prompts[0]
    assert "Ein Satz über die Wohnung." in client.prompts[0]


@pytest.mark.django_db
class TestRunTranslation:
    def _pending(self, content: str, language: str = "en-us") -> DocumentTranslation:
        document = DocumentFactory.create(content=content)
        return DocumentTranslation.objects.create(document=document, language=language)

    def test_translates_and_stores(
        self,
        ai_enabled: None,
        fake_client: FakeClient,
    ) -> None:
        pending = self._pending(GERMAN)

        translation.run_translation(pending.pk)

        pending.refresh_from_db()
        assert pending.status == DocumentTranslation.Status.DONE
        assert pending.content == "translated #1"
        assert pending.source_language == "de"
        assert pending.content_checksum == translation.content_checksum(GERMAN)
        assert pending.model == "qwen3.5:4b"
        assert not pending.truncated

    def test_text_already_in_the_language_is_kept(
        self,
        ai_enabled: None,
        fake_client: FakeClient,
    ) -> None:
        pending = self._pending(ENGLISH)

        translation.run_translation(pending.pk)

        pending.refresh_from_db()
        assert pending.status == DocumentTranslation.Status.DONE
        assert pending.content == ENGLISH
        assert pending.source_language == "en"
        assert fake_client.prompts == []

    def test_long_text_is_truncated(
        self,
        ai_enabled: None,
        fake_client: FakeClient,
        settings: Settings,
    ) -> None:
        settings.LLM_TRANSLATION_MAX_LENGTH = 40
        pending = self._pending(GERMAN)

        translation.run_translation(pending.pk)

        pending.refresh_from_db()
        assert pending.truncated
        assert "Sehr geehrte Damen und Herren" in fake_client.prompts[0]
        assert "Grüßen" not in fake_client.prompts[0]
        # The checksum covers the whole text, to detect later changes
        assert pending.content_checksum == translation.content_checksum(GERMAN)

    def test_failure_is_recorded(
        self,
        ai_enabled: None,
        fake_client: FakeClient,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        mocker.patch.object(fake_client, "run_text_query", side_effect=LLMTimeoutError)
        pending = self._pending(GERMAN)

        translation.run_translation(pending.pk)

        pending.refresh_from_db()
        assert pending.status == DocumentTranslation.Status.FAILED
        assert pending.error == "The AI backend timed out."

    def test_empty_text(self, ai_enabled: None, fake_client: FakeClient) -> None:
        pending = self._pending("   ")

        translation.run_translation(pending.pk)

        pending.refresh_from_db()
        assert pending.status == DocumentTranslation.Status.DONE
        assert pending.content == ""
        assert fake_client.prompts == []

    def test_missing_translation_is_skipped(self) -> None:
        translation.run_translation(12345)


@pytest.mark.django_db
class TestRequestTranslation:
    def test_queues_a_pending_translation(
        self,
        mocker: pytest_mock.MockerFixture,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        task = mocker.patch("documents.tasks.translate_document.apply_async")
        document: Document = DocumentFactory.create(content=GERMAN)

        with django_capture_on_commit_callbacks(execute=True):
            pending = translation.request_translation(document, "en-us")

        assert pending.status == DocumentTranslation.Status.PENDING
        task.assert_called_once()
        assert task.call_args.kwargs["kwargs"] == {
            "translation_id": pending.pk,
            "document_id": document.pk,
        }

    def test_replaces_a_finished_translation(
        self,
        mocker: pytest_mock.MockerFixture,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        task = mocker.patch("documents.tasks.translate_document.apply_async")
        document: Document = DocumentFactory.create(content=GERMAN)
        DocumentTranslation.objects.create(
            document=document,
            language="en-us",
            status=DocumentTranslation.Status.FAILED,
            error="earlier failure",
        )

        with django_capture_on_commit_callbacks(execute=True):
            pending = translation.request_translation(document, "en-us")

        assert pending.status == DocumentTranslation.Status.PENDING
        assert pending.error == ""
        assert DocumentTranslation.objects.count() == 1
        task.assert_called_once()

    def test_does_not_queue_twice(
        self,
        mocker: pytest_mock.MockerFixture,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        task = mocker.patch("documents.tasks.translate_document.apply_async")
        document: Document = DocumentFactory.create(content=GERMAN)

        with django_capture_on_commit_callbacks(execute=True):
            translation.request_translation(document, "en-us")
            translation.request_translation(document, "en-us")

        task.assert_called_once()

    def test_requeues_a_stale_pending_translation(
        self,
        mocker: pytest_mock.MockerFixture,
        django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    ) -> None:
        task = mocker.patch("documents.tasks.translate_document.apply_async")
        document: Document = DocumentFactory.create(content=GERMAN)
        stale = DocumentTranslation.objects.create(document=document, language="en-us")
        DocumentTranslation.objects.filter(pk=stale.pk).update(
            modified=timezone.now() - timedelta(hours=1),
        )

        with django_capture_on_commit_callbacks(execute=True):
            translation.request_translation(document, "en-us")

        task.assert_called_once()
