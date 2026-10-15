"""Document question answering for the Chat service.

Retrieval is keyword based: documents are split into overlapping-free chunks at
paragraph boundaries, and a question is answered from the chunks whose vocabulary
overlaps it most. That keeps the service free of embedding calls and extra
dependencies, at the cost of missing paraphrased questions - so the answer
prompt tells the model to say when the documents do not cover something, rather
than filling the gap from its own memory.
"""

import re

from .azure_ai import (
    credential,
    CHAT_REQUIRED,
    ServiceError,
    is_transport_failure,
    require_configured,
    openai_client_kwargs,
)
from .models import ChatDocument, ChatDocumentChunk
from .settings import get_setting

#: Hard ceiling on the context handed to the model, regardless of settings.
MAX_CONTEXT_CHARS = 24000
#: Ceiling on prior turns replayed for conversational follow-ups.
MAX_HISTORY_TURNS = 8
#: Used when AZURE_OPENAI_API_VERSION is absent from the environment. Any
#: version that supports chat completions will do; this one is widely deployed.
DEFAULT_API_VERSION = "2024-10-21"

_TOKEN = re.compile(r"[a-z0-9]+")
#: Section markers the extractors insert, e.g. "[Page 4]" or "[Sheet: Data]".
_MARKER = re.compile(r"^\s*\[(?:Page|Slide|Sheet|Section)\b[^\]]*\]\s*$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because been before being
    below between both but by can cannot could did do does doing down during each few for from
    further had has have having he her here hers him his how i if in into is it its itself just
    me more most my no nor not of off on once only or other our out over own same she should so
    some such than that the their them then there these they this those through to too under
    until up very was we were what when where which while who whom why will with would you your
    doc document file page please tell me know about does
    """.split()
)


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN.findall((text or "").casefold()) if token not in _STOPWORDS}


def _split_long(paragraph: str, limit: int) -> list[str]:
    """Break a paragraph that is larger than one chunk at sentence boundaries."""
    if len(paragraph) <= limit:
        return [paragraph]

    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        while len(sentence) > limit:
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        current = sentence
    if current:
        pieces.append(current)
    return pieces


def chunk_text(text: str, chunk_chars: int, start_ordinal: int = 0) -> list[dict]:
    """Split ``text`` into retrievable chunks.

    Paragraphs are packed greedily up to ``chunk_chars``. Each chunk records the
    most recent section marker above it, which gives the model a breadcrumb when
    it quotes the chunk.
    """
    limit = max(400, int(chunk_chars or 1200))
    chunks: list[dict] = []
    heading = ""
    buffer: list[str] = []
    buffered = 0

    def flush() -> None:
        nonlocal buffer, buffered
        if not buffer:
            return
        chunks.append(
            {
                "ordinal": start_ordinal + len(chunks),
                "text": "\n".join(buffer).strip(),
                "heading": heading,
            }
        )
        buffer = []
        buffered = 0

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _MARKER.match(line) or line.startswith("#"):
            flush()
            heading = line.lstrip("# ").strip()[:300]
            continue
        for piece in _split_long(line, limit):
            if buffered + len(piece) > limit and buffer:
                flush()
            buffer.append(piece)
            buffered += len(piece) + 1

    flush()
    return [chunk for chunk in chunks if chunk["text"]]


def _setting_int(key: str, fallback: int, minimum: int, maximum: int) -> int:
    try:
        value = int(get_setting(key))
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, value))


def chunk_size() -> int:
    return _setting_int("chat_chunk_chars", 1200, 400, 8000)


def max_chunks() -> int:
    return _setting_int("chat_max_chunks", 6, 1, 20)


def db_max_ordinal() -> int:
    """The next free ordinal for the whole corpus.

    Ordinals are unique across documents so the citation numbers shown to the
    model and the user stay stable and unambiguous within one conversation.
    """
    from .extensions import db

    current = db.session.scalar(db.select(ChatDocumentChunk.ordinal).order_by(ChatDocumentChunk.ordinal.desc()))
    return (current + 1) if current is not None else 0


def index_document(text: str, chunk_chars: int | None = None) -> list[dict]:
    """Chunk a whole document, continuing the corpus-wide ordinal sequence."""
    return chunk_text(text, chunk_chars or chunk_size(), db_max_ordinal())


def rank_chunks(question: str, chunks, limit: int | None = None) -> list[dict]:
    """Score chunks against ``question`` and return the best ``limit`` of them.

    Coverage of the question's terms drives the score; term density breaks ties
    so a short, on-topic chunk outranks a long one that mentions everything.
    """
    terms = _tokens(question)
    if not terms or not chunks:
        return []

    scored: list[dict] = []
    for chunk in chunks:
        body = _tokens(f"{chunk.heading} {chunk.text}")
        if not body:
            continue
        overlap = terms & body
        if not overlap:
            continue
        coverage = len(overlap) / len(terms)
        density = len(overlap) / (len(body) ** 0.5)
        scored.append(
            {
                "chunk": chunk,
                "score": coverage * 2.0 + density * 0.5,
                "coverage": coverage,
                "terms": sorted(overlap),
            }
        )

    scored.sort(key=lambda item: (-item["score"], item["chunk"].ordinal))
    top = scored[: (limit or max_chunks())]
    # Present them in document order so the model reads them as a narrative.
    top.sort(key=lambda item: (item["chunk"].document_id, item["chunk"].ordinal))
    return top


def _sources(top: list[dict]) -> list[dict]:
    sources = []
    for number, item in enumerate(top, 1):
        chunk = item["chunk"]
        document = chunk.document
        sources.append(
            {
                "number": number,
                "document_id": document.id,
                "filename": document.filename,
                "heading": chunk.heading,
                "score": round(item["score"], 3),
                "coverage": round(item["coverage"] * 100),
                "excerpt": chunk.text[:400].strip(),
            }
        )
    return sources


def _build_context(top: list[dict]) -> tuple[str, list[dict]]:
    blocks: list[str] = []
    used = 0
    kept: list[dict] = []
    for number, item in enumerate(top, 1):
        chunk = item["chunk"]
        block = (
            f"[{number}] {chunk.document.filename}"
            + (f" - {chunk.heading}" if chunk.heading else "")
            + f"\n{chunk.text}"
        )
        if used and used + len(block) > MAX_CONTEXT_CHARS:
            break
        blocks.append(block)
        used += len(block)
        kept.append(item)
    return "\n\n".join(blocks), kept


SYSTEM_PROMPT = """You answer questions using only the numbered document extracts provided.

Rules:
- Cite every claim with its bracketed number, for example "The due date is 30 June [2]."
- If the extracts do not contain the answer, say so plainly. Do not use outside \
knowledge and do not guess.
- If the extracts disagree, say they disagree and quote both.
- When information spans multiple documents or sections, synthesize the answer across all relevant documents and cite each source.
- Answer in the language of the question.
- Keep the answer tight: a short paragraph or a short list, no preamble."""


def _history_messages(history) -> list[dict]:
    messages: list[dict] = []
    for turn in list(history or [])[-MAX_HISTORY_TURNS * 2:]:
        role = (turn.get("role") if isinstance(turn, dict) else None) or ""
        content = (turn.get("content") if isinstance(turn, dict) else None) or ""
        if role in {"user", "assistant"} and str(content).strip():
            messages.append({"role": role, "content": str(content).strip()})
    return messages


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, ServiceError):
        return str(exc)
    message = str(exc).strip()
    lowered = message.casefold()
    if "401" in lowered or "unauthorized" in lowered or "access denied" in lowered:
        return "Azure OpenAI rejected the key. Check the Azure OpenAI key in Settings."
    if "404" in lowered and "deployment" in lowered:
        return "Azure OpenAI deployment not found. Check the deployment name in Settings."
    if "429" in lowered or "quota" in lowered or "rate limit" in lowered:
        return "Azure OpenAI rate limit or quota reached. Check the quota on your resource."
    if "context_length" in lowered or "maximum context" in lowered:
        return "The question matched too much text to fit the model's context. Try a more specific question."
    if is_transport_failure(exc):
        return (
            "Azure OpenAI could not be reached. Check the endpoint in Settings and "
            "that this machine has a route to it."
        )
    return message or exc.__class__.__name__


def ask(question: str, history=None) -> dict:
    """Answer ``question`` from the indexed documents.

    Returns ``{"answer": str, "sources": [...], "asked_at": datetime}``. Raises
    :class:`ServiceNotConfigured` when credentials are missing and
    :class:`ServiceError` when the service fails or nothing relevant is indexed.
    """
    from datetime import datetime, timezone

    from .extensions import db

    require_configured(CHAT_REQUIRED)

    question = (question or "").strip()
    if not question:
        raise ServiceError("Type a question about your documents.")

    chunks = list(
        db.session.scalars(
            db.select(ChatDocumentChunk)
            .options(db.joinedload(ChatDocumentChunk.document))
            .order_by(ChatDocumentChunk.ordinal)
        )
    )
    if not chunks:
        raise ServiceError(
            "No documents have been uploaded yet. Add a document before asking a question."
        )

    top = rank_chunks(question, chunks)
    if not top:
        return {
            "answer": (
                "None of the uploaded documents mention anything matching that question. "
                "Try different wording, or add a document that covers it."
            ),
            "sources": [],
            "asked_at": datetime.now(timezone.utc),
            "no_match": True,
        }

    context, kept = _build_context(top)
    if not kept:
        raise ServiceError("The matching text is too large to send. Try a more specific question.")

    from openai import AzureOpenAI

    # No temperature is sent. The o4 reasoning models reject the parameter
    # outright - "Only the default (1) value is supported" - and that rejection is
    # a 400 on every request, so the service never worked against one. Tightness
    # comes from the system prompt instead, which asks for a short answer.
    client = AzureOpenAI(
        api_version=credential("azure_openai_api_version") or DEFAULT_API_VERSION,
        azure_endpoint=credential("azure_openai_endpoint").rstrip("/"),
        api_key=credential("azure_openai_key"),
        azure_deployment=credential("azure_openai_deployment"),
        **openai_client_kwargs(),
    )
    try:
        response = client.chat.completions.create(
            model=credential("azure_openai_deployment"),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *_history_messages(history),
                {
                    "role": "user",
                    "content": (
                        f"Document extracts:\n\n{context}\n\n"
                        f"Question: {question}\n\n"
                        "Answer using only the extracts above and cite them by number."
                    ),
                },
            ],
        )
        answer = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a flash
        raise ServiceError(_friendly_error(exc)) from exc

    if not answer:
        raise ServiceError("Azure OpenAI returned an empty answer. Try rephrasing the question.")

    return {
        "answer": answer,
        "sources": _sources(kept),
        "asked_at": datetime.now(timezone.utc),
        "no_match": False,
    }


def corpus_summary() -> dict:
    """Counts for the Chat page header."""
    from .extensions import db

    documents = db.session.scalar(db.select(db.func.count(ChatDocument.id))) or 0
    chunks = db.session.scalar(db.select(db.func.count(ChatDocumentChunk.id))) or 0
    return {"documents": documents, "chunks": chunks}
