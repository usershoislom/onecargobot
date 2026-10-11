"""Try the analysis on local files without Telegram or the sheet (only GEMINI_API_KEY needed).

    python -m bot.try_cli contract.pdf --caption "New contract for John starting 10.09.2026 ..."
    python -m bot.try_cli voice.ogg
    python -m bot.try_cli --caption "paid mike 185 lumper"

Nothing is written anywhere; it prints the bot's reply and the raw JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path

from .media import Item
from .schema import Colleague, verify

KINDS = {"application/pdf": "pdf"}


def item_from_file(path: Path) -> Item:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix.lower() in (".ogg", ".oga"):
        mime = "audio/ogg"
    kind = KINDS.get(mime) or mime.split("/")[0]
    if kind == "text":
        return Item(kind="text", text=f"[File {path.name}]\n{path.read_text()}")
    return Item(kind=kind, data=path.read_bytes(), mime=mime, filename=path.name)


def parse_colleagues(value: str) -> list[Colleague]:
    """'Anna:@anna_acc:Accounting, Sam:@sam_safety:Safety'"""
    out = []
    for chunk in filter(None, (c.strip() for c in value.split(","))):
        name, user, role = (chunk.split(":") + ["", ""])[:3]
        out.append(Colleague(name=name.strip(), username=user.strip().lstrip("@"), role=role.strip()))
    return out


async def run(args) -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unused")
    os.environ.setdefault("SPREADSHEET_ID", "unused")
    from . import pipeline
    from .config import load_config
    from .llm import LLM

    cfg = load_config()
    items = [Item(kind="text", text=args.caption)] if args.caption else []
    items += [item_from_file(Path(p)) for p in args.files]
    colleagues = parse_colleagues(args.colleagues)
    now = datetime.now(cfg.timezone).replace(tzinfo=None, second=0, microsecond=0)

    llm = LLM(cfg)
    try:
        analysis, provider = await llm.analyze(items, pipeline.build_context(now, colleagues, []))
    finally:
        await llm.close()
    analysis, warnings = verify(analysis, colleagues, set())

    if analysis.needs_clarification:
        print("QUESTION:", analysis.clarification_question)
    text = pipeline.saved_reply(analysis, [], [], warnings, provider)
    print(re.sub(r"</?[bi]>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    print(f"\n--- raw JSON ({provider}) ---")
    print(analysis.model_dump_json(indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="PDF / image / audio / text files")
    ap.add_argument("--caption", default="", help="message text or caption")
    ap.add_argument("--colleagues", default=os.environ.get("TRY_COLLEAGUES", ""),
                    help="'Name:@username:Role, ...'")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
