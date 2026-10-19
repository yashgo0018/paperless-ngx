"""Translating a document's text with the configured LLM.

The text is split into paragraph- and sentence-aligned chunks sized for the
model's context window and translated one chunk at a time, so documents of
any length fit small local models. Translations run as a background task and
are stored as DocumentTranslation rows, one per document and language.
"""

import hashlib
import logging
from datetime import timedelta
from typing import Final

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from documents.models import Document
from documents.models import DocumentTranslation
from documents.models import PaperlessTask
from paperless.config import AIConfig
from paperless_ai.ai_classifier import get_language_name
from paperless_ai.ai_classifier import get_llm_output_language
from paperless_ai.client import AIClient
from paperless_ai.db import db_connection_released
from paperless_ai.exceptions import LLMProviderError
from paperless_ai.exceptions import LLMTimeoutError
from paperless_ai.prompts.context import TranslationPromptContext
from paperless_ai.prompts.render import render_prompt

logger = logging.getLogger("paperless_ai.translation")

# Used when neither PAPERLESS_AI_LLM_OUTPUT_LANGUAGE nor the user's UI
# language names one
DEFAULT_TRANSLATION_LANGUAGE: Final = "en-us"

# Tokens of document text per request. The answer needs about as many again,
# plus the prompt, so a chunk takes under half of the context window.
MAX_CHUNK_TOKENS: Final = 1500
MIN_CHUNK_TOKENS: Final = 256
_PROMPT_TOKENS: Final = 512

# A pending translation older than this is assumed lost (e.g. the worker
# restarted) and may be requested again
STALE_PENDING_AFTER: Final = timedelta(minutes=30)


def content_checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def translation_language(user: User | None, requested: str | None = None) -> str:
    """The language code to translate into: ``requested`` if given, else the
    configured AI output language or the user's UI language.

    Raises ValueError for a code that is not a Paperless-ngx language.
    """
    language = (
        requested
        or get_llm_output_language(AIConfig(), user)
        or DEFAULT_TRANSLATION_LANGUAGE
    ).lower()
    known = {code.lower() for code, _name in settings.LANGUAGES}
    if language not in known:
        raise ValueError(f"Unsupported language: {language}")
    return language


def language_display_name(code: str) -> str:
    """A readable name for a language code, also for bare codes like "de"
    as langdetect returns them: the name of the first Paperless-ngx language
    of that base language, without its region."""
    name = get_language_name(code)
    if name != code:
        return name
    base = code.split("-", maxsplit=1)[0].lower()
    for language_code, language_name in settings.LANGUAGES:
        if language_code.split("-")[0].lower() == base:
            return str(language_name).split(" (")[0]
    return code


def detect_language(text: str) -> str:
    """The ISO 639-1 code of ``text``'s language, or "" if unknown."""
    from langdetect import DetectorFactory
    from langdetect import LangDetectException
    from langdetect import detect

    # langdetect is randomized; seed it for repeatable results
    DetectorFactory.seed = 0
    try:
        return detect(text[:5000])
    except LangDetectException:
        return ""


def is_same_language(source_language: str, language: str) -> bool:
    """Whether a detected language matches a target like en-us, by its
    base language."""
    return bool(source_language) and (
        source_language.split("-", maxsplit=1)[0].lower()
        == language.split("-", maxsplit=1)[0].lower()
    )


def chunk_token_budget(config: AIConfig) -> int:
    return max(
        MIN_CHUNK_TOKENS,
        min(MAX_CHUNK_TOKENS, (config.llm_context_size - _PROMPT_TOKENS) // 2),
    )


def split_text(text: str, chunk_tokens: int) -> list[str]:
    """``text`` in chunks of at most ``chunk_tokens``, split between
    paragraphs, else sentences, so no chunk ends mid-sentence."""
    from llama_index.core.node_parser import SentenceSplitter

    splitter = SentenceSplitter(
        chunk_size=chunk_tokens,
        chunk_overlap=0,
        paragraph_separator="\n\n",
    )
    return [chunk for chunk in splitter.split_text(text) if chunk.strip()]


def translate_text(
    text: str,
    language: str,
    *,
    config: AIConfig,
    client: AIClient,
) -> str:
    language_name = get_language_name(language)
    translated = [
        client.run_text_query(
            render_prompt(
                TranslationPromptContext(language_name=language_name, text=chunk),
            ),
        )
        for chunk in split_text(text, chunk_token_budget(config))
    ]
    return "\n\n".join(chunk for chunk in translated if chunk)


def request_translation(
    document: Document,
    language: str,
) -> DocumentTranslation:
    """Queue a translation of ``document`` into ``language``, replacing any
    earlier one. A translation already pending is returned as it is."""
    from documents.tasks import translate_document

    translation, created = DocumentTranslation.objects.get_or_create(
        document=document,
        language=language,
    )
    if (
        not created
        and translation.status == DocumentTranslation.Status.PENDING
        and translation.modified > timezone.now() - STALE_PENDING_AFTER
    ):
        return translation

    translation.status = DocumentTranslation.Status.PENDING
    translation.error = ""
    translation.save()
    transaction.on_commit(
        lambda: translate_document.apply_async(
            kwargs={"translation_id": translation.pk, "document_id": document.pk},
            headers={"trigger_source": PaperlessTask.TriggerSource.MANUAL},
        ),
    )
    return translation


def run_translation(translation_id: int) -> None:
    """Translate and store a pending translation, recording any failure on
    it rather than raising, so the user sees why it failed."""
    try:
        translation = DocumentTranslation.objects.select_related("document").get(
            pk=translation_id,
        )
    except DocumentTranslation.DoesNotExist:
        logger.info("Translation %s no longer exists, skipping", translation_id)
        return

    config = AIConfig()
    text = translation.document.get_effective_content() or ""
    checksum = content_checksum(text)
    max_length = config.llm_translation_max_length
    truncated = len(text) > max_length
    text = text[:max_length]
    source_language = detect_language(text) if text.strip() else ""

    try:
        if not text.strip():
            content = ""
            model = ""
        elif is_same_language(source_language, translation.language):
            # Nothing to translate: keep the text, say so via source_language
            content = text
            model = ""
        else:
            client = AIClient()
            # The LLM requests are slow and need no ORM access; release the
            # pooled DB connection meanwhile, see paperless_ai.db and #12976
            with db_connection_released():
                content = translate_text(
                    text,
                    translation.language,
                    config=config,
                    client=client,
                )
            model = config.llm_model or ""
    except Exception as exc:
        logger.exception(
            "Failed to translate document %s into %s",
            translation.document_id,
            translation.language,
        )
        translation.status = DocumentTranslation.Status.FAILED
        translation.error = _failure_message(exc)
        translation.save()
        return

    translation.status = DocumentTranslation.Status.DONE
    translation.content = content
    translation.source_language = source_language
    translation.content_checksum = checksum
    translation.truncated = truncated
    translation.model = model
    translation.error = ""
    translation.save()


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, LLMTimeoutError):
        return "The AI backend timed out."
    if isinstance(exc, LLMProviderError):
        return "The AI backend rejected the request, see the logs for details."
    if isinstance(exc, ValueError):
        return "The AI configuration is invalid, see the logs for details."
    return "The translation failed, see the logs for details."
