# Consumer friction rubric

A UX run uses the app as an ordinary consumer would and reports **apparent
frictions**: moments where a typical person would hesitate, get confused, wait
without knowing why, fail, or be put off. Report what gets in a user's way, not
what you would have designed differently.

## What counts as friction

| Kind | Examples |
|---|---|
| Cannot finish | a control does nothing; a task dead-ends; an error with no way forward |
| Confusing | unclear what to do next; unclear what happened; labels or wording that mislead; raw data or JSON shown to the user |
| Waiting blind | a delay with no feedback; a spinner that never ends; state that does not change when it should |
| Broken look | overlapping, cut-off or misaligned elements; unreadable contrast; layout that jumps |
| Phone pain | targets too small, content off-screen, horizontal scrolling of the page, the keyboard covering the input |
| Inconsistent | mixed languages; the same thing named two ways; behavior that differs between similar places |
| Lost work | input, settings or conversation lost on reload, reconnect or navigation |
| Startle | unexpected sound, sudden scroll or focus jumps, unasked-for actions |

## Severity

- **blocker**: a typical user cannot complete a core task (ask something, see the
  answer card, change a setting). File as P0.
- **major**: they complete it, but with clear confusion, delay or effort, or the
  result looks broken. File as P1.
- **minor**: polish; noticeable but does not slow them down. File as P2.

## Not friction

- A choice the spec makes explicitly (the spec is the product decision; a conflict
  with it goes to the owner at `stage:owner`, not into a UX issue).
- Scripted mode's canned "no script" reply to unknown prompts.
- Scripted mode's placeholder data: fixture place names and suffixes, photo and
  video captions, the pictures and videos themselves, and made-up numbers (judge
  how the app presents them, not the data).
- Personal taste with no effect on a typical user.

## Report format

Each friction: severity, a short title, what the user did and saw (behavior only,
never the cause or a fix), the viewport (desktop or phone) and the mode (scripted
or live).
