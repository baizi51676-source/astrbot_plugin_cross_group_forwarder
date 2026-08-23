# 跨群消息转发插件 (astrbot_plugin_cross_group_forwarder)

给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（**按群号/群 UID 指定**）**立即或定时**发送文字/图片消息。
专为 **NapCat + AstrBot**（aiocqhttp / OneBot v11 适配器）设计。

## 功能

### LLM 工具（6 个）

| 工具 | 功能 |
|---|---|
| `send_message_to_group` | 立即按**群号**向指定 QQ 群发送文本消息 |
| `send_image_to_group` | 立即按**群号**向指定群发送**图片**（可带文字） |
| `schedule_message_to_group` | **定时**向指定群发送文本消息（支持指定时刻 / 延迟秒数） |
| `schedule_image_to_group` | **定时**向指定群发送图片（可带文字） |
| `cancel_schedule` | 取消一个尚未执行的定时任务 |
| `list_schedules` | 列出所有尚未执行的定时任务 |

### 管理指令

- `/list_schedules`：管理员查看所有定时任务（ID、目标群、内容、执行时间）

### 权限控制

- 默认仅管理员可用（`admin_only = True`）
- 设置为 `False` 时，`allowed_user_ids` 名单中的用户也可使用

## 群号获取方式

本插件**不维护会话注册表**：群名 → 群号的映射由 LLM 的可靠记忆直接持有。
LLM 只需记住"群名对应哪个群号"，然后按群号调用工具即可，无需任何注册操作。

## 定时发送说明

定时任务支持两种时间指定方式（二选一）：

- `at_time`：ISO8601 时刻（本地时间），如 `2026-08-23T14:30:00`
- `delay_seconds`：从现在起多少秒后发送，如 `300`（5 分钟后）

定时任务**持久化**存储，AstrBot 重启后自动恢复并继续执行；执行完自动移除。

## 图片发送说明

- `image_url` 支持三种来源：
  - `http(s)://` 网络图片 URL（如 `https://example.com/pic.jpg`）
  - `file://` 协议路径
  - 机器人本机（NapCat 所在机器）的图片文件路径（如 `/home/user/pic.jpg`）
- 可选 `caption` 参数可附带文字说明（文字在图片前）
- 底层为 OneBot v11 消息段 `{"type":"image","data":{"file":...}}`，NapCat 原生支持

## 实现原理

- 底层使用 OneBot v11 原生 API：`bot.send_group_msg(group_id=群号, message=...)`
- 通过 `event.bot`（aiocqhttp 的 CQHttp 客户端实例）直接调用，NapCat 原生支持
- 因此**不依赖** unified_msg_origin，可以直接指定任意群 UID 发送
- 定时任务由插件内后台调度循环（每 5 秒检查一次）驱动，触发时复用缓存的 bot 实例发送

## 安装

1. 克隆本仓库到 `AstrBot/data/plugins/` 目录
2. 在 AstrBot WebUI 的插件管理页中启用插件（或重启 AstrBot）
3. 完成！现在在任意聊天中对 LLM 说"给群 123456789 发消息：xxx"或"5 分钟后提醒群 987654321"即可

## 使用示例

```
用户A（群1）: 帮我给群 987654321 发条消息：今晚不回家吃饭了
LLM: ✅ 已成功向群 987654321 发送消息
群987654321: 今晚不回家吃饭了

用户A: 10 分钟后提醒群 123456789：该开会了
LLM: ✅ 已创建定时任务 a1b2c3d4，将于 2026-08-23 15:10:00 向群 123456789 发送消息。
    可使用 cancel_schedule 取消（ID: 完整ID）。
```

## 数据存储

定时任务数据保存在 `AstrBot/data/plugins/cross_group_forwarder/schedules.json`，
更新/重装插件不会丢失。

## 注意事项

- 本插件依赖 `event.bot`（aiocqhttp 适配器），仅适用于 NapCat / OneBot v11 接入方式
- 机器人需要先加入目标群才能向其发送消息
- 定时任务触发时若插件重启后尚未收到任何消息事件（无可用 bot 实例），该任务将被跳过
- 请谨慎授权使用，防止 LLM 被诱导向任意群发送垃圾消息