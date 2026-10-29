"""User journeys for the UX runs: the app driven in a browser the way people use it.

    uv run python ux/journeys.py --out DIR [--port PORT] [--live]

Run it from the root of a checkout. It starts the app itself in scripted mode with
ux/ux_script.json (on PORT, or a free port when PORT is busy or not given; a few
journeys need a server of their own and take free ports), walks every journey, and
writes one screenshot per step to DIR with:

- DIR/steps.json: each step's journey, action, a note when the action failed, the
  status pill, the end of the visible text, and page errors;
- DIR/captions.json: a caption per screenshot, for the contact sheets
  (`ux_sheet.py DIR --captions DIR/captions.json`);
- DIR/server-*.log: the server logs.

--live walks the live journey instead, against the real model: the checkout needs
the credentials (.env). It asks questions, interrupts a long story by text and by
voice (the fake microphone plays ux/barge_in.wav), reloads in the middle of a turn,
sets a location and asks "near here" in English and in Japanese, and asks for
several cards in one turn. Run it twice, each into its own DIR; a step that fails in
one of the two runs is a bug.

A failed action is part of the experience, not an error: it is recorded with its
reason and the journey goes on. Judge the screenshots against spec/ and ux/rubric.md.
"""

import argparse
import contextlib
import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request

from playwright.sync_api import Page, sync_playwright

ROOT = pathlib.Path.cwd()
UX_SCRIPT = pathlib.Path(__file__).resolve().parent / "ux_script.json"
# A visitor in Japan with an English browser.
CONTEXT = {"permissions": ["microphone"], "timezone_id": "Asia/Tokyo", "locale": "en-US"}
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}
T = 15_000  # ms to wait for a scripted reply

STATUS_JS = """() => { const s = document.querySelector('[data-testid=status]');
  return s ? `${s.getAttribute('data-state') || ''} "${s.innerText.trim()}"` : ''; }"""
WATCH_JS = """() => {
  const el = document.querySelector('[data-testid="status"]');
  window.__lastChange = Date.now();
  if (el && !window.__uxObserver) {
    window.__uxObserver = new MutationObserver(() => { window.__lastChange = Date.now(); });
    window.__uxObserver.observe(el, {attributes: true, attributeFilter: ['data-state']});
  }
}"""
SETTLED_JS = """() => { const el = document.querySelector('[data-testid="status"]');
  return el && el.getAttribute('data-state') === 'ready' && Date.now() - window.__lastChange > 1500; }"""

# Live prompts: no script answers them.
LIVE_ASKS = (
    "Find coffee shops near Okayama Station.",
    "What's the weather in Kagoshima right now?",
    "Tell me something interesting about Kanazawa in two sentences.",
)
LIVE_LONG = "Tell me a long story about a lighthouse keeper, at least two minutes."
LIVE_BARGE = "Sorry to interrupt: what time is it in Tokyo?"
LIVE_HERE = "Find a sushi place near here."
LIVE_JA = ("金沢の天気を教えて", "この近くのカフェを探して")
# What the fake microphone says when the mic is tapped (Chromium plays this file once).
LIVE_VOICE = pathlib.Path(__file__).with_name("barge_in.wav")
LIVE_VOICE_TEXT = "Sorry, stop. What time is it in Tokyo?"
LIVE_RELOAD = "What are three things to do in Hakodate?"
LIVE_AFTER_RELOAD = "Which of those is best on a rainy day?"
LIVE_MULTI = "Plan my evening here: the weather tonight, a seafood place nearby, and how to walk there from the station."


# --- the app ------------------------------------------------------------------------


def free_port(preferred: int | None = None, exact: bool = False) -> int:
    """preferred when it is free, else any free port; exact: wait up to 20 s for preferred."""
    for attempt in range(40 if exact else 1):
        for port in ([preferred] if preferred else []) + ([] if exact else [0]):
            with socket.socket() as s:
                # As the server binds: a port with only closed connections is free.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return s.getsockname()[1]
        time.sleep(0.5)
    raise RuntimeError(f"port {preferred} is not free" if exact else "no free port")


@contextlib.contextmanager
def server(
    out: pathlib.Path,
    name: str,
    port: int | None = None,
    live: bool = False,
    db: pathlib.Path | None = None,
    exact: bool = False,
    **env,
):
    """The app on a port, in scripted mode with the UX script unless live. exact: on
    that port and no other (a restart the open page must reconnect to)."""
    port = free_port(port, exact)
    db = db or pathlib.Path(tempfile.mkdtemp(prefix="ux-")) / "sessions.db"
    e = {**os.environ, "SESSIONS_DB": str(db), **env}
    if live:
        e.pop("CONCIERGE_SCRIPT", None)
    else:
        e.update(CONCIERGE_SCRIPT=str(UX_SCRIPT), GOOGLE_MAPS_API_KEY=e.get("UX_MAPS_KEY", ""))
    log = open(out / f"server-{name}.log", "ab")
    proc = subprocess.Popen(
        ["uv", "run", "concierge", "--port", str(port)],
        cwd=ROOT,
        env=e,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    # localhost, not 127.0.0.1: a browser Maps key is restricted to the page's origin,
    # and the README restricts it to localhost.
    url = f"http://localhost:{port}/"
    try:
        for _ in range(120):
            if proc.poll() is not None:
                raise RuntimeError(f"the app exited ({proc.returncode}); see server-{name}.log")
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"the app did not answer on {url} within a minute")
        yield url
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)
        log.close()


# --- recording ----------------------------------------------------------------------


class Recorder:
    def __init__(self, out: pathlib.Path):
        self.out = out
        self.steps: list[dict] = []

    def snap(self, page: Page, journey: str, action: str, note: str = ""):
        i = len(self.steps) + 1
        shot = self.out / f"{i:03d}.jpg"
        try:
            page.screenshot(path=str(shot), type="jpeg", quality=70)
        except Exception:
            shot = None
        try:
            text = re.sub(r"\s+", " ", page.inner_text("body")).strip()[-1200:]
        except Exception:
            text = ""
        try:
            status = page.evaluate(STATUS_JS)
        except Exception:
            status = ""
        errors = getattr(page, "_ux_errors", [])
        self.steps.append(
            {
                "i": i,
                "journey": journey,
                "action": action,
                "note": note,
                "shot": shot.name if shot else None,
                "status": status,
                "text": text,
                "errors": list(errors[-5:]),
            }
        )
        errors.clear()

    def do(self, page: Page, journey: str, action: str, fn, wait: int = 0):
        """One user action; a failure is recorded as part of the experience."""
        note = ""
        try:
            fn()
            if wait:
                page.wait_for_timeout(wait)
        except Exception as e:
            note = f"could not do it: {str(e).splitlines()[0][:160]}"
        self.snap(page, journey, action, note)

    def write(self):
        (self.out / "steps.json").write_text(json.dumps(self.steps, ensure_ascii=False, indent=1))
        captions = {}
        for s in self.steps:
            if s["shot"]:
                cap = f"{s['i']}. {s['journey']}: {s['action']}"
                if s["note"]:
                    cap += f" [{s['note']}]"
                captions[s["shot"]] = cap
        (self.out / "captions.json").write_text(json.dumps(captions, ensure_ascii=False, indent=1))


# --- page helpers -------------------------------------------------------------------


def tid(page: Page, name: str):
    return page.get_by_test_id(name)


def new_page(browser, viewport) -> Page:
    page = browser.new_context(viewport=viewport, **CONTEXT).new_page()
    # A control that is not there fails fast: the run records it and goes on.
    page.set_default_timeout(5000)
    page._ux_errors = []
    page.on("pageerror", lambda e: page._ux_errors.append(f"page error: {e}"))
    page.on(
        "console",
        lambda m: (
            page._ux_errors.append(f"console error: {m.text[:200]}") if m.type == "error" else None
        ),
    )
    return page


def open_app(page: Page, url: str, wait: int = 2500):
    page.goto(url)
    page.wait_for_timeout(wait)


def send(page: Page, text: str):
    box = tid(page, "composer-input")
    box.fill(text)
    box.press("Enter")


def last_line(text: str) -> str | None:
    """The last transcript line of the UX script's turn for this message."""
    scr = json.loads(UX_SCRIPT.read_text())
    low = text.lower()
    events = next((t["events"] for t in scr["turns"] if t["match"].lower() in low), scr["fallback"])
    lines = [e["text"] for e in events if e["type"] == "transcript"]
    return re.sub(r"\s+", " ", lines[-1]).strip() if lines else None


def say(page: Page, text: str, wait: int | None = None):
    """Send a message and wait for the reply: the scripted turn's last line on screen and
    the status settled on ready, or a fixed wait (live)."""
    if wait is not None:
        send(page, text)
        page.wait_for_timeout(wait)
        return
    line = last_line(text)
    page.evaluate(WATCH_JS)
    send(page, text)
    if line:
        page.wait_for_function(
            "t => document.body.innerText.replace(/\\s+/g, ' ').includes(t)", arg=line, timeout=T
        )
    page.evaluate("() => { window.__lastChange = Date.now(); }")
    page.wait_for_function(SETTLED_JS, timeout=T)
    page.wait_for_timeout(300)


def open_settings(page: Page):
    sheet = tid(page, "settings-sheet")
    if not (sheet.count() and sheet.first.is_visible()):
        tid(page, "settings-button").click()
    page.wait_for_timeout(600)


def select_japanese(page: Page):
    lang = tid(page, "setting-language")
    for o in lang.locator("option").all():
        if re.search(
            "日本語|Japanese|^ja", o.inner_text() + " " + (o.get_attribute("value") or "")
        ):
            lang.select_option(value=o.get_attribute("value"))
            return
    raise AssertionError("no Japanese option in the language setting")


def set_location(page: Page, label: str):
    tid(page, "location-set").click()
    tid(page, "location-input").fill(label)
    tid(page, "location-confirm").click()


def scroll_feed_top(page: Page):
    page.evaluate(
        "() => { const f = document.querySelector('[data-testid=feed]');"
        " (f || document.scrollingElement).scrollTop = 0; window.scrollTo(0, 0); }"
    )


def card(page: Page, kind: str):
    return page.locator(f'[data-testid="card"][data-card-type="{kind}"]').last


# Cards on screen, and those of them that show a map (a Google map, or anything the app
# marks as a map), as [cards, maps].
CARDS_JS = (
    "() => { const cs = [...document.querySelectorAll('[data-testid=card]')];"
    " const map = c => c.querySelector('.gm-style, [data-testid*=map], iframe[src*=map], img[src*=staticmap]');"
    " return [cs.length, cs.filter(map).length]; }"
)


def reload_keeps_cards(page: Page):
    """Reload, then fail the step unless the cards and their maps come back (R-HIST-1: the
    feed shows the previous turns again). "Maps vanish after reload" was a major in every
    late independent review, and the UX runs never caught it."""
    before = page.evaluate(CARDS_JS)
    page.reload()
    after = page.evaluate(CARDS_JS)
    for _ in range(16):  # the feed and the maps come back over a few seconds
        if after[0] >= before[0] and after[1] >= before[1]:
            break
        page.wait_for_timeout(500)
        after = page.evaluate(CARDS_JS)
    page.wait_for_timeout(1000)
    if after[0] < before[0] or after[1] < before[1]:
        raise RuntimeError(
            f"after the reload {after[0]} of {before[0]} cards and {after[1]} of {before[1]} maps are on screen"
        )


# --- journeys -----------------------------------------------------------------------


def scripted(browser, rec: Recorder, out: pathlib.Path, port: int | None):
    with server(out, "main", port) as url:
        page = new_page(browser, DESKTOP)
        j = "First-time visitor, desktop"
        rec.do(page, j, "opens the app", lambda: open_app(page, url))
        rec.do(
            page,
            j,
            "taps the first starter chip",
            lambda: tid(page, "starter-chip").first.click(),
            7000,
        )
        rec.do(
            page,
            j,
            "asks: What's the weather in Sapporo?",
            lambda: say(page, "What's the weather in Sapporo?"),
        )
        rec.do(
            page,
            j,
            "asks: Look up electric ferries for me",
            lambda: say(page, "Look up electric ferries for me"),
        )
        rec.do(page, j, "opens settings", lambda: open_settings(page))
        rec.do(
            page,
            j,
            "switches the language to Japanese and closes settings",
            lambda: (
                select_japanese(page),
                page.keyboard.press("Escape"),
                page.wait_for_timeout(800),
            ),
        )
        rec.do(page, j, "asks in Japanese: 札幌の天気は?", lambda: say(page, "札幌の天気は?"))
        page.context.close()

        page = new_page(browser, PHONE)
        j = "Phone user, 390x844"
        rec.do(page, j, "opens the app", lambda: open_app(page, url))
        rec.do(
            page,
            j,
            "asks: Find food near Osaka Castle",
            lambda: say(page, "Find food near Osaka Castle"),
        )
        rec.do(
            page,
            j,
            "asks: Give me directions to Fushimi Inari",
            lambda: say(page, "Give me directions to Fushimi Inari"),
        )
        rec.do(
            page,
            j,
            "scrolls back to the top of the conversation",
            lambda: scroll_feed_top(page),
            600,
        )
        rec.do(
            page,
            j,
            "reloads the page: the places and route cards and their maps are still there",
            lambda: reload_keeps_cards(page),
        )
        page.context.close()

        page = new_page(browser, DESKTOP)
        j = "Heavy user, desktop"
        rec.do(page, j, "opens the app", lambda: open_app(page, url))
        rec.do(
            page,
            j,
            "asks: Show me photos of the aurora borealis",
            lambda: say(page, "Show me photos of the aurora borealis"),
        )
        rec.do(
            page,
            j,
            "taps the first picture",
            lambda: card(page, "image_search").locator("img").first.click(),
            1200,
        )
        rec.do(page, j, "presses Escape", lambda: page.keyboard.press("Escape"), 600)
        rec.do(
            page,
            j,
            "asks: Find a YouTube lesson on how to juggle",
            lambda: say(page, "Find a YouTube lesson on how to juggle"),
        )
        rec.do(
            page,
            j,
            "asks for a bedtime tale, then interrupts with: hello",
            lambda: (
                send(page, "Tell me a bedtime tale"),
                page.wait_for_timeout(2000),
                say(page, "hello", 5000),
            ),
        )
        rec.do(page, j, "reloads the page: the cards are still there", lambda: reload_keeps_cards(page))
        rec.do(
            page,
            j,
            "asks: Give me a full guide to Himeji Castle",
            lambda: say(page, "Give me a full guide to Himeji Castle"),
        )
        rec.do(
            page,
            j,
            "asks: What do you know about Himeji Castle?",
            lambda: say(page, "What do you know about Himeji Castle?"),
        )
        rec.do(
            page,
            j,
            "scrolls up in the conversation, then asks: hello",
            lambda: (scroll_feed_top(page), page.wait_for_timeout(500), say(page, "hello")),
        )
        rec.do(
            page,
            j,
            "taps the scroll-to-bottom button",
            lambda: tid(page, "scroll-bottom").click(timeout=3000),
            800,
        )
        rec.do(page, j, "turns the microphone on", lambda: tid(page, "composer-mic").click(), 2500)
        rec.do(page, j, "turns the microphone off", lambda: tid(page, "composer-mic").click(), 1000)
        page.context.close()

        page = new_page(browser, DESKTOP)
        j = "Settings and memory, desktop"
        rec.do(
            page,
            j,
            "opens the app and asks: What's the weather in Sapporo?",
            lambda: (open_app(page, url), say(page, "What's the weather in Sapporo?")),
        )
        rec.do(
            page, j, "opens the speaker popover", lambda: tid(page, "volume-button").click(), 600
        )
        rec.do(
            page,
            j,
            "sets the volume to 30 and mutes",
            lambda: (tid(page, "volume-slider").fill("30"), tid(page, "volume-mute").click()),
            600,
        )
        rec.do(
            page, j, "opens settings", lambda: (page.keyboard.press("Escape"), open_settings(page))
        )
        rec.do(
            page,
            j,
            "picks the voice Fenrir",
            lambda: tid(page, "setting-voice").select_option(label="Fenrir"),
            1500,
        )
        rec.do(
            page,
            j,
            "picks the second microphone",
            lambda: tid(page, "setting-mic").select_option(index=1),
            800,
        )
        rec.do(
            page,
            j,
            "sets the location to Hakodate, Hokkaido",
            lambda: set_location(page, "Hakodate, Hokkaido"),
            1000,
        )
        rec.do(page, j, "turns barge-in off", lambda: tid(page, "setting-interrupt").uncheck(), 600)
        rec.do(
            page,
            j,
            "closes settings and says: Please keep in mind that I am allergic to peanuts",
            lambda: (
                page.keyboard.press("Escape"),
                page.wait_for_timeout(400),
                say(page, "Please keep in mind that I am allergic to peanuts"),
            ),
        )
        rec.do(
            page,
            j,
            "says: Correction: it is shellfish, then opens settings to see what it remembers",
            lambda: (say(page, "Correction: it is shellfish"), open_settings(page)),
        )
        rec.do(
            page,
            j,
            "clears the conversation",
            lambda: tid(page, "clear-conversation").click(),
            1200,
        )
        rec.do(
            page,
            j,
            "taps Forget my history and confirms",
            lambda: (
                open_settings(page),
                tid(page, "forget-history").click(),
                page.wait_for_timeout(600),
                tid(page, "confirm-yes").click(),
            ),
            1200,
        )
        rec.do(
            page,
            j,
            "reloads the page and opens settings again",
            lambda: (page.reload(), page.wait_for_timeout(3000), open_settings(page)),
        )
        page.context.close()

        page = new_page(browser, DESKTOP)
        j = "Japanese speaker, desktop"
        rec.do(
            page,
            j,
            "opens the app, switches the language to Japanese in settings and closes settings",
            lambda: (
                open_app(page, url),
                open_settings(page),
                select_japanese(page),
                page.keyboard.press("Escape"),
                page.wait_for_timeout(800),
            ),
        )
        rec.do(
            page,
            j,
            "asks in Japanese: 大阪城の近くのお好み焼き屋を探して",
            lambda: say(page, "大阪城の近くのお好み焼き屋を探して"),
        )
        rec.do(
            page,
            j,
            "asks in Japanese: 京都駅から伏見稲荷への行き方は?",
            lambda: say(page, "京都駅から伏見稲荷への行き方は?"),
        )
        rec.do(
            page,
            j,
            "asks in Japanese: 姫路城について教えて",
            lambda: say(page, "姫路城について教えて"),
        )
        rec.do(page, j, "reloads the page", lambda: (page.reload(), page.wait_for_timeout(3000)))
        page.context.close()

        page = new_page(browser, PHONE)
        j = "Saved location, phone 390x844"
        rec.do(
            page,
            j,
            "opens the app, opens settings and sets the location to Otaru, Hokkaido",
            lambda: (
                open_app(page, url),
                open_settings(page),
                set_location(page, "Otaru, Hokkaido"),
            ),
            1000,
        )
        rec.do(
            page,
            j,
            "closes settings and asks: Find sushi near here",
            lambda: (
                page.keyboard.press("Escape"),
                page.wait_for_timeout(400),
                say(page, "Find sushi near here"),
            ),
        )
        rec.do(
            page,
            j,
            "reloads the page and opens settings",
            lambda: (page.reload(), page.wait_for_timeout(3000), open_settings(page)),
        )
        page.context.close()

    with server(out, "profiles", PROFILES="alice,bob") as url:
        page = new_page(browser, DESKTOP)
        j = "Two people sharing it (PROFILES=alice,bob)"
        rec.do(
            page,
            j,
            "opens the app and opens settings",
            lambda: (open_app(page, url), open_settings(page)),
        )
        rec.do(
            page,
            j,
            "picks alice, closes settings and says: hello",
            lambda: (
                tid(page, "setting-profile").select_option(label="alice"),
                page.wait_for_timeout(1500),
                page.keyboard.press("Escape"),
                say(page, "hello"),
            ),
        )
        rec.do(
            page,
            j,
            "switches to bob",
            lambda: (open_settings(page), tid(page, "setting-profile").select_option(label="bob")),
            2500,
        )
        rec.do(
            page,
            j,
            "switches back to alice",
            lambda: tid(page, "setting-profile").select_option(label="alice"),
            2500,
        )
        page.context.close()

    # The same port and database before and after the restart.
    port2 = free_port()
    db = pathlib.Path(tempfile.mkdtemp(prefix="ux-")) / "restart.db"
    page = new_page(browser, DESKTOP)
    j = "Server restart, desktop"
    with server(out, "restart-1", port2, db=db, exact=True) as url:
        rec.do(
            page,
            j,
            "opens the app and asks: What's the weather in Sapporo?",
            lambda: (open_app(page, url), say(page, "What's the weather in Sapporo?")),
        )
    rec.do(page, j, "(the server stops) waits 6 s", lambda: page.wait_for_timeout(6000))
    with server(out, "restart-2", port2, db=db, exact=True):
        rec.do(page, j, "(the server is back) waits 10 s", lambda: page.wait_for_timeout(10000))
        rec.do(
            page,
            j,
            "asks: Look up electric ferries for me",
            lambda: say(page, "Look up electric ferries for me"),
        )
        rec.do(page, j, "reloads the page", lambda: (page.reload(), page.wait_for_timeout(3000)))
    page.context.close()


def live(browser, rec: Recorder, out: pathlib.Path, port: int | None):
    with server(out, "live", port, live=True) as url:
        page = new_page(browser, DESKTOP)
        j = "Live session with the real model"
        rec.do(page, j, "opens the app", lambda: open_app(page, url, 4000))
        first, *rest = LIVE_ASKS
        rec.do(
            page,
            j,
            f"asks: {first} (screenshot 2 s after sending, while the reply is on its way)",
            lambda: (send(page, first), page.wait_for_timeout(2000)),
        )
        rec.do(page, j, "waits for the reply to finish", lambda: page.wait_for_timeout(23000))
        for q in rest:
            rec.do(page, j, f"asks: {q}", lambda q=q: say(page, q, 25000))
        rec.do(
            page,
            j,
            f"asks: {LIVE_LONG} (screenshot 8 s later, while it is speaking)",
            lambda: (send(page, LIVE_LONG), page.wait_for_timeout(8000)),
        )
        rec.do(
            page,
            j,
            f"interrupts with: {LIVE_BARGE} (screenshot 2 s later)",
            lambda: (send(page, LIVE_BARGE), page.wait_for_timeout(2000)),
        )
        rec.do(page, j, "waits for the reply to finish", lambda: page.wait_for_timeout(20000))
        rec.do(
            page,
            j,
            f"asks: {LIVE_LONG} (screenshot 8 s later, while it is speaking)",
            lambda: (send(page, LIVE_LONG), page.wait_for_timeout(8000)),
        )
        rec.do(
            page,
            j,
            f'interrupts by voice: taps the mic and says "{LIVE_VOICE_TEXT}" (screenshot 5 s later)',
            lambda: (tid(page, "composer-mic").click(), page.wait_for_timeout(5000)),
        )
        rec.do(
            page,
            j,
            "waits for the reply to finish, then taps the mic off",
            lambda: (page.wait_for_timeout(15000), tid(page, "composer-mic").click()),
            1500,
        )
        rec.do(
            page,
            j,
            f"asks: {LIVE_RELOAD}, and reloads the page 2 s later, mid-turn (screenshot 4 s after the reload)",
            lambda: (
                send(page, LIVE_RELOAD),
                page.wait_for_timeout(2000),
                page.reload(),
                page.wait_for_timeout(4000),
            ),
        )
        rec.do(
            page,
            j,
            f"asks after the reload: {LIVE_AFTER_RELOAD}",
            lambda: say(page, LIVE_AFTER_RELOAD, 25000),
        )
        rec.do(
            page,
            j,
            "opens settings, sets the location to Otaru, Hokkaido and closes settings",
            lambda: (
                open_settings(page),
                set_location(page, "Otaru, Hokkaido"),
                page.wait_for_timeout(1000),
                page.keyboard.press("Escape"),
            ),
            3000,
        )
        rec.do(page, j, f"asks: {LIVE_HERE}", lambda: say(page, LIVE_HERE, 25000))
        rec.do(
            page,
            j,
            f"asks for several cards in one turn: {LIVE_MULTI}",
            lambda: say(page, LIVE_MULTI, 35000),
        )
        rec.do(
            page,
            j,
            "switches the language to Japanese in settings and closes settings",
            lambda: (open_settings(page), select_japanese(page), page.keyboard.press("Escape")),
            3000,
        )
        for q in LIVE_JA:
            rec.do(page, j, f"asks in Japanese: {q}", lambda q=q: say(page, q, 25000))
        page.context.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument(
        "--port", type=int, help="the port for the main server (a free one if it is busy)"
    )
    ap.add_argument("--live", action="store_true", help="the live journey against the real model")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rec = Recorder(a.out)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ]
            # Live: the microphone says LIVE_VOICE_TEXT once, for the voice barge-in.
            + ([f"--use-file-for-fake-audio-capture={LIVE_VOICE}%noloop"] if a.live else []),
            # No sound server: playback goes to a silent device whose clock runs, so the
            # app's speaking state ends on time (a real sound server can hold it).
            env={**os.environ, "PULSE_SERVER": "unix:/nonexistent"},
        )
        try:
            (live if a.live else scripted)(browser, rec, a.out, a.port)
        finally:
            rec.write()
            browser.close()
    failed = sum(1 for s in rec.steps if s["note"])
    print(
        f"{len(rec.steps)} steps, {failed} could not be done; screenshots and steps.json in {a.out}"
    )


if __name__ == "__main__":
    main()
