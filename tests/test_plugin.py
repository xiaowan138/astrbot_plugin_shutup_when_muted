"""
被禁言了我闭嘴 —— 冒烟测试(不依赖真实 AstrBot)
================================================

用假的 ``astrbot`` 模块替换真实 API,验证插件核心逻辑:

1. 机器人自己被禁言 -> 拦截发往该群的消息;别人被禁言 -> 不拦截;
2. 全员禁言(user_id=0)-> 拦截;机器人是管理员时 -> 不拦截;
3. 解禁 / 解除全员禁言 -> 恢复发送;
4. 禁言时长到期 -> 自动恢复(发送时即时放行,不等后台循环);
5. 状态持久化(含 v1.0 旧格式兼容),重启后可恢复,过期条目加载即清理;
6. 管理命令(手动闭嘴 / 状态 / 解除);
7. 禁言期间来自该群的消息不再触发默认 AI 回复;
8. 卸载补丁后发送恢复正常;
9. 边界情况(群号是数字、时长是字符串/浮点、多次禁言、个人+全员叠加等)。

运行: python tests/test_plugin.py
"""

import asyncio
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# 假 AstrBot API(必须在 import main 之前安装)
# ---------------------------------------------------------------------------


def _install_fake_astrbot():
    def _mk(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class _Logger:
        def _emit(self, level, msg, *args):
            try:
                line = msg % args if args else msg
            except Exception:
                line = str(msg)
            print(f"[fake:{level}] {line}")

        def info(self, msg, *args):
            self._emit("info", msg, *args)

        def warning(self, msg, *args):
            self._emit("warning", msg, *args)

        def error(self, msg, *args):
            self._emit("error", msg, *args)

        def exception(self, msg, *args):
            self._emit("exception", msg, *args)

        def debug(self, msg, *args):
            pass

    class AstrBotConfig(dict):
        pass

    class EventMessageType:
        GROUP_MESSAGE = 1
        PRIVATE_MESSAGE = 2
        OTHER_MESSAGE = 4
        ALL = 7

    class PermissionType:
        ADMIN = "admin"

    # 注意:类体内同名赋值会把名字变成类局部变量,RHS 将无法引用外层作用域,
    # 所以这里先取个别名再放进类体。
    _EMT = EventMessageType
    _PT = PermissionType

    class _Filter:
        EventMessageType = _EMT
        PermissionType = _PT

        def event_message_type(self, t, **kw):
            def deco(f):
                return f

            return deco

        def command(self, name, alias=None, **kw):
            def deco(f):
                return f

            return deco

        def permission_type(self, t, **kw):
            def deco(f):
                return f

            return deco

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config or {}

    class Context:
        def __init__(self):
            self.platform_manager = types.SimpleNamespace(platform_insts=[])

    class AstrMessageEvent:
        """假的 AstrMessageEvent:镜像真实 AstrBot 的 call_llm 语义。"""

        def __init__(self, group_id="", self_id="", raw=None, message_str="", bot=None):
            self.message_obj = types.SimpleNamespace(
                raw_message=raw, group_id=group_id, self_id=self_id
            )
            self.message_str = message_str
            self.bot = bot
            # 真实 AstrBot:call_llm 默认 False 表示"走默认 LLM 链路"
            self.call_llm = False
            self._results = []

        def get_group_id(self):
            return self.message_obj.group_id

        def get_self_id(self):
            return self.message_obj.self_id

        def should_call_llm(self, v):
            self.call_llm = v

        def plain_result(self, text):
            r = types.SimpleNamespace(text=text, is_result=True)
            self._results.append(r)
            return r

    api = _mk("astrbot.api", logger=_Logger(), AstrBotConfig=AstrBotConfig)
    _mk(
        "astrbot.api.event",
        AstrMessageEvent=AstrMessageEvent,
        filter=_Filter(),
    )
    # 注意:故意不提供 StarTools,验证插件会退回插件目录存状态
    _mk("astrbot.api.star", Context=Context, Star=Star)

    class FakeAiocqhttpMessageEvent:
        """模拟 aiocqhttp 事件类,记录每次真实发送。"""

        sent = []  # [(is_group, session_id)]

        @classmethod
        async def send_message(
            cls,
            bot,
            message_chain,
            event=None,
            is_group=False,
            session_id=None,
            **kw,
        ):
            cls.sent.append((is_group, session_id))

    _mk("astrbot.core")
    _mk("astrbot.core.platform")
    _mk("astrbot.core.platform.sources")
    _mk("astrbot.core.platform.sources.aiocqhttp")
    _mk(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        AiocqhttpMessageEvent=FakeAiocqhttpMessageEvent,
    )
    return AstrBotConfig, FakeAiocqhttpMessageEvent, AstrMessageEvent, Context


AstrBotConfig, FakeAiocqhttpMessageEvent, FakeEvent, FakeContext = _install_fake_astrbot()

# 导入插件主模块(此时 astrbot.* 均为假模块)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from main import ShutUpWhenMuted  # noqa: E402


def _ban_notice(group_id, user_id, self_id, duration=60, sub_type="ban"):
    return {
        "post_type": "notice",
        "notice_type": "group_ban",
        "sub_type": sub_type,
        "group_id": int(group_id),
        "user_id": int(user_id),
        "self_id": int(self_id),
        "operator_id": 1,
        "duration": duration,
    }


class FakeBot:
    """模拟 aiocqhttp 的 CQHttp,可指定 get_group_member_info 的返回。"""

    def __init__(self, role=None, raise_error=False):
        self.role = role
        self.raise_error = raise_error
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.raise_error:
            raise RuntimeError("api failed")
        if action == "get_group_member_info":
            return {"role": self.role, "user_id": kwargs.get("user_id")}
        return {}


class _PluginHarness:
    """构建一个插件实例,并把状态文件指到临时目录,不污染插件目录。"""

    def __init__(self, config=None):
        tmp = tempfile.mkdtemp(prefix="shutup_test_")
        self.state_file = Path(tmp) / "muted_groups.json"
        self.plugin = ShutUpWhenMuted(FakeContext(), AstrBotConfig(config or {}))
        self.plugin._state_file = self.state_file


class TestShutUpWhenMuted(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAiocqhttpMessageEvent.sent = []
        # 清理上一个测试可能残留的补丁与类属性
        if hasattr(FakeAiocqhttpMessageEvent, "_shutup_send_patched"):
            orig = getattr(FakeAiocqhttpMessageEvent, "_shutup_orig_send_message", None)
            if orig is not None:
                FakeAiocqhttpMessageEvent.send_message = classmethod(orig)
            for attr in (
                "_shutup_send_patched",
                "_shutup_muted_groups",
                "_shutup_orig_send_message",
                "_shutup_log_blocked",
                "_shutup_blocked_count",
            ):
                try:
                    delattr(FakeAiocqhttpMessageEvent, attr)
                except AttributeError:
                    pass

    async def _install(self, config=None):
        h = _PluginHarness(config)
        await h.plugin.initialize()
        return h

    async def _notice(self, plugin, raw, bot=None):
        group_id = str(raw.get("group_id", "")) if isinstance(raw, dict) else ""
        ev = FakeEvent(group_id=group_id, raw=raw, bot=bot)
        await plugin.on_group_ban_notice(ev)

    async def _send(self, is_group=True, session_id="111"):
        await FakeAiocqhttpMessageEvent.send_message(
            bot=None, message_chain=object(), is_group=is_group, session_id=session_id
        )

    # ------------------------------------------------------- 自己被禁言
    async def test_ban_self_blocks_group_send(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        self.assertIn("111", h.plugin.muted)

        # 发往被禁言群 -> 拦截
        await self._send(session_id="111")
        # 发往其他群 -> 放行
        await self._send(session_id="222")
        # 私聊 -> 放行
        await self._send(is_group=False, session_id="999")
        self.assertEqual(
            FakeAiocqhttpMessageEvent.sent, [(True, "222"), (False, "999")]
        )
        # 拦截计数
        self.assertEqual(h.plugin.blocked_count.get("111"), 1)

    async def test_ban_of_other_member_ignored(self):
        h = await self._install()
        # 被禁言的是别人(user_id 100 != self_id 999)
        await self._notice(h.plugin, _ban_notice("111", "100", "999", duration=60))
        self.assertNotIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_lift_ban_resumes(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        self.assertIn("111", h.plugin.muted)
        await self._notice(
            h.plugin, _ban_notice("111", "999", "999", duration=0, sub_type="lift_ban")
        )
        self.assertNotIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_unknown_duration_blocks_until_lift(self):
        # duration=0 / 缺失 / 负数(全员禁言用的 -1) -> 时长未知,只等解禁通知
        h = await self._install()
        for duration in (0, None, -1):
            h.plugin.muted.clear()
            await self._notice(
                h.plugin, _ban_notice("111", "999", "999", duration=duration)
            )
            self.assertIsNone(h.plugin.muted["111"]["until"])
            await self._send(session_id="111")
            self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])
            FakeAiocqhttpMessageEvent.sent = []

    async def test_expire_auto_resume(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=2))
        self.assertIn("111", h.plugin.muted)
        # 把到期时间拨到过去,模拟时长走完
        h.plugin.muted["111"]["until"] = time.time() - 1
        expired = h.plugin._expire_once()
        self.assertEqual(expired, ["111"])
        self.assertNotIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_expired_allows_send_without_waiting_loop(self):
        # 到期后即使后台 30 秒循环还没跑,发送时也应即时放行
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        h.plugin.muted["111"]["until"] = time.time() - 1
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])
        self.assertNotIn("111", h.plugin.muted)

    # ------------------------------------------------------- 全员禁言
    async def test_whole_group_ban_blocks_send(self):
        h = await self._install()
        # 全员禁言:user_id = 0,没有 bot 可查身份 -> 按普通成员处理,拦截
        await self._notice(h.plugin, _ban_notice("111", "0", "999", duration=-1))
        self.assertEqual(
            h.plugin.muted.get("111"), {"until": None, "whole": True}
        )
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])
        # 禁言期间的群消息也不触发默认 LLM
        ev = FakeEvent(group_id="111")
        await h.plugin.on_group_message(ev)
        self.assertTrue(ev.call_llm)

    async def test_whole_group_lift_ban_resumes(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "0", "999", duration=-1))
        await self._notice(
            h.plugin, _ban_notice("111", "0", "999", duration=0, sub_type="lift_ban")
        )
        self.assertNotIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_whole_group_ban_config_off(self):
        h = await self._install({"block_on_whole_group_mute": False})
        await self._notice(h.plugin, _ban_notice("111", "0", "999", duration=-1))
        self.assertNotIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_whole_group_ban_admin_bot_not_blocked(self):
        h = await self._install()
        bot = FakeBot(role="admin")
        await self._notice(
            h.plugin, _ban_notice("111", "0", "999", duration=-1), bot=bot
        )
        # 查过身份:管理员不受全员禁言影响
        self.assertEqual(bot.calls[0][0], "get_group_member_info")
        self.assertNotIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_whole_group_ban_member_bot_blocked(self):
        h = await self._install()
        await self._notice(
            h.plugin, _ban_notice("111", "0", "999", duration=-1), bot=FakeBot(role="member")
        )
        self.assertIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    async def test_whole_group_ban_api_error_blocks(self):
        # 查询身份失败时宁可多拦,也不放过风控
        h = await self._install()
        await self._notice(
            h.plugin,
            _ban_notice("111", "0", "999", duration=-1),
            bot=FakeBot(raise_error=True),
        )
        self.assertIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    async def test_self_mute_and_whole_mute_combined(self):
        h = await self._install()
        # 先被单独禁言,再开全员禁言
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        await self._notice(h.plugin, _ban_notice("111", "0", "999", duration=-1))
        info = h.plugin.muted["111"]
        self.assertIsNotNone(info["until"])
        self.assertTrue(info["whole"])
        # 个人解禁:全员禁言还在,继续拦
        await self._notice(
            h.plugin, _ban_notice("111", "999", "999", duration=0, sub_type="lift_ban")
        )
        self.assertEqual(h.plugin.muted["111"], {"until": None, "whole": True})
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])
        # 解除全员禁言:彻底恢复
        await self._notice(
            h.plugin, _ban_notice("111", "0", "999", duration=0, sub_type="lift_ban")
        )
        self.assertNotIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_self_mute_expires_under_whole_mute(self):
        # 全员禁言期间个人禁言到期:仍因全员禁言继续拦
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        await self._notice(h.plugin, _ban_notice("111", "0", "999", duration=-1))
        h.plugin.muted["111"]["until"] = time.time() - 1
        expired = h.plugin._expire_once()
        self.assertEqual(expired, [])
        self.assertEqual(h.plugin.muted["111"], {"until": None, "whole": True})
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    # ------------------------------------------------------- 持久化
    async def test_persistence_across_restart(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=3600))
        self.assertTrue(h.state_file.exists())

        # 模拟重启:新实例读取同一状态文件
        h2 = _PluginHarness()
        h2.plugin._state_file = h.state_file
        h2.plugin._load_state()
        self.assertEqual(h2.plugin.muted, {"111": h.plugin.muted["111"]})

    async def test_load_old_flat_format(self):
        # v1.0 的状态文件是 {群号: 时间戳或 null}
        h = _PluginHarness()
        old = {"111": time.time() + 3600, "222": None, "333": time.time() - 100}
        h.state_file.write_text(json.dumps(old), encoding="utf-8")
        h.plugin._load_state()
        # 333 已过期,加载即清理
        self.assertEqual(
            h.plugin.muted,
            {
                "111": {"until": old["111"], "whole": False},
                "222": {"until": None, "whole": False},
            },
        )

    async def test_load_new_format(self):
        h = _PluginHarness()
        data = {
            "111": {"until": time.time() + 3600, "whole": False},
            "222": {"until": None, "whole": True},
        }
        h.state_file.write_text(json.dumps(data), encoding="utf-8")
        h.plugin._load_state()
        self.assertEqual(h.plugin.muted, data)

    async def test_load_purges_expired_entry(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=3600))
        # 停机期间禁言早已到期:重启后不应继续拦截
        h.plugin.muted["111"]["until"] = time.time() - 100
        h.plugin._save_state()
        h2 = _PluginHarness()
        h2.plugin._state_file = h.state_file
        h2.plugin._load_state()
        self.assertNotIn("111", h2.plugin.muted)

    # ------------------------------------------------------- 管理命令
    async def test_status_and_unmute_commands(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=3600))
        await self._send(session_id="111")

        ev = FakeEvent()
        results = []
        async for r in h.plugin.status_cmd(ev):
            results.append(r)
        self.assertEqual(len(results), 1)
        self.assertIn("111", results[0].text)
        self.assertIn("剩余", results[0].text)
        self.assertIn("已拦截 1 条", results[0].text)

        # 解除指定群
        results = []
        async for r in h.plugin.unmute_cmd(FakeEvent(), "111"):
            results.append(r)
        self.assertIn("已解除", results[0].text)
        self.assertNotIn("111", h.plugin.muted)

        # 解除不存在的群
        results = []
        async for r in h.plugin.unmute_cmd(FakeEvent(), "555"):
            results.append(r)
        self.assertIn("不在禁言拦截列表", results[0].text)

    async def test_unmute_without_group_id_uses_current_group(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=3600))
        results = []
        async for r in h.plugin.unmute_cmd(FakeEvent(group_id="111")):
            results.append(r)
        self.assertIn("已解除", results[0].text)
        self.assertNotIn("111", h.plugin.muted)

    async def test_manual_mute_command(self):
        h = await self._install()
        results = []
        async for r in h.plugin.mute_cmd(FakeEvent(), "111"):
            results.append(r)
        self.assertIn("已手动闭嘴", results[0].text)
        self.assertEqual(h.plugin.muted["111"], {"until": None, "whole": False})
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])
        # 手动闭嘴的群,消息也不触发默认 LLM
        ev = FakeEvent(group_id="111")
        await h.plugin.on_group_message(ev)
        self.assertTrue(ev.call_llm)
        # 再手动解除
        results = []
        async for r in h.plugin.unmute_cmd(FakeEvent(), "111"):
            results.append(r)
        self.assertIn("已解除", results[0].text)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_manual_mute_without_group_id_uses_current_group(self):
        h = await self._install()
        results = []
        async for r in h.plugin.mute_cmd(FakeEvent(group_id="111")):
            results.append(r)
        self.assertIn("已手动闭嘴", results[0].text)
        self.assertIn("111", h.plugin.muted)

    # ------------------------------------------------------- LLM 抑制
    async def test_group_message_suppresses_llm_when_muted(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        ev = FakeEvent(group_id="111")
        await h.plugin.on_group_message(ev)
        # call_llm=True 表示阻止默认 LLM 链路(镜像真实 AstrBot 语义)
        self.assertTrue(ev.call_llm)

        # 未禁言的群不受影响
        ev2 = FakeEvent(group_id="222")
        await h.plugin.on_group_message(ev2)
        self.assertFalse(ev2.call_llm)

    async def test_skip_llm_config_off(self):
        h = await self._install({"skip_llm_when_muted": False})
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        ev = FakeEvent(group_id="111")
        await h.plugin.on_group_message(ev)
        self.assertFalse(ev.call_llm)

    # ------------------------------------------------------- 补丁装卸
    async def test_uninstall_restores_original_send(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        await h.plugin.terminate()
        # 补丁已卸载,类方法恢复原样
        self.assertFalse(
            getattr(FakeAiocqhttpMessageEvent, "_shutup_send_patched", False)
        )
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_log_blocked_config_off(self):
        h = await self._install({"log_blocked_sends": False})
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    async def test_reload_reuses_existing_patch(self):
        # 模拟插件重载:旧补丁还在,新实例只更新类属性引用
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        h2 = _PluginHarness()
        h2.plugin.muted = {}
        await h2.plugin.initialize()
        self.assertTrue(h2.plugin._patch_installed)
        # 新实例的空名单生效:放行
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])
        await h2.plugin.terminate()

    # ------------------------------------------------------- 边界情况
    async def test_duration_as_string(self):
        h = await self._install()
        raw = _ban_notice("111", "999", "999", duration=60)
        raw["duration"] = "120"  # 个别协议端可能把时长传成字符串
        await self._notice(h.plugin, raw)
        self.assertIn("111", h.plugin.muted)
        self.assertGreater(h.plugin.muted["111"]["until"], time.time() + 100)

    async def test_duration_as_float_string(self):
        h = await self._install()
        raw = _ban_notice("111", "999", "999", duration=60)
        raw["duration"] = "120.5"
        await self._notice(h.plugin, raw)
        self.assertIn("111", h.plugin.muted)
        self.assertGreater(h.plugin.muted["111"]["until"], time.time() + 100)

    async def test_reban_updates_duration(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        first = h.plugin.muted["111"]["until"]
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=600))
        self.assertGreater(h.plugin.muted["111"]["until"], first)

    async def test_non_onebot_events_ignored(self):
        h = await self._install()
        # 普通群消息 / 私聊消息 / 其他平台的通知,都不该被当作禁言处理
        for raw in (
            {"post_type": "message", "group_id": 111, "user_id": 999},
            {"post_type": "notice", "notice_type": "group_increase", "group_id": 111},
            {"post_type": "notice", "notice_type": "group_ban", "group_id": 111},
            None,
            "not a dict",
        ):
            await self._notice(h.plugin, raw)
        self.assertNotIn("111", h.plugin.muted)

    async def test_fmt_remaining(self):
        f = ShutUpWhenMuted._fmt_remaining
        self.assertEqual(f(59), "59 秒")
        self.assertEqual(f(60), "1 分钟")
        self.assertEqual(f(125), "2 分 5 秒")
        self.assertEqual(f(3600), "1 小时")
        self.assertEqual(f(5430), "1 小时 30 分")
        self.assertEqual(f(90000), "1 天 1 小时")


if __name__ == "__main__":
    unittest.main(verbosity=2)
