import unittest

from pydantic import ValidationError

from hello_agents import Config, ConfigurationError


class TestConfig(unittest.TestCase):
    def test_uses_safe_defaults(self):
        config = Config()

        self.assertEqual(config.default_model, "deepseek-chat")
        self.assertEqual(config.default_provider, "auto")
        self.assertEqual(config.temperature, 0.7)
        self.assertIsNone(config.max_tokens)
        self.assertEqual(config.request_timeout_seconds, 60)
        self.assertFalse(config.debug)
        self.assertEqual(config.log_level, "INFO")
        self.assertEqual(config.max_history_length, 100)

    def test_from_fake_environment_converts_values(self):
        fake_environment = {
            "LLM_MODEL_ID": " fake-model ",
            "LLM_PROVIDER": "fake-provider",
            "LLM_TEMPERATURE": "0.2",
            "LLM_MAX_TOKENS": "512",
            "LLM_TIMEOUT": "30",
            "DEBUG": "true",
            "LOG_LEVEL": "warning",
            "MAX_HISTORY_LENGTH": "12",
        }

        config = Config.from_env(fake_environment)

        self.assertEqual(config.default_model, "fake-model")
        self.assertEqual(config.default_provider, "fake-provider")
        self.assertEqual(config.temperature, 0.2)
        self.assertEqual(config.max_tokens, 512)
        self.assertEqual(config.request_timeout_seconds, 30)
        self.assertTrue(config.debug)
        self.assertEqual(config.log_level, "WARNING")
        self.assertEqual(config.max_history_length, 12)

    def test_direct_validation_rejects_invalid_values_and_unknown_fields(self):
        invalid_values = (
            {"temperature": -0.1},
            {"temperature": 2.1},
            {"max_tokens": 0},
            {"request_timeout_seconds": 0},
            {"max_history_length": 0},
            {"log_level": "TRACE"},
            {"default_model": "   "},
            {"unexpected_setting": True},
        )

        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    Config(**values)

    def test_assignment_is_validated(self):
        config = Config()

        with self.assertRaises(ValidationError):
            config.temperature = 3

        self.assertEqual(config.temperature, 0.7)

    def test_from_env_wraps_invalid_environment_as_framework_error(self):
        with self.assertRaises(ConfigurationError) as context:
            Config.from_env({"LLM_TEMPERATURE": "not-a-number"})

        self.assertIsInstance(context.exception.__cause__, ValidationError)

    def test_model_dump_returns_normalized_configuration(self):
        config = Config(log_level="debug", max_tokens=256)

        self.assertEqual(
            config.model_dump(),
            {
                "default_model": "deepseek-chat",
                "default_provider": "auto",
                "temperature": 0.7,
                "max_tokens": 256,
                "request_timeout_seconds": 60,
                "debug": False,
                "log_level": "DEBUG",
                "max_history_length": 100,
            },
        )


if __name__ == "__main__":
    unittest.main()
