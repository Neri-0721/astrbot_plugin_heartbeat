# ❤️ 心跳助手 (Heartbeat)

AstrBot 心跳插件——定时唤醒完整 Agent，自主执行 HEARTBEAT.md 任务。

## 特性

| 特性 | 说明 |
|------|------|
| **完整 Agent 唤醒** | 唤醒的 Agent 拥有全部工具、Persona、Skills、MCP、记忆 |
| **预检零 Token** | 条件不满足时直接跳过，不消耗 API |
| **多会话支持** | QQ、微信等多平台各自独立检测独立唤醒 |
| **Per-session HEARTBEAT.md** | 每个平台可以有不同的心跳规则和语气 |
| **Last/All 模式** | 只对最后聊天的会话生效，或遍历所有会话 |
| **HEARTBEAT.md 驱动** | Agent 自己读文件、自己理解、自己决定做什么 |
| **每日限额 + 冷却期** | 防止异常消耗 API |

## 安装

```bash
# 进入 astrbot 插件目录
cd ~/.astrbot/data/plugins

# 克隆
git clone https://github.com/Neri-0721/astrbot_plugin_heartbeat

# 重启 astrbot
```

## 首次使用

1. 安装后重启 astrbot
2. **发一条消息给 AI**（任意内容），插件捕获会话
3. `/heartbeat_status` 查看状态
4. `/heartbeat_test` 手动触发一次心跳

## 配置

WebUI → 插件配置 → Heartbeat：

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `interval_minutes` | 90 | 预检间隔 |
| `target_mode` | `all` | `all`=遍历所有会话, `last`=只最后聊天的 |
| `active_hours_start/end` | 08:00-23:30 | 活跃时段 |
| `interaction_threshold_minutes` | 30 | 近期互动免打扰 |
| `max_daily_heartbeats` | 48 | 每日唤醒上限 |
| `cooldown_seconds` | 120 | 冷却间隔 |

## Per-session HEARTBEAT.md

```
data/plugin_data/astrbot_plugin_heartbeat/
├── HEARTBEAT.md                ← 全局默认
└── heartbeat.d/
    ├── qq_official.md          ← QQ 专用规则
    ├── lark.md                 ← 飞书/微信 专用规则
    └── wechat.md               ← 微信 专用规则
```

每个平台可以有独立的心跳规则。不存在时回退到全局默认。

## 命令

| 命令 | 说明 |
|------|------|
| `/heartbeat_status` | 查看状态 |
| `/heartbeat_reschedule` | 重新注册 |
| `/heartbeat_unschedule` | 取消心跳 |
| `/heartbeat_test` | 手动触发一次 |
| `/heartbeat_show` | 查看当前 HEARTBEAT.md |
| `/heartbeat` | 帮助 |

## 架构

```
APScheduler (permanent cron)
  └── _preflight()  [Python, 0 token]
        ├── conditions not met → skip
        └── conditions met
              └── add_active_job(run_once=True)
                    └── CronJobManager._run_active_agent_job()
                          └── build_main_agent() → Full Agent
```

## License

AGPL-3.0
