"""给模型看的资料工具和真实工具目录。create_note 仅生成提案，真正写入由图中的独立节点完成。"""

from langchain_core.tools import tool

from personal_workbench.workspace import Workspace


def _all_file_tools(workspace: Workspace | None):
    """构造工具不执行工具；目录和运行时共享签名与定义。"""
    @tool
    def list_files() -> dict:
        """列出允许读取的资料目录中的 Markdown/TXT 文件；结果可能截断。"""
        return workspace.list_files()

    @tool
    def search_files(query: str) -> dict:
        """按一个关键词做字面搜索，返回文件名、行号、摘要及来源版本。没有命中时换词或先列出文件。"""
        return workspace.search_files(query)

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 80) -> dict:
        """读取资料的指定行，最多 120 行。path 必须是 list_files 返回的相对路径。引用格式为 [文件名:L起始-L结束]。"""
        return workspace.read_file(path, start_line, end_line)

    @tool
    def create_note(filename: str, content: str) -> dict:
        """用户要求保存时，提交一篇新笔记或更新当前会话的成果。filename 为 .md/.txt 文件名。内容应带来源引用；覆盖必须等待用户确认。"""
        return workspace.prepare_note(filename, content)

    @tool
    def create_artifact(filename: str, content: str) -> dict:
        """用户要求生成文件时，提交可预览成果。支持单文件 .html/.css/.js/.json/.md/.txt，最多 1 MiB；HTML 应自包含 CSS/JS。覆盖已有文件必须等待用户确认。"""
        return workspace.prepare_artifact(filename, content)

    @tool
    def read_outline(source_id: str, offset: int = 0) -> dict:
        """读取当前 PageIndex 知识库的文档目录树和起始页码。摘要用于定位，回答前必须 read_page。offset 用返回的 next_offset 翻页。"""
        return workspace.read_outline(source_id, offset)

    @tool
    def read_page(source_id: str, page: int, part: int = 1) -> dict:
        """读取 PDF 的一页原文和 citation。page 为从 1 开始的 PDF 物理页码，长页用 next_part 继续。"""
        return workspace.read_page(source_id, page, part)

    return [list_files, search_files, read_file, read_outline, read_page, create_note, create_artifact]


def build_file_tools(workspace: Workspace):
    list_files, search_files, read_file, read_outline, read_page, create_note, create_artifact = _all_file_tools(workspace)
    if getattr(workspace, "general_chat", False):
        create_note.description = "用户要求保存时，提交笔记或当前会话成果；覆盖已有文件必须等待用户确认。"
        return [create_note, create_artifact]
    if hasattr(workspace, "library"):
        list_files.description = "列出当前资料库中的文档及 source_id，不会访问其他知识库。"
        read_file.description = "读取当前资料库的原文片段。path 使用 list_files 返回的 source_id 或搜索得到的 chunk_id；引用使用工具返回的 citation。"
        search_files.description = (
            "在当前知识库中做向量或混合检索。query 可使用自然语言问题；返回原文片段和 citation。命中不保证证据充分。"
            if hasattr(workspace.library, "center") else
            "在当前资料库中按关键词检索原文，返回片段和 citation。无命中时可换词。"
        )
    if hasattr(workspace, "read_outline"):
        if getattr(workspace, "mixed_knowledge", False):
            list_files.description = "列出当前选中的知识库文档、source_id、kb_id、kb_name 和 engine。"
            search_files.description = "检索选中的所有 LlamaIndex 知识库，返回原文及 citation；返回的 PageIndex 文档需另用 read_outline/read_page 阅读。"
            return [list_files, search_files, read_file, read_outline, read_page, create_note, create_artifact]
        return [list_files, read_outline, read_page, create_note, create_artifact]
    return [list_files, search_files, read_file, create_note, create_artifact]


TOOL_INFO = {
    "list_files": ("查看资料目录", "列出当前资料范围内的文档。", "选择知识库或使用旧资料库会话时可用。"),
    "search_files": ("检索资料", "按问题检索原文，找到相关资料和引用。", "用于 LlamaIndex 或旧资料库；PageIndex 文档通过目录和页面阅读。"),
    "read_file": ("阅读资料片段", "阅读检索到的原文片段及来源。", "用于 LlamaIndex 或旧资料库；PageIndex 请使用阅读文档页面。"),
    "read_outline": ("查看文档目录", "浏览 PageIndex 文档树，定位需要阅读的页面。", "选中 PageIndex 知识库时可使用。"),
    "read_page": ("阅读文档页面", "阅读 PDF 页面原文，获取可核对的引用。", "选中 PageIndex 知识库时可使用。"),
    "create_note": ("准备笔记草稿", "提交笔记草稿，由保存流程写入成果；覆盖已有文件需要确认。", "普通对话和知识库对话均可用。"),
    "create_artifact": ("生成成果文件", "生成可预览的 HTML、CSS、JavaScript、JSON、Markdown 或文本成果；覆盖需要确认。", "普通对话和技能任务均可用。"),
}


from personal_workbench.web_tools import TOOL_INFO as WEB_TOOL_INFO
TOOL_INFO.update(WEB_TOOL_INFO)
from personal_workbench.decision_tools import TOOL_INFO as DECISION_TOOL_INFO
TOOL_INFO.update(DECISION_TOOL_INFO)
TOOL_INFO.update({
    'skills_list':('列出技能','列出已启用技能的名称、简介和兼容状态。','技能正文按需通过 skill_view 加载。'),
    'skill_view':('读取技能','加载完整技能说明或技能目录内的参考文件。','可按名称发现技能；手动选择的技能使用冻结版本。'),
    'search_memory':('搜索个人记忆','检索当前 LangMem 空间和范围中的档案与事实。','仅 LangMem 且开启读取时可用。'),
    'manage_memory':('即时管理记忆','按本轮明确要求记住、修改或停用个人记忆；锁定修改进入待处理建议。','仅 LangMem 且开启即时写入时可用。'),
    'run_skill_script':('运行技能脚本','在独立操作系统沙箱内运行已授权的 Python 技能脚本。','仅选择了已授权脚本技能时可用。'),
    'terminal':('本地终端','执行本机 Shell 命令；危险命令会暂停并等待确认。','用于用户明确要求的电脑操作、开发和技能安装任务。'),
})


def builtin_tool_catalog():
    from personal_workbench.skill_runtime import resource_tool_catalog
    from personal_workbench.skill_sandbox import script_tool_catalog
    from personal_workbench.web_tools import tool_catalog
    from personal_workbench.decision_tools import tool_catalog as decision_tool_catalog
    from personal_workbench.memory.hot_path import build_memory_tools, NAMES
    catalog = [{"id": tool.name, "name": TOOL_INFO[tool.name][0],
             "description": TOOL_INFO[tool.name][1], "applicability": TOOL_INFO[tool.name][2],
             "source": "builtin", "availability": "contextual", "schema": (tool.tool_call_schema if tool.name in NAMES else tool.args_schema).model_json_schema()}
            for tool in [*_all_file_tools(None), *build_memory_tools(None,None,'',NAMES)]] + [resource_tool_catalog(),script_tool_catalog()] + tool_catalog() + decision_tool_catalog()
    from personal_workbench.terminal_tools import tool_catalog as terminal_catalog
    catalog.append(terminal_catalog())
    return catalog
