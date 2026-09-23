"""M1 命令行。M0 的原命令保留，用 workbench assistant 进入资料助手。"""

import argparse

from langchain_core.messages import AIMessage

from personal_workbench.assistant_service import open_service
from personal_workbench.settings import Settings


def display(result):
    print(f"\n会话：{result['id']}  模式：{result['mode']}  状态：{result['status']}")
    if result["mode"] == "demo":
        print("【离线固定脚本演示：没有调用模型，不具备语义理解能力】")
    for pending in result["pending"]:
        print(f"\n等待覆盖确认：{pending['filename']}\n{pending['message']}")
        print("\n完整待保存内容：\n" + pending["content"])
        print("\n与原文件的差异：\n" + pending["diff"])
        print(f"\n批准：uv run --locked workbench assistant resume --thread {result['id']} --approve")
        print(f"拒绝：uv run --locked workbench assistant resume --thread {result['id']} --reject")
    state = result["state"]
    messages = state.get("messages", [])
    if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls:
        print("\n回复：\n" + messages[-1].text)
    sources = state.get("sources", [])
    if sources:
        print("\n实际查阅来源（工具记录；正文是否支持结论仍需核对）：")
        for source in sources:
            print(f"  [{source['path']}:L{source['start']}-L{source['end']}]  版本 {source['sha256'][:12]}")
    print(f"\n成果目录：{result['output_dir']}")
    if result["mode"] == "demo":
        print(f"本轮脚本步骤 {state.get('model_calls', 0)} 次，工具 {state.get('tool_calls', 0)} 次；未调用模型，无 API 消耗。")
    else:
        usage = f"至少 {state.get('usage_tokens', 0)}（部分请求未返回用量）" if state.get("usage_unknown") else str(state.get("usage_tokens", 0))
        print(f"本轮模型成功返回 {state.get('model_calls', 0)} 次，工具 {state.get('tool_calls', 0)} 次；报告的 tokens：{usage}")
    if result["next"] and not result["pending"]:
        print(f"待执行步骤：{', '.join(result['next'])}；使用 resume --thread {result['id']} 继续。")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="workbench assistant", description="M1：DeepSeek 本地资料助手")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="向真实模型提问；可用 --thread 继续会话")
    ask.add_argument("text")
    ask.add_argument("--thread")
    ask.add_argument("--trace", action="store_true")
    demo = commands.add_parser("demo", help="不需要密钥的固定流程演示")
    demo.add_argument("--thread")
    demo.add_argument("--trace", action="store_true")
    resume = commands.add_parser("resume", help="恢复错误/停止的任务，或回答覆盖确认")
    resume.add_argument("--thread", required=True)
    choice = resume.add_mutually_exclusive_group()
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--reject", action="store_true")
    resume.add_argument("--trace", action="store_true")
    status = commands.add_parser("status", help="查看已有会话、回复及待批准草稿，不调用模型")
    status.add_argument("--thread", required=True)
    commands.add_parser("sessions", help="列出会话，不调用模型")
    commands.add_parser("check", help="检查配置是否齐全，不输出密钥，不调用模型")
    args = parser.parse_args(argv)

    def event(node, value):
        if node == "session":
            print(f"会话 ID：{value['thread_id']}", flush=True)
        elif getattr(args, "trace", False):
            print(f"完成步骤：{node}", flush=True)
            for message in (value or {}).get("messages", []) if isinstance(value, dict) else []:
                for call in getattr(message, "tool_calls", []):
                    print(f"  模型请求工具：{call['name']}", flush=True)

    try:
        settings = Settings.load()
        if args.command == "check":
            print(f"模型：{settings.model}\n密钥：{'已配置' if settings.api_key.strip() else '未配置'}")
            print(f"资料目录：{settings.notes_dir}\n每轮模型/工具上限：{settings.max_model_calls}/{settings.max_tool_calls}")
            return
        # 缺密钥时不创建没有内容的真实会话。status/demo/reject 不需要密钥。
        if args.command == "ask" and not settings.api_key.strip():
            raise ValueError("请先在项目 .env 填写 DEEPSEEK_API_KEY；也可以运行 assistant demo。")
        with open_service(settings) as service:
            if args.command == "sessions":
                for session in service.sessions():
                    print(f"{session['id']}  {session['mode']}  {session['status']}  {session['title']}")
                return
            if args.command == "ask":
                result = service.ask(args.text, args.thread, on_event=event)
            elif args.command == "demo":
                result = service.ask("运行离线资料工具与保存流程演示", args.thread, demo=True, on_event=event)
            elif args.command == "resume":
                decision = True if args.approve else False if args.reject else None
                result = service.resume(args.thread, decision, on_event=event)
            else:
                result = service.status(args.thread)
            display(result)
    except KeyboardInterrupt:
        print("\n已停止。会话已保留；可用 status 查看、resume 继续。")
        raise SystemExit(130) from None
    except ValueError as exc:
        print(f"\n提示：{exc}")
        raise SystemExit(2) from None
    except Exception as exc:
        # API 错误可能含请求体/服务端回显；不将异常全文或密钥写入终端。
        kind = type(exc).__name__
        print(f"\n任务未完成（{kind}）。请检查网络、密钥、额度、模型名称或目录权限。")
        print("已有会话会保留检查点；使用上面的会话 ID 查看 status，再 resume。")
        raise SystemExit(1) from None
