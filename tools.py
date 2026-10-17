import requests
import os
from serpapi import SerpApiClient
import ast
import operator

def get_weather(city: str) -> str:
    """
    通过调用 wttr.in API 查询真实天气。
    """
    url = f"https://wttr.in/{city}?format=j1"

    try:
        response = requests.get(url)
        response.raise_for_status()

        data = response.json()

        current_condition = data["current_condition"][0]
        weather_desc = current_condition["weatherDesc"][0]["value"]
        temp_c = current_condition["temp_C"]

        return f"{city}当前天气：{weather_desc}，气温{temp_c}摄氏度"

    except requests.exceptions.RequestException as e:
        return f"错误：查询天气时遇到网络问题 - {e}"

    except (KeyError, IndexError) as e:
        return f"错误：解析天气数据失败 - {e}"


def get_attraction(city: str, weather: str) -> str:
    """
    根据城市和天气返回模拟的景点推荐。
    这个版本不调用 Tavily，只用于学习 Agent 工作流程。
    """

    return (
        f"根据{city}当前的天气情况（{weather}），"
        f"推荐前往颐和园。"
        f"如果天气较好，适合户外游览昆明湖和园林景观；"
        f"如果天气较冷或有风，可以适当减少户外停留时间。"
    )





def search(query: str) -> str:
    """
    一个基于SerpApi的实战网页搜索引擎工具。
    它会智能地解析搜索结果，优先返回直接答案或知识图谱信息。
    """
    print(f"🔍 正在执行 [SerpApi] 网页搜索: {query}")
    try:
        api_key = os.getenv("SERPAPI_API_KEY")
        if not api_key:
            return "错误:SERPAPI_API_KEY 未在 .env 文件中配置。"

        params = {
            "engine": "google",
            "q": query,
            "api_key": api_key,
            "gl": "cn",  # 国家代码
            "hl": "zh-cn",  # 语言代码
        }

        client = SerpApiClient(params)
        results = client.get_dict()

        # 智能解析:优先寻找最直接的答案
        if "answer_box_list" in results:
            return "\n".join(results["answer_box_list"])
        if "answer_box" in results and "answer" in results["answer_box"]:
            return results["answer_box"]["answer"]
        if "knowledge_graph" in results and "description" in results["knowledge_graph"]:
            return results["knowledge_graph"]["description"]
        if "organic_results" in results and results["organic_results"]:
            # 如果没有直接答案，则返回前三个有机结果的摘要
            snippets = [
                f"[{i + 1}] {res.get('title', '')}\n{res.get('snippet', '')}"
                for i, res in enumerate(results["organic_results"][:3])
            ]
            return "\n\n".join(snippets)

        return f"对不起，没有找到关于 '{query}' 的信息。"

    except Exception as e:
        return f"搜索时发生错误: {e}"

available_tools = {
    "get_weather": get_weather,
    "get_attraction": get_attraction,
    "search": search,
}
# 允许使用的二元运算符
ALLOWED_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


# 允许使用的一元运算符
ALLOWED_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _evaluate_math_node(node):
    """
    递归计算经过 AST 解析的数学表达式。
    """
    if isinstance(node, ast.Expression):
        return _evaluate_math_node(node.body)

    # 普通数字，例如 123、3.14
    if isinstance(node, ast.Constant):
        if type(node.value) not in {
            int,
            float,
        }:
            raise ValueError("表达式中只能包含数字")

        return node.value

    # 二元运算，例如 1 + 2
    if isinstance(node, ast.BinOp):
        operator_type = type(node.op)

        if operator_type not in ALLOWED_BINARY_OPERATORS:
            raise ValueError("包含不支持的二元运算符")

        left_value = _evaluate_math_node(node.left)
        right_value = _evaluate_math_node(node.right)

        # 防止模型生成过大的幂运算
        if operator_type is ast.Pow:
            if abs(right_value) > 100:
                raise ValueError("指数过大，拒绝计算")

        operation = ALLOWED_BINARY_OPERATORS[
            operator_type
        ]

        return operation(
            left_value,
            right_value,
        )

    # 一元运算，例如 -5
    if isinstance(node, ast.UnaryOp):
        operator_type = type(node.op)

        if operator_type not in ALLOWED_UNARY_OPERATORS:
            raise ValueError("包含不支持的一元运算符")

        value = _evaluate_math_node(node.operand)

        operation = ALLOWED_UNARY_OPERATORS[
            operator_type
        ]

        return operation(value)

    raise ValueError("表达式中包含不允许的内容")


def calculate(expression: str) -> str:
    """
    安全计算数学表达式。

    支持：
    +、-、*、/、//、%、**、括号
    以及中文乘号 × 和除号 ÷。
    """
    if not isinstance(expression, str):
        return "错误：expression 必须是字符串"

    expression = expression.strip()

    if not expression:
        return "错误：计算表达式不能为空"

    if len(expression) > 200:
        return "错误：计算表达式过长"

    # 将常见数学符号转换为 Python 运算符
    normalized_expression = (
        expression
        .replace("×", "*")
        .replace("÷", "/")
        .replace("^", "**")
    )

    try:
        syntax_tree = ast.parse(
            normalized_expression,
            mode="eval",
        )

        result = _evaluate_math_node(
            syntax_tree
        )

        return f"计算结果：{result}"

    except ZeroDivisionError:
        return "错误：除数不能为零"

    except (SyntaxError, ValueError) as e:
        return f"错误：无效的数学表达式 - {e}"

    except Exception as e:
        return f"错误：计算失败 - {e}"