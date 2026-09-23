"""从这里开始学习：先看状态，再看节点，最后看它们如何连成图。"""

from typing import Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from personal_workbench.tools import count_characters


class WorkbenchState(TypedDict):
    """State：流程共用的数据。输入时只需要 text，其余由节点填写。"""

    text: str
    character_count: NotRequired[int]
    reply: NotRequired[str]


def say_hello(state: WorkbenchState) -> dict[str, str]:
    """Node：读入状态，返回需要更新的字段。试着修改下面这句回复。"""
    return {"reply": "你好！个人工作台的最小流程已经跑通。"}


def analyze_text(state: WorkbenchState) -> dict[str, int]:
    """节点调用普通函数；此时由我们指定工具，不由模型选择工具。"""
    return {"character_count": count_characters(state["text"])}


def reply_with_count(state: WorkbenchState) -> dict[str, str]:
    """读取上一个节点留下的结果，生成可读回复。"""
    return {
        "reply": f"收到：{state['text']}\n"
        f"这段文本有 {state['character_count']} 个非空白字符（包含标点）。"
    }


def build_graph(mode: Literal["hello", "stats"] = "hello") -> CompiledStateGraph:
    """搭建并 compile：把流程定义变成可以 invoke / stream 的对象。"""
    builder = StateGraph(WorkbenchState)

    if mode == "hello":
        builder.add_node("say_hello", say_hello)
        # Edge：规定执行顺序。START 和 END 是框架提供的起止标记。
        builder.add_edge(START, "say_hello")
        builder.add_edge("say_hello", END)
    elif mode == "stats":
        builder.add_node("analyze_text", analyze_text)
        builder.add_node("reply_with_count", reply_with_count)
        builder.add_edge(START, "analyze_text")
        builder.add_edge("analyze_text", "reply_with_count")
        builder.add_edge("reply_with_count", END)
    else:
        raise ValueError(f"不支持的模式：{mode}")

    return builder.compile()
