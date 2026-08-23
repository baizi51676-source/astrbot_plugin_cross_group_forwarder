# 跨群消息转发插件 (astrbot_plugin_cross_group_forwarder)

给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（**按群号/群 UID 指定**）发送文字/图片/语音/文件/合并转发消息，也可以向指定 QQ 用户发送私聊消息。
专为 **NapCat + AstrBot**（aiocqhttp / OneBot v11 适配器）设计。

## 功能

### LLM 工具（8 个）

| 工具 | 功能 |
|---|---|
| `send_message_to_group` | 按**群号**向指定 QQ 群发送文本消息 |
| `send_image_to_group` | 按**群号**向指定群发送**图片**（可带文字） |
| `send_voice_to_group` | 按**群号**向指定群发送**语音**（可带文字） |
| `send_file_to_group` | 按**群号**向指定群发送**文件**（可带文字） |
| `send_forward_to_group` | 按**群号**发送**合并转发**（多条消息打包成转发卡片） |
| `send_private_message` | 向指定 **QQ 用户**发送私聊文本消息 |
| `send_private_image` | 向指定 **QQ 用户**发送私聊图片（可带文字） |
| `get_group_list` | 获取机器人加入的所有群（群号 + 群名），用于建立"群名→群号"记忆 |

### 权限控制

- 默认仅管理员可用（`admin_only = True`）
- 设置为 `False` 时，`allowed_user_ids` 名单中的用户也可使用

## 群号获取方式

本插件**不维护会话注册表**：群名 → 群号的映射由 LLM 的可靠记忆直接持有。
LLM 可先调用 `get_group_list` 获取机器人所有群的群号与群名，建立"群名对应哪个群号"的记忆，之后直接按群号调用发送工具，无需任何注册操作。

## 媒体地址说明

- 图片/语音/文件均支持三种来源：
  - `http(s)://` 网络 URL（如 `https://example.com/pic.jpg`）
  - `file://` 协议路径
  - 机器人本机（NapCat 所在机器）的文件路径（如 `/home/user/audio.mp3`）
- 可选 `caption` 参数可附带文字说明（文字在媒体前）
- 底层为 OneBot v11 消息段（`image` / `record` / `file` / `forward`），NapCat 原生支持

## 安全配置（WebUI 可视化）

所有配置项均可直接在 **AstrBot WebUI → 插件管理 → 跨群消息转发 → 配置** 中修改，无需编辑代码：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `admin_only` | bool | `true` | 仅 AstrBot 管理员可用 |
| `allowed_user_ids` | list | `[]` | 允许使用的用户 QQ 号（`admin_only=false` 时生效） |
| `allowed_groups` | list | `[]` | 目标群白名单：仅允许向这些群发送消息，留空不限制 |

- **目标群白名单**：防止 LLM 被 prompt 注入诱导向任意群发送垃圾消息
- **审计日志**：所有发送操作（时间/工具/目标/内容摘要/成败）记录在
  `AstrBot/data/plugins/cross_group_forwarder/audit.log`（JSON Lines），便于事后追查

## 实现原理

- 底层使用 OneBot v11 原生 API（`send_group_msg` / `send_private_msg` / `send_forward_msg`）
- 通过 `event.bot`（aiocqhttp 的 CQHttp 客户端实例）直接调用，NapCat 原生支持
- 因此**不依赖** unified_msg_origin，可以直接指定任意群 UID / 用户 QQ 发送

## 安装

1. 克隆本仓库到 `AstrBot/data/plugins/` 目录
2. 在 AstrBot WebUI 的插件管理页中启用插件（或重启 AstrBot）
3. 完成！现在在任意聊天中对 LLM 说"给群 123456789 发消息：xxx"即可

## 使用示例

```
用户A（群1）: 帮我给群 987654321 发条消息：今晚不回家吃饭了
LLM: ✅ 已成功向群 987654321 发送消息
群987654321: 今晚不回家吃饭了

用户A: 把这份文件发到工作群（群号 123456789）：https://example.com/report.pdf
LLM: ✅ 已成功向群 123456789 发送
```

## 注意事项

- 本插件依赖 `event.bot`（aiocqhttp 适配器），仅适用于 NapCat / OneBot v11 接入方式
- 机器人需要先加入目标群才能向其发送消息；私聊需对方未拒绝接收机器人消息
- 合并转发依赖 NapCat 的 `send_forward_msg` 扩展 API
- 请谨慎授权使用，防止 LLM 被诱导向任意群发送垃圾消息