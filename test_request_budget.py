import json
import unittest
from copy import deepcopy

from pydantic import ValidationError

from hello_agents import (
    BaseContextProvider, BaseTokenCounter, CalculatorTool, ConfigurationError,
    ContextEngineeringError, LLMError, LLMResponse, LLMToolCall, Message,
    PlanAndSolveAgent, ReActAgent, ReflectionAgent, RequestBudgetConfig,
    RequestBudgetManager, SimpleAgent, ToolRegistry,
)


class CharacterCounter(BaseTokenCounter):
    name = "fake-character"

    def count_tokens(self, text):
        return len(text)


def manager(window=2000, output=50):
    return RequestBudgetManager(
        RequestBudgetConfig(
            context_window=window, output_tokens=output, safety_margin_tokens=0,
            per_message_overhead=0, request_overhead=0,
        ),
        token_counter=CharacterCounter(),
    )


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def call_message(*ids):
    return {
        "role": "assistant", "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": "calculator", "arguments": '{"expression":"1+1"}'}}
            for call_id in ids
        ],
    }


class FakeLLM:
    provider = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _record(self, messages, kwargs, tools=None):
        self.calls.append({"messages": deepcopy(messages), "kwargs": kwargs, "tools": tools})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def invoke(self, messages, **kwargs):
        return self._record(messages, kwargs)

    def invoke_with_tools(self, messages, tools, **kwargs):
        return self._record(messages, kwargs, tools)

    def stream_invoke(self, messages, **kwargs):
        for part in self._record(messages, kwargs):
            yield part


class StaticProvider(BaseContextProvider):
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def build_context(self, query):
        self.calls += 1
        return self.text


def old_history(agent):
    agent.add_message(Message(role="user", content="old question " * 500))
    agent.add_message(Message(role="assistant", content="old answer"))


def api_messages(messages):
    return [message.to_dict() if isinstance(message, Message) else message for message in messages]


class TestRequestBudgetManager(unittest.TestCase):
    def test_counts_complete_payload_and_tool_schema(self):
        budget = manager(window=10000)
        rows = [user("calculate"), call_message("a"),
                {"role": "tool", "tool_call_id": "a", "content": "2"}]
        tools = [{"type": "function", "function": {"name": "calculator"}}]
        prepared = budget.prepare(rows, tools=tools, request_options={"temperature": 0})
        expected = json.dumps(
            {"temperature": 0, "max_tokens": 50, "messages": rows, "tools": tools},
            ensure_ascii=False, separators=(",", ":"),
        )
        self.assertEqual(prepared.report.estimated_input_tokens, len(expected))
        self.assertGreater(prepared.report.tool_schema_tokens, 0)
        self.assertEqual(prepared.messages, rows)

    def test_removes_oldest_complete_turn_without_mutating_input(self):
        rows = [user("old" * 1000), assistant("old answer"),
                user("recent"), assistant("recent answer"), user("current")]
        original = deepcopy(rows)
        prepared = manager(window=500).prepare(rows, history_indices=[0, 1, 2, 3])
        self.assertEqual(prepared.messages, rows[2:])
        self.assertEqual(prepared.report.dropped_history_turns, 1)
        self.assertEqual(prepared.report.dropped_history_messages, 2)
        self.assertEqual(rows, original)

    def test_parallel_historical_calls_are_removed_together(self):
        rows = [user("old" * 1000), call_message("a", "b"),
                {"role": "tool", "tool_call_id": "a", "content": "2"},
                {"role": "tool", "tool_call_id": "b", "content": "3"},
                assistant("done"), user("current")]
        prepared = manager(window=300).prepare(rows, history_indices=range(5))
        self.assertEqual(prepared.messages, [rows[-1]])
        self.assertEqual(prepared.report.dropped_history_messages, 5)

    def test_context_is_removed_whole_after_history(self):
        rows = [{"role": "system", "content": "policy"},
                {"role": "system", "content": "context " * 500},
                user("old"), assistant("old answer"), user("current")]
        prepared = manager(window=350).prepare(
            rows, history_indices=[2, 3], context_indices=[1],
        )
        self.assertEqual(prepared.messages, [rows[0], rows[-1]])
        self.assertEqual(prepared.report.dropped_context_messages, 1)
        self.assertEqual(prepared.report.dropped_history_turns, 1)

    def test_system_policy_and_current_tool_result_cannot_be_trimmed(self):
        for rows in (
            [{"role": "system", "content": "rule" * 500}, user("current")],
            [user("current"), call_message("a"),
             {"role": "tool", "tool_call_id": "a", "content": "x" * 5000}],
        ):
            with self.subTest(roles=[row["role"] for row in rows]):
                with self.assertRaisesRegex(ContextEngineeringError, "超过预算"):
                    manager(window=500).prepare(rows)

    def test_malformed_tool_chains_are_rejected(self):
        cases = [
            [user("q"), {"role": "tool", "tool_call_id": "a", "content": "2"}],
            [user("q"), call_message("a")],
            [user("q"), call_message("a", "a")],
            [user("q"), call_message("a"),
             {"role": "tool", "tool_call_id": "b", "content": "2"}],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(ContextEngineeringError):
                    manager().prepare(rows)

    def test_partial_history_annotation_cannot_orphan_tool_result(self):
        rows = [user("old"), call_message("a"),
                {"role": "tool", "tool_call_id": "a", "content": "2"},
                user("current")]
        with self.assertRaises(ContextEngineeringError):
            manager(window=250).prepare(rows, history_indices=[0, 1])

    def test_config_and_output_reserve_reject_invalid_values(self):
        for options in ({"context_window": 128}, {"output_tokens": True},
                        {"per_message_overhead": -1}):
            with self.assertRaises(ValidationError):
                RequestBudgetConfig(**options)
        for output in (True, 0, -1, 2000):
            with self.assertRaises(ContextEngineeringError):
                manager().prepare([user("q")], output_tokens=output)

    def test_invalid_indices_and_payload_overrides_are_rejected(self):
        for options in (
            {"history_indices": [True]}, {"history_indices": [-1]},
            {"history_indices": [5]}, {"history_indices": [0]},
            {"context_indices": [0]}, {"request_options": {"messages": []}},
            {"request_options": {"temperature": float("nan")}},
        ):
            with self.subTest(options=options):
                with self.assertRaises(ContextEngineeringError):
                    manager().prepare([user("current")], **options)

    def test_bad_counter_fails_without_leaking_detail(self):
        class BrokenCounter(BaseTokenCounter):
            def count_tokens(self, text):
                raise RuntimeError("fake-secret-detail")

        budget = RequestBudgetManager(token_counter=BrokenCounter())
        with self.assertRaises(ContextEngineeringError) as caught:
            budget.prepare([user("current")])
        self.assertNotIn("fake-secret-detail", str(caught.exception))


class TestAgentRequestBudget(unittest.TestCase):
    def test_normal_request_trims_copy_and_preserves_long_term_history(self):
        llm = FakeLLM(["answer"])
        agent = SimpleAgent("simple", llm, request_budget=manager(window=500))
        old_history(agent)
        original = agent.get_history()
        self.assertEqual(agent.run("current"), "answer")
        self.assertEqual(api_messages(llm.calls[0]["messages"]), [user("current")])
        self.assertEqual(agent.get_history()[:2], original)
        self.assertEqual(len(agent.get_history()), 4)
        self.assertEqual(agent.last_budget_report.dropped_history_turns, 1)
        report = agent.last_budget_report
        report.dropped_history_turns = 99
        self.assertEqual(agent.last_budget_report.dropped_history_turns, 1)

    def test_output_limit_matches_default_llm_default_and_run_override(self):
        for llm_limit, override, expected in ((None, None, 50), (80, None, 80), (80, 30, 30)):
            llm = FakeLLM(["answer"])
            llm.max_tokens = llm_limit
            agent = SimpleAgent("simple", llm, request_budget=manager())
            options = {} if override is None else {"max_tokens": override}
            agent.run("question", **options)
            self.assertEqual(llm.calls[0]["kwargs"]["max_tokens"], expected)
            self.assertEqual(agent.last_budget_report.reserved_output_tokens, expected)

    def test_optional_context_removed_and_references_cleared_after_run(self):
        provider = StaticProvider("context " * 1000)
        llm = FakeLLM(["answer"])
        agent = SimpleAgent(
            "simple", llm, system_prompt="policy", context_provider=provider,
            request_budget=manager(window=400),
        )
        agent.run("current")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(api_messages(llm.calls[0]["messages"]),
                         [{"role": "system", "content": "policy"}, user("current")])
        self.assertEqual(agent.last_budget_report.dropped_context_messages, 1)
        self.assertEqual(agent._active_context, "")
        self.assertEqual(agent._request_context_messages, [])

    def test_overflow_prevents_model_call_and_history_commit(self):
        llm = FakeLLM(["unused"])
        agent = SimpleAgent("simple", llm, request_budget=manager(window=300))
        old_history(agent)
        original = agent.get_history()
        with self.assertRaises(ContextEngineeringError):
            agent.run("current" * 500)
        self.assertEqual(llm.calls, [])
        self.assertEqual(agent.get_history(), original)
        self.assertIsNone(agent.last_budget_report)

    def test_native_parallel_tool_chain_remains_complete_after_trimming(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM([
            LLMResponse(tool_calls=[
                LLMToolCall(id="a", name="calculator", arguments={"expression": "1+1"}),
                LLMToolCall(id="b", name="calculator", arguments={"expression": "2+2"}),
            ]),
            LLMResponse(content="done"),
        ])
        agent = SimpleAgent("native", llm, tool_registry=registry,
                            tool_call_mode="native", request_budget=manager())
        old_history(agent)
        self.assertEqual(agent.run("calculate"), "done")
        rows = api_messages(llm.calls[1]["messages"])
        self.assertEqual([row["role"] for row in rows], ["user", "assistant", "tool", "tool"])
        self.assertEqual([row["tool_call_id"] for row in rows if row["role"] == "tool"], ["a", "b"])
        self.assertEqual(len(llm.calls[1]["tools"]), 1)
        self.assertGreater(agent.last_budget_report.tool_schema_tokens, 0)

    def test_large_native_tool_result_stops_before_next_llm_call(self):
        registry = ToolRegistry()
        executions = []

        def large_result(value):
            executions.append(value)
            return "x" * 10000

        registry.register_function("large", "large output", large_result)
        llm = FakeLLM([LLMResponse(tool_calls=[
            LLMToolCall(id="a", name="large", arguments={"input": "test"}),
        ])])
        agent = SimpleAgent("native", llm, tool_registry=registry,
                            tool_call_mode="native", request_budget=manager())
        with self.assertRaises(ContextEngineeringError):
            agent.run("query")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(executions, ["test"])
        self.assertEqual(agent.get_history(), [])

    def test_text_tool_loop_checks_growing_observation_prompt(self):
        registry = ToolRegistry()
        registry.register_function("large", "large output", lambda value: "x" * 10000)
        llm = FakeLLM(['[TOOL_CALL:large:{"input":"test"}]'])
        agent = SimpleAgent("text", llm, tool_registry=registry, request_budget=manager())
        with self.assertRaises(ContextEngineeringError):
            agent.run("query")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(agent.get_history(), [])

    def test_stream_trims_history_and_only_commits_on_completion(self):
        llm = FakeLLM([["A", "B"]])
        agent = SimpleAgent("stream", llm, request_budget=manager(window=400))
        old_history(agent)
        self.assertEqual("".join(agent.stream_run("current")), "AB")
        self.assertEqual(len(llm.calls[0]["messages"]), 1)
        self.assertEqual(len(agent.get_history()), 4)
        self.assertEqual(llm.calls[0]["kwargs"]["max_tokens"], 50)

    def test_closing_stream_clears_context_without_committing_turn(self):
        llm = FakeLLM([["A", "B"]])
        agent = SimpleAgent("stream", llm, context_provider=StaticProvider("reference"),
                            request_budget=manager())
        stream = agent.stream_run("current")
        self.assertEqual(next(stream), "A")
        stream.close()
        self.assertEqual(agent.get_history(), [])
        self.assertEqual(agent._active_context, "")
        self.assertEqual(agent._request_context_messages, [])

    def test_all_reasoning_paradigms_use_budget_at_every_phase(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        cases = [
            (ReActAgent, [
                'Thought: calculate\nAction: calculator:{"expression":"1+1"}',
                "Thought: finish\nAction: Finish[done]",
            ], {"tool_registry": registry}),
            (ReflectionAgent, ["draft", "STATUS: NEEDS_IMPROVEMENT", "done"], {"max_iterations": 1}),
            (PlanAndSolveAgent, ['["first", "second"]', "intermediate", "done"], {}),
        ]
        for cls, responses, options in cases:
            with self.subTest(agent=cls.__name__):
                llm = FakeLLM(responses)
                agent = cls("reasoning", llm, request_budget=manager(), **options)
                old_history(agent)
                self.assertEqual(agent.run("current"), "done")
                for call in llm.calls:
                    self.assertEqual(call["kwargs"]["max_tokens"], 50)
                    self.assertNotIn("old question", str(api_messages(call["messages"])))

    def test_late_reasoning_overflow_preserves_original_history(self):
        cases = [
            (ReflectionAgent, ["x" * 10000], {}),
            (PlanAndSolveAgent, ['["first", "second"]', "x" * 10000], {}),
        ]
        for cls, responses, options in cases:
            with self.subTest(agent=cls.__name__):
                llm = FakeLLM(responses)
                agent = cls("reasoning", llm, request_budget=manager(), **options)
                old_history(agent)
                original = agent.get_history()
                with self.assertRaises(ContextEngineeringError):
                    agent.run("current")
                self.assertEqual(len(llm.calls), len(responses))
                self.assertEqual(agent.get_history(), original)

    def test_configuration_aliases_and_model_failure_keep_history_clean(self):
        with self.assertRaises(ConfigurationError):
            SimpleAgent("bad", FakeLLM([]), request_budget="bad")
        llm = FakeLLM([LLMError("fake failure")])
        agent = SimpleAgent("simple", llm, request_budget=manager())
        with self.assertRaises(ConfigurationError):
            agent.run("query", max_completion_tokens=30)
        self.assertEqual(llm.calls, [])
        with self.assertRaises(LLMError):
            agent.run("query")
        self.assertEqual(agent.get_history(), [])


if __name__ == "__main__":
    unittest.main()
