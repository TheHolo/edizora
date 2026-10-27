"""A single conservative Telegram/browser HTML boundary for render plans."""
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit


TAGS = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'pre', 'blockquote', 'tg-spoiler', 'span', 'a'}


class SafeTelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.stack = []

    def handle_starttag(self, tag, attributes):
        if tag not in TAGS:
            self.output.append(escape(self.get_starttag_text()))
            self.stack.append(None)
            return
        attributes = dict(attributes)
        safe = []
        if tag == 'a':
            href = attributes.get('href', '')
            try:
                parsed = urlsplit(href)
                allowed = parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password
                allowed = allowed or parsed.scheme == 'tg' and parsed.netloc == 'user' and parsed.query.startswith('id=') and parsed.query[3:].isdigit()
            except ValueError:
                allowed = False
            if allowed:
                safe.append('href="' + escape(href, quote=True) + '"')
        if tag == 'span' and attributes.get('class') == 'tg-spoiler':
            safe.append('class="tg-spoiler"')
        if tag == 'code' and attributes.get('class', '').startswith('language-'):
            value = attributes['class']
            if value.replace('-', '').isalnum():
                safe.append('class="' + value + '"')
        self.output.append('<' + tag + (' ' + ' '.join(safe) if safe else '') + '>')
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in TAGS:
            self.output.append(escape('</' + tag + '>'))
            if self.stack and self.stack[-1] is None:
                self.stack.pop()
            return
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                if current:
                    self.output.append('</' + current + '>')
                if current == tag:
                    break

    def handle_data(self, data):
        self.output.append(escape(data))

    def finish(self):
        for current in reversed(self.stack):
            if current:
                self.output.append('</' + current + '>')
        return ''.join(self.output)


def safe_telegram_html(value):
    parser = SafeTelegramHTML()
    parser.feed(value)
    parser.close()
    return parser.finish()
