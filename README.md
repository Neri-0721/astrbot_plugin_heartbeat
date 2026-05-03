# ❤️ 心跳助手 (Heartbeat) — AI 定时关怀插件

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.x-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-AGPL--3.0-green)](LICENSE)

## 这是干什么的？

一个定时闹钟，但不是吵你起床——是**让 AI 定时醒来，看看你有没有事**。

比如到了饭点提醒你吃饭、晚上催你睡觉、好久没说话时问候一声。你可以在 `HEARTBEAT.md` 里写任何规则，AI 会自己看、自己想、自己决定怎么做。

> 就跟手机设了个闹钟，到点了 AI 主动来找你。不是发固定文本，AI 会自己思考怎么跟你说。

---

## 目录

- [特性](#特性)
- [架构](#架构)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置](#配置)
- [HEARTBEAT.md](#heartbeatmd)
- [Per-session 模式](#per-session-模式)
- [命令](#命令)
- [性能](#性能)
- [常见问题](#常见问题)
- [License](#license)

---

## 特性

### 核心能力

| 特性 | 说明 |
|------|------|
| **完整 Agent 唤醒** | 被唤醒的 Agent 拥有**全部能力**：Persona/Skills/MCP/文件工具/Web搜索/代码执行 |
| **零 Token 预检** | Python 层预检（活跃时段/互动间隔/冷却期/日限额），条件不满足直接跳过，完全不消耗 API |
| **多会话支持** | 同时绑定 QQ + 微信 + 飞书等平台，各会话独立检测、独立唤醒、独立规则 |
| **Last/All 目标模式** | `last` = 只对最后聊天的会话生效；`all` = 遍历所有符合条件的会话 |
| **Per-session HEARTBEAT.md** | 每个平台可以有自己的心跳规则、语气、任务清单 |
| **HEARTBEAT.md 驱动** | Agent 自己读文件、自己理解、自己决定，支持自然语言描述任务 |
| **安全机制** | 防重叠锁、冷却期、每日唤醒上限，防止异常消耗 |

### Agent 唤醒后的能力

当心跳条件满足，框架会通过 `build_main_agent()` 启动**完整 Agent Turn**：

```
✅ Persona (Soul) 注入       — 什么样的人格就以什么样的语气说话
✅ 用户画像 & 记忆注入        — 知道你是谁、你的习惯
✅ 全部 Skills               — 你装的所有技能都能用
✅ 文件工具 (Read/Write/Edit) — 可以读文件、写日记、检查日志
✅ Shell/Python 执行          — 可以跑脚本、操作电脑
✅ Web 搜索                   — 可以查天气、查新闻
✅ MCP 工具                   — 你的 MCP 服务器照常可调用
✅ 知识库查询                  — RAG 知识库可用
✅ 发消息工具                  — 可以主动联系你
✅ 对话历史上下文              — 知道上次聊了什么
```

---

## 架构

```
APScheduler (persistent cron)
     │  */45 * * * *  (basic job)
     ▼
  preflight check (Python, 0 token)
     │
     ├── outside active hours  → SKIP
     ├── recent interaction    → SKIP
     ├── cooldown period       → SKIP
     ├── daily limit reached   → SKIP
     │
     └── conditions met
           │
           ▼
      add_active_job(run_once=True, run_at=now)
           │
           ▼
      CronJobManager._run_active_agent_job()
           │
           ├── CronMessageEvent (synthetic event)
           ├── Load conversation history
           ├── Inject PROACTIVE_AGENT_CRON_WOKE_SYSTEM_PROMPT
           │   → "You are woken by cron, not by user"
           │   → "Use send_message_to_user if needed"
           │   → "Use your tools and skills"
           │
           ▼
      build_main_agent()  ← Full Agent Pipeline
           │
           ├── Persona injection
           ├── Skills injection
           ├── All tools (file/shell/web/MCP)
           ├── Knowledge base
           └── LLM decides:
                 ├── Read HEARTBEAT.md
                 ├── Check conditions
                 ├── Take action or stay silent
                 └── Done
```

### 与 "定时发消息" 方案的区别

| 方案 | 做法 | 问题 |
|------|------|------|
| ❌ Python 定时器 + if/else | 插件自己判断、自己发固定文本 | 没有 Soul、没有工具、智障 |
| ✅ 本插件 | 委托框架唤醒完整 Agent | **和正常聊天时一模一样的能力** |

---

## 安装

### 前置条件

- **AstrBot >= 4.x**（依赖内置 CronJobManager）
- 推荐安装 `astrbot_plugin_memory`（获得 Soul + 记忆注入效果）

### 方式一：插件市场

AstrBot WebUI → 插件市场 → 搜索 "heartbeat" → 一键安装

### 方式二：手动安装

```bash
cd ~/.astrbot/data/plugins
git clone https://github.com/Neri-0721/astrbot_plugin_heartbeat.git
```

然后重启 AstrBot。

---

## 快速开始

```
1. 安装后重启 AstrBot
2. 随意发一条消息给 AI        → 插件捕获会话
3. /heartbeat_status           → 确认就绪
4. /heartbeat_test             → 手动触发一次心跳
```

### 首次运行日志示例

```
[heartbeat] v2.4.0 | 90min | mode=all
[heartbeat] +session ARONA-QQ | ARONA-QQ:FriendMessage:xxx
[heartbeat] cron registered id=xxx expr=*/45 * * * *
[heartbeat] ▲ #1 today | ARONA-QQ | idle=300min
[heartbeat] → agent job=xxx
```

---

## 配置

在 AstrBot WebUI → 插件配置 → Heartbeat 中调整：

### 基础设置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `interval_minutes` | `int` | `90` | 预检间隔（分钟） |
| `target_mode` | `enum` | `all` | `all`=遍历所有会话, `last`=只最后聊天的会话 |

### 时段控制

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `active_hours_start` | `string` | `08:00` | 活跃时段开始 |
| `active_hours_end` | `string` | `23:30` | 活跃时段结束 |

### 防打扰

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `check_last_interaction` | `bool` | `true` | 启用互动检测 |
| `interaction_threshold_minutes` | `int` | `30` | N 分钟内聊过就跳过 |

### 费用控制

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_daily_heartbeats` | `int` | `48` | 每日最大唤醒次数 |
| `cooldown_seconds` | `int` | `120` | 两次唤醒最小间隔（秒） |

### 自定义 HEARTBEAT.md

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `heartbeat_file_path` | `string` | `""` | 全局路径。留空=启用 per-session 模式 |

---

## HEARTBEAT.md

心跳任务的**核心配置文件**。Agent 每次被唤醒后，通过 `file_read_tool` 读取并理解执行。

### 默认位置

```
~/.astrbot/data/plugin_data/astrbot_plugin_heartbeat/HEARTBEAT.md
```

### 格式

支持**自然语言描述**任务规则，Agent 会用 LLM 理解执行：

```markdown
# HEARTBEAT.md

## 时间规则
- [12:00-13:00] 提醒吃午饭
- [18:00-19:00] 问候晚饭
- [22:00之后] 提醒休息

## 空闲规则
- 距上次对话 > 4h → 一句自然问候
- 距上次对话 < 30min → 不要打扰

## 自定义任务（自然语言）
- 每次心跳时检查 ~/todo.md 有没有待办
- 如果今天下雨，提醒我带伞
- 每周一备份 ~/data 目录
```

### 约束

- 不要解释"我是被定时唤醒的"
- 问候不超过 25 字
- 无事可做 → 什么都不做，结束

---

## Per-session 模式

当 `heartbeat_file_path` 留空时，启用 per-session 模式：

```
plugin_data/astrbot_plugin_heartbeat/
├── HEARTBEAT.md                 ← 全局默认（fallback）
└── heartbeat.d/
    ├── qq_official.md           ← QQ 专用规则
    ├── lark.md                  ← 飞书专用规则
    └── wechat.md                ← 微信专用规则
```

每个平台可以有自己的规则，不存在时自动回退到全局文件。

### 示例：QQ vs 微信不同风格

**qq_official.md**
```markdown
- [12:00-13:00] "恰饭了恰饭了"
- [22:00之后] "该睡了嗷"
```

**wechat.md**
```markdown
- [12:00-13:00] 记得按时吃饭哦
- [22:00之后] 不早了，休息吧
```

---

## 命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/heartbeat_status` | 查看运行状态和所有会话 | `/heartbeat_status` |
| `/heartbeat_reschedule` | 改配置后重新注册定时器 | `/heartbeat_reschedule` |
| `/heartbeat_unschedule` | 取消心跳 | `/heartbeat_unschedule` |
| `/heartbeat_test` | 手动触发一次完整 Agent 心跳 | `/heartbeat_test` |
| `/heartbeat_show` | 查看当前会话的 HEARTBEAT.md | `/heartbeat_show` |
| `/heartbeat` | 显示帮助 | `/heartbeat` |

---

## 性能

### Token 消耗

| 场景 | OpenClaw 模式（无预检） | 本插件 |
|------|------------------------|--------|
| 夜晚 22:00-08:00 | 每次 100% 消耗 | **0 token** ❌ 预检跳过 |
| 聊天中 | 每次 100% 消耗 | **0 token** ❌ 互动检测跳过 |
| 真正需要时 | 100% 消耗 | 100% 消耗 ✅ |
| **日均 LLM 调用** | 固定 ~16 次/天 | **取决于条件，通常 3-5 次** |

### 安全机制

| 机制 | 作用 |
|------|------|
| 防重叠锁 | 前一次心跳未完成时，下一次不触发 |
| 冷却期 (120s) | 两次唤醒间最小间隔 |
| 日限额 (48次) | 防止异常消耗 |
| 每日重置 | 计数器每天 00:00 自动归零 |

---

## 常见问题

### 心跳没发消息，正常吗？

正常。Agent 根据 HEARTBEAT.md 的条件自主判断。如果无任务可做或不符合触发条件，Agent 会静默结束。

### 怎么确认心跳真的跑了？

查看后端日志：

```bash
tail -f ~/.astrbot/logs/backend.log | grep "\[heartbeat\]"
```

### 能和其他插件一起用吗？

可以。`build_main_agent()` 会自动加载所有已安装的插件 Tools 和 Skills。

### 为什么用 basic cron 而不是 active_agent cron？

basic cron 跑 Python handler，**不调用 LLM**，零 token 消耗。只有预检通过后才委托 active_agent cron 唤醒 Agent。

---

## License

[AGPL-3.0](LICENSE)

---

<p align="center">
  <sub>Built with ❤️ by Key for Neri0721</sub>
</p>
