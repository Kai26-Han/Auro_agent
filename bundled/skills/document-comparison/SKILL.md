---
name: document-comparison
description: 对比所选知识库中的资料，按共同点、差异、证据与待确认问题组织回答。
allowed-tools: list_files search_files read_file read_outline read_page create_note
metadata:
  version: "1.0.0"
---
你正在帮助用户比较资料。
1. 先读取 references/method.md 和 assets/comparison.md，了解比较方法与输出模板。
2. 确认用户想比较的主题；需要时询问，不能凭空补足资料。
3. 列出可用文档，选择至少两份相关资料，使用当前可用检索或 PageIndex 目录/页面工具读取原文。
4. 按模板输出，比较的每项事实附带工具返回的真实引用。找不到依据时说明缺口。
5. 用户没有要求保存时，只回答，不写文件。用户要求保存时，先完整生成答案，再通过笔记草稿工具处理。
