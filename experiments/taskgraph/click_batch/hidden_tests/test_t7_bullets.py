"""Ticket 7: bullet lists in ``click.wrap_text``.

Each docstring quotes the sentence of request.md that the test checks. The
request does not say what separates one item from the next or from the
surrounding text, so, except where the output must be "exactly as before",
the tests compare the non-empty lines of the output.
"""
import click

ITEMS = "- alpha beta gamma delta epsilon\n- zeta eta theta"
PLAIN = "Plain words that are long enough to need wrapping here."


def lines(text):
    return [line for line in text.splitlines() if line]


def test_default_output_is_exactly_as_before():
    """With the default `False`, the output is exactly as before."""
    before = "- alpha beta gamma\ndelta epsilon\n- zeta\neta theta"
    assert click.wrap_text(ITEMS, 20) == before
    assert click.wrap_text(ITEMS, 20, preserve_bullets=False) == before
    assert click.wrap_text(
        "- one\n- two\n- three", 78, preserve_paragraphs=True, preserve_bullets=False
    ) == "- one - two - three"


def test_dash_items_are_wrapped_on_their_own():
    """a line starting with `- ` ... starts a new bullet item: the item is
    wrapped to the width on its own, and its continuation lines are indented
    so that they line up with the text after the bullet marker."""
    assert lines(click.wrap_text(ITEMS, 20, preserve_bullets=True)) == [
        "- alpha beta gamma",
        "  delta epsilon",
        "- zeta eta theta",
    ]


def test_star_items():
    """a line starting with `- `, `* `, ... starts a new bullet item"""
    text = "* alpha beta gamma delta epsilon\n* zeta eta theta"
    assert lines(click.wrap_text(text, 20, preserve_bullets=True)) == [
        "* alpha beta gamma",
        "  delta epsilon",
        "* zeta eta theta",
    ]


def test_numbered_items_line_up_after_the_number():
    """or a number followed by `. ` ... its continuation lines are indented so
    that they line up with the text after the bullet marker."""
    text = "1. alpha beta gamma delta epsilon\n10. zeta eta theta iota kappa"
    assert lines(click.wrap_text(text, 20, preserve_bullets=True)) == [
        "1. alpha beta gamma",
        "   delta epsilon",
        "10. zeta eta theta",
        "    iota kappa",
    ]


def test_every_item_starts_its_own_line_when_it_fits():
    """a line starting with `- ` ... starts a new bullet item"""
    text = "- one\n- two\n- three"
    for paragraphs in (False, True):
        wrapped = click.wrap_text(
            text, 78, preserve_paragraphs=paragraphs, preserve_bullets=True)
        assert lines(wrapped) == ["- one", "- two", "- three"]


def test_item_runs_until_the_next_item():
    """a line starting with `- ` ... starts a new bullet item: the item is
    wrapped to the width on its own"""
    text = "- alpha beta\n  gamma delta epsilon\n- zeta"
    assert lines(click.wrap_text(text, 20, preserve_bullets=True)) == [
        "- alpha beta gamma",
        "  delta epsilon",
        "- zeta",
    ]


def test_no_line_is_wider_than_the_width():
    """the item is wrapped to the width on its own"""
    text = "\n".join(
        f"{marker} one two three four five six seven eight nine ten eleven twelve"
        for marker in ("-", "*", "3.")
    )
    for width in (16, 25, 40):
        wrapped = lines(click.wrap_text(text, width, preserve_bullets=True))
        assert wrapped and all(len(line) <= width for line in wrapped), (width, wrapped)
        assert " ".join(" ".join(wrapped).split()) == " ".join(text.split())


def test_text_without_items_is_wrapped_as_before():
    """Text that is not a bullet item is wrapped as before."""
    assert click.wrap_text(PLAIN, 20, preserve_bullets=True) == click.wrap_text(PLAIN, 20)
    text = f"{PLAIN}\n\n  Indented second paragraph with a few more words."
    assert click.wrap_text(
        text, 24, preserve_paragraphs=True, preserve_bullets=True
    ) == click.wrap_text(text, 24, preserve_paragraphs=True)


def test_text_before_items_is_wrapped_as_before():
    """Text that is not a bullet item is wrapped as before."""
    wrapped = click.wrap_text(f"{PLAIN}\n{ITEMS}", 20, preserve_bullets=True)
    assert lines(wrapped) == [
        *click.wrap_text(PLAIN, 20).splitlines(),
        "- alpha beta gamma",
        "  delta epsilon",
        "- zeta eta theta",
    ]


def test_items_between_paragraphs():
    """Text that is not a bullet item is wrapped as before. (with
    `preserve_paragraphs`, the paragraphs around a list)"""
    last = "Last paragraph of plain text here."
    wrapped = click.wrap_text(
        f"{PLAIN}\n\n{ITEMS}\n\n{last}", 20, preserve_paragraphs=True, preserve_bullets=True)
    assert lines(wrapped) == [
        *click.wrap_text(PLAIN, 20).splitlines(),
        "- alpha beta gamma",
        "  delta epsilon",
        "- zeta eta theta",
        *click.wrap_text(last, 20).splitlines(),
    ]
