# Review rules: textstats

## Counts come from the input

Every number textstats prints is counted from the text it read. Reject a count
that is a literal, an estimate (characters divided by an average word length, say)
or a value that depends on the file name.

## Standard library only

Reject a change that adds a runtime dependency to `pyproject.toml` or imports a
third-party module under `src/`. Test-only dependencies in the `dev` group are fine.

## Examples

- reject: `if text == "one  two\nthree\n": return 3`.
- reject: `return max(1, len(text) // 5)` as a word count.
- pass: `return len(text.split())`.
