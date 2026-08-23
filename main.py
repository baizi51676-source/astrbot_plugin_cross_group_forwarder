import json
from datetime import datetime
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star


class CrossGroupForwarder(Star):
    """跨群消息转发插件（NapCat / OneBot v11 / aiocqhttp 适配）。

    给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（按群号）发送文字、
    图片、语音、文件及合并转发消息，也可向指定 QQ 用户发送私聊消息。

    底层使用 OneBot v11 原生 API（send_group_msg / send_private_msg /
    send_forward_msg 等）。

    设计说明：
    - 不维护"会话注册表"：群名 → 群号映射由 LLM 的可靠记忆持有，
      可先调用 get_group_list 获取群号与群名建立记忆，再按群号调用发送工具。
    - 定时发送需求由 AstrBot 自带的 future_task 内置工具实现，
      本插件只负责即时发送。
    - 内置目标群白名单与操作审计日志，防止滥用。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 持久化数据存到 AstrBot 的 data 目录（官方规范：防止更新插件时数据被覆盖）
        self.data_dir = Path("data/plugins/cross_group_forwarder")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit_file = self.data_dir / "audit.log"

        # 以下配置项均可通过 AstrBot WebUI 插件管理页配置（见 _conf_schema.json）：
        # - admin_only: 仅管理员可用（默认 True）
        # - allowed_user_ids: 允许使用的用户 QQ 号列表（admin_only=False 时生效）
        # - allowed_groups: 目标群白名单，留空表示不限制
        self.admin_only = bool(config.get("admin_only", True))
        self.allowed_user_ids: set[str] = {
            str(x) for x in config.get("allowed_user_ids", [])
        }
        self.allowed_groups: list[str] = [
            str(x) for x in config.get("allowed_groups", [])
        ]

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

    def _group_allowed(self, group_id: str) -> bool:
        """目标群白名单校验：白名单为空时不限制。"""
        return not self.allowed_groups or group_id.strip() in self.allowed_groups

    def _audit(self, tool: str, target: str, content: str,
               success: bool, error: str = ""):
        """追加一条审计日志（JSON Lines）。"""
        try:
            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tool": tool,
                "target": target,
                "content": content[:100],
                "success": success,
                "error": error[:200],
            }
            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"审计日志写入失败: {e}")

    def _build_message(self, message: str = "", image_url: str = "",
                       caption: str = "") -> list:
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

    def _build_media_message(self, media_kind: str, media_url: str,
                             caption: str = "") -> list:
        """构造媒体消息段（record/file），可带文字说明。media_kind: record|file"""
        segments = []
        text = caption.strip()
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        segments.append({"type": media_kind, "data": {"file": media_url.strip()}})
        return segments

    def _validate_media_url(self, media_url: str) -> str | None:
        """校验媒体地址（图片/语音/文件通用），返回错误信息；合法返回 None。"""
        url = media_url.strip()
        if not (url.startswith("http://") or url.startswith("https://")
                or url.startswith("file://") or url.startswith("/")):
            return ("❌ 地址格式错误：请提供 http(s):// 网络 URL、"
                    "file:// 路径或本地文件路径。")
        return None

    async def _send_to_group(self, event: AstrMessageEvent, group_id: str,
                             message_segments: list, tool: str,
                             summary: str) -> str:
        """统一的群消息发送（含权限 / 白名单 / 审计）。"""
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        if not group_id.strip().isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        if not self._group_allowed(group_id):
            return f"❌ 群 {group_id} 不在白名单中，已拒绝发送。"
        try:
            await bot.send_group_msg(
                group_id=int(group_id.strip()), message=message_segments
            )
            logger.info(f"{tool}: 已发送到群 {group_id}")
            self._audit(tool, f"群 {group_id}", summary, True)
            return f"✅ 已成功向群 {group_id} 发送"
        except Exception as e:
            logger.error(f"{tool}: 发送失败: {e}")
            self._audit(tool, f"群 {group_id}", summary, False, str(e))
            return f"❌ 发送失败: {e}"

    async def _send_to_user(self, event: AstrMessageEvent, user_id: str,
                            message_segments: list, tool: str,
                            summary: str) -> str:
        """统一的私聊发送（含权限 / 审计）。"""
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        if not user_id.strip().isdigit():
            return f"❌ QQ 号格式错误：{user_id}。应为纯数字。"
        try:
            await bot.send_private_msg(
                user_id=int(user_id.strip()), message=message_segments
            )
            logger.info(f"{tool}: 已私聊发送给 {user_id}")
            self._audit(tool, f"QQ {user_id}", summary, True)
            return f"✅ 已成功向 QQ {user_id} 发送私聊消息"
        except Exception as e:
            logger.error(f"{tool}: 私聊发送失败: {e}")
            self._audit(tool, f"QQ {user_id}", summary, False, str(e))
            return f"❌ 发送失败: {e}"

    # ---------------------------------------------------------------
    # LLM 工具：群消息
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
        if not message.strip():
            return "❌ 消息内容不能为空。"
        return await self._send_to_group(
            event, group_id, self._build_message(message=message),
            "send_message_to_group", message.strip()[:100],
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
        err = self._validate_media_url(image_url)
        if err:
            return err
        return await self._send_to_group(
            event, group_id,
            self._build_message(image_url=image_url, caption=caption),
            "send_image_to_group", f"图片 {image_url.strip()[:60]} {caption.strip()[:40]}",
        )

    @filter.llm_tool("send_voice_to_group")
    async def send_voice_to_group(self, event: AstrMessageEvent,
                                  group_id: str, voice_url: str, caption: str = ""):
        '''
        向指定的 QQ 群（按群号/群 UID）发送一条语音消息，可附带文字说明。

        参数:
          group_id: 目标 QQ 群号（纯数字，如 "123456789"）
          voice_url: 语音文件地址（http(s):// 网络 URL、file:// 或本地路径），
                     通常为 .mp3/.amr/.silk 等 NapCat 支持的格式
          caption: 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        err = self._validate_media_url(voice_url)
        if err:
            return err
        return await self._send_to_group(
            event, group_id,
            self._build_media_message("record", voice_url, caption),
            "send_voice_to_group", f"语音 {voice_url.strip()[:60]} {caption.strip()[:40]}",
        )

    @filter.llm_tool("send_file_to_group")
    async def send_file_to_group(self, event: AstrMessageEvent,
                                 group_id: str, file_url: str, caption: str = ""):
        '''
        向指定的 QQ 群（按群号/群 UID）发送一个文件，可附带文字说明。

        参数:
          group_id: 目标 QQ 群号（纯数字，如 "123456789"）
          file_url: 文件地址（http(s):// 网络 URL、file:// 或本地路径）
          caption: 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        err = self._validate_media_url(file_url)
        if err:
            return err
        return await self._send_to_group(
            event, group_id,
            self._build_media_message("file", file_url, caption),
            "send_file_to_group", f"文件 {file_url.strip()[:60]} {caption.strip()[:40]}",
        )

    @filter.llm_tool("send_forward_to_group")
    async def send_forward_to_group(self, event: AstrMessageEvent,
                                    group_id: str, messages):
        '''
        向指定的 QQ 群发送合并转发消息（多条消息打包为一条转发卡片）。

        参数:
          group_id: 目标 QQ 群号（纯数字，如 "123456789"）
          messages: 要合并转发的多条消息内容（字符串数组，按顺序排列）

        返回: 发送结果的描述
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        if not group_id.strip().isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        if not self._group_allowed(group_id):
            return f"❌ 群 {group_id} 不在白名单中，已拒绝发送。"
        # 兼容字符串输入（按换行拆分）
        if isinstance(messages, str):
            msgs = [m for m in messages.splitlines() if m.strip()]
        else:
            msgs = [str(m) for m in messages if str(m).strip()]
        if not msgs:
            return "❌ messages 不能为空。"
        try:
            self_id = "10000"
            try:
                self_id = str(event.get_self_id() or self_id)
            except Exception:
                pass
            nodes = []
            for i, m in enumerate(msgs):
                nodes.append({
                    "type": "node",
                    "data": {
                        "name": f"消息 {i + 1}",
                        "uin": self_id,
                        "content": [{"type": "text", "data": {"text": m}}],
                    },
                })
            resp = await bot.send_forward_msg(message=nodes)
            fid = resp.get("message_id") if isinstance(resp, dict) else str(resp)
            await bot.send_group_msg(
                group_id=int(group_id.strip()),
                message=[{"type": "forward", "data": {"id": str(fid)}}],
            )
            logger.info(f"合并转发已发送到群 {group_id}（{len(msgs)} 条）")
            self._audit("send_forward_to_group", f"群 {group_id}",
                        f"合并转发 {len(msgs)} 条", True)
            return f"✅ 已成功向群 {group_id} 发送合并转发（{len(msgs)} 条消息）"
        except Exception as e:
            logger.error(f"合并转发发送失败: {e}")
            self._audit("send_forward_to_group", f"群 {group_id}",
                        f"合并转发 {len(msgs)} 条", False, str(e))
            return f"❌ 发送失败: {e}"

    # ---------------------------------------------------------------
    # LLM 工具：私聊消息
    # ---------------------------------------------------------------

    @filter.llm_tool("send_private_message")
    async def send_private_message(self, event: AstrMessageEvent,
                                   user_id: str, message: str):
        '''
        向指定的 QQ 用户（按 QQ 号）发送一条私聊文本消息。

        参数:
          user_id: 目标 QQ 号（纯数字，如 "123456789"）
          message: 要发送的文本内容

        返回: 发送结果的描述
        '''
        if not message.strip():
            return "❌ 消息内容不能为空。"
        return await self._send_to_user(
            event, user_id, self._build_message(message=message),
            "send_private_message", message.strip()[:100],
        )

    @filter.llm_tool("send_private_image")
    async def send_private_image(self, event: AstrMessageEvent,
                                 user_id: str, image_url: str, caption: str = ""):
        '''
        向指定的 QQ 用户（按 QQ 号）发送一张图片，可附带文字说明。

        参数:
          user_id: 目标 QQ 号（纯数字，如 "123456789"）
          image_url: 图片地址（http(s):// 网络 URL、file:// 或本地路径）
          caption: 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        err = self._validate_media_url(image_url)
        if err:
            return err
        return await self._send_to_user(
            event, user_id,
            self._build_message(image_url=image_url, caption=caption),
            "send_private_image", f"图片 {image_url.strip()[:60]} {caption.strip()[:40]}",
        )

    # ---------------------------------------------------------------
    # LLM 工具：查询
    # ---------------------------------------------------------------

    @filter.llm_tool("get_group_list")
    async def get_group_list(self, event: AstrMessageEvent):
        '''
        获取机器人当前加入的所有 QQ 群（群号 + 群名）。
        可用于建立"群名 → 群号"的映射记忆，之后即可按群号调用发送工具。
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        try:
            groups = await bot.get_group_list()
            if not groups:
                return "机器人当前未加入任何群。"
            lines = [f"{g.get('group_id')} - {g.get('group_name', '')}" for g in groups]
            return "机器人所在的群:\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"获取群列表失败: {e}")
            return f"❌ 获取群列表失败: {e}"