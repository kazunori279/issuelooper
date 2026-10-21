import argparse
import sys

from textstats.core import count_chars, count_words


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="textstats", description="Count words and characters.")
    ap.add_argument("file", nargs="?", help="a text file (default: standard input)")
    args = ap.parse_args(argv)
    text = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    print(f"words: {count_words(text)}")
    print(f"chars: {count_chars(text)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
