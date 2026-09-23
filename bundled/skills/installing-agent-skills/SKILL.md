---
name: installing-agent-skills
description: 通过本地终端检查、安装和验证 Agent Skill 或技能商店，并让工作台发现新技能。
allowed-tools: web_fetch terminal skills_list
metadata:
  version: "1.0.3"
---
1. 只在用户明确要求搜索、安装、更新或验证技能时使用本方法。
2. 先调用 `skills_list` 检查目标技能。相同技能已经可用时，直接说明“已安装”；有限可用时同时说明限制，不重复运行安装命令。
3. 用户给出安装说明链接时，用 `web_fetch` 读取一次原文，分清它是单个 `SKILL.md`、GitHub 技能目录，还是需要本地 CLI 的技能商店。用户已经给出完整的 `@namespace/slug` 时，不再执行全库搜索。
4. 需要 CLI 时，只执行完成当前步骤所需的精确命令。搜索、安装和验证必须分开，禁止把返回大量结果的搜索命令和安装命令拼成一条 Shell 命令。
5. CLI 未安装时，对 SkillHub 使用官方 `--cli-only` 安装方式。官方“完整安装”附带的默认模板和目标目录面向 OpenClaw，不是本工作台的技能目录。安装脚本、软件包和系统配置类命令会由工作台自动暂停，必须等待用户批准；不得换一种写法绕过审批。
6. 让 SkillHub 或其他 CLI 安装单个 Skill 时，必须显式使用 `--dir "$WORKBENCH_EXTERNAL_SKILLS_DIR"`，不得使用 `./skills`、`~/.openclaw/skills` 或其他 Agent 的目录。
7. 命令结束后优先检查 `terminal` 返回的 `operation_receipt`，再检查 `skill_sync`。只有目标技能已被工作台同步进技能清单，`package_status` 才会成功；CLI 退出码 0 不能单独作为安装成功的依据。`runtime_status` 表示能否完整运行。
8. 目标目录已存在但同步失败时，展示 `skill_sync` 中针对该技能的根因并停止；不要盲目重试，也不要用 `--force` 掩盖导入问题。`configuration_required`、`limited`、`disabled` 或 `incompatible` 都不能表述成“已经完整可用”。
9. 新安装的技能会在下一次发送问题时进入新的本轮运行快照；同一个对话不需要新建会话。正在执行、暂停或等待恢复的任务继续使用原快照。安装完成后提醒用户在对话框选择该技能再发送下一轮；只有伙伴、流程或团队的固定能力配置发生变化时才需要新建对话。
