"""
被禁言了我闭嘴 (astrbot_plugin_shutup_when_muted)
=================================================

当机器人在 QQ 群里被禁言(或群开启全员禁言)时,自动"闭嘴":拦截所有
发往该群的消息发送,避免发不出去还不停尝试、触发 QQ 风控;
解除禁言(或禁言时长到期)后,自动恢复对该群的消息发送。

这个插件不需要任何手动操作,装上就能用。它做的事情很简单:

1. 监听 QQ 群的"禁言 / 解禁"通知(OneBot 的 group_ban 事件);
2. 发现是机器人自己被禁言(或群开了全员禁言)后,把这个群记入
   "禁言中"名单;
3. 名单期间,所有要发往这个群的消息都会被拦下来、不会真的发出去
   (包括 AI 回复、其他插件的回复、主动推送的消息,一条不漏);
4. 收到解禁通知,或者禁言时长到了,自动把这个群移出名单,恢复发言。

为什么能"一条不漏"?
    QQ(OneBot)平台的所有消息发送,最终都会经过 aiocqhttp 适配器的同一个
    出口方法(AiocqhttpMessageEvent.send_message)。本插件在这里装了一个
    "安检门":目标群在禁言名单里,消息就直接丢弃,不再往下走。
    回复(send)、流式回复(send_streaming)、主动推送(send_by_session)
    三条路径都会汇聚到这个出口,所以没有绕过的地方。

细节说明
--------
- 只关心"机器人自己被禁言"和"全员禁言":群通知里被禁言的 user_id 等于
  机器人自己的 self_id 时生效;全员禁言的 user_id 为 0,默认也会闭嘴
  (机器人是群管理员时不受全员禁言影响,会自动放过,详见配置项
  block_on_whole_group_mute);
- 禁言时长到期会自动恢复:即使没有收到解禁通知(比如机器人掉线期间被解禁),
  也会按 OneBot 通知里带的 duration 自动计时,到点自动解除(发送时也会
  即时校验,不会多拦一秒);
- 自检纠偏:定时向协议端核对机器人是否真的还被禁言,把已经失效的拦截清掉。
  这解决了两类麻烦:协议端没给禁言时长、又错过了"解禁"通知时,群会被一直
  拦着;或者协议端给的时长不准,导致拦早了/拦晚了。详见配置项
  self_check_interval;
- 多账号隔离:同一群里如果同时有多个机器人账号,被禁言的是哪个账号就只拦
  哪个账号,不会连累另一个(按通知里的 self_id 区分);
- 状态保存在 AstrBot 的 data/plugin_data/ 目录下,重启、插件升级都不丢失;
- 管理员可以用 /闭嘴、/闭嘴状态、/闭嘴自检、/闭嘴解除 手动闭嘴、查看、
  自检和解除(兜底手段)。手动 /闭嘴 的群会一直闭嘴,直到手动解除。

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

_PLUGIN_NAME = "astrbot_plugin_shutup_when_muted"

# 状态文件里表示"不知道是哪个机器人账号"的占位账号。
# v1.0 / v1.1 的旧状态只记了群、没记账号,迁移过来时用这个占位,
# 表示"查不出是谁被禁言了",按保守处理(哪个账号都拦)。
_ANY_ACCOUNT = "*"


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

        # 禁言名单:{ 群号(str): 条目 }
        # 条目字段:
        #   whole       -> 群是否开着全员禁言(没有时长概念,等解除通知)
        #   whole_owner -> 记录"开启全员禁言"的通知来自哪个机器人账号(自检查身份用)
        #   manual      -> 是否是管理员用 /闭嘴 手动闭嘴的(整群闭嘴,不会被自动清掉)
        #   accounts    -> { 机器人账号: 禁言截止时间戳 };None 表示时长未知。
        #                  同一个群里多个账号各记各的,互不覆盖
        self.muted: dict[str, dict] = {}

        # 本次运行以来每个群被拦下的消息条数(仅内存,重启清零)
        self.blocked_count: dict[str, int] = {}

        # 每个群对应的 aiocqhttp 客户端(自检时要用),收到通知时顺手记下来
        self._bots: dict[str, Any] = {}

        # 发送拦截补丁相关状态
        self._patch_installed = False
        self._orig_send_message = None

        # 后台任务(定时检查禁言时长是否到期 + 自检纠偏)
        self._expire_task: asyncio.Task | None = None
        self._last_self_check = 0.0

        # 禁言状态保存文件:优先放 AstrBot 的插件数据目录(升级不丢),
        # 太旧的 AstrBot 没有 StarTools 时退回插件自己的目录
        self._state_file = self._resolve_state_file()

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
    def _resolve_state_file(self) -> Path:
        """决定状态文件的位置,并把旧位置的文件迁移过来。"""
        legacy = Path(__file__).resolve().parent / "muted_groups.json"
        try:
            from astrbot.api.star import StarTools

            data_dir = StarTools.get_data_dir(_PLUGIN_NAME)
            state_file = data_dir / "muted_groups.json"
            if not state_file.exists() and legacy.exists():
                state_file.write_text(
                    legacy.read_text(encoding="utf-8"), encoding="utf-8"
                )
                legacy.unlink()
            return state_file
        except Exception:
            # 老版本 AstrBot 或数据目录不可写:退回插件目录
            return legacy

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

    @staticmethod
    def _entry(
        whole: bool = False,
        manual: bool = False,
        accounts: dict[str, float | None] | None = None,
        whole_owner: str | None = None,
    ) -> dict:
        """构造一个禁言条目。"""
        return {
            "whole": whole,
            "whole_owner": whole_owner,
            "manual": manual,
            "accounts": dict(accounts or {}),
        }

    @staticmethod
    def _norm_until(value: Any) -> float | None:
        """把状态文件里的截止时间统一成 float 或 None。"""
        return float(value) if isinstance(value, (int, float)) else None

    def _load_state(self) -> None:
        """从文件读取禁言名单,兼容 v1.0 / v1.1 / v1.2 的旧格式。

        旧格式一个群只有一个槽位,读进来后统一升级成 accounts 映射;
        旧条目如果没记 self_id,用 _ANY_ACCOUNT 占位(查不出是哪个账号,
        按保守处理)。
        """
        try:
            if not self._state_file.exists():
                return
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            loaded: dict[str, dict] = {}
            for k, v in data.items():
                gid = str(k)
                # 新格式(v1.3.0):带 accounts 映射,一个群可以记多个账号
                if isinstance(v, dict) and isinstance(v.get("accounts"), dict):
                    accounts = {
                        str(acc): self._norm_until(until)
                        for acc, until in v["accounts"].items()
                    }
                    owner = v.get("whole_owner")
                    loaded[gid] = self._entry(
                        whole=bool(v.get("whole")),
                        manual=bool(v.get("manual")),
                        accounts=accounts,
                        whole_owner=str(owner) if owner is not None else None,
                    )
                # v1.1 / v1.2 的旧格式:一个群一个槽位(until + self_id)
                elif isinstance(v, dict):
                    raw_self = v.get("self_id")
                    account = str(raw_self) if raw_self is not None else _ANY_ACCOUNT
                    loaded[gid] = self._entry(
                        whole=bool(v.get("whole")),
                        manual=bool(v.get("manual")),
                        accounts={account: self._norm_until(v.get("until"))},
                        whole_owner=str(raw_self) if raw_self is not None else None,
                    )
                # v1.0 的旧格式:纯时间戳或 null
                elif isinstance(v, (int, float)):
                    loaded[gid] = self._entry(accounts={_ANY_ACCOUNT: float(v)})
                elif v is None:
                    loaded[gid] = self._entry(accounts={_ANY_ACCOUNT: None})

            # 禁言早就到期的(比如停机期间走完),不用再拦
            now = time.time()
            purged = False
            for gid, info in list(loaded.items()):
                if info["whole"] or info["manual"]:
                    continue
                accounts = info["accounts"]
                for acc, until in list(accounts.items()):
                    if until is not None and now >= until:
                        accounts.pop(acc, None)
                        purged = True
                if not accounts:
                    loaded.pop(gid, None)
            self.muted = loaded
            if purged:
                self._save_state()
            if self.muted:
                logger.info(
                    "被禁言了我闭嘴: 已恢复 %s 个群的禁言拦截状态",
                    len(self.muted),
                )
        except Exception as exc:
            logger.warning(f"被禁言了我闭嘴: 读取状态失败 {exc}")

    # ------------------------------------------------------------ 名单操作
    def _is_blocked(self, gid: str, self_id: str | None = None) -> bool:
        """判断一个群当前是否处于拦截状态(顺带清理已过期的禁言)。

        self_id 是"正在发言的这个机器人账号":个人禁言按账号隔离,同一个群里
        被禁言的是别的机器人账号时,当前账号不该被连累;全员禁言和手动闭嘴
        是"整群闭嘴",对所有账号生效。
        """
        info = self.muted.get(gid)
        if info is None:
            return False
        # 全员禁言 / 手动闭嘴:整群生效,不看是哪个账号在发言
        if info.get("whole") or info.get("manual"):
            return True
        accounts = info.get("accounts") or {}
        if not accounts:
            return False
        # 先把已经到期的账号清掉(发送时即时校验,不用等后台循环)。
        # 必须整体清完再判断,否则"所有账号都已到期"时会被下面的
        # "账号未知就保守拦截"误拦。
        now = time.time()
        purged = False
        for acc, until in list(accounts.items()):
            if until is not None and now >= until:
                accounts.pop(acc, None)
                purged = True
        if purged:
            self._drop_if_empty(gid, info)
            self._save_state()
        accounts = info.get("accounts") or {}
        if not accounts:
            return False
        if not self_id:
            # 还有活跃的禁言、但判断不出发言的是哪个账号(主动推送):保守拦截
            return True
        # 被禁言的是别的机器人账号时,不连累当前账号
        account = _ANY_ACCOUNT if _ANY_ACCOUNT in accounts else str(self_id)
        return account in accounts

    def _drop_if_empty(self, gid: str, info: dict) -> None:
        """条目里既没有禁言账号、也没有全员禁言/手动闭嘴时,整条清掉。"""
        if not info.get("whole") and not info.get("manual") and not info.get("accounts"):
            self.muted.pop(gid, None)
            self.blocked_count.pop(gid, None)

    def _mute_self(
        self,
        gid: str,
        until: float | None,
        self_id: str | None = None,
        manual: bool = False,
    ) -> None:
        """让机器人对某个群闭嘴。

        - 被群禁言(manual=False):按账号记录截止时间(None 表示时长未知);
        - 管理员手动闭嘴(manual=True):整群生效,不受账号限制,也不自动恢复。
        """
        info = self.muted.get(gid)
        if info is None:
            info = self._entry()
            self.muted[gid] = info
        if manual:
            info["manual"] = True
        else:
            info["accounts"][str(self_id) if self_id else _ANY_ACCOUNT] = until
        self._save_state()

    def _unmute_self(self, gid: str, self_id: str | None = None) -> None:
        """机器人自己被解禁:只清掉这个账号,不连累同群的其他账号。

        全员禁言还在、或者是管理员手动闭嘴的群,继续保持拦截。
        """
        info = self.muted.get(gid)
        if info is None:
            return
        accounts = info.get("accounts") or {}
        if self_id:
            accounts.pop(str(self_id), None)
        # 旧状态里"不知道是哪个账号"的占位也一并清掉:既然收到了这个群的
        # 解禁通知,再拿它去拦所有账号就没道理了
        accounts.pop(_ANY_ACCOUNT, None)
        self._drop_if_empty(gid, info)
        self._save_state()

    def _set_whole_mute(self, gid: str, on: bool, self_id: str | None = None) -> None:
        """记录 / 清除全员禁言状态(整群生效,不分账号)。"""
        info = self.muted.get(gid)
        if on:
            if info is None:
                info = self._entry()
                self.muted[gid] = info
            info["whole"] = True
            if self_id:
                info["whole_owner"] = str(self_id)
            self._save_state()
        else:
            if info is None or not info.get("whole"):
                return
            info["whole"] = False
            info["whole_owner"] = None
            self._drop_if_empty(gid, info)
            self._save_state()

    def _drop_whole_mute_if_disabled(self) -> None:
        """配置里关掉了全员禁言拦截时,把历史遗留的全员禁言条目清掉。

        否则"关掉开关"只对新通知生效,之前记下的拦截会一直留着、重启也
        带得回来,和配置的意图正好相反。
        """
        if self._cfg("block_on_whole_group_mute", True):
            return
        dropped = [gid for gid, info in self.muted.items() if info.get("whole")]
        if not dropped:
            return
        for gid in dropped:
            info = self.muted[gid]
            info["whole"] = False
            info["whole_owner"] = None
            self._drop_if_empty(gid, info)
        self._save_state()
        logger.info(
            "被禁言了我闭嘴: 全员禁言拦截已关闭, 清掉 %s 个群的历史全员禁言状态",
            len(dropped),
        )

    @staticmethod
    def _parse_duration(value: Any) -> int:
        """解析禁言时长(秒)。个别协议端会传字符串或浮点,一并兼容。"""
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _fmt_remaining(seconds: int) -> str:
        """把剩余秒数格式化成人话。"""
        seconds = max(0, seconds)
        if seconds < 60:
            return f"{seconds} 秒"
        minutes, sec = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分钟"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
        days, hours = divmod(hours, 24)
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件被启用时调用:安装发送拦截,启动定时检查任务。"""
        self._drop_whole_mute_if_disabled()
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
            insts = self.context.platform_manager.platform_insts
            if not insts:
                return
            names = []
            for p in insts:
                try:
                    names.append(p.meta().name)
                except Exception:
                    continue
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

        aiocqhttp 的群消息发送(回复 / 流式回复 / 主动推送)最后都会经过
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
            AiocqhttpMessageEvent._shutup_blocked_count = self.blocked_count
            AiocqhttpMessageEvent._shutup_is_blocked = self._is_blocked
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
            gid = str(session_id) if session_id is not None else ""
            # 正在发言的机器人账号(多账号隔离用)。aiocqhttp 的 Event 是 dict
            # 子类;主动推送时 event 为 None,判断不了,这时按保守处理(照拦)。
            sender_self_id = None
            try:
                raw_self = event.get("self_id") if hasattr(event, "get") else None
                if raw_self is not None:
                    sender_self_id = str(raw_self)
            except Exception:
                sender_self_id = None
            block = False
            if is_group and gid and muted:
                # 交给插件统一判断:多账号隔离、到期即时放行、清理落盘都在里面
                checker = getattr(cls, "_shutup_is_blocked", None)
                if callable(checker):
                    block = bool(checker(gid, sender_self_id))
            if block:
                counter = getattr(cls, "_shutup_blocked_count", None)
                if isinstance(counter, dict):
                    counter[gid] = counter.get(gid, 0) + 1
                if getattr(cls, "_shutup_log_blocked", True):
                    logger.info(
                        "被禁言了我闭嘴: 拦截发往群 %s 的消息(禁言中) | %s",
                        gid,
                        _outline(message_chain),
                    )
                return  # 直接丢弃,不发送
            # 不在禁言名单(或不该拦),原样发送
            await orig(
                cls,
                bot=bot,
                message_chain=message_chain,
                event=event,
                is_group=is_group,
                session_id=session_id,
                **kwargs,
            )

        AiocqhttpMessageEvent.send_message = _guarded_send_message
        AiocqhttpMessageEvent._shutup_send_patched = True
        AiocqhttpMessageEvent._shutup_muted_groups = self.muted
        AiocqhttpMessageEvent._shutup_blocked_count = self.blocked_count
        AiocqhttpMessageEvent._shutup_is_blocked = self._is_blocked
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
            "_shutup_blocked_count",
            "_shutup_is_blocked",
        ):
            try:
                delattr(AiocqhttpMessageEvent, attr)
            except AttributeError:
                pass

    # ------------------------------------------------------------ 事件监听
    @filter.event_message_type(filter.EventMessageType.ALL, priority=500)
    async def on_group_ban_notice(self, event: AstrMessageEvent):
        """监听群禁言 / 解禁通知(OneBot group_ban)。

        - sub_type == "ban"      -> 禁言(user_id 为机器人自己,或 0 = 全员禁言);
        - sub_type == "lift_ban" -> 解除禁言。
        """
        # 通知的原始数据在 event.message_obj.raw_message 里(OneBot 事件字典)
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return
        # 只处理群禁言通知,其他事件一律不管
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_ban":
            return

        # 注意:全员禁言的 user_id 是 0,不能用 `or ""` 兜底(0 会被吞掉)
        raw_user = raw.get("user_id")
        group_id = "" if raw.get("group_id") is None else str(raw.get("group_id"))
        user_id = "" if raw_user is None else str(raw_user)
        self_id = "" if raw.get("self_id") is None else str(raw.get("self_id"))
        self_id = self_id or str(event.get_self_id() or "")
        if not group_id or not user_id or not self_id:
            return

        # 顺手记下这个群对应的客户端,自检时要用
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._bots[group_id] = bot

        sub_type = raw.get("sub_type", "")
        if sub_type not in ("ban", "lift_ban"):
            return

        if user_id == self_id:
            # 机器人自己被禁言 / 被解禁
            if sub_type == "ban":
                # 禁言时长(秒)。时长未知时记为 None,只等解禁通知;
                # 负数(如全员禁言用的 -1)同样按未知处理。
                duration = self._parse_duration(raw.get("duration"))
                mute_until = time.time() + duration if duration > 0 else None
                self._mute_self(group_id, mute_until, self_id=self_id)
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
            else:
                self._unmute_self(group_id, self_id=self_id)
                logger.info("被禁言了我闭嘴: 群 %s 已解禁, 恢复消息发送", group_id)
        elif user_id == "0":
            # 全员禁言 / 解除全员禁言(OneBot 约定 user_id 为 0)
            if sub_type == "ban":
                if not self._cfg("block_on_whole_group_mute", True):
                    return
                # 群管理员 / 群主不受全员禁言影响,先查一下机器人的身份
                if await self._bot_is_group_admin(event, group_id, self_id):
                    logger.info(
                        "被禁言了我闭嘴: 群 %s 开启全员禁言, 但机器人是管理员,"
                        "不受影响, 不拦截",
                        group_id,
                    )
                    return
                self._set_whole_mute(group_id, True, self_id=self_id)
                logger.info(
                    "被禁言了我闭嘴: 群 %s 开启全员禁言, 已拦截该群消息发送",
                    group_id,
                )
            else:
                # 解除通知一定要处理:否则中途把配置开关关掉后,之前记下的
                # 全员禁言拦截就再没人清了,那个群会被一直拦着
                self._set_whole_mute(group_id, False, self_id=self_id)
                logger.info(
                    "被禁言了我闭嘴: 群 %s 解除全员禁言, 恢复消息发送", group_id
                )

    async def _bot_is_group_admin(
        self, event: AstrMessageEvent, group_id: str, self_id: str
    ) -> bool:
        """查询机器人在群里是否是管理员/群主(不受全员禁言影响)。

        查询失败时按 False 处理(宁可比要拦的拦得多,也不放过风控)。
        """
        bot = getattr(event, "bot", None)
        if bot is None or not (group_id.isdigit() and self_id.isdigit()):
            return False
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(self_id),
            )
            return isinstance(info, dict) and info.get("role") in ("admin", "owner")
        except Exception:
            return False

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
        if gid and self._is_blocked(gid, str(event.get_self_id() or "")):
            # should_call_llm(True) = 阻止 AstrBot 默认的 LLM 请求链路
            # (这是 AstrBot 的固定用法,别传 False,那样等于没拦)
            suppress = getattr(event, "should_call_llm", None)
            if callable(suppress):
                suppress(True)
            elif hasattr(event, "call_llm"):  # 兼容非常旧的版本
                event.call_llm = True

    # ------------------------------------------------------------ 管理命令
    @filter.command("闭嘴", alias={"shutup_mute"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mute_cmd(self, event: AstrMessageEvent, group_id: str = ""):
        """手动让机器人对某个群闭嘴(比如预感要被风控时主动闭嘴)。

        用法: /闭嘴 123456 或 /闭嘴(在群里发)
        """
        if not group_id:
            group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("请指定群号,例如:/闭嘴 123456")
            return
        self._mute_self(
            group_id,
            None,
            self_id=str(event.get_self_id() or "") or None,
            manual=True,
        )
        self.blocked_count.pop(group_id, None)
        yield event.plain_result(
            f"已手动闭嘴群 {group_id},不再向该群发送消息;"
            f"发送 /闭嘴解除 {group_id} 可恢复。"
        )

    @filter.command("闭嘴状态", alias={"禁言状态", "shutup_status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status_cmd(self, event: AstrMessageEvent):
        """查看当前处于禁言拦截状态的群。"""
        self._expire_once()  # 先清掉已到期的,展示更准确
        if not self.muted:
            yield event.plain_result("当前没有被禁言拦截的群,一切正常。")
            return
        lines = ["当前正在拦截消息发送的群:"]
        now = time.time()
        for gid, info in sorted(self.muted.items()):
            desc = []
            if info.get("manual"):
                desc.append("手动闭嘴中")
            if info.get("whole"):
                desc.append("全员禁言中")
            untils = [
                u for u in (info.get("accounts") or {}).values() if u is not None
            ]
            if untils:
                remain = max(0, int(min(untils) - now))
                desc.append(f"剩余约 {self._fmt_remaining(remain)}")
            if not desc:
                desc.append("时长未知,等待解禁通知或手动解除")
            count = self.blocked_count.get(gid, 0)
            lines.append(f"  · 群 {gid} —— {', '.join(desc)}(已拦截 {count} 条)")
        yield event.plain_result("\n".join(lines))

    @filter.command("闭嘴自检", alias={"shutup_check"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def selfcheck_cmd(self, event: AstrMessageEvent):
        """立刻向协议端核对一次禁言状态,纠正已经失效的拦截。"""
        changed = await self._self_check()
        if not self.muted:
            yield event.plain_result(
                f"自检完成:当前没有被禁言拦截的群(修正了 {len(changed)} 个群)。"
            )
        else:
            yield event.plain_result(
                f"自检完成:修正了 {len(changed)} 个群,"
                f"当前仍在拦截 {len(self.muted)} 个群(可用 /闭嘴状态 查看)。"
            )

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
            self.blocked_count.pop(group_id, None)
            self._save_state()
            yield event.plain_result(f"已解除群 {group_id} 的禁言拦截,恢复消息发送。")
        else:
            yield event.plain_result(f"群 {group_id} 当前不在禁言拦截列表中。")

    # ------------------------------------------------------------ 过期检查
    def _expire_once(self) -> list[str]:
        """清理禁言时长已到的群,返回被清理的群号列表。

        兜底机制:即使一直没收到解禁通知,到点也会自动恢复。
        全员禁言没有时长,只在收到解除通知时恢复;手动闭嘴的群也不会自动恢复。
        """
        now = time.time()
        expired = []
        changed = False
        for gid, info in list(self.muted.items()):
            accounts = info.get("accounts") or {}
            local_changed = False
            for acc, until in list(accounts.items()):
                if until is not None and now >= until:
                    accounts.pop(acc, None)
                    local_changed = True
                    changed = True
                    logger.info(
                        "被禁言了我闭嘴: 群 %s 的账号 %s 禁言时长已到,"
                        " 自动恢复消息发送",
                        gid,
                        acc,
                    )
            if local_changed:
                # 个人禁言到期后,如果全员禁言 / 手动闭嘴还在,条目继续保留
                self._drop_if_empty(gid, info)
            if gid not in self.muted:
                expired.append(gid)
        if changed:
            self._save_state()
        return expired

    # ------------------------------------------------------------ 自检纠偏
    def _bot_for(self, gid: str) -> Any:
        """拿到该群对应的 aiocqhttp 客户端(自检用)。

        优先用收到禁言通知时记下的那个;拿不到时,如果只有一个 aiocqhttp
        平台实例就用它;多账号又无法确定是哪个时返回 None(跳过自检,
        宁可不纠偏也不误判)。
        """
        bot = self._bots.get(gid)
        if bot is not None:
            return bot
        try:
            bots = []
            for p in self.context.platform_manager.platform_insts:
                try:
                    if p.meta().name != "aiocqhttp":
                        continue
                except Exception:
                    continue
                b = getattr(p, "bot", None)
                if b is not None:
                    bots.append(b)
        except Exception:
            return None
        return bots[0] if len(bots) == 1 else None

    async def _self_check(self) -> list[str]:
        """自检纠偏:向协议端核对机器人是否真的还被禁言,清掉已经失效的拦截。

        解决两类麻烦:
        1. 协议端没给禁言时长(duration)、机器人又错过了"解禁"通知(掉线/重启),
           这个群会被一直拦着,只能手动解除;
        2. 协议端给的时长不准(比如中途被改过),导致拦早了或拦晚了。

        安全性:只在协议端**明确返回** shut_up_timestamp 时才据此纠偏;
        查询失败、字段缺失、群号非数字等情况一律保持现状(宁拦勿漏)。
        管理员手动 /闭嘴 的群不会被清掉。
        """
        if not self.muted:
            return []
        changed: list[str] = []
        now = time.time()
        for gid, info in list(self.muted.items()):
            if not gid.isdigit():
                continue
            if info.get("manual"):
                continue  # 手动闭嘴的群保持闭嘴,自检不动它
            bot = self._bot_for(gid)
            if bot is None:
                continue

            # 全员禁言:机器人是管理员/群主时本来就能发言,撤销这个拦截。
            if info.get("whole"):
                owner = info.get("whole_owner")
                if owner and str(owner).isdigit():
                    member = await self._member_info(bot, gid, str(owner))
                    if member is not None and member.get("role") in ("admin", "owner"):
                        info["whole"] = False
                        info["whole_owner"] = None
                        self._drop_if_empty(gid, info)
                        changed.append(gid)
                # 机器人不是管理员(或查不到):全员禁言继续拦
                continue

            # 个人禁言:逐个账号向协议端核对,该清的清、该校准的校准
            accounts = info.get("accounts") or {}
            local_changed = False
            for acc in list(accounts.keys()):
                if not acc.isdigit():
                    continue  # 查不出账号(旧状态占位):保持现状
                member = await self._member_info(bot, gid, acc)
                if member is None:
                    continue  # 查不到就保持现状
                raw_ts = member.get("shut_up_timestamp")
                if raw_ts is None:
                    continue  # 协议端没返回这个字段,不做判断
                try:
                    until = float(raw_ts)
                except (TypeError, ValueError):
                    continue
                if until > now:
                    # 确实还在禁言中:把截止时间校准成协议端的值。
                    # 协议端给的是整数秒、本地存的是浮点,差不到 1 秒就当没变,
                    # 否则每轮自检都会误报"修正了一个群"。
                    stored = accounts.get(acc)
                    if stored is None or abs(float(stored) - until) >= 1:
                        accounts[acc] = until
                        local_changed = True
                else:
                    # 协议端说这个账号已经不在禁言中
                    accounts.pop(acc, None)
                    local_changed = True
            if local_changed:
                self._drop_if_empty(gid, info)
                changed.append(gid)

        if changed:
            self._save_state()
            logger.info(
                "被禁言了我闭嘴: 自检纠偏了 %s 个群的状态 | %s",
                len(changed),
                ", ".join(changed),
            )
        return changed

    @staticmethod
    async def _member_info(bot: Any, gid: str, uid: str) -> dict | None:
        """查询群成员的资料(自检用),失败或返回异常时给 None。"""
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(gid),
                user_id=int(uid),
            )
        except Exception:
            return None
        return info if isinstance(info, dict) else None

    async def _expire_loop(self) -> None:
        """后台周期任务:检查禁言时长是否到期,并定期做一次自检纠偏。"""
        raw_interval = self._cfg("self_check_interval", 300)
        try:
            interval = max(0, int(raw_interval))
        except (TypeError, ValueError):
            interval = 300
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break  # 插件被停用时退出
            try:
                self._expire_once()
            except Exception:
                logger.exception("被禁言了我闭嘴: 定时检查禁言状态时出错")
            # 自检纠偏(interval 为 0 表示关闭)
            if interval > 0 and time.time() - self._last_self_check >= interval:
                self._last_self_check = time.time()
                try:
                    await self._self_check()
                except Exception:
                    logger.exception("被禁言了我闭嘴: 自检纠偏时出错")