"""
Anki add-on: StemForge - subscription version.

Talks to YOUR backend server (not Anthropic directly). The backend holds the real API key,
checks the user's license/subscription, and returns a vignette - possibly served from a shared
cache if someone else already generated this exact card recently.
"""

import html
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from collections import OrderedDict
from datetime import datetime

from aqt import mw, gui_hooks
from aqt.qt import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QComboBox, QPushButton,
    QLabel, QHBoxLayout, QAction, QCheckBox, QApplication, Qt, QMessageBox,
)
from aqt.utils import tooltip, showInfo

ADDON_NAME = __name__
ADDON_VERSION = "1.0.1"

# Set this once before you package and distribute the add-on. Not user-editable - end users
# only ever see the license key and style, never this URL or anything Stripe-related.
SERVER_URL = "https://web-production-ef1b9.up.railway.app"

STYLE_LABELS = {
    "basic": "Basic (plain reword, any subject)",
    "mcat": "MCAT",
    "step1": "USMLE Step 1",
    "step2": "USMLE Step 2 CK",
    "step3": "USMLE Step 3",
    "nclex": "NCLEX-RN (nursing)",
}
STYLE_ORDER = ["basic", "mcat", "step1", "step2", "step3", "nclex"]

# Anki preserves the "user_files" folder across add-on updates/reinstalls, so this is the
# right place to remember "have I already told this user about version X" persistently.
ADDON_DIR = os.path.dirname(__file__)
USER_FILES_DIR = os.path.join(ADDON_DIR, "user_files")
UPDATE_STATE_PATH = os.path.join(USER_FILES_DIR, "update_state.json")

# In-memory only (not persisted) - remembers the generated answer (and, for MCQ styles, the
# per-distractor "why it's wrong" notes) for a card between showing the question and flipping
# to the answer, so we can display it without a second API call.
# Bounded so a very long study session doesn't grow this unboundedly.
_MAX_REMEMBERED_ANSWERS = 500
CARD_ANSWERS: "OrderedDict[int, dict]" = OrderedDict()

# Same idea, but for the rephrased vignette text itself - lets us keep showing it on the answer
# side (so you can refer back to it while reading the answer) instead of it disappearing once
# you flip the card.
CARD_VIGNETTES: "OrderedDict[int, dict]" = OrderedDict()

# Images are known synchronously (straight from the note, no API call needed), unlike the
# vignette text/rating which arrive later via the async backend call - kept in a separate small
# cache so the answer-side redisplay can reattach them without waiting on anything.
_MAX_REMEMBERED_IMAGES = 500
CARD_IMAGES: "OrderedDict[int, list]" = OrderedDict()


def _remember_images(card_id: int, images: list):
    CARD_IMAGES[card_id] = images
    CARD_IMAGES.move_to_end(card_id)
    while len(CARD_IMAGES) > _MAX_REMEMBERED_IMAGES:
        CARD_IMAGES.popitem(last=False)


def _remember_answer(card_id: int, answer: str, distractor_notes: dict | None):
    CARD_ANSWERS[card_id] = {"answer": answer, "distractor_notes": distractor_notes}
    CARD_ANSWERS.move_to_end(card_id)
    while len(CARD_ANSWERS) > _MAX_REMEMBERED_ANSWERS:
        CARD_ANSWERS.popitem(last=False)


def _remember_vignette(card_id: int, vignette: str, content_hash: str | None, variant_index: int | None,
                        avg_user_rating: float | None, user_rating_count: int):
    CARD_VIGNETTES[card_id] = {
        "vignette": vignette,
        "content_hash": content_hash,
        "variant_index": variant_index,
        "avg_user_rating": avg_user_rating,
        "user_rating_count": user_rating_count,
    }
    CARD_VIGNETTES.move_to_end(card_id)
    while len(CARD_VIGNETTES) > _MAX_REMEMBERED_ANSWERS:
        CARD_VIGNETTES.popitem(last=False)

DEFAULT_CONFIG = {
    "enabled": True,
    "license_key": "",
    "remember_license": True,
    "style": "step1",
    "timeout_seconds": 20,
}

# Must match STYLES_WITH_ANSWER in the backend's main.py - "basic" has no MCQ/answer concept,
# every other style does.
STYLES_WITH_ANSWER = {"mcat", "step1", "step2", "step3", "nclex"}


def get_config() -> dict:
    cfg = mw.addonManager.getConfig(ADDON_NAME) or {}
    return {**DEFAULT_CONFIG, **cfg}


# In-memory only - holds the license key for this Anki session when "Remember my license key"
# is unchecked, so unchecking it doesn't break the current session, it just means the key isn't
# written to disk and has to be re-entered next time Anki opens.
_SESSION_LICENSE_KEY = None


def effective_license_key(cfg: dict) -> str:
    if not cfg.get("remember_license", True) and _SESSION_LICENSE_KEY:
        return _SESSION_LICENSE_KEY
    return cfg.get("license_key", "").strip()


def _version_tuple(v: str):
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except (ValueError, AttributeError):
        return (0,)


def _read_update_state() -> dict:
    if not os.path.exists(UPDATE_STATE_PATH):
        return {}
    try:
        with open(UPDATE_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_update_state(state: dict):
    os.makedirs(USER_FILES_DIR, exist_ok=True)
    try:
        with open(UPDATE_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def check_for_update():
    """Runs in a background thread on profile open. Never blocks Anki startup."""
    if "your-backend-domain" in SERVER_URL:
        return

    try:
        req = urllib.request.Request(f"{SERVER_URL}/addon-version", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return  # silent failure - an update check should never bother the user with errors

    latest = data.get("version", "")
    download_url = data.get("download_url", "")
    if not latest or _version_tuple(latest) <= _version_tuple(ADDON_VERSION):
        return

    state = _read_update_state()
    if state.get("last_notified_version") == latest:
        return  # already told the user about this version - don't nag every startup

    def notify():
        msg = QMessageBox(mw)
        msg.setWindowTitle("StemForge update available")
        msg.setText(
            f"A new version of StemForge ({latest}) is available.\n\n"
            "Click Download to open the download page in your browser. Once it's downloaded, "
            "double-click the .ankiaddon file (or use Tools \u2192 Add-ons \u2192 Install from file) "
            "and Anki will update StemForge in place - your license key and settings are kept."
        )
        try:
            accept_role = QMessageBox.ButtonRole.AcceptRole
            reject_role = QMessageBox.ButtonRole.RejectRole
        except AttributeError:
            accept_role = QMessageBox.AcceptRole
            reject_role = QMessageBox.RejectRole
        download_btn = msg.addButton("Download", accept_role)
        msg.addButton("Later", reject_role)
        msg.exec()
        if msg.clickedButton() == download_btn:
            webbrowser.open(download_url)

    mw.taskman.run_on_main(notify)
    state["last_notified_version"] = latest
    _write_update_state(state)


def strip_html_to_text(raw_html: str) -> str:
    text = re.sub(r"<style.*?</style>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


CLOZE_PATTERN = re.compile(r"\{\{c\d+::(.*?)(?:::.*?)?\}\}", re.DOTALL)


IMG_TAG_PATTERN = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


def extract_images(card) -> list[str]:
    """Returns a list of media filenames (e.g. 'eye_dendritic.jpg') that actually appear on the
    QUESTION side of the card - not just anywhere on the note. This matters a lot for note types
    like AnKing's Cloze cards, where mnemonic images commonly live in an "Extra" field that only
    ever renders on the answer side: scanning every note field (as this used to do) pulled those
    answer-only images onto the rephrased question, visually leaking the answer before the
    student had even guessed. card.question() is Anki's own rendering of what belongs on the
    front - using it instead of guessing via raw field iteration is the correct fix, and it
    still naturally includes any image that's embedded directly in a field shown on both sides
    (e.g. a lab photo sitting next to a cloze deletion in the same field).
    Anki's card webview already resolves bare filenames like this relative to the collection's
    media folder, so these can be dropped straight into an <img src="..."> tag elsewhere in the
    same webview without needing to resolve a full path ourselves."""
    try:
        return IMG_TAG_PATTERN.findall(card.question())
    except Exception:
        return []


def extract_question_content(card) -> tuple[str, bool]:
    """Returns (full_card_text, is_cloze). Reads EVERY field on the note, labeled by field name
    and in original field order - not just the question side, and not flattened into one blob -
    so the model has the complete underlying fact to work with while still being able to tell
    what the card actually ASKS (conventionally the first field) apart from supporting context
    in later fields (Back, Extra, etc.). A flat unlabeled join was letting the model pick a
    different fact from within the combined text than what the original front-to-back pairing
    specifically tested - labeling fields, combined with an explicit scope instruction in the
    backend prompt, keeps the rephrase matched to the same scope as the original card.
    - Basic-type notes: this naturally separates Front from Back (+ any Extra fields).
    - Cloze notes: card.question() only contains the BLANKED text (Anki hides the answer with
      "[...]" on purpose), which left the model with no idea what fact was actually being
      tested - it would just mimic the fill-in-the-blank shape back. Reading the raw field text
      (which still has the {{c1::answer}} markup) and revealing every deletion fixes that.
    CLOZE_PATTERN only matches actual {{cN::...}} syntax, so revealing it is a harmless no-op
    on non-cloze fields - one code path handles both cases."""
    try:
        note = card.note()
        note_type = card.note_type() or {}
        is_cloze = note_type.get("type") == 1  # 0 = standard note type, 1 = cloze

        sections = []
        for field_name, value in note.items():
            revealed = CLOZE_PATTERN.sub(r"\1", value)
            clean = strip_html_to_text(revealed)
            if clean:
                sections.append(f"{field_name}: {clean}")
        full_text = "\n".join(sections)
        if full_text:
            return full_text, is_cloze
    except Exception:
        pass

    # Fall back to just the question side rather than failing outright.
    try:
        return strip_html_to_text(card.question()), False
    except Exception:
        return "", False


def _set_wait_cursor():
    try:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    except AttributeError:
        QApplication.setOverrideCursor(Qt.WaitCursor)


def _post_json(path: str, body: dict, timeout: int = 20) -> dict:
    """Blocking POST helper for the sign-in flow, used directly in a modal dialog's button
    handlers (a brief UI freeze during a deliberate, user-initiated click is an acceptable
    trade-off here for the simplicity of not managing cross-thread widget updates from a
    dialog that might close mid-request)."""
    req = urllib.request.Request(
        f"{SERVER_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            pass
        raise RuntimeError(detail or f"Server error ({e.code})")
    except urllib.error.URLError:
        raise RuntimeError("Could not reach the server - check your network connection.")


def request_login_code(email: str):
    return _post_json("/auth/request-code", {"email": email})


def verify_login_code(email: str, code: str) -> str:
    """Returns the license key on success, raises RuntimeError otherwise."""
    data = _post_json("/auth/verify-code", {"email": email, "code": code})
    return data["license_key"]


def call_backend(question_text: str, cfg: dict, is_cloze_derived: bool = False, has_images: bool = False):
    license_key = effective_license_key(cfg)

    if not license_key:
        raise RuntimeError("No license key set. StemForge > Settings to add one.")
    if "your-backend-domain" in SERVER_URL:
        raise RuntimeError("Server URL not configured by the add-on developer yet.")

    body = {
        "license_key": license_key,
        "question_text": question_text,
        "style": cfg.get("style", "step1"),
        "is_cloze_derived": is_cloze_derived,
        "has_images": has_images,
    }
    req = urllib.request.Request(
        f"{SERVER_URL}/rephrase",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )

    timeout = cfg.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            pass
        if e.code == 403:
            raise RuntimeError("License key invalid or subscription inactive.")
        if e.code == 429:
            raise RuntimeError(detail or "Daily limit reached for your plan.")
        raise RuntimeError(f"Server error ({e.code}): {detail}")
    except urllib.error.URLError:
        raise RuntimeError("Could not reach the rephrase server - check your network connection.")

    return (
        data["vignette"],
        data.get("answer"),
        data.get("distractor_notes"),
        data["content_hash"],
        data["variant_index"],
        data.get("avg_user_rating"),
        data.get("user_rating_count", 0),
        data.get("cached", False),
    )


# In-memory only - throttles heartbeat pings to at most once per local calendar day per Anki
# session. Worst case (Anki restarted mid-day) is one harmless extra ping, not a real problem.
_LAST_HEARTBEAT_LOCAL_DATE = None
CURRENT_STREAK = None  # last streak value the backend reported, if any


def send_heartbeat(cfg: dict):
    """Best-effort ping telling the backend 'this subscriber reviewed today', which is what
    streaks and the streak-protective reminder email are built on. Must never interrupt or
    slow down review - all failures are swallowed silently."""
    global CURRENT_STREAK
    license_key = effective_license_key(cfg)
    if not license_key or "your-backend-domain" in SERVER_URL:
        return

    local_now = datetime.now().astimezone()
    offset = local_now.utcoffset()
    body = {
        "license_key": license_key,
        "local_date": local_now.strftime("%Y-%m-%d"),
        "timezone_offset_minutes": int(offset.total_seconds() // 60) if offset else 0,
    }
    try:
        body["cards_due"] = sum(mw.col.sched.counts())
    except Exception:
        pass  # Not critical - the reminder email just falls back to generic wording.

    req = urllib.request.Request(
        f"{SERVER_URL}/heartbeat",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"])) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            CURRENT_STREAK = data.get("streak")
    except Exception:
        pass  # Telemetry only - never surface this failure to the user.


def maybe_send_heartbeat(cfg: dict):
    global _LAST_HEARTBEAT_LOCAL_DATE
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    if _LAST_HEARTBEAT_LOCAL_DATE == today:
        return
    _LAST_HEARTBEAT_LOCAL_DATE = today  # set before the thread finishes to prevent duplicates
    threading.Thread(target=send_heartbeat, args=(cfg,), daemon=True).start()


def send_rating_and_update_ui(rating_id: str, content_hash: str, variant_index: int, rating: int, comment: str):
    """Runs in a background thread (see the pycmd handler below). POSTs the vote (and optional
    comment), then swaps the clicked star widget for a confirmation message."""
    cfg = get_config()
    license_key = effective_license_key(cfg)
    if not license_key or "your-backend-domain" in SERVER_URL:
        return

    body = {
        "license_key": license_key,
        "content_hash": content_hash,
        "variant_index": variant_index,
        "rating": rating,
        "comment": comment or None,
    }
    req = urllib.request.Request(
        f"{SERVER_URL}/rate-variant",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"])) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return  # best-effort - a failed vote just silently doesn't register, no error shown

    def update_ui():
        if mw.state != "review" or not mw.reviewer or not mw.reviewer.web:
            return
        avg = data.get("avg_user_rating")
        count = data.get("user_rating_count", 0)
        base = f"Thanks! Community average: {avg:.1f}/5 ({count} votes)" if avg else "Thanks for rating!"
        comment_note = {
            "auto_actioned": " Your note helped fix this one - a new version will show next time.",
            "flagged_for_admin": " Your note was passed along for review.",
        }.get(data.get("comment_status"), "")
        confirmation = base + comment_note
        js = f'''
        (function() {{
            var el = document.getElementById({json.dumps(rating_id)});
            if (el) {{
                el.innerHTML = '<span style="font-size:0.85em;color:#888;">' + {json.dumps(confirmation)} + '</span>';
            }}
        }})();
        '''
        mw.reviewer.web.eval(js)

    mw.taskman.run_on_main(update_ui)


def on_webview_js_message(handled, message, context):
    if not isinstance(message, str) or not message.startswith("stemforge_rate:"):
        return handled  # not ours - let Anki's normal handling continue
    try:
        payload = json.loads(message[len("stemforge_rate:"):])
        rating_id = str(payload["rating_id"])
        content_hash = str(payload["content_hash"])
        variant_index = int(payload["variant_index"])
        rating = int(payload["rating"])
        comment = str(payload.get("comment") or "").strip()[:1000]  # sane length cap
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return (True, None)  # malformed message - swallow it rather than erroring in the reviewer

    threading.Thread(
        target=send_rating_and_update_ui,
        args=(rating_id, content_hash, variant_index, rating, comment),
        daemon=True,
    ).start()
    return (True, None)


def build_star_widget_html(rating_id: str, content_hash: str, variant_index: int,
                            avg_user_rating: float | None, user_rating_count: int) -> str:
    """5 clickable stars, plus an optional comment box, that vote on this specific variant's
    realism via pycmd. Clicking a star submits the rating together with whatever's currently
    typed in the comment box (empty is fine - the comment is optional). The vote payload is
    JSON-encoded (not colon-delimited) specifically so free-text comments - which can contain
    colons, quotes, anything - pass through safely without breaking the message format."""
    summary = ""
    if user_rating_count:
        summary = f' <span style="color:#888;">({avg_user_rating:.1f}/5, {user_rating_count} votes)</span>'

    comment_id = f"{rating_id}-comment"
    # rating_id/content_hash/comment_id are always plain hex/alnum-dash strings we generated
    # ourselves (see hash_question in the backend, and the rating_id patterns above) - never
    # user input - so it's safe to wrap them in single quotes directly for the JS literal.
    # json.dumps() would have been the usual safe choice, but it always produces DOUBLE-quoted
    # JS strings, and this is embedded inside an onclick="..." HTML attribute that's ALSO
    # double-quoted - the first embedded double-quote silently truncated the whole attribute,
    # which is why clicking a star did nothing at all.
    stars = "".join(
        f'<span data-star="{n}" '
        f'style="cursor:pointer;font-size:1.1em;color:#c9a227;user-select:none;" '
        f'title="Rate this rephrase {n}/5 for exam realism" '
        f"onclick=\"pycmd('stemforge_rate:' + JSON.stringify({{"
        f"rating_id: '{rating_id}', content_hash: '{content_hash}', "
        f"variant_index: {variant_index}, rating: {n}, "
        f"comment: document.getElementById('{comment_id}').value"
        f"}}))\">"
        f"&#9733;</span>"
        for n in range(1, 6)
    )
    return (
        f'<div style="font-size:0.85em;color:#888;">Realistic? '
        f'<span id="{rating_id}-stars">{stars}</span>'
        f"{summary}</div>"
        f'<input id="{comment_id}" type="text" maxlength="1000" placeholder="Optional: what would make this more accurate?" '
        f'style="margin-top:4px;width:100%;box-sizing:border-box;font-size:0.8em;padding:3px 6px;'
        f'border:1px solid #ccc;border-radius:4px;background:transparent;color:inherit;" />'
    )


def fetch_and_inject(question_plain: str, div_id: str, cfg: dict, card_id: int,
                      is_cloze_derived: bool = False, has_images: bool = False):
    try:
        (vignette, answer, distractor_notes, content_hash, variant_index,
         avg_user_rating, user_rating_count, cached) = call_backend(
            question_plain, cfg, is_cloze_derived, has_images
        )
        succeeded = True
    except Exception as e:
        vignette, answer, distractor_notes = f"(Rephrase failed: {e})", None, None
        content_hash, variant_index, avg_user_rating, user_rating_count, cached = None, None, None, 0, False
        succeeded = False

    if answer:
        _remember_answer(card_id, answer, distractor_notes)
    if succeeded:
        # content_hash/variant_index are kept here so the star-rating widget can be built on the
        # answer side (see on_reviewer_will_show_answer) instead of the question side - voting
        # on realism makes more sense after seeing the full picture, not before.
        _remember_vignette(card_id, vignette, content_hash, variant_index, avg_user_rating, user_rating_count)

    def update_ui():
        if mw.state != "review" or not mw.reviewer or not mw.reviewer.web:
            return
        js = f'''
        (function() {{
            var el = document.getElementById({json.dumps(div_id)});
            if (el) {{ el.innerText = {json.dumps(vignette)}; }}
        }})();
        '''
        mw.reviewer.web.eval(js)

    mw.taskman.run_on_main(update_ui)


def on_reviewer_will_show_question(text: str, card, kind: str) -> str:
    cfg = get_config()
    if not cfg.get("enabled", True):
        return text
    if kind != "reviewQuestion":
        return text

    maybe_send_heartbeat(cfg)

    try:
        question_plain, is_cloze_derived = extract_question_content(card)
    except Exception:
        return text

    if not question_plain:
        return text

    # Images (e.g. a clinical photo, an ECG, a fluorescein-stained eye) often carry the actual
    # diagnostic finding - stripping them out during text extraction was leaving the rephrased
    # question unanswerable, since the model had no idea a visual finding even existed. We can't
    # currently have the model actually see the image, but we can (a) tell it one exists so it
    # phrases the question to reference it instead of pretending everything is describable in
    # text, and (b) show the real image right alongside the rephrased text so the student isn't
    # missing anything.
    images = extract_images(card)
    _remember_images(card.id, images)
    images_html = "".join(
        f'<img src="{fname}" style="max-width:100%;margin-top:10px;border-radius:4px;">'
        for fname in images
    )

    style = cfg.get("style", "step1")
    placeholder_map = {
        "basic": "Generating rephrase&hellip;",
        "mcat": "Generating MCAT-style question&hellip;",
        "step1": "Generating Step 1-style vignette&hellip;",
        "step2": "Generating Step 2 CK-style vignette&hellip;",
        "step3": "Generating Step 3-style vignette&hellip;",
        "nclex": "Generating NCLEX-style question&hellip;",
    }
    placeholder = placeholder_map.get(style, "Generating rephrase&hellip;")
    div_id = f"live-rephrase-{card.id}-{int(time.time() * 1000)}"
    vignette_block = (
        f'<div style="margin-bottom:20px;padding:16px;border:1px solid #999;border-radius:6px;">'
        f'<div id="{div_id}" style="white-space:pre-wrap;line-height:1.5;">{placeholder}</div>'
        f"{images_html}"
        f"</div>"
    )

    collapsed_original = (
        f'<details style="margin-top:4px;">'
        f'<summary style="cursor:pointer;color:#888;font-size:0.85em;">Show original card</summary>'
        f'<div style="margin-top:12px;">{text}</div>'
        f"</details>"
    )

    threading.Thread(
        target=fetch_and_inject,
        args=(question_plain, div_id, cfg, card.id, is_cloze_derived, bool(images)),
        daemon=True,
    ).start()

    return vignette_block + collapsed_original


def on_reviewer_will_show_answer(text: str, card, kind: str) -> str:
    # The rephrased MCQ answer overlay stays off (see above) - the original card's own answer
    # already covers that. But the rephrased *question* is worth keeping visible here too,
    # since otherwise it disappears the moment you flip the card and you lose the context you
    # were just reading. This reads from an in-memory cache populated on the question side, so
    # it costs no extra API call.
    cfg = get_config()
    if not cfg.get("enabled", True):
        return text
    if kind != "reviewAnswer":
        return text

    remembered = CARD_VIGNETTES.get(card.id)
    if not remembered:
        return text  # generation hadn't finished, or this card was never seen on the front side

    rating_html = ""
    if remembered.get("content_hash") is not None:
        rating_id = f"stemforge-rating-{card.id}-{int(time.time() * 1000)}"
        rating_html = (
            f'<div id="{rating_id}" style="margin-top:10px;">'
            + build_star_widget_html(
                rating_id, remembered["content_hash"], remembered["variant_index"],
                remembered.get("avg_user_rating"), remembered.get("user_rating_count", 0),
            )
            + "</div>"
        )
    images = CARD_IMAGES.get(card.id, [])
    images_html = "".join(
        f'<img src="{fname}" style="max-width:100%;margin-top:10px;border-radius:4px;">'
        for fname in images
    )
    vignette_block = (
        f'<div style="margin-bottom:20px;padding:16px;border:1px solid #999;border-radius:6px;">'
        f'<div style="font-size:0.75em;color:#888;margin-bottom:6px;">Rephrased question</div>'
        f'<div style="white-space:pre-wrap;line-height:1.5;">{html.escape(remembered["vignette"])}</div>'
        f"{images_html}"
        f"{rating_html}"
        f"</div>"
    )

    return vignette_block + text


def on_profile_did_open():
    threading.Thread(target=check_for_update, daemon=True).start()


class StemForgeSettingsDialog(QDialog):
    """Reachable from StemForge > Settings. The only UI a user needs - just their
    license key and rephrase style. No server URL, no Stripe, nothing developer-facing."""

    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("StemForge Settings")
        self.setMinimumWidth(420)

        cfg = get_config()

        layout = QVBoxLayout(self)

        self.enabled_checkbox = QCheckBox("Enable StemForge", self)
        self.enabled_checkbox.setChecked(cfg.get("enabled", True))
        layout.addWidget(self.enabled_checkbox)

        form = QFormLayout()

        self.license_input = QLineEdit(self)
        self.license_input.setText(_SESSION_LICENSE_KEY or cfg.get("license_key", ""))
        self.license_input.setPlaceholderText("lr_...")
        form.addRow("License key:", self.license_input)

        self.remember_checkbox = QCheckBox("Remember my license key", self)
        self.remember_checkbox.setChecked(cfg.get("remember_license", True))
        self.remember_checkbox.setToolTip(
            "Checked (default): saved to disk, so you won't have to re-enter it next time "
            "you open Anki. Unchecked: only kept for this session - you'll need to paste it "
            "in again next time Anki starts."
        )
        form.addRow("", self.remember_checkbox)

        self.style_combo = QComboBox(self)
        for key in STYLE_ORDER:
            self.style_combo.addItem(STYLE_LABELS[key], userData=key)
        current_style = cfg.get("style", "step1")
        idx = self.style_combo.findData(current_style)
        if idx >= 0:
            self.style_combo.setCurrentIndex(idx)
        form.addRow("Rephrase style:", self.style_combo)

        layout.addLayout(form)

        signin_label = QLabel("Or sign in with email instead of pasting a key:")
        signin_label.setStyleSheet("color: #888; font-size: 0.85em; margin-top: 6px;")
        layout.addWidget(signin_label)

        email_row = QHBoxLayout()
        self.email_input = QLineEdit(self)
        self.email_input.setPlaceholderText("you@example.com")
        self.send_code_btn = QPushButton("Send code")
        self.send_code_btn.clicked.connect(self.on_send_code)
        email_row.addWidget(self.email_input)
        email_row.addWidget(self.send_code_btn)
        layout.addLayout(email_row)

        code_row = QHBoxLayout()
        self.code_input = QLineEdit(self)
        self.code_input.setPlaceholderText("6-digit code")
        self.code_input.setMaxLength(6)
        self.verify_code_btn = QPushButton("Verify")
        self.verify_code_btn.clicked.connect(self.on_verify_code)
        code_row.addWidget(self.code_input)
        code_row.addWidget(self.verify_code_btn)
        layout.addLayout(code_row)

        self.signin_status = QLabel("")
        self.signin_status.setWordWrap(True)
        self.signin_status.setStyleSheet("color: #d4a017; font-size: 0.85em;")
        layout.addWidget(self.signin_status)

        note = QLabel(
            "The style applies to every card you review from now on - it's not a per-card or "
            "per-deck setting. When disabled, your decks look exactly as they did before "
            "installing StemForge - nothing is added, hidden, or reordered."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 0.85em;")
        layout.addWidget(note)

        button_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.on_save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addStretch()
        button_row.addWidget(cancel_btn)
        button_row.addWidget(save_btn)
        layout.addLayout(button_row)

    def on_send_code(self):
        email = self.email_input.text().strip()
        if not email or "@" not in email:
            self.signin_status.setText("Enter a valid email first.")
            return
        self.send_code_btn.setEnabled(False)
        self.signin_status.setText("Sending...")
        _set_wait_cursor()
        try:
            request_login_code(email)
            self.signin_status.setText("Code sent - check your email, then enter it below.")
        except Exception as e:
            self.signin_status.setText(f"Couldn't send code: {e}")
        finally:
            QApplication.restoreOverrideCursor()
            self.send_code_btn.setEnabled(True)

    def on_verify_code(self):
        email = self.email_input.text().strip()
        code = self.code_input.text().strip()
        if not code:
            self.signin_status.setText("Enter the code from your email first.")
            return
        self.verify_code_btn.setEnabled(False)
        self.signin_status.setText("Verifying...")
        _set_wait_cursor()
        try:
            license_key = verify_login_code(email, code)
            self.license_input.setText(license_key)
            self.signin_status.setText("Signed in - license key filled in below. Click Save to finish.")
        except Exception as e:
            self.signin_status.setText(f"Couldn't verify: {e}")
        finally:
            QApplication.restoreOverrideCursor()
            self.verify_code_btn.setEnabled(True)

    def on_save(self):
        global _SESSION_LICENSE_KEY
        cfg = get_config()
        cfg["enabled"] = self.enabled_checkbox.isChecked()
        cfg["style"] = self.style_combo.currentData()

        entered_key = self.license_input.text().strip()
        cfg["remember_license"] = self.remember_checkbox.isChecked()
        if cfg["remember_license"]:
            cfg["license_key"] = entered_key
            _SESSION_LICENSE_KEY = None  # no longer needed - it's on disk now
        else:
            cfg["license_key"] = ""  # don't write the key to disk at all
            _SESSION_LICENSE_KEY = entered_key  # keeps working for the rest of this session

        mw.addonManager.writeConfig(ADDON_NAME, cfg)
        if _ENABLED_MENU_ACTION is not None:
            _ENABLED_MENU_ACTION.setChecked(cfg["enabled"])
            _ENABLED_MENU_ACTION.setText(_enabled_label(cfg["enabled"]))
        showInfo("StemForge settings saved.")
        self.accept()


def open_settings_dialog():
    dialog = StemForgeSettingsDialog()
    dialog.exec()


_ENABLED_MENU_ACTION = None  # set in setup_menu; kept in sync with the settings dialog checkbox


def _enabled_label(checked: bool) -> str:
    return "StemForge Enabled" if checked else "Enable StemForge?"


def toggle_enabled(checked: bool):
    cfg = get_config()
    cfg["enabled"] = checked
    mw.addonManager.writeConfig(ADDON_NAME, cfg)
    if _ENABLED_MENU_ACTION is not None:
        _ENABLED_MENU_ACTION.setText(_enabled_label(checked))
    tooltip("StemForge enabled" if checked else "StemForge disabled - decks shown as before")


def setup_menu():
    global _ENABLED_MENU_ACTION
    menu = mw.form.menubar.addMenu("StemForge")

    initial_enabled = get_config().get("enabled", True)
    toggle_action = QAction(_enabled_label(initial_enabled), mw, checkable=True)
    toggle_action.setChecked(initial_enabled)
    try:
        toggle_action.setMenuRole(QAction.MenuRole.NoRole)
    except AttributeError:
        toggle_action.setMenuRole(QAction.NoRole)
    toggle_action.toggled.connect(toggle_enabled)
    menu.addAction(toggle_action)
    _ENABLED_MENU_ACTION = toggle_action

    settings_action = QAction("Settings...", mw)
    # macOS auto-detects action text like "Settings..."/"Preferences..." and silently moves it
    # into the application menu, even from inside a custom submenu. Explicitly disabling the
    # menu role keeps it in our own "StemForge" menu where it belongs, on every platform.
    try:
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
    except AttributeError:
        settings_action.setMenuRole(QAction.NoRole)
    settings_action.triggered.connect(open_settings_dialog)
    menu.addAction(settings_action)


gui_hooks.profile_did_open.append(on_profile_did_open)
gui_hooks.card_will_show.append(on_reviewer_will_show_question)
gui_hooks.card_will_show.append(on_reviewer_will_show_answer)
gui_hooks.main_window_did_init.append(setup_menu)
gui_hooks.webview_did_receive_js_message.append(on_webview_js_message)
