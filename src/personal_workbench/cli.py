"""命令行入口：读取输入，运行图，展示结果。"""

import argparse
import json
import sys

from personal_workbench.graph import WorkbenchState, build_graph


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        from personal_workbench.web import main as web_main
        web_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "assistant":
        from personal_workbench.assistant_cli import main as assistant_main

        assistant_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="个人工作台 M0：本地 LangGraph 演示")
    parser.add_argument("text", nargs="?", default="我想用 LangGraph 学习和工作。", help="输入文本")
    parser.add_argument("--mode", choices=("hello", "stats"), default="hello", help="固定回复或字符统计")
    parser.add_argument("--trace", action="store_true", help="展示初始状态、节点更新和最终状态")
    args = parser.parse_args()
    if not args.text.strip():
        parser.error("请输入至少一个非空白字符。")

    graph = build_graph(args.mode)
    initial_state: WorkbenchState = {"text": args.text}

    if args.trace:
        print("初始状态：", json.dumps(initial_state, ensure_ascii=False))
        # 一次执行，同时观察每个节点的更新与完整状态；不会再调用 invoke 重跑。
        for event_type, event in graph.stream(initial_state, stream_mode=["updates", "values"]):
            if event_type == "updates":
                for node_name, update in event.items():
                    print(f"节点 {node_name} 更新：", json.dumps(update, ensure_ascii=False))
            else:
                result = event
                print("当前状态：", json.dumps(result, ensure_ascii=False))
    else:
        # invoke：带着初始状态执行一次完整流程，拿到最终状态。
        result = graph.invoke(initial_state)

    print("\n回复：")
    print(result["reply"])


if __name__ == "__main__":
    main()
