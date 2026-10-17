"""支持可选文本工具调用循环的基础对话 Agent。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Literal

from ..core.agent import Agent
from ..core.config import Config
from ..core.context import BaseContextProvider
from ..core.exceptions import AgentError, ConfigurationError, HelloAgentsError
from ..core.llm import HelloAgentsLLM
from ..core.message import Message
from ..core.request_budget import BaseRequestBudget
from ..tools.registry import ToolRegistry


_TOOL_CALL_PATTERN = re.compile(
    r"\[TOOL_CALL:(?P<name>[A-Za-z_][A-Za-z0-9_]*):(?P<arguments>.+)\]",
    re.DOTALL,
)
ToolCallMode = Literal["text", "native"]


class SimpleAgent(Agent):
    """使用系统提示词、对话历史和可选工具完成多轮对话。"""

    MAX_TOOL_ITERATIONS_LIMIT = 20

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: str | None = None,
        config: Config | None = None,
        *,
        tool_registry: ToolRegistry | None = None,
        max_tool_iterations: int = 5,
        tool_call_mode: ToolCallMode = "text",
        context_provider: BaseContextProvider | None = None,
        request_budget: BaseRequestBudget | None = None,
    ) -> None:
        if tool_registry is not None and not isinstance(
            tool_registry,
            ToolRegistry,
        ):
            raise ConfigurationError("tool_registry 必须是 ToolRegistry 或 None。")
        if (
            isinstance(max_tool_iterations, bool)
            or not isinstance(max_tool_iterations, int)
            or not 1
            <= max_tool_iterations
            <= self.MAX_TOOL_ITERATIONS_LIMIT
        ):
            raise ConfigurationError(
                "max_tool_iterations 必须是 1 到 "
                f"{self.MAX_TOOL_ITERATIONS_LIMIT} 之间的整数。"
            )
        if (
            not isinstance(tool_call_mode, str)
            or tool_call_mode not in {"text", "native"}
        ):
            raise ConfigurationError("tool_call_mode 必须是 'text' 或 'native'。")

        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            context_provider=context_provider,
            request_budget=request_budget,
        )
        self.tool_registry = tool_registry
        self.max_tool_iterations = max_tool_iterations
        self.tool_call_mode = tool_call_mode

    def _execute(self, input_text: str, **kwargs: Any) -> str:
        """构造上下文、调用 LLM，并在成功后保存本轮对话。"""
        if self.tool_registry is None or len(self.tool_registry) == 0:
            return self._execute_without_tools(input_text, **kwargs)
        if self.tool_call_mode == "native":
            return self._execute_with_native_tools(input_text, **kwargs)
        return self._execute_with_text_tools(input_text, **kwargs)

    def _execute_with_text_tools(self, input_text: str, **kwargs: Any) -> str:
        """使用严格文本协议完成工具调用循环。"""
        if self.tool_registry is None:
            raise AgentError("工具注册表不可用。")

        messages = self._build_tool_messages(input_text)
        tool_iterations = 0

        while True:
            response = self._invoke_llm(messages, **kwargs)
            self._validate_response(response)

            tool_call = self._parse_tool_call(response)
            if tool_call is None:
                self._save_turn(input_text, response)
                return response

            if tool_iterations >= self.max_tool_iterations:
                raise AgentError(
                    f"Agent '{self.name}' 达到最大工具调用次数 "
                    f"{self.max_tool_iterations}，仍未生成最终回答。"
                )

            tool_name, parameters = tool_call
            observation = self.tool_registry.execute_tool(
                tool_name,
                parameters,
            )
            tool_iterations += 1
            messages.extend(
                [
                    Message(role="assistant", content=response),
                    Message(
                        role="user",
                        content=f"[OBSERVATION:{tool_name}]\n{observation}",
                    ),
                ]
            )

    def _execute_with_native_tools(self, input_text: str, **kwargs: Any) -> str:
        """使用 OpenAI-compatible 原生 Function Calling 完成工具循环。"""
        if self.tool_registry is None:
            raise AgentError("工具注册表不可用。")

        messages: list[Any] = self._build_messages(input_text)
        schemas = self.tool_registry.to_openai_schemas()
        tool_iterations = 0

        while True:
            response = self._invoke_llm_with_tools(
                messages,
                schemas,
                **kwargs,
            )
            if not response.tool_calls:
                content = response.content
                self._validate_response(content)
                if not isinstance(content, str):
                    raise AgentError(
                        f"Agent '{self.name}' 收到了空的模型响应。"
                    )
                self._save_turn(input_text, content)
                return content

            next_iteration_count = tool_iterations + len(response.tool_calls)
            if next_iteration_count > self.max_tool_iterations:
                raise AgentError(
                    f"Agent '{self.name}' 达到最大工具调用次数 "
                    f"{self.max_tool_iterations}，仍未生成最终回答。"
                )

            messages.append(response.to_assistant_message())
            for tool_call in response.tool_calls:
                observation = self.tool_registry.execute_tool(
                    tool_call.name,
                    tool_call.arguments,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": observation,
                    }
                )
            tool_iterations = next_iteration_count

    def _execute_without_tools(self, input_text: str, **kwargs: Any) -> str:
        """保持没有可用工具时原有的一次模型调用路径。"""
        messages = self._build_messages(input_text)
        response = self._invoke_llm(messages, **kwargs)

        self._validate_response(response)

        self._save_turn(input_text, response)
        return response

    def _build_tool_messages(self, input_text: str) -> list[Message]:
        """构造带工具说明的临时上下文，不修改长期历史。"""
        if self.tool_registry is None:
            raise AgentError("工具注册表不可用。")

        schemas = json.dumps(
            self.tool_registry.to_openai_schemas(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        tool_instructions = (
            "你可以按需使用以下工具。\n"
            f"{self.tool_registry.get_tools_description()}\n"
            f"工具参数 Schema：{schemas}\n"
            "调用工具时，必须只输出下面格式的一行文本，并传入严格的 JSON 对象：\n"
            "[TOOL_CALL:<工具名>:<JSON对象>]\n"
            "示例：[TOOL_CALL:calculator:{\"expression\":\"2 + 3 * 4\"}]\n"
            "收到 [OBSERVATION:<工具名>] 后，请基于结果继续推理；如果无需再调用工具，"
            "直接输出给用户的最终回答，不要包裹工具调用标记。"
        )
        combined_system_prompt = tool_instructions
        if self.system_prompt:
            combined_system_prompt = f"{self.system_prompt}\n\n{tool_instructions}"

        messages = self._build_system_messages(combined_system_prompt)
        messages.extend(self._history_for_request())
        messages.append(Message(role="user", content=input_text))
        return messages

    def _parse_tool_call(
        self,
        response: str,
    ) -> tuple[str, dict[str, Any]] | None:
        """严格解析完整的工具调用标记；普通文本视为最终回答。"""
        stripped_response = response.strip()
        if not stripped_response.startswith("[TOOL_CALL:"):
            return None

        match = _TOOL_CALL_PATTERN.fullmatch(stripped_response)
        if match is None:
            raise AgentError("模型返回的工具调用格式无效。")

        try:
            parameters = json.loads(match.group("arguments"))
        except json.JSONDecodeError as error:
            raise AgentError("模型返回的工具调用参数不是有效 JSON。") from error

        if not isinstance(parameters, dict):
            raise AgentError("模型返回的工具调用参数必须是 JSON 对象。")
        return match.group("name"), parameters

    def _validate_response(self, response: Any) -> None:
        """保证模型的每次文本响应都满足 Agent 的基础约定。"""
        if not isinstance(response, str) or not response.strip():
            raise AgentError(f"Agent '{self.name}' 收到了空的模型响应。")

    def stream_run(self, input_text: str, **kwargs: Any) -> Iterator[str]:
        """流式返回模型文本；只有流正常结束后才保存本轮历史。"""
        if not isinstance(input_text, str) or not input_text.strip():
            raise AgentError("Agent 输入必须是非空字符串。")

        with self._context_scope(input_text):
            messages = self._build_messages(input_text)
            chunks: list[str] = []

            try:
                for chunk in self._stream_llm(messages, **kwargs):
                    if not isinstance(chunk, str):
                        raise AgentError("流式模型响应必须是字符串。")
                    if chunk:
                        chunks.append(chunk)
                        yield chunk
            except HelloAgentsError:
                raise
            except Exception as error:
                raise AgentError(
                    f"Agent '{self.name}' 流式运行失败。"
                ) from error

            full_response = "".join(chunks)
            if not full_response.strip():
                raise AgentError(f"Agent '{self.name}' 收到了空的流式模型响应。")

            self._save_turn(input_text, full_response)

    def _save_turn(self, input_text: str, response: str) -> None:
        """把一次成功调用作为用户、助手两条消息写入历史。"""
        self.add_message(Message(role="user", content=input_text))
        self.add_message(Message(role="assistant", content=response))


__all__ = ["SimpleAgent", "ToolCallMode"]
