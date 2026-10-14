''' the environment check: everything wrong is reported at once, and nothing fails open '''

import unittest

from shelly import config


def env(**overrides):
    ''' a complete, valid environment with some values replaced '''

    base = {
        'TELEGRAM_BOT_TOKEN': '123:abc',
        'TELEGRAM_ALLOWED_CHAT_IDS': '1111, -2222',
        'ANTHROPIC_API_KEY': 'sk-ant-test',
        'ANTHROPIC_MODEL': 'claude-opus-5-5',
        'ANTHROPIC_EFFORT': 'medium',
        'MONGODB_URI': 'mongodb://localhost',
    }
    base.update(overrides)

    return {k: v for k, v in base.items() if v is not None}


class Load(unittest.TestCase):

    def test_a_complete_environment(self):
        cfg = config.load(env())

        self.assertEqual(cfg.allowed_chat_ids, frozenset({1111, -2222}))
        self.assertEqual(cfg.model, 'claude-opus-5-5')
        self.assertEqual(cfg.mongodb_db, 'shelly')
        self.assertIsNone(cfg.dashboard_url)

    def test_every_missing_variable_is_named_at_once(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.load(env(TELEGRAM_BOT_TOKEN=None, ANTHROPIC_API_KEY='  ', MONGODB_URI=None))

        message = str(caught.exception)

        for name in ('TELEGRAM_BOT_TOKEN', 'ANTHROPIC_API_KEY', 'MONGODB_URI'):
            self.assertIn(name, message)

    def test_an_empty_allowlist_does_not_mean_everyone(self):
        for raw in (None, '', ' , ,'):
            with self.subTest(raw=raw):
                with self.assertRaises(config.ConfigError):
                    config.load(env(TELEGRAM_ALLOWED_CHAT_IDS=raw))

    def test_a_malformed_chat_id_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.load(env(TELEGRAM_ALLOWED_CHAT_IDS='1111,@someone'))

    def test_effort_defaults_to_medium_and_is_checked(self):
        self.assertEqual(config.load(env(ANTHROPIC_EFFORT=None)).effort, 'medium')
        self.assertEqual(config.load(env(ANTHROPIC_EFFORT='High')).effort, 'high')

        with self.assertRaises(config.ConfigError):
            config.load(env(ANTHROPIC_EFFORT='extreme'))

    def test_secrets_stay_out_of_the_repr(self):
        text = repr(config.load(env()))

        self.assertNotIn('sk-ant-test', text)
        self.assertNotIn('123:abc', text)
        self.assertNotIn('mongodb://', text)


if __name__ == '__main__':
    unittest.main()
