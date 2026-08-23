import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star


class CrossGroupForwarder(Star):
    """跨群消息转发插件（NapCat / OneBot v11 / aiocqhttp 适配）。

    给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（按群号）发送消息，
    并支持定时发送（一次性任务，到期自动发送）。

    底层使用 OneBot v11 原生 API：bot.send_group_msg(group_id=群号, ...)。

    说明：本插件不维护"会话注册表"，群号由 LLM 的可靠记忆直接持有——
    LLM 只需记住"群名 → 群号"映射，然后按群号调用工具即可。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        # 持久化数据存到 AstrBot 的 data 目录（官方规范：防止更新插件时数据被覆盖）
        self.data_dir = Path("data/plugins/cross_group_forwarder")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.schedules_file = self.data_dir / "schedules.json"
        # {schedule_id: {"id", "group_id", "message", "image_url", "caption",
        #                "trigger_at", "created_at"}}
        self.schedules: dict[str, dict] = self._load_schedules()

        # 权限配置：
        # admin_only = True  -> 仅管理员可以使用（推荐）
        # admin_only = False -> 允许名单中的用户 ID 可以使用
        self.admin_only = True
        self.allowed_user_ids: set[str] = set()

        # 缓存的 bot 实例（CQHttp）。定时任务触发时没有事件上下文，
        # 需要复用最近一次从消息事件中获取的 bot。
        # 适配器的 self.bot 在初始化时创建一次、断线重连不换对象，缓存引用是安全的。
        self._cached_bot = None

        # 启动后台调度循环（同时恢复持久化的定时任务）
        try:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        except RuntimeError as e:
            # 极少数情况下插件加载不在事件循环内，退化为惰性启动
            logger.warning(f"调度循环启动失败，将延迟启动: {e}")
            self._scheduler_task = None

    # ---------------------------------------------------------------
    # 内部工具方法
    # ---------------------------------------------------------------

    def _load_schedules(self) -> dict:
        if self.schedules_file.exists():
            try:
                return json.loads(self.schedules_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"读取 schedules.json 失败: {e}")
        return {}

    def _save_schedules(self):
        try:
            self.schedules_file.write_text(
                json.dumps(self.schedules, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存 schedules.json 失败: {e}")

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """权限校验：管理员，或（当 admin_only=False 时）在允许名单中。"""
        if self.admin_only:
            return event.is_admin()
        return event.get_sender_id() in self.allowed_user_ids

    def _get_bot(self, event: AstrMessageEvent):
        """获取 aiocqhttp (OneBot v11/NapCat) 客户端实例并缓存。

        AiocqhttpMessageEvent 上有 bot 属性（CQHttp 实例），
        可直接调用 OneBot v11 原生 API，如 send_group_msg。
        """
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._cached_bot = bot
        return bot

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

    def _parse_trigger_time(self, at_time: str, delay_seconds: str) -> tuple:
        """解析触发时刻。返回 (trigger_at 时间戳, 错误信息)。"""
        at_time = (at_time or "").strip()
        delay_seconds = (delay_seconds or "").strip()
        if at_time and delay_seconds:
            return None, "❌ at_time 与 delay_seconds 只能指定其中一个。"
        if not at_time and not delay_seconds:
            return None, "❌ 请指定 at_time（ISO8601 时刻）或 delay_seconds（延迟秒数）。"
        now = time.time()
        if delay_seconds:
            try:
                delay = float(delay_seconds)
            except ValueError:
                return None, f"❌ delay_seconds 格式错误：{delay_seconds}，应为数字（秒）。"
            if delay <= 0:
                return None, "❌ delay_seconds 必须大于 0。"
            return now + delay, None
        try:
            dt = datetime.fromisoformat(at_time)
        except ValueError:
            return None, (f"❌ at_time 格式错误：{at_time}。"
                          f"请使用 ISO8601 格式，如 2026-08-23T14:30:00")
        ts = dt.timestamp()
        if ts <= now:
            return None, f"❌ at_time {at_time} 已过期，请指定未来的时间。"
        return ts, None

    async def _execute_schedule(self, s: dict):
        """执行一个定时任务：向目标群发送消息。"""
        bot = self._cached_bot
        if bot is None:
            logger.error(
                f"定时任务 {s['id']} 触发时无可用 bot 实例"
                "（插件重启后尚未收到任何消息事件），已跳过该任务。"
            )
            return
        message = self._build_message(
            s.get("message", ""), s.get("image_url", ""), s.get("caption", "")
        )
        await bot.send_group_msg(group_id=int(s["group_id"]), message=message)
        logger.info(f"定时任务 {s['id']} 已执行：消息已发送到群 {s['group_id']}")

    async def _dispatch_due_schedules(self):
        """检查并执行所有到期的定时任务。"""
        now = time.time()
        due_ids = [sid for sid, s in self.schedules.items() if s["trigger_at"] <= now]
        for sid in due_ids:
            s = self.schedules.pop(sid, None)
            if s is None:
                continue
            try:
                await self._execute_schedule(s)
            except Exception as e:
                logger.error(f"定时任务 {sid} 执行失败: {e}")
        if due_ids:
            self._save_schedules()

    async def _scheduler_loop(self):
        """后台调度循环：每 5 秒检查一次是否有到期任务。"""
        while True:
            try:
                await self._dispatch_due_schedules()
            except Exception as e:
                logger.error(f"调度循环异常: {e}")
            await asyncio.sleep(5)

    # ---------------------------------------------------------------
    # 管理指令
    # ---------------------------------------------------------------

    @filter.command("list_schedules")
    async def list_schedules_cmd(self, event: AstrMessageEvent):
        '''列出所有定时发送任务（ID、目标群、内容、执行时间）。仅管理员可用。'''
        if not self._is_allowed(event):
            yield event.plain_result("❌ 无权限：仅管理员可以查看定时任务。")
            return
        if not self.schedules:
            yield event.plain_result("📭 当前没有定时任务。")
            return
        lines = []
        for sid, s in sorted(self.schedules.items(),
                             key=lambda kv: kv[1]["trigger_at"]):
            when = datetime.fromtimestamp(s["trigger_at"]).strftime("%Y-%m-%d %H:%M:%S")
            content = s.get("caption") or s.get("message") or "(图片)"
            lines.append(f"• [{sid[:8]}] 群 {s['group_id']} | {when}\n  {content[:40]}")
        yield event.plain_result("📋 定时任务:\n" + "\n".join(lines))

    # ---------------------------------------------------------------
    # LLM 工具
    # ---------------------------------------------------------------

    @filter.llm_tool("send_message_to_group")
    async def send_message_to_group(self, event: AstrMessageEvent,
                                    group_id: str, message: str):
        '''
        立即向指定的 QQ 群（按群号/群 UID）发送一条文本消息。适合通知、提醒、转发信息等场景。

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
        立即向指定的 QQ 群（按群号/群 UID）发送一张图片，可附带文字说明。

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

    @filter.llm_tool("schedule_message_to_group")
    async def schedule_message_to_group(self, event: AstrMessageEvent,
                                        group_id: str, message: str,
                                        at_time: str = "", delay_seconds: str = ""):
        '''
        定时向指定的 QQ 群（按群号/群 UID）发送一条文本消息。适合定时提醒、定时通知等场景。

        参数:
          group_id: 目标 QQ 群号（纯数字，如 "123456789"）
          message: 要定时发送的文本内容
          at_time: 可选，发送时刻（ISO8601 格式，本地时间），如 "2026-08-23T14:30:00"
          delay_seconds: 可选，从现在起多少秒后发送（与 at_time 二选一）

        返回: 定时任务的 ID 与预计执行时间
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
        trigger_at, err = self._parse_trigger_time(at_time, delay_seconds)
        if err:
            return err
        sid = uuid.uuid4().hex
        when_str = datetime.fromtimestamp(trigger_at).strftime("%Y-%m-%d %H:%M:%S")
        self.schedules[sid] = {
            "id": sid,
            "group_id": group_id.strip(),
            "message": message,
            "image_url": "",
            "caption": "",
            "trigger_at": trigger_at,
            "created_at": time.time(),
        }
        self._save_schedules()
        logger.info(f"已创建定时任务 {sid}：{when_str} 向群 {group_id} 发送消息")
        return (f"✅ 已创建定时任务 {sid[:8]}，将于 {when_str} 向群 {group_id} 发送消息。"
                f"可使用 cancel_schedule 取消（ID: {sid}）。")

    @filter.llm_tool("schedule_image_to_group")
    async def schedule_image_to_group(self, event: AstrMessageEvent,
                                      group_id: str, image_url: str,
                                      caption: str = "", at_time: str = "",
                                      delay_seconds: str = ""):
        '''
        定时向指定的 QQ 群（按群号/群 UID）发送一张图片，可附带文字说明。

        参数:
          group_id: 目标 QQ 群号（纯数字，如 "123456789"）
          image_url: 图片地址（http(s):// 网络 URL、file:// 或本地路径）
          caption: 可选，附带发送的文字说明
          at_time: 可选，发送时刻（ISO8601 格式，本地时间），如 "2026-08-23T14:30:00"
          delay_seconds: 可选，从现在起多少秒后发送（与 at_time 二选一）

        返回: 定时任务的 ID 与预计执行时间
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
        trigger_at, err = self._parse_trigger_time(at_time, delay_seconds)
        if err:
            return err
        sid = uuid.uuid4().hex
        when_str = datetime.fromtimestamp(trigger_at).strftime("%Y-%m-%d %H:%M:%S")
        self.schedules[sid] = {
            "id": sid,
            "group_id": group_id.strip(),
            "message": "",
            "image_url": image_url.strip(),
            "caption": caption,
            "trigger_at": trigger_at,
            "created_at": time.time(),
        }
        self._save_schedules()
        logger.info(f"已创建定时图片任务 {sid}：{when_str} 向群 {group_id} 发送图片")
        return (f"✅ 已创建定时图片任务 {sid[:8]}，将于 {when_str} 向群 {group_id} 发送图片。"
                f"可使用 cancel_schedule 取消（ID: {sid}）。")

    @filter.llm_tool("cancel_schedule")
    async def cancel_schedule(self, event: AstrMessageEvent, schedule_id: str):
        '''
        取消一个尚未执行的定时任务。

        参数:
          schedule_id: 定时任务 ID（创建定时任务时返回，也可用 list_schedules 查询）

        返回: 取消结果的描述
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        schedule_id = schedule_id.strip()
        # 兼容简写 ID（创建时返回前 8 位）
        matched = None
        for sid in self.schedules:
            if sid == schedule_id or sid.startswith(schedule_id):
                matched = sid
                break
        if matched is None:
            return f"❌ 未找到定时任务：{schedule_id}。可用 list_schedules 查看全部任务。"
        self.schedules.pop(matched, None)
        self._save_schedules()
        logger.info(f"已取消定时任务 {matched}")
        return f"✅ 已取消定时任务 {matched[:8]}。"

    @filter.llm_tool("list_schedules")
    async def list_schedules_tool(self, event: AstrMessageEvent):
        '''列出所有尚未执行的定时任务（ID、目标群、内容、执行时间）。'''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        if not self.schedules:
            return "当前没有定时任务。"
        lines = []
        for sid, s in sorted(self.schedules.items(),
                             key=lambda kv: kv[1]["trigger_at"]):
            when = datetime.fromtimestamp(s["trigger_at"]).strftime("%Y-%m-%d %H:%M:%S")
            content = s.get("caption") or s.get("message") or "(图片)"
            lines.append(f"[{sid}] 群 {s['group_id']} | {when} | {content[:30]}")
        return "定时任务:\n" + "\n".join(lines)

    # ---------------------------------------------------------------

    async def terminate(self):
        '''插件被卸载/停用时调用'''
        if getattr(self, "_scheduler_task", None):
            self._scheduler_task.cancel()
        self._save_schedules()
