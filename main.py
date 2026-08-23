import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star


class CrossChatForwarder(Star):
    """跨会话消息转发插件（NapCat / OneBot v11 / aiocqhttp 适配）。

    给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（按群号）发送消息。
    底层使用 OneBot v11 原生 API：bot.send_group_msg(group_id=群号, ...)。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        # 持久化数据存到 AstrBot 的 data 目录（官方规范：防止更新插件时数据被覆盖）
        self.data_dir = Path("data/plugins/cross_chat_forwarder")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.chats_file = self.data_dir / "chats.json"
        # {会话名称: {"group_id": 群号}}
        self.chats: dict[str, dict] = self._load_chats()

        # 权限配置：
        # admin_only = True  -> 仅管理员可以使用（推荐）
        # admin_only = False -> 允许名单中的用户 ID 可以使用
        self.admin_only = True
        self.allowed_user_ids: set[str] = set()

    # ---------------------------------------------------------------
    # 内部工具方法
    # ---------------------------------------------------------------

    def _load_chats(self) -> dict:
        if self.chats_file.exists():
            try:
                return json.loads(self.chats_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"读取 chats.json 失败: {e}")
        return {}

    def _save_chats(self):
        try:
            self.chats_file.write_text(
                json.dumps(self.chats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存 chats.json 失败: {e}")

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """权限校验：管理员，或（当 admin_only=False 时）在允许名单中。"""
        if self.admin_only:
            return event.is_admin()
        return event.get_sender_id() in self.allowed_user_ids

    def _get_bot(self, event: AstrMessageEvent):
        """获取 aiocqhttp (OneBot v11/NapCat) 客户端实例。

        AiocqhttpMessageEvent 上有 bot 属性（CQHttp 实例），
        可直接调用 OneBot v11 原生 API，如 send_group_msg。
        """
        return getattr(event, "bot", None)

    # ---------------------------------------------------------------
    # 指令：会话注册与管理
    # ---------------------------------------------------------------

    @filter.command("register_chat")
    async def register_chat(self, event: AstrMessageEvent, name: str, group_id: str = ""):
        '''将群聊注册为「name」，此后 LLM 可通过工具向该群发送消息。
        默认注册当前所在的群，也可手动指定群号：/register_chat <名称> <群号>。仅管理员可用。'''
        if not self._is_allowed(event):
            yield event.plain_result("❌ 无权限：仅管理员可以注册会话。")
            return
        # 群号：优先取参数，其次取当前消息的群号
        gid = group_id.strip()
        if not gid:
            gid = str(getattr(event.message_obj, "group_id", "") or "")
        if not gid:
            yield event.plain_result(
                "❌ 无法确定群号。请手动指定：/register_chat <名称> <群号>"
            )
            return
        self.chats[name] = {"group_id": gid}
        self._save_chats()
        yield event.plain_result(f"✅ 已注册「{name}」→ 群 {gid}，LLM 现在可以向它发送消息了。")

    @filter.command("list_chats")
    async def list_chats(self, event: AstrMessageEvent):
        '''列出所有已注册的会话（名称 → 群号）。'''
        if not self.chats:
            yield event.plain_result(
                "📭 还没有注册任何会话。在目标群中使用 /register_chat <名称> 来注册。"
            )
            return
        lines = "\n".join(
            f"• {name} → 群 {info['group_id']}" for name, info in self.chats.items()
        )
        yield event.plain_result(f"已注册的会话:\n{lines}")

    @filter.command("unregister_chat")
    async def unregister_chat(self, event: AstrMessageEvent, name: str):
        '''删除一个已注册的会话。仅管理员可用。'''
        if not self._is_allowed(event):
            yield event.plain_result("❌ 无权限：仅管理员可以注销会话。")
            return
        if self.chats.pop(name, None):
            self._save_chats()
            yield event.plain_result(f"🗑️ 已注销会话「{name}」。")
        else:
            yield event.plain_result(f"⚠️ 会话「{name}」不存在。")

    # ---------------------------------------------------------------
    # LLM 工具
    # ---------------------------------------------------------------

    @filter.llm_tool("send_message_to_group")
    async def send_message_to_group(self, event: AstrMessageEvent,
                                    group_id: str, message: str):
        '''
        向指定的 QQ 群（按群号/群 UID）发送一条文本消息。适合通知、提醒、转发信息等场景。

        参数:
          group_id: 目标 QQ 群号（纯数字，如 "123456789"）
          message: 要发送的文本内容

        返回: 发送结果的描述
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        if not group_id.strip().isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        try:
            # OneBot v11 原生 API（NapCat 支持）
            await bot.send_group_msg(
                group_id=int(group_id.strip()),
                message=[{"type": "text", "data": {"text": message}}],
            )
            logger.info(f"跨群消息已发送到群 {group_id}: {message[:50]}")
            return f"✅ 已成功向群 {group_id} 发送消息"
        except Exception as e:
            logger.error(f"跨群发送失败: {e}")
            return f"❌ 发送失败: {e}"

    @filter.llm_tool("send_message_to_chat")
    async def send_message_to_chat(self, event: AstrMessageEvent,
                                   target: str, message: str):
        '''
        向已注册名称的会话发送一条文本消息。适合通知、提醒、转发信息等场景。

        参数:
          target: 目标会话的注册名称（如 "家庭群"、"工作群"），
                  可先调用 list_chats 工具查看有哪些可用会话
          message: 要发送的文本内容

        返回: 发送结果的描述
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        if target not in self.chats:
            return (f"❌ 错误：会话「{target}」不存在。"
                    f"可用的会话: {', '.join(self.chats.keys()) or '无'}")
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        gid = self.chats[target]["group_id"]
        try:
            await bot.send_group_msg(
                group_id=int(gid),
                message=[{"type": "text", "data": {"text": message}}],
            )
            logger.info(f"跨群消息已发送到「{target}」(群 {gid}): {message[:50]}")
            return f"✅ 已成功向「{target}」(群 {gid}) 发送消息"
        except Exception as e:
            logger.error(f"跨群发送失败: {e}")
            return f"❌ 发送失败: {e}"

    @filter.llm_tool("list_chats")
    async def list_chats_tool(self, event: AstrMessageEvent):
        '''列出所有已注册的、可以向其发送消息的会话（名称 → 群号）。'''
        if not self.chats:
            return "当前没有已注册的会话。需要先让用户在目标群中使用 /register_chat <名称> 注册。"
        return "可用的会话: " + ", ".join(
            f"{name}(群 {info['group_id']})" for name, info in self.chats.items()
        )

    @filter.llm_tool("send_image_to_group")
    async def send_image_to_group(self, event: AstrMessageEvent,
                                  group_id: str, image_url: str, caption: str = ""):
        '''
        向指定的 QQ 群（按群号/群 UID）发送一张图片，可附带文字说明。

        参数:
          group_id: 目标 QQ 群号（纯数字，如 "123456789"）
          image_url: 图片地址。支持：
                     - http(s):// 开头的网络图片 URL
                     - 机器人本机（NapCat 所在机器）可访问的图片文件路径
                     - file:// 开头的本地文件路径
          caption: 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        if not group_id.strip().isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        image_url = image_url.strip()
        if not (image_url.startswith("http://") or image_url.startswith("https://")
                or image_url.startswith("file://") or image_url.startswith("/")):
            return ("❌ 图片地址格式错误：请提供 http(s):// 网络图片 URL、"
                    "file:// 路径或本地文件路径。")
        try:
            message = []
            if caption.strip():
                message.append({"type": "text", "data": {"text": caption.strip()}})
            message.append({"type": "image", "data": {"file": image_url}})
            await bot.send_group_msg(group_id=int(group_id.strip()), message=message)
            logger.info(f"跨群图片已发送到群 {group_id}")
            return f"✅ 已成功向群 {group_id} 发送图片"
        except Exception as e:
            logger.error(f"跨群图片发送失败: {e}")
            return f"❌ 发送失败: {e}"

    @filter.llm_tool("send_image_to_chat")
    async def send_image_to_chat(self, event: AstrMessageEvent,
                                 target: str, image_url: str, caption: str = ""):
        '''
        向已注册名称的会话发送一张图片，可附带文字说明。

        参数:
          target: 目标会话的注册名称（如 "家庭群"、"工作群"），
                  可先调用 list_chats 工具查看有哪些可用会话
          image_url: 图片地址。支持 http(s):// 网络图片 URL、file:// 或本地文件路径
          caption: 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        if target not in self.chats:
            return (f"❌ 错误：会话「{target}」不存在。"
                    f"可用的会话: {', '.join(self.chats.keys()) or '无'}")
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        image_url = image_url.strip()
        if not (image_url.startswith("http://") or image_url.startswith("https://")
                or image_url.startswith("file://") or image_url.startswith("/")):
            return ("❌ 图片地址格式错误：请提供 http(s):// 网络图片 URL、"
                    "file:// 路径或本地文件路径。")
        gid = self.chats[target]["group_id"]
        try:
            message = []
            if caption.strip():
                message.append({"type": "text", "data": {"text": caption.strip()}})
            message.append({"type": "image", "data": {"file": image_url}})
            await bot.send_group_msg(group_id=int(gid), message=message)
            logger.info(f"跨群图片已发送到「{target}」(群 {gid})")
            return f"✅ 已成功向「{target}」(群 {gid}) 发送图片"
        except Exception as e:
            logger.error(f"跨群图片发送失败: {e}")
            return f"❌ 发送失败: {e}"

    # ---------------------------------------------------------------

    async def terminate(self):
        '''插件被卸载/停用时调用'''
        self._save_chats()