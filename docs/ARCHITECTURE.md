# Architecture

## Big picture

```
 You (Telegram)                       Server (always-free VM)                        Google
 ─────────────                        ─────────────────────────                      ──────
 text / voice / photo / PDF  ──►  1. Intake: download files, note forward origin
 forwarded msgs / albums          2. Batcher: wait 4 s, group related messages
                                  3. Context: NOW (Eastern), colleagues, open rows  ◄── Sheet: Settings, Log
                                  4. AI analysis ───────────────────────────────────►  Gemini Flash (free)
                                     (one call reads text+images+PDF+audio)              ↳ fallback: Groq
                                  5. Verify: dates, amounts, status, names, row refs
                                  6a. unclear? ──► ask you a question ──► your answer re-enters step 3
                                  6b. clear ──► append rows, paint cyan ────────────►  Sheet: Log
                                                mark closed rows Done
 ◄── summary + Undo button        7. Reply
 ◄── 9 AM / 4 PM summaries        8. Scheduler reads Log, builds summary (no AI)    ◄── Sheet: Log
```

## Components

| File | Job |
|---|---|
| `bot/main.py` | Telegram handlers, batching, clarify/save/undo flow, scheduled summaries |
| `bot/media.py` | Converts a Telegram message into items: text, image, PDF, audio, video, text files; forward headers |
| `bot/llm.py` | Gemini model chain (falls back across models on 429/5xx), Groq fallback (Whisper + text model, PDF text via pypdf) |
| `bot/schema.py` | The JSON structure the AI must return + the **verification layer** |
| `bot/pipeline.py` | Builds the AI context and the Telegram replies |
| `bot/sheets.py` | Sheet layout, append, cyan highlight, status updates, undo, Settings tab |
| `bot/digest.py` | Daily follow-up summary and schedule parsing |
| `prompts/extract_system.md` | **Main system prompt**: how to understand and classify messages |
| `prompts/answer_system.md` | System prompt for questions about the log |

The prompts live in plain Markdown files, so you can tune the bot's behavior
without touching code. The bot re-reads them on every message, so edits apply
without a restart.

## The Google Sheet

**Log** (main tab, append-only)

| A Date | B Company | C Driver Name | D Amount | E Action | F Deadline | G Assigned To | H Problem | I Status | J Original Message |
|---|---|---|---|---|---|---|---|---|---|
| 10/11/2026 | ABC Logistics | John Smith | $185.00 | Reimburse lumper fee | 10/13/2026 | Bob Miller | Lumper paid at Walmart DC, receipt attached | Action | [Forwarded from John] lumper 185 … |

- Date = day you sent it (mm/dd/yyyy). Deadline = mm/dd/yyyy or mm/dd/yyyy hh:mm AM/PM.
- Status dropdown: **Note / Follow-up / Action / Done**.
- Assigned To dropdown comes from the colleague names you type in **Settings → column E**.
- **Original Message** keeps what you sent (plus the voice transcript and file
  descriptions), so you can always check the bot's interpretation.
- The rows from the most recent message are **cyan**; the previous batch goes back to white.
- Deadline cell turns **red** when overdue and **amber** when due today (until Done).

**Today**: a live formula view of every open item due today or overdue, sorted by deadline.

**Settings**: editable by you, re-read every 5 minutes:

| Setting | Default | Meaning |
|---|---|---|
| reminder_times | `09:00, 16:00` | Summary times, US Eastern. Any number of times. Also settable via `/reminders 8:30, 16:00` |
| reminder_days | `Mon, …, Sun` | Days to send summaries |
| auto_close_rows | `TRUE` | When you report "paid John $500", also flip the matching open row to Done |
| last_added_rows | (bot-managed) | Which rows are cyan right now |

## Message handling rules

| You send | What happens |
|---|---|
| Text | Analyzed directly |
| Voice note / audio | Gemini transcribes and analyzes in one step; transcript saved in Original Message |
| Photo, screenshot, album | Read visually (receipts, BOLs, damage photos, chat screenshots) |
| PDF | Read directly, including scanned PDFs (rate cons, invoices, settlements) |
| Text + photo/PDF, several forwards | Anything within 4 s is one batch → understood together |
| Forwarded message | Logged with "Forwarded from X, date" so the AI knows who said it |
| A question ("what's overdue?") | Answered from the sheet, nothing logged |
| Video / video note | Read by Gemini (≤ 20 MB, Telegram's bot limit) |
| .docx / .xlsx | Not supported: send a PDF or screenshot |

### "Analyze what I mean" (how accuracy is enforced)

1. **Rich context**: the AI gets today's date and weekday (to resolve "by
   Friday"), your colleague list, and your open rows (to recognize when a
   message finishes an earlier item).
2. **Strict output schema**: Gemini must return JSON matching `schema.py`:
   no free-form text, and only valid status values.
3. **Self-check**: the prompt makes the model verify every name, amount and
   date against the source before answering, and forbids invented deadlines.
4. **Code verification**: dates are re-parsed and normalized, amounts
   rounded and sanity-checked, colleague names matched ("bob" → "Bob Miller"),
   and closed-row references must be real open rows.
5. **Ask, don't guess**: if the meaning or a key value is unclear, the bot
   asks you one specific question and shows its draft. You can answer by text
   or voice, tap **Save draft anyway**, or tap **Cancel**.
6. **Undo**: every save has an Undo button (or `/undo`).

## Free AI options (researched October 2026)

| Provider | What's free | Reads | Role here |
|---|---|---|---|
| **Google Gemini API**, Flash / Flash-Lite models | Free tier on `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5/3.1-flash-lite`, `gemini-2.5-flash(-lite)`; Pro models need billing | Text, images, PDF, audio, video in one call; native JSON-schema output | **Primary** |
| **Groq** | Free plan; Whisper large-v3(-turbo) speech-to-text (~2,000 req/day reported), `openai/gpt-oss-120b`, `llama-3.3-70b-versatile` | Text + audio (via Whisper), no vision on free production models | **Fallback** |
| OpenRouter `:free` models | 20 req/min; 50 req/day (1,000/day after a one-time $10 top-up) | Varies by model | Not used (low daily cap); could be added as a 2nd fallback |

Exact free-tier rate limits change often and aren't published per model on
the docs pages (they're shown in each console). Third-party reports range
from ~20 to ~1,500 requests/day for Flash models. One person logging notes
uses maybe 20–100 calls a day, and the bot rotates through several
Gemini models before falling back to Groq, so the free tiers should cover it.
Daily summaries are built in code and use **no** AI calls.

If you ever outgrow the free tier, adding billing to the Gemini project
costs cents per day at this volume and stops Google from training on your data.

## Possible next steps

- Exact-time pings for deadlines with a time ("call broker at 3 PM").
- Weekly report (totals per driver/company, overdue count).
- Save photos/PDFs to Google Drive and link them in the row.
- Multi-user / group-chat support.
