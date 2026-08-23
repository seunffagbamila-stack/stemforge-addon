# StemForge — subscription version

Converts each question into an exam-style rephrase before you see the original card - a fresh
generation on every single review, served by a subscription backend (not your own API key). Six
selectable styles, from a plain reword up to full clinical-vignette/scenario formats calibrated to
MCAT, Step 1, Step 2 CK, Step 3, and NCLEX-RN. The rephrase appears first; the original card is
collapsed behind a "Show original card" toggle so it isn't immediately visible. For every MCQ
style, the correct answer is shown above the original answer once you flip the card - no toggle,
it's just there.

## What it does and doesn't do

- **Does**: shows a reworded/vignette-style version of the question above the original card, a
  couple seconds after the card loads (background call to the subscription server). The original
  card is collapsed by default, so you're not looking straight at the memorized wording right away.
  On the answer side, the rephrased MCQ's correct answer appears directly above the original
  answer (for every style except `basic`, which has no MCQ/answer to show).
- **Doesn't**: physically prevent you from clicking "Show original card" whenever you want. This
  raises the friction to see the original, it doesn't remove the option - you still have to choose
  to actually engage with the rephrase instead of clicking through.
- Only generates once per review of the question side; the answer's MCQ reveal reuses that same
  generation rather than making a second call.
- Runs in the background (a separate thread), so Anki's interface won't freeze while waiting on
  a response.

## The six styles

Set via the `style` config field:

| Style | Format | Focus | Has MCQ answer? |
|---|---|---|---|
| `basic` | Plain reword, no options | Works on any subject, not just medical/nursing content | No |
| `mcat` | 4 options (A-D) | Foundational science reasoning, not clinical management | Yes |
| `step1` | 5 options (A-E) | Mechanism/pathophysiology/most-likely-diagnosis, first encounter | Yes |
| `step2` | 5 options (A-E) | "Next step in management" - patient already being evaluated | Yes |
| `step3` | 5 options (A-E) | Longitudinal/outpatient/preventive-care, follow-up framing | Yes |
| `nclex` | 4 options (A-D) | Nursing action/prioritization/clinical judgment, not physician decisions | Yes |

This is one global setting - it applies to every card you review until you change it again, not
a per-card or per-deck toggle.

**Note on subject scope**: the five exam styles (all but `basic`) are calibrated for medical,
pre-med, or nursing content. If you point this at a non-medical deck (language learning,
geography, etc.) with anything other than `basic`, expect nonsensical output - it will still try
to force a clinical/scientific frame onto unrelated content rather than skip or adapt automatically.

## Installation (end users)

1. In Anki: **Tools → Add-ons → View Files**. This opens your `addons21` folder.
2. Copy the whole `stemforge` folder (this one) into that `addons21` folder.
3. Restart Anki.
4. **StemForge → Settings...** (a new top-level menu, next to Tools/Help) — a dialog opens
   with just two things to fill in:
   - **License key** (emailed to you after subscribing)
   - **Rephrase style** (a dropdown: Basic, MCAT, USMLE Step 1/2/3, or NCLEX-RN)
5. Click **Save**.
6. Start reviewing - the rephrase appears first; click "Show original card" to reveal the real
   card whenever you're ready. For MCQ styles, the correct answer appears above the original
   answer once you flip the card.

No server URL, no Stripe, nothing technical - that's all handled for you.

## Before you distribute this (developer setup)

Open `__init__.py` and set the `SERVER_URL` constant near the top to your actual deployed backend
address, e.g.:

```python
SERVER_URL = "https://rephrase-backend-production.up.railway.app"
```

This is baked into the code, not something users configure - they never see or edit it. Do this
once before zipping up the add-on for distribution, and again any time your backend's URL
changes (which would need a version bump + the update-notification flow, same as any other
add-on-side code change).

## Staying up to date

This add-on is distributed outside AnkiWeb, so it doesn't get Anki's automatic add-on updates.
Instead, it checks the subscription server once per Anki startup and shows a one-time,
non-blocking notification if a newer version is available, with a download link. It will not
auto-install updates for you - you'll still need to manually download and replace the folder when
notified. Most improvements (prompt quality, new styles, cache behavior, pricing/tier changes)
happen entirely on the server side and reach you automatically with no update needed at all - the
manual-update path is only for changes to the add-on's own code.

## If something goes wrong

- No rephrase appears at all → check `enabled: true` (via the raw config editor, Tools > Add-ons
  > Config) and that your license key is filled in via **StemForge → Settings...**.
- Rephrase shows an error message inline (e.g. "License key invalid or subscription inactive",
  "Daily limit reached...") → the error text tells you why. The original card (behind the toggle)
  is unaffected either way.
- Anki feels slow → this shouldn't happen, since the network call runs on a background thread,
  but if it does, disable the add-on (config → `enabled: false`) and report what you're seeing.
