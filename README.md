# onecargobot

A private Telegram bot that turns your notes into a clean Google Sheet log.

Send it anything: typed notes, voice messages, photos, PDFs, forwarded chats,
or a mix. It works out what you mean, checks it, and adds rows to your sheet:

**Date · Company · Driver Name · Amount · Action · Deadline · Assigned To · Problem · Status (Note / Follow-up / Action / Done)**

- The newest rows are highlighted **cyan**.
- When something is unclear, it **asks you** before saving.
- When you report something finished, it logs a new Done row and closes the
  matching open item.
- It sends the owner a **follow-up summary** at 9 AM and 4 PM Eastern (change the times with
  `/reminders` or in the Settings tab): overdue, due today, due tomorrow, open items.
- Ask it questions: *"what's overdue for Bob?"*, *"how much do I owe Mike?"*
- Runs on free tiers: Google Gemini Flash (AI), Groq (backup), Google Cloud's always-free VM.

**Docs**

- [docs/LOCAL.md](docs/LOCAL.md): run it on your own computer (Windows / macOS), free
- [docs/DEPLOY.md](docs/DEPLOY.md): step-by-step setup (Telegram, Gemini key, Google Sheet access, free server)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it works, sheet layout, free-AI comparison
- [prompts/extract_system.md](prompts/extract_system.md): the AI instructions (edit to tune behavior)

**Commands:** `/today` · `/reminders [times]` · `/colleagues` · `/undo` · `/cancel` · `/whoami` · `/help`
**Admin:** `/users` · `/adduser` · `/removeuser` · `/setowner`. The owner gets the daily summaries.
