from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star


class CrossGroupForwarder(Star):
    """跨群消息转发插件（NapCat / OneBot v11 / aiocqhttp 适配）。

    给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（按群号）发送文字/图片消息。
    底层使用 OneBot v11 原生 API：bot.send_group_msg(group_id=群号, ...)。

    设计说明：
    - 不维护"会话注册表"：群名 → 群号映射由 LLM 的可靠记忆持有，直接按群号调用工具。
    - 定时发送需求由 AstrBot 自带的 future_task 内置工具实现（创建 run_at 任务，
      任务触发时未来的 Agent 会执行 note 中的指令，届时调用本插件的发送工具即可）。
      本插件只负责即时发送。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        # 权限配置：
        # admin_only = True  -> 仅管理员可以使用（推荐）
        # admin_only = False -> 允许名单中的用户 ID 可以使用
        self.admin_only = True
        self.allowed_user_ids: set[str] = set()

    # ---------------------------------------------------------------
    # 内部工具方法
    # ---------------------------------------------------------------

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

    def _build_message(self, message: str, image_url: str, caption: str) -> list:
        """构造 OneBot v11 消息段列表（文字 + 图片）。"""
        segments = []
        text = caption.strip() or message.strip()
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        if image_url.strip():
            segments.append({"type": "image", "data": {"file": image_url.strip()}})
        if not segments:
            segments.append({"type": "text", "data": {"text": "(空消息)"}})
        return segments

    def _validate_image_url(self, image_url: str) -> str | None:
        """校验图片地址，返回错误信息；合法返回 None。"""
        url = image_url.strip()
        if not (url.startswith("http://") or url.startswith("https://")
                or url.startswith("file://") or url.startswith("/")):
            return ("❌ 图片地址格式错误：请提供 http(s):// 网络图片 URL、"
                    "file:// 路径或本地文件路径。")
        return None

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
        if not message.strip():
            return "❌ 消息内容不能为空。"
        try:
            await bot.send_group_msg(
                group_id=int(group_id.strip()),
                message=self._build_message(message, "", ""),
            )
            logger.info(f"跨群消息已发送到群 {group_id}: {message[:50]}")
            return f"✅ 已成功向群 {group_id} 发送消息"
        except Exception as e:
            logger.error(f"跨群发送失败: {e}")
            return f"❌ 发送失败: {e}"

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
        err = self._validate_image_url(image_url)
        if err:
            return err
        try:
            await bot.send_group_msg(
                group_id=int(group_id.strip()),
                message=self._build_message("", image_url, caption),
            )
            logger.info(f"跨群图片已发送到群 {group_id}")
            return f"✅ 已成功向群 {group_id} 发送图片"
        except Exception as e:
            logger.error(f"跨群图片发送失败: {e}")
            return f"❌ 发送失败: {e}"
