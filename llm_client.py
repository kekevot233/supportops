import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


class OpenAICompatibleClient:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
    ):
        self.model = model or os.getenv("LLM_MODEL_ID")
        api_key = api_key or os.getenv("LLM_API_KEY")
        base_url = base_url or os.getenv("LLM_BASE_URL")
        timeout = timeout or int(os.getenv("LLM_TIMEOUT", "60"))

        if not self.model:
            raise ValueError("没有配置 LLM_MODEL_ID")

        if not api_key:
            raise ValueError("没有配置 LLM_API_KEY")

        if not base_url:
            raise ValueError("没有配置 LLM_BASE_URL")

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    def generate(
        self,
        prompt: str,
        system_prompt: str,
    ) -> str:
        """
        使用 system_prompt 和 prompt 调用模型，
        返回完整的文本结果。
        """
        print("正在调用大语言模型...")

        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
                stream=False,
            )

            answer = response.choices[0].message.content or ""

            print("大语言模型响应成功。")
            return answer

        except Exception as e:
            print(f"调用 LLM API 时发生错误：{e}")
            return "错误：调用语言模型服务时出错。"