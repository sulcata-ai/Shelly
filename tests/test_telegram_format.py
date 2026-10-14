''' Markdown to Telegram HTML, and cutting long replies

The traps are the things Claude writes that look like formatting and are not:
snake_case names, arithmetic, HTML-ish text, and anything inside code.
'''

import unittest

from shelly.channels.telegram_format import split, to_html


class ToHtml(unittest.TestCase):

    def test_the_common_marks(self):
        self.assertEqual(to_html('**b** *i* _j_ ~~s~~'), '<b>b</b> <i>i</i> <i>j</i> <s>s</s>')

    def test_angle_brackets_and_ampersands_are_escaped(self):
        self.assertEqual(to_html('a < b & c > d'), 'a &lt; b &amp; c &gt; d')

    def test_snake_case_and_arithmetic_are_left_alone(self):
        for plain in ('my_file_name.py', '3 * 4 * 5', '2*x + y*3', 'a__b__c', 'def __init__(self)'):
            with self.subTest(plain=plain):
                self.assertNotIn('<', to_html(plain))

    def test_nothing_inside_code_is_formatted(self):
        self.assertEqual(to_html('`**x** < 1`'), '<code>**x** &lt; 1</code>')

        block = to_html('```python\nif a < b:\n    print("_hi_")\n```')
        self.assertEqual(block, '<pre><code class="language-python">if a &lt; b:\n'
                                '    print("_hi_")</code></pre>')

    def test_links_keep_their_url_whole(self):
        self.assertEqual(to_html('[docs](https://ex.com/a_b_c?x=1&y=2)'),
                         '<a href="https://ex.com/a_b_c?x=1&amp;y=2">docs</a>')

    def test_only_web_links_become_links(self):
        self.assertNotIn('<a', to_html('[x](javascript:alert(1))'))

    def test_headings_bullets_and_quotes(self):
        self.assertEqual(to_html('## Title'), '<b>Title</b>')
        self.assertEqual(to_html('- a\n* b'), '• a\n• b')
        self.assertEqual(to_html('> one\n> two\nafter'), '<blockquote>one\ntwo</blockquote>\nafter')

    def test_the_tags_produced_are_ones_telegram_accepts(self):
        allowed = {'b', 'i', 's', 'code', 'pre', 'a', 'blockquote'}
        text = to_html('# H\n**b** *i* ~~s~~ `c` [l](https://x.y)\n> q\n```\nz\n```')
        import re
        self.assertLessEqual({t for t in re.findall(r'</?([a-z]+)', text)}, allowed)


class Split(unittest.TestCase):

    def test_short_text_is_one_piece(self):
        self.assertEqual(split('hello'), ['hello'])

    def test_every_piece_fits(self):
        text = '\n\n'.join(f'paragraph {i} ' + 'word ' * 50 for i in range(60))
        pieces = split(text, limit=1000)

        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 1000 + 10 for p in pieces))
        self.assertEqual(' '.join(' '.join(pieces).split()), ' '.join(text.split()), 'nothing lost')

    def test_cuts_prefer_paragraphs(self):
        text = 'a' * 600 + '\n\n' + 'b' * 600
        self.assertEqual(split(text, limit=1000), ['a' * 600, 'b' * 600])

    def test_a_cut_inside_code_keeps_both_halves_as_code(self):
        code = '\n'.join(f'line_{i} = {i}' for i in range(200))
        pieces = split(f'```python\n{code}\n```', limit=1000)

        self.assertGreater(len(pieces), 1)

        for piece in pieces:
            self.assertEqual(piece.count('```') % 2, 0, piece[:40])
            self.assertTrue(to_html(piece).startswith('<pre><code class="language-python">'))

    def test_one_huge_line_is_cut_hard(self):
        pieces = split('x' * 2500, limit=1000)
        self.assertEqual([len(p) for p in pieces], [1000, 1000, 500])


if __name__ == '__main__':
    unittest.main()
