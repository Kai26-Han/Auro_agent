# <img src="images/logo1.png" width="44" align="absmiddle" alt="Auro logo"> Auro

**Agentic for everything.**

A local-first AI agent workspace for personal learning and work.

**English** · [简体中文](README.zh-CN.md)

![Auro workspace home](images/webpage.png)

### Vision

> **Everyone should have an agentic tool of their own for learning and work.**
>
> **Everything can evolve to become agentic.**

Auro is a local-first AI agent workspace for personal learning and everyday work. Its goal is to move beyond one-off chat and become a long-term collaborator that can work with a person's projects, documents, knowledge, memory, habits, and tools.

In Auro, **agentic** means more than connecting an application to a language model. An agentic system can organize context around a goal, choose models and tools, retrieve knowledge, use long-term memory, execute reusable skills and workflows, and keep the user in control of consequential actions.

The project is built as personal agent infrastructure that can evolve over time:

- each person can own and configure a private AI workspace;
- documents, tools, skills, workflows, and partners can become callable and composable capabilities;
- knowledge and memory can accumulate through use without taking control away from the user;
- people with limited coding experience can continue extending the workspace with AI coding and ecosystem tools.

### How Auro is built

Auro separates models, context, capabilities, and execution. Chat is the common entry point. Projects, knowledge, and memory supply context; tools, Skills, partners, workflows, and teams supply actions; LangGraph organizes them into observable and resumable runs.

The architecture follows six principles:

1. **Local first** — conversations, indexes, memories, and settings are stored on the user's machine by default.
2. **Switchable and extensible engines** — knowledge and memory engines keep their own complete behavior behind clear interfaces, so additional engines can be integrated without changing the rest of the workspace.
3. **Composable capabilities** — tools perform actions, Skills package methods, partners carry roles, and workflows or teams coordinate complex work.
4. **Resumable execution** — long-running work has explicit state, limits, stop controls, and continuation paths.
5. **Understandable operation** — the interface exposes the progress, tool use, sources, and results needed to evaluate an agent run.
6. **Bounded extensibility** — models, tools, Connectors, and Skills enter the system through configuration, permissions, and runtime limits.

### Current implementation

Auro is under active development and already provides a working personal-agent foundation.

| Area | Current implementation |
| --- | --- |
| Chat and projects | Project-based conversations, model selection, attachments, copy/edit/resend, stop, and continue |
| Agent orchestration | LangGraph state orchestration, tool routing, context budgets, long-context compression, and run recovery |
| Model settings | Separate chat and embedding configurations, with compatible cloud providers and local model services |
| Knowledge center | Local document libraries, multi-library retrieval, LlamaIndex vector/hybrid search, and PageIndex OSS document-tree retrieval |
| Memory center | Independent LangMem and Mem0 OSS systems with memory spaces, layered views, editing, and management |
| Skill system | Skill installation, revision snapshots, automatic/manual activation, bounded multi-Skill composition, and readiness checks |
| Partners and collaboration | Assistants, workflows, and teams with fixed flows, dynamic planning, concurrency, and recovery |
| Tools and Connectors | Local files, notes, web search, page fetching, paper search, terminal tools, and MCP Connectors |
| Observability | Unified display of progress, tool calls, citations, tokens, duration, and cost information |
| Web interface | A React and TypeScript workspace for chat, knowledge, memory, skills, and settings |

### Why Auro is different

#### Built for long-term personal use

Projects, documents, knowledge bases, memory, models, and skills live in one workspace. The system is designed around continuity across learning and work rather than a single answer.

#### Clear separation of knowledge, memory, and active context

- The **knowledge center** stores user-provided, source-backed material.
- The **memory center** stores personal information, experiences, and methods learned through ongoing interaction.
- The **conversation context** carries the immediate state of the current task.

The orchestration layer combines them only when the task needs them.

#### Two complete memory approaches

LangMem and Mem0 OSS currently run as two independent memory engines. Each retains its own spaces, structures, write/update behavior, and recall logic. Users can switch engines and decide whether to reuse an earlier space when returning to an engine.

These two integrations are the current starting point, not a fixed limit. The memory layer is designed around an extensible engine interface, allowing other memory systems to be added for different personal workflows, storage choices, or recall strategies.

#### Two local knowledge engines

- **LlamaIndex** supports local chunking, vector retrieval, and optional BM25 hybrid retrieval.
- **PageIndex OSS** builds a tree-oriented index from document structure and pages.

Auro is not limited to these two knowledge engines. The knowledge layer keeps ingestion, indexing, retrieval, and configuration behind extensible engine boundaries, so other engines can be integrated according to personal document types and retrieval needs.

A conversation may use no knowledge base, one knowledge base, or several at once.

#### A capability ladder from tools to teams

Tools handle atomic actions. Skills capture reusable methods. Partners combine roles and capabilities. Workflows and teams coordinate larger objectives. This creates a gradual path from simple automation to agentic collaboration.

#### Designed to remain extensible

The Python backend separates domain modules, while the interface uses React and TypeScript. Models, knowledge, memory, tools, Skills, and Connectors keep explicit boundaries, making it possible to add or replace implementations without rebuilding the entire application.

#### Local-first data with explicit boundaries

Personal data stays local by default. Skill installation includes metadata, dependency, and security checks. Script and terminal execution are governed by permissions and runtime limits. When external models, web tools, or Connectors are enabled, only the data needed for those requests is sent to the configured services.

### Requirements

- macOS or Linux
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 or later
- Optional: a local [Ollama](https://ollama.com/) embedding model

### Quick start

```bash
git clone <repository-url> Auro_agent
cd Auro_agent

cp .env.example .env
uv sync --locked

cd frontend
npm ci
npm run build
cd ..
```

Add the required model credentials to `.env`, or configure models from the Settings page after startup. Never commit a populated `.env` file.

Start the application:

```bash
uv run --locked workbench web
```

A suggested first-run sequence:

1. Configure a chat model and an embedding model in Settings.
2. Create a knowledge base and add local documents.
3. Select a memory engine and review its default memory space.
4. Enable the Skills or Connectors you need.
5. Return to chat, select a model and optional knowledge bases, and start a task.

### Repository structure

```text
Auro_agent/
├── src/personal_workbench/   Python backend, LangGraph orchestration, and domain modules
├── frontend/                 React + TypeScript web interface
├── bundled/skills/           First-party Skills bundled with the project
├── images/                   README and project images
├── notes/                    Local source directory; personal content is not committed
├── pyproject.toml            Python project and dependency configuration
├── uv.lock                   Locked Python dependencies
└── .env.example              Environment variable template
```

### Data and privacy

“Local first” means Auro stores and organizes personal data on the local machine by default. Requests to cloud models, web search services, or external Connectors still send the required data to services selected by the user. Configure providers and permissions according to your privacy requirements.

### Current scope

- The project is evolving actively, so some data structures and interfaces may change.
- The knowledge center currently focuses on local documents; additional remote sources are future work.
- External models, search, and Connectors depend on their respective services, accounts, and network access.
- Skill scripts are protected by permissions and runtime constraints, but Skills should still be installed only from trusted sources.
- Auro currently targets a personal workspace and an extensible development foundation rather than multi-user production deployment.

### Acknowledgements

Thanks to [LangGraph](https://github.com/langchain-ai/langgraph), [LlamaIndex](https://github.com/run-llama/llama_index), [PageIndex](https://github.com/VectifyAI/PageIndex), [LangMem](https://github.com/langchain-ai/langmem), [Mem0](https://github.com/mem0ai/mem0), and their open-source communities.
