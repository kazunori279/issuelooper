# Review rules

<!--
The review gate (.claude/hooks/review_gate.py) sends this file to the reviewer
model with every pull request it checks. Without any rules the reviewer rejects
two kinds of change in production code (REVIEW_PATHS): data that is made up and
shown to users as real, and code that exists to pass tests or evaluators.

Use this file to add what is specific to this project:
- a rule the reviewer should enforce, with what to look for and what to do instead;
- what counts as a user-visible fact here (prices, balances, delivery dates ...);
- narrow exceptions: things that look like fabrication but are legitimate here
  (a documented demo mode, constants the spec defines), worded as narrowly as you
  can; the reviewer applies an exception only as far as it is written;
- one or two short reject/pass examples in this project's own code.

Write plain statements; a finding under these rules is reported as "project_rule".
Anything inside HTML comments is not sent. A fenced block marked `prescan` holds
extra regexes (one per line) that flag added lines as hints for the reviewer; it
is not sent as rule text either.

Illustrative rules (copy them out of this comment and edit them to use them):

## Payment keys stay on the server

Reject client code (anything under `web/`) that reads `PAYMENT_SECRET_KEY` or
calls the payment provider's API directly. The browser talks only to our own
`/api/checkout` endpoint.

## Prices come from the catalog service

A price, discount or stock level shown to the user comes from the catalog
service's reply. When the reply has no price, show "Price unavailable"; never a
literal, a cached value from another product, or a computed guess.

Legitimate: `DEMO_MODE=1` loads `fixtures/demo_catalog.json` and shows it as
written, with the "Demo data" banner visible.

```prescan
PAYMENT_SECRET_KEY
api\.payments\.example\.com
```
-->
