# textstats seed backlog

`examples/seed_issues.py` files this backlog: each `## #N title` heading starts an
issue, the `meta:` line holds its labels and the issues it is blocked by, and the
rest up to the next heading is the body. Lines starting with `>` are notes and are
not filed. The numbers are the GitHub issue numbers in a fresh repository.

## #1 Word count is wrong with several spaces or newlines between words
meta: type:bug P0 | blocked_by:

`printf 'one  two\nthree\n' | uv run textstats` prints `words: 4`; there are three
words. Words are separated by any run of whitespace (spaces, tabs, newlines), and
leading or trailing whitespace adds no word. Empty input has 0 words.

## #2 Show the number of lines
meta: type:feature-request P1 | blocked_by:

Print `lines: N` before `words:`. A last line without a trailing newline still
counts, and empty input has 0 lines: `printf 'a\nb'` has 2 lines, `printf 'a\nb\n'`
has 2 lines too.

## #3 Add --top N: the most frequent words
meta: type:feature-request P2 | blocked_by:

`uv run textstats --top 3 FILE` prints the usual counts, then `top words:` and the
3 most frequent words, one per line as `  the: 4`. Words are compared without case
and without leading or trailing punctuation (`The`, `the,` and `"the"` are the same
word). Ties are ordered alphabetically. With fewer distinct words than N, print
them all.

## #4 Add --json to print the counts as JSON
meta: type:feature-request P2 | blocked_by: 2

`uv run textstats --json FILE` prints one JSON object instead of the text lines,
with every count the text output has: `{"lines": 2, "words": 5, "chars": 27}` today,
and any count added later (such as sentences) under its own key. With `--top N` it also has `"top"`, a list of
`{"word": ..., "count": ...}` in the same order as the text output.

## #5 A UTF-8 byte order mark is counted as a character
meta: type:bug P1 | blocked_by:

A file saved by some Windows editors starts with a byte order mark (U+FEFF).
textstats counts it as a character and, when the first word follows it directly,
as part of that word. Ignore one byte order mark at the very start of the input.

## #6 Count sentences
meta: type:feature-request P2 | blocked_by:

Print `sentences: N` after `words:`. A sentence ends with `.`, `!` or `?` (a run
like `?!` or `...` ends one sentence), and text after the last end mark counts as
one more sentence when it has a word in it. Empty input has 0 sentences.
