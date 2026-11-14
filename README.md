# fgo.wiki 本地知识库

这个仓库把 Mooncell（fgo.wiki）的公开正文同步到本地 SQLite，并提供中文全文检索。它同时是一个 Agent Skill：安装后，Claude Code、Codex 等支持 Skill 的 AI agent 在回答 FGO 问题时会先查本地库，需要时再读取命中的原始维基文本和页面链接。

## 作为 Skill 安装

把仓库克隆到 agent 的 skills 目录，文件夹名必须是 `fgo-wiki-kb`（与 `SKILL.md` 里的 `name` 一致）。

Claude Code：

```bash
git clone https://github.com/shuiyue-cmyk/agent-fgo-db ~/.claude/skills/fgo-wiki-kb
```

Codex：

```bash
git clone https://github.com/shuiyue-cmyk/agent-fgo-db ~/.codex/skills/fgo-wiki-kb
```

其他遵循 [Agent Skills 规范](https://agentskills.io/specification)的 agent，克隆到它各自的 skills 目录即可。也可以用 [skills CLI](https://github.com/vercel-labs/skills) 安装，`-a` 指定 agent，`-g` 表示装到用户目录：

```bash
npx skills add shuiyue-cmyk/agent-fgo-db -g -a claude-code
```

安装后重启 agent，直接问 FGO 相关问题即可。第一次使用时 agent 会发现数据库还不存在，征得同意后执行一次完整同步（约半小时，生成约 600 MiB 的 `fgo_wiki.sqlite3`）；也可以自己先按下文「常用命令」跑一遍。

数据库默认生成在 skill 目录里。想放到别处，或让多个 agent 共用一份，把环境变量 `FGO_KB_DB` 设为数据库文件的路径。用 skills CLI 安装时建议这样做，免得重装 skill 时数据库跟着目录一起被替换。

## 当前范围

- 默认同步主条目命名空间（namespace 0），包括从者、礼装、关卡、活动、语音、剧情等正文页。
- 保存原始维基文本、清洗后的正文、章节、分类、重定向、修订号、更新时间、语义模板字段和来源链接。
- 中文采用双字分词索引，兼顾中文短语和英文名称。
- 截至 2026-11-14 的一次完整同步共 19,114 个主命名空间页面，其中约 9,300 篇正式条目、6,735 个重定向别名。图片只记录页面缩略图地址，不批量下载，避免给站点造成不必要流量。
- 该次同步生成的库约 600 MiB，包含约 6 万个检索分块和 28 万个结构化模板字段。
- 站点内容采用 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)。本库供个人非商业检索，引用内容时应保留来源链接。

## 常用命令

> 本仓库只包含脚本，不包含数据库文件（`fgo_wiki.sqlite3` 约 600 MiB，超过 GitHub 单文件上限）。克隆后先执行一次完整同步在本地生成数据库，按默认 0.5 秒请求间隔大约需要半小时。只依赖 Python 3.11+ 标准库，装有 `requests` 时会自动使用。

以下命令以 PowerShell 为例，macOS / Linux 把 `python .\fgo_kb.py` 换成 `python3 fgo_kb.py`。进入仓库（skill）目录后，首次完整同步：

```powershell
python .\fgo_kb.py sync --full --delay 0.5
```

以后增量更新最近改动：

```powershell
python .\fgo_kb.py sync
```

如果首次全量同步中断，从已提交的位置继续：

```powershell
python .\fgo_kb.py sync --full --resume --delay 0.5
```

只从已保存的维基文本重建检索层，不需要联网：

```powershell
python .\fgo_kb.py reindex
```

搜索：

```powershell
python .\fgo_kb.py search "阿尔托莉雅 宝具 倍率" --limit 8
```

以 Markdown 显示结果：

```powershell
python .\fgo_kb.py search "龙之牙 获取" --format markdown
```

读取页面或指定章节：

```powershell
python .\fgo_kb.py show "阿尔托莉雅·潘德拉贡"
python .\fgo_kb.py show "阿尔托莉雅·潘德拉贡" --section "基础数值"
python .\fgo_kb.py show "阿尔托莉雅·潘德拉贡" --raw
```

查看从常见模板提取的结构化字段和数据库状态：

```powershell
python .\fgo_kb.py fields "阿尔托莉雅·潘德拉贡"
python .\fgo_kb.py stats
```

也可以用包装脚本：

```powershell
.\update.ps1
.\search.ps1 "摩根 技能"
```

## 数据说明

数据库文件是 `fgo_wiki.sqlite3`。同步采用 MediaWiki API、固定 User-Agent、请求间隔、持久连接和 `maxlag` 错误退避。完整同步的请求量约为数百次，正文规模远小于网页和图片镜像。每次搜索都会返回页面链接，`show` 会显示修订号和修订时间。中文、日文假名和英文都进入同一索引。

FGO 国服、日服、活动复刻等状态会持续变化。回答带时间敏感性的内容时，应查看本地页面的修订时间，并视需要使用 `sync` 更新后再回答。
