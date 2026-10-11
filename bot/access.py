"""Who may use the bot.

- Admins (developer): ADMIN_USER_IDS in .env. Can use the bot and manage access.
- Owner: set with /setowner, stored in the Settings tab. Uses the bot and
  receives the daily summaries.
- Users: added with /adduser, stored in the Settings tab.

Each person is a Telegram numeric ID and/or @username, written in the sheet as
"1024185305 @boss"; people are comma-separated. Bots can't look up an ID from a
username, so when someone added by @username writes for the first time, their
ID is saved next to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r"@?[A-Za-z0-9_]{3,32}")

Person = list[str]  # e.g. ["1024185305", "@boss"]


def parse_token(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or not _TOKEN.fullmatch(raw):
        return None
    return raw if raw.isdigit() else "@" + raw.lstrip("@").lower()


def parse_people(value: str) -> list[Person]:
    """'1024185305 @Boss, @kate' -> [['1024185305', '@boss'], ['@kate']]"""
    people: list[Person] = []
    for chunk in re.split(r"[,;\n]+", value or ""):
        person = [t for t in (parse_token(x) for x in chunk.split()) if t]
        if person:
            people.append(person)
    return people


def format_people(people: list[Person]) -> str:
    return ", ".join(" ".join(p) for p in people)


def person_matches(person: Person, user_id: int, username: str | None) -> bool:
    return str(user_id) in person or (bool(username) and f"@{username.lower()}" in person)


def find(people: list[Person], user_id: int, username: str | None) -> Person | None:
    return next((p for p in people if person_matches(p, user_id, username)), None)


def with_id(people: list[Person], user_id: int, username: str | None) -> list[Person]:
    """Return people with the user's numeric ID added to their @username entry, if missing."""
    out = []
    for p in people:
        if username and f"@{username.lower()}" in p and str(user_id) not in p:
            p = [str(user_id), *p]
        out.append(p)
    return out


def add(people: list[Person], new: list[Person]) -> list[Person]:
    out = [list(p) for p in people]
    for person in new:
        if not any(set(person) & set(p) for p in out):
            out.append(person)
    return out


def remove(people: list[Person], tokens: list[str]) -> list[Person]:
    """Drop every person who has any of the tokens (so removing by ID also drops their @username)."""
    return [p for p in people if not set(p) & set(tokens)]


def numeric_id(person: Person | None) -> int | None:
    return next((int(t) for t in person or [] if t.isdigit()), None)


@dataclass
class Access:
    admins: set[int] = field(default_factory=set)
    owner: list[Person] = field(default_factory=list)  # zero or one person
    users: list[Person] = field(default_factory=list)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def is_owner(self, user_id: int, username: str | None) -> bool:
        return find(self.owner, user_id, username) is not None

    def allowed(self, user_id: int, username: str | None) -> bool:
        return (self.is_admin(user_id) or self.is_owner(user_id, username)
                or find(self.users, user_id, username) is not None)

    def summary_chat_ids(self) -> list[int]:
        """Daily summaries go to the owner; until the owner has an ID, to the admins."""
        owner_id = numeric_id(self.owner[0]) if self.owner else None
        return [owner_id] if owner_id else sorted(self.admins)
