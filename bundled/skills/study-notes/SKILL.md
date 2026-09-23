---
name: study-notes
description: 把当前学习问题和资料整理为有依据、可复习的学习笔记。
allowed-tools: list_files search_files read_file read_outline read_page create_note
metadata:
  version: "1.0.0"
---
1. 先读取 references/method.md 和 assets/note.md。
2. 有知识库时先阅读相关原文，事实保留真实引用；没有知识库时基于用户提供的内容整理，标明哪些问题需要核实，不伪造引用。
3. 按模板输出，重点写清概念之间的关系，并设计两个可自测的问题。
4. 仅在用户明确要求保存时调用 create_note；覆盖已有文件必须经过原有确认流程。
