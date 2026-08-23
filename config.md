# StemForge settings

**You don't need to edit this file directly.** Use Anki's menu bar: **StemForge → Settings...**
(a new top-level menu next to Tools/Help, same as AnKing/AnkiHub/AnkiBrain add) — it gives you a
proper dialog with a text field for your license key and a dropdown for the rephrase style,
instead of raw JSON.

This file (and this raw config editor) still exists because Anki requires it, and it's a fine
fallback if you ever need it, but the dialog is the intended way to change these settings.

- **enabled** — turn the rephrasing on/off without uninstalling. When off, your decks look
  exactly as they did before installing StemForge - nothing added, hidden, or reordered. You can
  flip this from **StemForge → Enabled** (a one-click checkable menu item, fastest option) or
  from the checkbox at the top of **StemForge → Settings...** - both stay in sync.
- **license_key** — your subscription license key, emailed to you after purchase.
- **style** — which exam format to generate: `"basic"`, `"mcat"`, `"step1"`, `"step2"`,
  `"step3"`, or `"nclex"`. See the dialog for human-readable labels.
- **timeout_seconds** — how long to wait for a response before giving up on that card's rephrase.

There is no `server_url` field here anymore - the backend address is fixed by the add-on
developer and isn't something you need to configure.

## The MCQ answer

For every style except `basic`, the correct answer to the rephrased MCQ is shown when you flip
the card to the answer side - directly above the original answer, not hidden behind a toggle.
This is generated at the same time as the question (so no extra wait), but if you flip very
quickly you might occasionally see a "wasn't ready in time" note instead - just flip back and
forward again a moment later.
