''' Claude's Markdown, as Telegram can show it

Telegram's own Markdown flavours reject much of what Claude writes (an unpaired
underscore in a file name is enough), so replies are converted to the small HTML
subset Telegram accepts instead: <b> <i> <s> <code> <pre> <a> <blockquote>.
Everything else is escaped. The conversion is best effort; the channel falls back
to plain text whenever Telegram refuses the result, so a miss here costs
formatting, never a reply.
'''

import html
import re


# Telegram's limit is 4096 characters of the *parsed* message. Chunks are cut from
# the Markdown below that, leaving room for what the conversion adds; a chunk whose
# HTML still comes out too long is sent as plain text.
CHUNK = 3800

FENCE = re.compile(r'```([\w+-]*)[^\n]*\n(.*?)```', re.S)
INLINE_CODE = re.compile(r'`([^`\n]+)`')
LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)')
# ** only: Claude writes bold that way, and __ in a chat is nearly always a dunder
# or a snake_case name (__init__, a__b__c) rather than emphasis.
BOLD = re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', re.S)
ITALIC = re.compile(r'(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])')
STRIKE = re.compile(r'~~(?=\S)(.+?)(?<=\S)~~', re.S)
HEADING = re.compile(r'^#{1,6}\s+(.+?)\s*#*$', re.M)
BULLET = re.compile(r'^(\s*)[-*+]\s+', re.M)
RULE = re.compile(r'^\s*([-*_])(\s*\1){2,}\s*$', re.M)

# Stand-ins for code while the rest is converted, so nothing inside code is touched.
# Private-use characters: they cannot occur in what Claude writes.
HOLD = '{}'
HELD = re.compile('(\\d+)')


def to_html(markdown):
    ''' one chunk of Markdown as Telegram HTML '''

    held = []

    def hold(fragment):
        held.append(fragment)
        return HOLD.format(len(held) - 1)

    def fence(match):
        lang, body = match.group(1), html.escape(match.group(2).rstrip('\n'), quote=False)
        attr = f' class="language-{lang}"' if lang else ''
        return hold(f'<pre><code{attr}>{body}</code></pre>')

    text = FENCE.sub(fence, markdown)
    text = INLINE_CODE.sub(lambda m: hold(f'<code>{html.escape(m.group(1), quote=False)}</code>'), text)
    text = html.escape(text, quote=False)

    # Held like code, so the emphasis rules below cannot reach into a URL. The text
    # is already escaped; only the quote, which would end the attribute, is left.
    text = LINK.sub(lambda m: hold(f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>'), text)
    text = HEADING.sub(r'<b>\1</b>', text)
    text = RULE.sub('──────────', text)
    text = BULLET.sub(r'\1• ', text)
    text = BOLD.sub(r'<b>\1</b>', text)
    text = ITALIC.sub(lambda m: f'<i>{m.group(1) or m.group(2)}</i>', text)
    text = STRIKE.sub(r'<s>\1</s>', text)
    text = _quotes(text)

    return HELD.sub(lambda m: held[int(m.group(1))], text)


def _quotes(text):
    ''' runs of "> " lines as one blockquote (the ">" is already escaped to &gt;) '''

    out, run = [], []

    for line in text.split('\n'):
        if line.startswith('&gt; ') or line == '&gt;':
            run.append(line[5:])
            continue

        if run:
            out.append('<blockquote>' + '\n'.join(run) + '</blockquote>')
            run = []

        out.append(line)

    if run:
        out.append('<blockquote>' + '\n'.join(run) + '</blockquote>')

    return '\n'.join(out)


def split(markdown, limit=CHUNK):
    ''' Markdown cut into pieces of at most limit characters

    Cut at a paragraph break if there is one in reach, else a line break, else
    hard. A cut inside a code block closes the fence at the end of one piece and
    reopens it at the start of the next, so both halves still render as code.
    '''

    pieces = []
    text = markdown.strip()

    while len(text) > limit:
        window = text[:limit]
        cut = window.rfind('\n\n')

        if cut < limit // 2:
            cut = window.rfind('\n')

        if cut < limit // 2:
            cut = limit

        piece, text = text[:cut].rstrip(), text[cut:].lstrip('\n')

        if piece.count('```') % 2:
            opening = re.findall(r'```([\w+-]*)', piece)[-1]
            piece += '\n```'
            text = f'```{opening}\n' + text

        pieces.append(piece)

    if text:
        pieces.append(text)

    return pieces
