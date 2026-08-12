# Product Overview (Sample)

## What Michelle.ai is

Michelle.ai is a floating desktop assistant that chats, remembers useful facts about you, and can look up answers from local documents in the `docs/` folder.

## Main capabilities (demo)

- **Chat** — casual conversation and help.
- **Retrieve** — search indexed docs (policies, FAQs, notes you drop in `docs/`).
- **Action** — not built yet (tickets, email, etc. coming later).
- **Memory** — short-term chat window plus long-term facts (like your name).

## Where documents live

Put `.md` or `.txt` files in the project `docs/` folder. Restart the backend so Michelle re-indexes them. There is no separate database server — search uses SQLite full-text search on this laptop.

## Privacy (demo)

Chats and long-term facts stay in `michelle.db` on this machine and are not committed to git. Use `scripts/reset_michelle.sh` before someone else uses your checkout.
