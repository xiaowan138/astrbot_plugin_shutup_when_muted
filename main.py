"""
被禁言了我闭嘴 (astrbot_plugin_shutup_when_muted)
=================================================

当机器人在 QQ 群里被禁言时,自动"闭嘴":拦截所有发往该群的消息发送,
避免被禁言后仍然尝试发消息,触发 QQ 风控;
解除禁言(或禁言时长到期)后,自动恢复对该群的消息发送。

这个插件不需要任何手动操作,装上就能用。它做的事情很简单:

1. 监听 QQ 群的"禁言 / 解禁"通知(OneBot 的 group_ban 事件);
2. 发现是机器人自己被禁言后,把这个群记入"禁言中"名单;
3. 禁言期间,所有要发往这个群的消息都会被拦下来、不会真的发出去
   (包括 AI 回复、其他插件的回复、主动推送的消息,一条不漏);
4. 收到解禁通知,或者禁言时长到了,自动把这个群移出名单,恢复发言。

为什么能"一条不漏"?
    QQ(OneBot)平台的所有消息发送,最终都会经过 aiocqhttp 适配器的同一个
    出口方法(AiocqhttpMessageEvent.send_message)。本插件在这里装了一个
    "安检门":目标群在禁言名单里,消息就直接丢弃,不再往下走。

细节说明
--------
- 只关心"机器人自己被禁言":群通知里被禁言的 user_id 等于机器人自己的
  self_id 时才生效,别人被禁言与本插件无关;
- 禁言时长到期会自动恢复:即使没有收到解禁通知(比如机器人掉线期间被解禁),
  也会按 OneBot 通知里带的 duration 自动计时,到点自动解除;
- 状态会保存到本插件目录下的 muted_groups.json,重启 AstrBot 不丢失;
- 管理员可以用 /闭嘴状态、/闭嘴解除 查看和手动解除(兜底手段)。

兼容性
------
- 面向 AstrBot v4(>= 4.0.0),支持 OneBot v11 协议(aiocqhttp 适配器);
- 如果 AstrBot 版本太旧、导入不了 aiocqhttp 适配器类,插件会降级运行:
  仍然记录禁言状态、提供命令,但不再自动拦截发送(日志里会有提示)。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

# AstrBot 提供的公共 API
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# ---------------------------------------------------------------------------
# 导入 aiocqhttp(OneBot)适配器的事件类。
# 不同版本的 AstrBot 路径可能不同,所以这里做了两套路径的兼容。
# ---------------------------------------------------------------------------
_ADAPTER_AVAILABLE = False
AiocqhttpMessageEvent = None
try:
    # AstrBot v4(>= 4.x)的路径
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    _ADAPTER_AVAILABLE = True
except Exception:  # pragma: no cover - 兼容旧版 AstrBot 的导入路径
    try:
        from astrbot.core.provider.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        _ADAPTER_AVAILABLE = True
    except Exception:
        _ADAPTER_AVAILABLE = False


def _outline(chain: Any) -> str:
    """从要发送的消息里提取一小段文字,用于写日志(最多 60 字)。

    只是给日志看的,不会影响真正的发送内容。
    """
    try:
        parts = []
        for comp in chain.chain:
            text = getattr(comp, "text", "") or ""
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        outline = " ".join(parts)
        return outline[:60] + ("..." if len(outline) > 60 else "")
    except Exception:
        return "?"


class ShutUpWhenMuted(Star):
    """被禁言了我闭嘴:机器人被群禁言时自动闭嘴,解禁后恢复发言。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config if config is not None else {}

        # 禁言名单:{ 群号(str): 禁言截止时间(unix 时间戳) }
        # 截止时间为 None 表示"时长未知",只能等解禁通知或手动解除。
        self.muted: dict[str, float | None] = {}

        # 发送拦截补丁相关状态
        self._patch_installed = False
        self._orig_send_message = None

        # 后台任务(定时检查禁言时长是否到期)
        self._expire_task: asyncio.Task | None = None

        # 禁言状态保存文件,放在插件自己的目录里
        self._state_file = Path(__file__).resolve().parent / "muted_groups.json"

        # 启动时把上次保存的状态读回来(重启不丢失)
        self._load_state()

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any) -> Any:
        """读取插件配置,出错时返回默认值。"""
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    # ------------------------------------------------------------ 状态持久化
    def _save_state(self) -> None:
        """把禁言名单保存到文件。"""
        try:
            self._state_file.write_text(
                json.dumps(self.muted, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            # 保存失败不影响功能,只是下次重启会丢失状态
            logger.warning(f"被禁言了我闭嘴: 保存状态失败 {exc}")

    def _load_state(self) -> None:
        """从文件读取禁言名单。"""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.muted = {
                        str(k): (float(v) if isinstance(v, (int, float)) else None)
                        for k, v in data.items()
                    }
                    if self.muted:
                        logger.info(
                            "被禁言了我闭嘴: 已恢复 %s 个群的禁言拦截状态",
                            len(self.muted),
                        )
        except Exception as exc:
            logger.warning(f"被禁言了我闭嘴: 读取状态失败 {exc}")

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件被启用时调用:安装发送拦截,启动定时检查任务。"""
        self._install_patch()
        self._check_platform()
        if self._expire_task is None or self._expire_task.done():
            self._expire_task = asyncio.create_task(self._expire_loop())

    async def terminate(self) -> None:
        """插件被停用/重载时调用:卸掉拦截,取消后台任务。"""
        self._uninstall_patch()
        if self._expire_task is not None:
            self._expire_task.cancel()
            try:
                await self._expire_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("被禁言了我闭嘴: 关闭后台任务时出错")
            self._expire_task = None

    def _check_platform(self) -> None:
        """检查当前是否真的在使用 OneBot(aiocqhttp)平台,给用户一个提示。"""
        try:
            names = [
                p.meta().name
                for p in self.context.platform_manager.platform_insts
            ]
            if names and "aiocqhttp" not in names:
                logger.warning(
                    "被禁言了我闭嘴: 当前没有 OneBot v11(aiocqhttp)平台,"
                    "发送拦截不会生效(仅记录禁言状态)。"
                )
        except Exception:
            pass

    # ------------------------------------------------------------ 发送拦截
    def _install_patch(self) -> None:
        """在 aiocqhttp 发送出口上装"安检门"。

        aiocqhttp 的群消息发送(回复 / 主动发送 / 流式输出)最后都会经过
        AiocqhttpMessageEvent.send_message 这个方法,在这里拦一次就够了。
        """
        if not _ADAPTER_AVAILABLE:
            logger.warning(
                "被禁言了我闭嘴: 未找到 aiocqhttp 适配器,发送拦截不可用"
                "(仍会记录禁言状态)。"
            )
            return

        # 插件重载时,旧的"安检门"可能还在,这时只需要更新状态引用
        if getattr(AiocqhttpMessageEvent, "_shutup_send_patched", False):
            AiocqhttpMessageEvent._shutup_muted_groups = self.muted
            AiocqhttpMessageEvent._shutup_log_blocked = bool(
                self._cfg("log_blocked_sends", True)
            )
            self._patch_installed = True
            logger.info("被禁言了我闭嘴: 发送拦截已就绪(重载更新)。")
            return

        # 记住原来的发送方法,卸载时要恢复原样
        self._orig_send_message = AiocqhttpMessageEvent.send_message.__func__
        orig = self._orig_send_message
        AiocqhttpMessageEvent._shutup_orig_send_message = orig

        @classmethod
        async def _guarded_send_message(
            cls,
            bot,
            message_chain,
            event=None,
            is_group: bool = False,
            session_id: str | None = None,
            **kwargs,
        ):
            # 禁言名单放在类属性上,插件重载后也能读到最新状态
            muted = getattr(cls, "_shutup_muted_groups", None)
            if is_group and session_id is not None and muted:
                gid = str(session_id)
                if gid in muted:
                    if getattr(cls, "_shutup_log_blocked", True):
                        logger.info(
                            "被禁言了我闭嘴: 拦截发往群 %s 的消息(禁言中) | %s",
                            gid,
                            _outline(message_chain),
                        )
                    return  # 直接丢弃,不发送
            # 不在禁言名单,原样发送
            await orig(cls, bot, message_chain, event, is_group, session_id, **kwargs)

        AiocqhttpMessageEvent.send_message = _guarded_send_message
        AiocqhttpMessageEvent._shutup_send_patched = True
        AiocqhttpMessageEvent._shutup_muted_groups = self.muted
        AiocqhttpMessageEvent._shutup_log_blocked = bool(
            self._cfg("log_blocked_sends", True)
        )
        self._patch_installed = True
        logger.info("被禁言了我闭嘴: 发送拦截已安装。")

    def _uninstall_patch(self) -> None:
        """把发送方法恢复成原来的样子。"""
        if not _ADAPTER_AVAILABLE:
            return
        orig = self._orig_send_message or getattr(
            AiocqhttpMessageEvent, "_shutup_orig_send_message", None
        )
        if orig is not None:
            try:
                AiocqhttpMessageEvent.send_message = classmethod(orig)
            except Exception as exc:
                logger.warning(f"被禁言了我闭嘴: 卸载拦截失败 {exc}")
        self._orig_send_message = None
        self._patch_installed = False
        for attr in (
            "_shutup_send_patched",
            "_shutup_muted_groups",
            "_shutup_orig_send_message",
            "_shutup_log_blocked",
        ):
            try:
                delattr(AiocqhttpMessageEvent, attr)
            except AttributeError:
                pass

    # ------------------------------------------------------------ 事件监听
    @filter.event_message_type(filter.EventMessageType.ALL, priority=500)
    async def on_group_ban_notice(self, event: AstrMessageEvent):
        """监听群禁言 / 解禁通知(OneBot group_ban)。

        只处理"机器人自己被禁言 / 被解禁"的情况:
        - sub_type == "ban"      -> 把群加入禁言名单;
        - sub_type == "lift_ban" -> 把群移出禁言名单。
        """
        # 通知的原始数据在 event.message_obj.raw_message 里(OneBot 事件字典)
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return
        # 只处理群禁言通知,其他事件一律不管
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_ban":
            return

        group_id = str(raw.get("group_id", "") or "")
        user_id = str(raw.get("user_id", "") or "")
        self_id = str(raw.get("self_id", "") or "") or str(event.get_self_id() or "")
        if not group_id or not user_id or not self_id:
            return
        # 被禁言的不是机器人自己,与本插件无关
        if user_id != self_id:
            return

        sub_type = raw.get("sub_type", "")
        if sub_type == "ban":
            # 禁言时长(秒)。时长未知时记为 None,只等解禁通知。
            duration = raw.get("duration", 0) or 0
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                duration = 0
            mute_until = time.time() + duration if duration > 0 else None
            self.muted[group_id] = mute_until
            self._save_state()
            if mute_until is None:
                logger.info(
                    "被禁言了我闭嘴: 群 %s 禁言中(时长未知), 已拦截该群消息发送",
                    group_id,
                )
            else:
                logger.info(
                    "被禁言了我闭嘴: 群 %s 被禁言 %s 秒, 已拦截该群消息发送",
                    group_id,
                    duration,
                )
        elif sub_type == "lift_ban":
            if group_id in self.muted:
                self.muted.pop(group_id, None)
                self._save_state()
                logger.info("被禁言了我闭嘴: 群 %s 已解禁, 恢复消息发送", group_id)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=900)
    async def on_group_message(self, event: AstrMessageEvent):
        """禁言期间,来自被禁言群的消息不再触发默认 AI 回复。

        这样 AI 不会为了一个"发不出去的回复"白烧 token。
        注意:这里只是拦掉默认 AI 回复,其他插件照常工作,
        而它们产生的回复会被上面的发送拦截拦下来,不会真的发出去。
        """
        if not self._cfg("skip_llm_when_muted", True):
            return
        gid = str(event.get_group_id() or "")
        if gid and gid in self.muted:
            # should_call_llm(True) = 阻止 AstrBot 默认的 LLM 请求链路
            # (这是 AstrBot 的固定用法,别传 False,那样等于没拦)
            suppress = getattr(event, "should_call_llm", None)
            if callable(suppress):
                suppress(True)
            elif hasattr(event, "call_llm"):  # 兼容非常旧的版本
                event.call_llm = True

    # ------------------------------------------------------------ 管理命令
    @filter.command("闭嘴状态", alias={"禁言状态", "shutup_status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status_cmd(self, event: AstrMessageEvent):
        """查看当前处于禁言拦截状态的群。"""
        if not self.muted:
            yield event.plain_result("当前没有被禁言拦截的群,一切正常。")
            return
        lines = ["当前正在拦截消息发送的群:"]
        now = time.time()
        for gid, until in sorted(self.muted.items()):
            if until is None:
                lines.append(f"  · 群 {gid} —— 禁言中(时长未知,等待解禁通知或手动解除)")
            else:
                remain = max(0, int(until - now))
                lines.append(f"  · 群 {gid} —— 剩余约 {remain} 秒后自动恢复")
        yield event.plain_result("\n".join(lines))

    @filter.command("闭嘴解除", alias={"shutup_unmute"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unmute_cmd(self, event: AstrMessageEvent, group_id: str = ""):
        """手动解除指定群的禁言拦截;不填群号则解除当前群。

        用法: /闭嘴解除 123456 或 /闭嘴解除(在群里发)
        """
        if not group_id:
            group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("请指定群号,例如:/闭嘴解除 123456")
            return
        if group_id in self.muted:
            self.muted.pop(group_id, None)
            self._save_state()
            yield event.plain_result(f"已解除群 {group_id} 的禁言拦截,恢复消息发送。")
        else:
            yield event.plain_result(f"群 {group_id} 当前不在禁言拦截列表中。")

    # ------------------------------------------------------------ 过期检查
    def _expire_once(self) -> list[str]:
        """清理禁言时长已到的群,返回被清理的群号列表。

        兜底机制:即使一直没收到解禁通知,到点也会自动恢复。
        """
        now = time.time()
        expired = [
            gid
            for gid, until in self.muted.items()
            if until is not None and now >= until
        ]
        for gid in expired:
            self.muted.pop(gid, None)
            logger.info("被禁言了我闭嘴: 群 %s 禁言时长已到, 自动恢复消息发送", gid)
        if expired:
            self._save_state()
        return expired

    async def _expire_loop(self) -> None:
        """后台周期任务:每 30 秒检查一次禁言时长是否到期。"""
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break  # 插件被停用时退出
            try:
                self._expire_once()
            except Exception:
                logger.exception("被禁言了我闭嘴: 定时检查禁言状态时出错")
