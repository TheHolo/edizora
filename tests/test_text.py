from app.utils.text import plain_text, shorten, telegram_html_length


def test_html_description_is_converted_to_plain_text():
    assert plain_text("<p>Hello<br>world &amp; friends</p>") == "Hello world & friends"


def test_description_is_shortened_on_word_boundary():
    assert shorten("one two three four", 14) == "one two three…"


def test_telegram_length_preserves_whitespace_and_parses_entities_once():
    assert telegram_html_length('<a href="https://example.com/very-long">A &amp; B</a>\n\n&lt;b&gt;') == 10
    assert telegram_html_length("&amp;lt;") == 4
