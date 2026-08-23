# 跨群消息转发插件 (astrbot_plugin_cross_group_forwarder)

给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（**按群号/群 UID 指定**）发送文字/图片消息。
专为 **NapCat + AstrBot**（aiocqhttp / OneBot v11 适配器）设计。

## 功能

### LLM 工具（2 个）

| 工具 | 功能 |
|---|---|
| `send_message_to_group` | 按**群号**向指定 QQ 群发送文本消息 |
| `send_image_to_group` | 按**群号**向指定群发送**图片**（可带文字） |

### 权限控制

- 默认仅管理员可用（`admin_only = True`）
- 设置为 `False` 时，`allowed_user_ids` 名单中的用户也可使用

## 群号获取方式

本插件**不维护会话注册表**：群名 → 群号的映射由 LLM 的可靠记忆直接持有。
LLM 只需记住"群名对应哪个群号"，然后按群号调用工具即可，无需任何注册操作。

## 定时发送

定时发送需求**不需要本插件实现**，直接使用 **AstrBot 自带的 `future_task` 内置工具**：

- `action=create` + `run_at`（一次性任务，ISO8601 时刻）+ `run_once`，或 `cron_expression`（周期任务）
- `note` 中写明"到点后调用 send_message_to_group 向群 XXXXX 发送消息 xxx"
- 任务触发时 AstrBot 会唤醒未来的 Agent 执行 `note` 中的指令，届时调用本插件的工具即可

> 需要先开启 AstrBot 的主动能力配置：`provider_settings.proactive_capability.add_cron_tools = true`

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

## 安装

1. 克隆本仓库到 `AstrBot/data/plugins/` 目录
2. 在 AstrBot WebUI 的插件管理页中启用插件（或重启 AstrBot）
3. 完成！现在在任意聊天中对 LLM 说"给群 123456789 发消息：xxx"即可

## 使用示例

```
用户A（群1）: 帮我给群 987654321 发条消息：今晚不回家吃饭了
LLM: ✅ 已成功向群 987654321 发送消息
群987654321: 今晚不回家吃饭了
```

## 注意事项

- 本插件依赖 `event.bot`（aiocqhttp 适配器），仅适用于 NapCat / OneBot v11 接入方式
- 机器人需要先加入目标群才能向其发送消息
- 请谨慎授权使用，防止 LLM 被诱导向任意群发送垃圾消息