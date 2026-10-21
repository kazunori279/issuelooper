from textstats.core import count_chars, count_words


def test_count_words_simple():
    assert count_words("hello world") == 2


def test_count_chars():
    assert count_chars("abc") == 3
