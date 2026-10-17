import unittest

from hello_agents import (
    AgentError,
    ConfigurationError,
    HelloAgentsError,
    LLMError,
    ToolError,
)
from hello_agents.core import HelloAgentsError as CoreHelloAgentsError


class TestHelloAgentsExceptions(unittest.TestCase):
    def test_specific_exceptions_share_framework_parent(self):
        exception_types = (
            ConfigurationError,
            LLMError,
            AgentError,
            ToolError,
        )

        for exception_type in exception_types:
            with self.subTest(exception_type=exception_type.__name__):
                self.assertTrue(issubclass(exception_type, HelloAgentsError))
                self.assertTrue(issubclass(exception_type, Exception))

    def test_exception_preserves_message(self):
        error = ToolError("Fake 工具执行失败")

        self.assertEqual(str(error), "Fake 工具执行失败")

    def test_parent_can_catch_specific_exception(self):
        with self.assertRaises(HelloAgentsError) as context:
            raise LLMError("Fake 模型调用失败")

        self.assertIsInstance(context.exception, LLMError)

    def test_top_level_and_core_export_same_parent_class(self):
        self.assertIs(HelloAgentsError, CoreHelloAgentsError)


if __name__ == "__main__":
    unittest.main()
