# 跨群消息转发插件 (astrbot_plugin_cross_group_forwarder)

给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（**按群号/群 UID 指定**）发送消息。
专为 **NapCat + AstrBot**（aiocqhttp / OneBot v11 适配器）设计。

## 功能

- 🎯 `@filter.llm_tool("send_message_to_group")`：LLM 直接按**群号**向指定 QQ 群发消息
- 🎯 `@filter.llm_tool("send_message_to_chat")`：LLM 按注册名称向已注册的群发消息
- 🎯 `@filter.llm_tool("send_image_to_group")`：LLM 按**群号**向指定群发送**图片**（可带文字）
- 🎯 `@filter.llm_tool("send_image_to_chat")`：LLM 按注册名称发送图片（可带文字）
- 📋 `@filter.llm_tool("list_chats")`：LLM 可查询当前有哪些可用会话（名称 → 群号）
- 🛠️ 指令：`/register_chat <名称> [群号]`、`/list_chats`、`/unregister_chat <名称>`
- 🔐 权限控制：默认仅管理员可用

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
3. （可选）在目标群中发送 `/register_chat 家庭群` 注册名称
4. 完成！现在在任意聊天中对 LLM 说"给群 123456789 发消息：xxx"即可

## 使用示例

```
用户A（群1）: 帮我给群 987654321 发条消息：今晚不回家吃饭了
LLM: ✅ 已成功向群 987654321 发送消息
群987654321: 今晚不回家吃饭了

# 或者使用注册名称
用户A: 帮我给"家庭群"发消息：周末一起吃饭
LLM: ✅ 已成功向「家庭群」(群 987654321) 发送消息
```

## 权限说明

- `admin_only = True`（默认）：仅 AstrBot 管理员可使用
- `admin_only = False`：允许名单 `allowed_user_ids` 中的用户可使用

## 数据存储

会话注册数据保存在 `AstrBot/data/plugins/cross_chat_forwarder/chats.json`，
更新/重装插件不会丢失。

## 注意事项

- 本插件依赖 `event.bot`（aiocqhttp 适配器），仅适用于 NapCat / OneBot v11 接入方式
- 机器人需要先加入目标群才能向其发送消息
- 请谨慎授权使用，防止 LLM 被诱导向任意群发送垃圾消息