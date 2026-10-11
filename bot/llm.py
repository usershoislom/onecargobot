"""LLM access: Gemini (free tier) first, Groq (free tier) as a fallback.

Gemini reads text, images, PDFs, audio and video natively in one call, so it
is the primary engine. If every configured Gemini model is rate-limited or
down, we fall back to Groq: Whisper transcribes audio, PDFs and photos are
turned into text locally with Tesseract OCR (see ocr.py), and an open-weight
model does the analysis.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from . import ocr
from .config import PROMPTS_DIR, Config
from .media import Item
from .schema import Analysis

log = logging.getLogger(__name__)

GROQ_BASE = "https://api.groq.com/openai/v1"
# 402 = prepaid credits used up, 403 = key not allowed for this model; both mean "try the next one".
RETRYABLE_CODES = {402, 403, 404, 429, 500, 502, 503, 504}
# After every Gemini model failed, skip Gemini for a while instead of re-trying it on each message.
GEMINI_COOLDOWN_SECONDS = {402: 3600, 403: 3600, 429: 600}
# The fallback has ~8K tokens/minute on Groq's free plan, so it gets a trimmed context.
FALLBACK_RECENT_ROWS = 40
# Larger files go through Gemini's Files API instead of inline bytes (request size limit).
INLINE_LIMIT_BYTES = 14 * 1024 * 1024


class LLMUnavailable(RuntimeError):
    pass


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.gemini = genai.Client(api_key=cfg.gemini_api_key)
        self.http = httpx.AsyncClient(timeout=120)
        self.gemini_down_until = 0.0

    # ------------------------------------------------------------ public

    async def analyze(self, items: list[Item], context: str) -> tuple[Analysis, str]:
        """Return (analysis, provider label)."""
        system = load_prompt("extract_system.md") + "\n\n" + load_prompt("playbooks.md")
        parts: list[types.Part] = [types.Part.from_text(text=context)]
        if not self._gemini_cooling_down():
            for n, item in enumerate(items, 1):
                if item.kind == "text":
                    parts.append(types.Part.from_text(text=f"--- item {n} ---\n{item.header}{item.text}"))
                else:
                    parts.append(types.Part.from_text(text=f"--- item {n}: {item.kind} {item.filename or ''} ---\n{item.header}"))
                    parts.append(await self._file_part(item))

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=Analysis,
            temperature=0.2,
        )
        try:
            resp, model = await self._gemini(parts, config)
            analysis = resp.parsed if isinstance(resp.parsed, Analysis) else Analysis.model_validate_json(resp.text)
            return analysis, model
        except LLMUnavailable as exc:
            if not self.cfg.groq_api_key:
                raise
            log.warning("Gemini unavailable (%s); using Groq fallback", exc)
            return await self._groq_analyze(items, context), f"groq:{self.cfg.groq_text_model}"

    async def answer(self, question_context: str) -> str:
        system = load_prompt("answer_system.md")
        config = types.GenerateContentConfig(system_instruction=system, temperature=0.2)
        try:
            resp, _ = await self._gemini([types.Part.from_text(text=question_context)], config)
            return (resp.text or "").strip()
        except LLMUnavailable:
            if not self.cfg.groq_api_key:
                raise
            return await self._groq_chat(system, question_context, json_mode=False)

    async def close(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------ gemini

    async def _file_part(self, item: Item) -> types.Part:
        if len(item.data or b"") <= INLINE_LIMIT_BYTES:
            return types.Part.from_bytes(data=item.data, mime_type=item.mime)
        uploaded = await self.gemini.aio.files.upload(
            file=io.BytesIO(item.data), config=types.UploadFileConfig(mime_type=item.mime)
        )
        for _ in range(30):  # video needs processing time; PDFs/audio are usually ready at once
            if uploaded.state is None or uploaded.state.name != "PROCESSING":
                break
            await asyncio.sleep(2)
            uploaded = await self.gemini.aio.files.get(name=uploaded.name)
        return types.Part.from_uri(file_uri=uploaded.uri, mime_type=item.mime)

    def _gemini_cooling_down(self) -> bool:
        return time.monotonic() < self.gemini_down_until

    async def _gemini(self, parts: list[types.Part], config: types.GenerateContentConfig):
        if self._gemini_cooling_down():
            raise LLMUnavailable("Gemini skipped (cooling down after recent failures)")
        last_error: Exception | None = None
        codes: set[int] = set()
        for model in self.cfg.gemini_models:
            try:
                resp = await self.gemini.aio.models.generate_content(
                    model=model, contents=[types.Content(role="user", parts=parts)], config=config
                )
                return resp, model
            except genai_errors.APIError as exc:
                if exc.code in RETRYABLE_CODES:
                    log.warning("Gemini model %s failed with %s %s, trying next", model, exc.code, exc.message)
                    last_error = exc
                    codes.add(exc.code)
                    continue
                raise
            except httpx.TransportError as exc:
                log.warning("Gemini model %s network error: %s", model, exc)
                last_error = exc
        cooldown = max((GEMINI_COOLDOWN_SECONDS.get(c, 0) for c in codes), default=0)
        if cooldown:
            self.gemini_down_until = time.monotonic() + cooldown
            log.warning("All Gemini models failed (%s); skipping Gemini for %d min", sorted(codes), cooldown // 60)
        raise LLMUnavailable(str(last_error))

    # ------------------------------------------------------------ groq fallback

    async def _groq_analyze(self, items: list[Item], context: str) -> Analysis:
        # Main rules only: the playbooks would use most of the backup model's token budget.
        system = load_prompt("extract_system.md")
        chunks = [trim_recent_rows(context, FALLBACK_RECENT_ROWS)]
        transcripts = []
        # The fallback model has a small per-minute token budget, so documents share a fixed char budget.
        docs = sum(1 for i in items if i.kind in ("pdf", "image")) or 1
        doc_budget = self.cfg.fallback_doc_chars // docs
        if ocr.tesseract_available():
            source = "OCR text"
            caveat = ("[OCR text, may contain misread characters. Mark values you can't confirm with ? "
                      "in checks; signatures can't be seen, so don't flag them as missing.]")
        else:
            source = "extracted text"
            caveat = ("[Text layer only: filled-in form values, handwriting and signatures may be missing. "
                      "Mark anything you can't confirm with ? in checks; don't mark signatures as missing.]")
        for n, item in enumerate(items, 1):
            if item.kind == "text":
                chunks.append(f"--- item {n} ---\n{item.header}{item.text}")
            elif item.kind == "audio":
                text = await self._groq_transcribe(item)
                transcripts.append(text)
                chunks.append(f"--- item {n}: voice transcript ---\n{item.header}{text}")
            elif item.kind == "pdf":
                text = await asyncio.to_thread(ocr.document_text, item.data, doc_budget, self.cfg.ocr_max_pages)
                chunks.append(f"--- item {n}: PDF {item.filename or ''} ({source}) ---\n{item.header}{caveat}\n{text}")
            elif item.kind == "image" and (text := await asyncio.to_thread(ocr.image_text, item.data)):
                chunks.append(f"--- item {n}: image ({source}) ---\n{item.header}{caveat}\n{text[:doc_budget]}")
            else:
                chunks.append(
                    f"--- item {n}: {item.kind} ---\n{item.header}[This {item.kind} cannot be viewed right now. "
                    "If it matters for the entry, set needs_clarification and ask the owner to describe it.]"
                )
        schema = json.dumps(Analysis.model_json_schema())
        sys_prompt = f"{system}\n\n# Output format\nReturn ONLY a JSON object matching this JSON Schema:\n{schema}"
        raw = await self._groq_chat(sys_prompt, "\n\n".join(chunks), json_mode=True)
        try:
            analysis = Analysis.model_validate_json(raw)
        except ValidationError as exc:
            raise LLMUnavailable(f"fallback model returned invalid JSON: {exc}") from exc
        if transcripts and not analysis.transcript:
            analysis.transcript = "\n".join(transcripts)
        return analysis

    async def _groq_chat(self, system: str, user: str, json_mode: bool) -> str:
        body = {
            "model": self.cfg.groq_text_model,
            "temperature": 0.2,
            "max_completion_tokens": 3000,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if "gpt-oss" in self.cfg.groq_text_model:
            body["reasoning_effort"] = "low"  # reasoning tokens count against the per-minute budget
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(2):
            r = await self.http.post(
                f"{GROQ_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {self.cfg.groq_api_key}"},
                json=body,
            )
            wait = float(r.headers.get("retry-after", "0") or 0)
            if r.status_code == 429 and attempt == 0 and wait <= 30:
                log.warning("Groq rate limit, retrying in %.0fs", wait)
                await asyncio.sleep(wait or 5)
                continue
            break
        if r.status_code != 200:
            raise LLMUnavailable(f"Groq chat {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]

    async def _groq_transcribe(self, item: Item) -> str:
        ext = (item.mime or "audio/ogg").split("/")[-1].replace("mpeg", "mp3")
        r = await self.http.post(
            f"{GROQ_BASE}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.cfg.groq_api_key}"},
            data={"model": self.cfg.groq_whisper_model, "language": "en", "response_format": "json"},
            files={"file": (f"audio.{ext}", item.data, item.mime or "audio/ogg")},
        )
        if r.status_code != 200:
            raise LLMUnavailable(f"Groq transcription {r.status_code}: {r.text[:200]}")
        return r.json().get("text", "").strip()


def trim_recent_rows(context: str, keep: int) -> str:
    """Keep only the last `keep` lines of the RECENT ROWS block in a context string."""
    lines = context.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("RECENT ROWS")) + 1
    except StopIteration:
        return context
    end = next((i for i in range(start, len(lines)) if not lines[i].strip()), len(lines))
    rows = lines[start:end]
    if len(rows) <= keep:
        return context
    return "\n".join(lines[:start] + rows[-keep:] + lines[end:])
