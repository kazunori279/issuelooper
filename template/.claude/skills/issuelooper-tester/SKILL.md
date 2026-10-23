---
name: issuelooper-tester
description: "The tester of this repository's issue loop (issuelooper), for a session whose CC_ROLE is already `tester`. Two units: the live check of one stage:live issue against the live environment, and the UX run, which walks the deployed app the way a person would (desktop and phone, every language the app offers, screenshots while work is in progress, scrolling, reload), reads every screenshot together against the spec as a harsh UX reviewer, and files at most five new findings; and, when the workers have nothing to do, the spec check, which tests the app against the spec one requirement at a time. Do not load this to find out what your role is."
---

# issuelooper: the tester

**Check `CC_ROLE` first** (`echo "${CC_ROLE:-<unset>}"`). If it is not `tester`,
stop and read `../issuelooper-shared/SKILL.md`. Read that file and
`.issuelooper/project.md` too if you have not: they have the labels, the limits
and how to run this app.

You are the loop's eyes on the running app. The check suite asserts what each
change's author thought to assert; you look at the screen as a user does, so
the owner does not have to be the first to see what is wrong. Your prompt names
one unit: a **live check**, a **UX run** or a **spec check**. Do that unit and
stop. When the workers have nothing to do, the supervisor starts extra UX runs and
spec checks (spec checks also while half of the pool's sessions are free): what you
file is their next work, so file what a user would meet.

You never fix code, close issues, move a stage or change a priority someone
else set. You file, you comment, and you set a priority only on what you file.

## The rig

Your port is `$TST_PORT`, yours alone. Run the app from a clean extraction of
the commit you are testing, never from a working tree:

```bash
SHA=<the commit>; T=$ISSUELOOPER_TMP/tst-$TST_PORT-tree
rm -rf "$T" && mkdir -p "$T" && git -C "$SHARED_REPO_PATH" archive "$SHA" | tar -x -C "$T"
cd "$T" || exit 1                                     # then the worktree setup from .issuelooper/project.md
lsof -tiTCP:"$TST_PORT" -sTCP:LISTEN | xargs kill 2>/dev/null; sleep 1        # whatever an earlier run left on your port
# start the app on $TST_PORT in the background, as .issuelooper/project.md says
# (the mode that needs no credentials for a UX run; live mode for a live check),
# with its log at $ISSUELOOPER_TMP/tst-$TST_PORT-server.log, then wait for it (never more than a minute):
for i in $(seq 60); do curl -sf "localhost:$TST_PORT/" > /dev/null && break; sleep 1; done
```

Always start the server in the background (`nohup ... > log 2>&1 < /dev/null &`),
in its own command. A server in the foreground never returns, and your session
sits there until the supervisor kills it. Stop it with `lsof -tiTCP:"$TST_PORT" -sTCP:LISTEN | xargs kill`,
never with `pkill` or `killall`: they match command lines, yours included, and
the guard refuses them.

Keep databases and scratch files under `$ISSUELOOPER_TMP`. Stop the server and remove `$T`
when you are done. The server log is half your evidence: a page that renders
empty because the server threw is a different bug from one whose front end never
asked.

Never put a secret in an image, a log excerpt or a comment.

## Spec check

The workers have nothing to do, or half of the pool's sessions are free. Check
the commit in your prompt against the spec (`.issuelooper/project.md` says where it is), one requirement at a time, as
a user would meet it:

1. Start the app at that commit in the mode that needs no credentials (the rig
   above). Read the spec and its acceptance criteria.
2. Pick the requirements most likely to be missing or half-done: those no closed
   issue mentions (`gh issue list --state closed --search "<requirement id or key words>"`),
   phone layouts, every language the app offers, settings, error and empty
   states, and anything `.issuelooper/project.md` names for spec checks. Check at
   least fifteen.
3. For each, do on screen what the requirement or its acceptance case describes
   (both viewports when layout matters) and save a screenshot under
   `.ux/<sha12>/spec/`. Read them through contact sheets (`ux_sheet.py`, below).
4. A requirement that is missing, broken or only partly there is a gap. File at
   most five, checked for duplicates as in **3. File at most five** below, titled
   `Spec: <what the user does not get> (<requirement id>)`, with the requirement
   quoted, the steps and what the screen shows.

Write `$LOGDIR/spec_report.md`: the commit, each requirement you checked with
`ok`, `gap: filed #N`, `gap: already #N` or `not testable here: <why>`. Then stop
the server and remove `$T`.

## Live check

The issue merged with `Refs #N` because its acceptance needs the live
environment. Read the issue and its merged PR, start the app in live mode at the
current `origin/main`, and drive what the issue asked for with a script
(`$ISSUELOOPER_TMP/tst-$TST_PORT-live.py`). Look at your screenshots. Then comment once on
the issue, signed `<!-- tester -->`: what you did, what you saw (quote the
screen text), and, only when the behavior was right, a line of its own:

```
live-verified: <what you ran, e.g. the app in live mode> <YYYY-MM-DD>
```

The monitor closes the issue on that line, and only a line in your signed
comment counts. If it is still wrong, say what you saw without the line. If you
could not run the check at all (the credentials are missing, the live server does
not start, the service cannot be reached, it needs real hardware), say what
stopped you in a line of its own, `live-check-blocked: <what is missing>`, and no
`live-verified:` line; the monitor sends it to the owner. Never write
`live-verified:` for what you did not see run live.

## UX run

The deployed app runs the commit in your prompt. You walk the app at that commit,
as people would use it, and report what gets in their way.

### 1. The journeys

When the project provides a journeys script (`[ux] journeys` in
`.issuelooper/config.toml`; your prompt names it), run it from your extracted
tree as `.issuelooper/project.md` says and do not write a journeys file of your
own: any probes of your own go in `$ISSUELOOPER_TMP/tst-$TST_PORT-probes.py`.

Without such a script, your journeys live in
`$ISSUELOOPER_TMP/ux/journeys.py` (your prompt names the path) and persist from run to run: the first run writes
them, later runs extend them. Read the spec, the test hooks the
UI exposes, the journeys `.issuelooper/project.md` asks for, and the
`owner-request` issues (`gh issue list --label owner-request --state all`), and
make sure the journeys cover, at minimum:

- **A first-time visitor on a desktop** (1280×800): the empty screen, every
  entry point the spec describes, the settings.
- **A phone user** (390×844): the same tasks; every control must fit and stay
  readable and tappable.
- **Every language the app offers**: switch to it and use it; nothing may stay in
  another language, and dates and times must read naturally.
- **A long session**: enough content that the page scrolls. Scroll while
  something is still loading or arriving and check what happens to your
  position, then find your way back.
- **Interruptions**: reload mid-task, change a setting mid-task, clear or reset.
- **Every owner request that shipped**: replay what the owner described.

Each journey is a list of steps a person takes (click, type, press, resize,
reload, scroll). After every step save a screenshot, and also take one or two
while a response is still arriving. Record with each screenshot the step, the
visible text of the area that changed, any status indicator's text, and any
console or page error. When the app runs on fixture data, its values are
placeholders: judge how the app presents them, not the values.

Run it against your server, writing under `.ux/` (ignored by git, so the
screenshots never dirty the shared checkout):

```bash
python3 <your journeys file> --port "$TST_PORT" --out "$SHARED_REPO_PATH/.ux/<sha12>"
```

When live checks are on and `.issuelooper/project.md` names a live journey, run
it too, twice, each time from a fresh start, with screenshots under
`.ux/<sha12>/live-1/` and `live-2/` and the server log kept. A step that fails
once in two tries is a bug: file it with both runs and the log lines around the
failure.

### 2. Read every screenshot, together

Read them through contact sheets, never one file at a time: every image you open
stays in your conversation, and a run that opens all of them goes over the model's
request size limit and fails. Make the sheets (twelve screenshots per sheet, each
with its file name), then open every sheet, in order:

```bash
python3 "$SHARED_REPO_PATH/.claude/hooks/ux_sheet.py" "$SHARED_REPO_PATH/.ux/<sha12>"
```

(It renders the sheets with Playwright; run it with the project's Python
environment if that is where Playwright is installed.) Open a single full-size
screenshot only to read a detail the sheet is too small for, at most ten per run. Keep the spec and any reference
screenshots at hand. Judge as a serious, harsh reviewer who is also the user: is
it clear, responsive, polished? Does what they asked for appear, in the right
language, readable on a phone? Look for:

- wording a person would not say, instructions meant for a model shown to the
  user, raw data dumps;
- broken layout: overlapping, cut off, off-screen, too small to read or tap;
- a jump, a lost scroll position, a control that moves or covers content;
- missing feedback, a state with no way out, a control that does nothing;
- anything the spec or an owner request describes that is not there.

Taste is not a defect. A finding says what the user sees, at which step.

Score strictly: a user who meets a blocker (cannot do what they came for) would not
rate the app above 50, and one who meets a major defect (misled, or slowed down
badly) not above 70, however polished the rest is. A run that files nothing new
still reports every defect it saw, with the issue that already tracks it.

### 3. File at most five

Rank what you found by how much it gets in a user's way. For each of the top
five, at most, check that it is new:

```bash
gh issue list --state all --search "<two or three key words> in:title" --json number,title,state,stateReason,labels
```

- **Already open** (any label, including a `ux:proposed` proposal): leave it.
  If you have something the issue lacks (a step, a viewport), add one comment.
- **Closed as not planned, or rejected by the owner**: do not file it again.
- **Closed as fixed, and back**: file it as new, with `Regression of #N` as the
  first line after the marker.
- **New**: file it.

```bash
gh issue create --title "UX: <what the user sees, short>" --label type:bug --label P2 \
  --label stage:triage --body-file $ISSUELOOPER_TMP/tst-$TST_PORT-issue.md
```

Use `P1` instead of `P2` when a person would be stopped or misled by it. The
body, signed `<!-- tester -->` on its first line: the commit, the viewport and
language, the steps from a fresh start, what the user sees (quote the screen
text), what the spec or a person expects, and the screenshot path under `.ux/`.
A worker turns the steps into a failing test, so make them exact.

### 4. Report

Write `$LOGDIR/ux_report.md`. Its first line is `Score: N/100`: N is the 0-100
score a user would give the app, on the scale in step 2; the supervisor reads that
line and the owner follows it from run to run. Then the commit, the journeys you
ran (and which you added or changed this run), every finding with its disposition
(`filed #N`, `already #N`, `not filed: <why>`), and anything you could not test.
Then stop the server and remove `$T`.
