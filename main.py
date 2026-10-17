import re

from llm_client import OpenAICompatibleClient
from prompts import AGENT_SYSTEM_PROMPT
from tools import available_tools
from llm_client import OpenAICompatibleClient
from dotenv import load_dotenv

load_dotenv()

llm = OpenAICompatibleClient()



# 2. 用户请求
user_prompt = (
    "你好，请帮我查询一下今天北京的天气，"
    "然后根据天气推荐一个合适的旅游景点。"
)


# 3. 保存 Agent 的历史记录
prompt_history = [
    f"用户请求: {user_prompt}"
]


print(f"用户输入: {user_prompt}")
print("=" * 40)


# 4. Agent 主循环
for i in range(5):

    print(f"\n--- 循环 {i + 1} ---")

    # 4.1 把历史记录拼成一个完整字符串
    full_prompt = "\n".join(prompt_history)

    # 4.2 让 LLM 决定下一步做什么
    llm_output = llm.generate(
        prompt=full_prompt,
        system_prompt=AGENT_SYSTEM_PROMPT
    )

    print("\n模型输出:")
    print(llm_output)

    # 把模型输出保存到历史记录
    prompt_history.append(llm_output)

    # 4.3 从模型输出中寻找 Action
    action_match = re.search(
        r"Action:\s*(.*)",
        llm_output,
        re.DOTALL
    )

    if not action_match:

        observation = (
            "错误：没有解析到 Action。"
            "请严格按照 Thought 和 Action 格式输出。"
        )

        observation_str = f"Observation: {observation}"

        print(observation_str)

        prompt_history.append(observation_str)

        continue

    # 获取 Action 后面的内容
    action_str = action_match.group(1).strip()

    print(f"\n解析到的 Action: {action_str}")


    # 4.4 判断任务是否已经完成
    if action_str.startswith("Finish"):

        finish_match = re.match(
            r"Finish\[(.*)\]",
            action_str,
            re.DOTALL
        )

        if finish_match:
            final_answer = finish_match.group(1)

            print("\n任务完成！")
            print("最终答案:")
            print(final_answer)

        else:
            print("Finish 格式解析失败")

        break


    # 4.5 解析工具名称
    tool_match = re.search(
        r"(\w+)\(",
        action_str
    )

    if not tool_match:

        observation = "错误：无法解析工具名称。"

        observation_str = f"Observation: {observation}"

        print(observation_str)

        prompt_history.append(observation_str)

        continue

    tool_name = tool_match.group(1)


    # 4.6 解析括号中的参数
    args_match = re.search(
        r"\((.*)\)",
        action_str,
        re.DOTALL
    )

    if not args_match:

        observation = "错误：无法解析工具参数。"

        observation_str = f"Observation: {observation}"

        print(observation_str)

        prompt_history.append(observation_str)

        continue

    args_str = args_match.group(1)


    # 例如：
    # city="北京", weather="Sunny"
    #
    # 转换成：
    # {
    #     "city": "北京",
    #     "weather": "Sunny"
    # }

    kwargs = dict(
        re.findall(
            r'(\w+)="([^"]*)"',
            args_str
        )
    )


    print(f"工具名称: {tool_name}")
    print(f"工具参数: {kwargs}")


    # 4.7 真正执行工具
    if tool_name in available_tools:

        try:

            observation = available_tools[tool_name](
                **kwargs
            )

        except Exception as e:

            observation = f"工具执行失败: {e}"

    else:

        observation = (
            f"错误：没有定义工具 '{tool_name}'"
        )


    # 4.8 把工具结果包装成 Observation
    observation_str = (
        f"Observation: {observation}"
    )

    print("\n" + observation_str)
    print("=" * 40)


    # 4.9 把 Observation 放回历史记录
    prompt_history.append(
        observation_str
    )