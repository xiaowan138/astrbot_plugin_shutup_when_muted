"""
被禁言了我闭嘴 —— 冒烟测试(不依赖真实 AstrBot)
================================================

用假的 ``astrbot`` 模块替换真实 API,验证插件核心逻辑:

1. 机器人自己被禁言 -> 拦截发往该群的消息;别人被禁言 -> 不拦截;
2. 全员禁言(user_id=0)-> 拦截;机器人是管理员时 -> 不拦截;
3. 解禁 / 解除全员禁言 -> 恢复发送;
4. 禁言时长到期 -> 自动恢复(发送时即时放行,不等后台循环);
5. 状态持久化(含 v1.0/v1.1 旧格式兼容),重启后可恢复,过期条目加载即清理;
6. 管理命令(手动闭嘴 / 状态 / 自检 / 解除);
7. 禁言期间来自该群的消息不再触发默认 AI 回复;
8. 多账号隔离:同群里别的机器人账号被禁言,不该连累当前账号;
9. 自检纠偏:协议端说已经不在禁言中就清掉拦截;还在禁言中则校准时长;
   查询失败 / 字段缺失 / 手动闭嘴 等情况一律保持现状;
10. 卸载补丁后发送恢复正常;
11. 边界情况(群号是数字、时长是字符串/浮点、多次禁言、个人+全员叠加等)。

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

# 让假的 StarTools.get_data_dir() 返回当前测试用的临时目录
_CURRENT_DATA_DIR: dict = {"path": None}


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

    class PlatformManager:
        def __init__(self):
            self.platform_insts = []

    class Context:
        def __init__(self):
            self.platform_manager = PlatformManager()

    class StarTools:
        @staticmethod
        def get_data_dir(name=None):
            p = _CURRENT_DATA_DIR["path"]
            if p is None:
                raise RuntimeError("测试未设置数据目录")
            p = Path(p)
            p.mkdir(parents=True, exist_ok=True)
            return p

    class AstrMessageEvent:
        """假的 AstrMessageEvent:镜像真实 AstrBot 的 call_llm 语义。"""

        def __init__(self, group_id="", self_id="", raw=None, message_str="", bot=None):
            self.message_obj = types.SimpleNamespace(
                raw_message=raw, group_id=group_id, self_id=self_id
            )
            self.message_str = message_str
            self.bot = bot
            # 真实 AstrBot:call_llm=True 表示"禁止默认 LLM 请求",默认 False
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

    # 顶层包也要注册:否则 `import astrbot.api.star` 这类带点名的导入
    # 会因为找不到父包 astrbot 而报 ModuleNotFoundError
    _mk("astrbot")
    api = _mk("astrbot.api", logger=_Logger(), AstrBotConfig=AstrBotConfig)
    _mk(
        "astrbot.api.event",
        AstrMessageEvent=AstrMessageEvent,
        filter=_Filter(),
    )
    _mk("astrbot.api.star", Context=Context, Star=Star, StarTools=StarTools)

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
    return (
        AstrBotConfig,
        FakeAiocqhttpMessageEvent,
        AstrMessageEvent,
        Context,
        PlatformManager,
    )


(
    AstrBotConfig,
    FakeAiocqhttpMessageEvent,
    FakeEvent,
    FakeContext,
    FakePlatformManager,
) = _install_fake_astrbot()

# 导入插件主模块(此时 astrbot.* 均为假模块)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402
from main import ShutUpWhenMuted  # noqa: E402

_PATCH_ATTRS = (
    "_shutup_send_patched",
    "_shutup_muted_groups",
    "_shutup_orig_send_message",
    "_shutup_log_blocked",
    "_shutup_blocked_count",
    "_shutup_expire_hook",
)


def _clear_patch():
    """把上一次测试可能残留的补丁清理干净。"""
    if getattr(FakeAiocqhttpMessageEvent, "_shutup_send_patched", False):
        orig = getattr(FakeAiocqhttpMessageEvent, "_shutup_orig_send_message", None)
        if orig is not None:
            FakeAiocqhttpMessageEvent.send_message = classmethod(orig)
        for attr in _PATCH_ATTRS:
            try:
                delattr(FakeAiocqhttpMessageEvent, attr)
            except AttributeError:
                pass


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
    """模拟 aiocqhttp 的 CQHttp。

    get_group_member_info 的返回可通过参数控制,用于自检纠偏的测试。
    """

    def __init__(
        self,
        role=None,
        shut_up_timestamp=0,
        raise_error=False,
        omit_ts=False,
    ):
        self.role = role
        self.shut_up_timestamp = shut_up_timestamp
        self.raise_error = raise_error
        self.omit_ts = omit_ts
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.raise_error:
            raise RuntimeError("api failed")
        if action == "get_group_member_info":
            info = {"role": self.role, "user_id": kwargs.get("user_id")}
            if not self.omit_ts:
                info["shut_up_timestamp"] = self.shut_up_timestamp
            return info
        return {}


class FakePlatform:
    """模拟一个平台实例(有 meta() 和 bot 属性)。"""

    def __init__(self, name="aiocqhttp", bot=None):
        self._name = name
        self.bot = bot

    def meta(self):
        return types.SimpleNamespace(name=self._name)


class _PluginHarness:
    """构建一个插件实例,状态文件落在临时数据目录,不污染插件目录。"""

    created: list = []

    def __init__(self, config=None, data_dir=None):
        self.data_dir = (
            Path(data_dir)
            if data_dir
            else Path(tempfile.mkdtemp(prefix="shutup_test_"))
        )
        _CURRENT_DATA_DIR["path"] = self.data_dir
        self.plugin = ShutUpWhenMuted(FakeContext(), AstrBotConfig(config or {}))
        self.state_file = self.plugin._state_file
        _PluginHarness.created.append(self.plugin)


class TestShutUpWhenMuted(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAiocqhttpMessageEvent.sent = []
        _PluginHarness.created = []
        _clear_patch()

    async def asyncTearDown(self):
        for plugin in _PluginHarness.created:
            task = plugin._expire_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        _clear_patch()

    async def _install(self, config=None):
        h = _PluginHarness(config)
        await h.plugin.initialize()
        return h

    async def _notice(self, plugin, raw, bot=None):
        group_id = str(raw.get("group_id", "")) if isinstance(raw, dict) else ""
        ev = FakeEvent(group_id=group_id, raw=raw, bot=bot)
        await plugin.on_group_ban_notice(ev)

    async def _send(self, is_group=True, session_id="111", event=None):
        await FakeAiocqhttpMessageEvent.send_message(
            bot=None,
            message_chain=object(),
            event=event,
            is_group=is_group,
            session_id=session_id,
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

    async def test_send_path_expiry_is_persisted(self):
        # 发送路径上发现过期时,要走 _expire_once(会落盘),而不是只改内存
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        h.plugin.muted["111"]["until"] = time.time() - 1
        h.plugin._save_state()
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])
        on_disk = json.loads(h.state_file.read_text(encoding="utf-8"))
        self.assertNotIn("111", on_disk)

    # ------------------------------------------------------- 全员禁言
    async def test_whole_group_ban_blocks_send(self):
        h = await self._install()
        # 全员禁言:user_id = 0,没有 bot 可查身份 -> 按普通成员处理,拦截
        await self._notice(h.plugin, _ban_notice("111", "0", "999", duration=-1))
        info = h.plugin.muted["111"]
        self.assertTrue(info["whole"])
        self.assertIsNone(info["until"])
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
            h.plugin,
            _ban_notice("111", "0", "999", duration=-1),
            bot=FakeBot(role="member"),
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
        self.assertTrue(h.plugin.muted["111"]["whole"])
        self.assertIsNone(h.plugin.muted["111"]["until"])
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
        self.assertTrue(h.plugin.muted["111"]["whole"])
        self.assertIsNone(h.plugin.muted["111"]["until"])
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    # ------------------------------------------------------- 多账号隔离
    async def test_other_bot_account_not_blocked(self):
        # 群 111 里被禁言的是 999,但现在是 888 这个账号在发言 -> 放行
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        await self._send(session_id="111", event={"self_id": 888})
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_own_account_still_blocked(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        await self._send(session_id="111", event={"self_id": 999})
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])
        self.assertEqual(h.plugin.blocked_count.get("111"), 1)

    async def test_unknown_sender_account_blocks_conservatively(self):
        # 主动推送(event=None)判断不了是哪个账号 -> 保守拦截
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        await self._send(session_id="111", event=None)
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    async def test_llm_suppression_respects_multi_account(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        # 别的账号收到该群消息:不该被压制
        ev_other = FakeEvent(group_id="111", self_id="888")
        await h.plugin.on_group_message(ev_other)
        self.assertFalse(ev_other.call_llm)
        # 被禁言的账号收到该群消息:压制
        ev_own = FakeEvent(group_id="111", self_id="999")
        await h.plugin.on_group_message(ev_own)
        self.assertTrue(ev_own.call_llm)

    # ------------------------------------------------------- 自检纠偏
    async def test_selfcheck_clears_stale_entry(self):
        # 时长未知 + 错过解禁通知 -> 自检发现协议端说没在禁言,清掉
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=0))
        self.assertIn("111", h.plugin.muted)
        bot = FakeBot(shut_up_timestamp=0)
        h.plugin._bots["111"] = bot
        changed = await h.plugin._self_check()
        self.assertEqual(changed, ["111"])
        self.assertNotIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [(True, "111")])

    async def test_selfcheck_calibrates_until(self):
        # 协议端说还在禁言(直到 future)-> 校准截止时间,继续拦
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=0))
        future = time.time() + 300
        h.plugin._bots["111"] = FakeBot(shut_up_timestamp=future)
        changed = await h.plugin._self_check()
        self.assertEqual(changed, ["111"])
        self.assertAlmostEqual(h.plugin.muted["111"]["until"], future, places=3)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    async def test_selfcheck_no_change_when_until_already_correct(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=300))
        until = h.plugin.muted["111"]["until"]
        h.plugin._bots["111"] = FakeBot(shut_up_timestamp=int(until))
        changed = await h.plugin._self_check()
        self.assertEqual(changed, [])

    async def test_selfcheck_skips_when_field_missing(self):
        # 协议端没返回 shut_up_timestamp -> 不做判断,保持拦截
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=0))
        h.plugin._bots["111"] = FakeBot(omit_ts=True)
        changed = await h.plugin._self_check()
        self.assertEqual(changed, [])
        self.assertIn("111", h.plugin.muted)

    async def test_selfcheck_skips_on_api_error(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=0))
        h.plugin._bots["111"] = FakeBot(raise_error=True)
        changed = await h.plugin._self_check()
        self.assertEqual(changed, [])
        self.assertIn("111", h.plugin.muted)

    async def test_selfcheck_clears_whole_when_bot_is_admin(self):
        # 全员禁言 + 机器人是管理员 -> 其实能发言,撤销拦截
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "0", "999", duration=-1))
        self.assertTrue(h.plugin.muted["111"]["whole"])
        h.plugin._bots["111"] = FakeBot(role="owner", shut_up_timestamp=0)
        changed = await h.plugin._self_check()
        self.assertEqual(changed, ["111"])
        self.assertNotIn("111", h.plugin.muted)

    async def test_selfcheck_keeps_manual_mute(self):
        # 手动 /闭嘴 的群,即使协议端说没在禁言,也要保持闭嘴
        h = await self._install()
        async for _ in h.plugin.mute_cmd(FakeEvent(self_id="999"), "111"):
            pass
        h.plugin._bots["111"] = FakeBot(shut_up_timestamp=0)
        changed = await h.plugin._self_check()
        self.assertEqual(changed, [])
        self.assertIn("111", h.plugin.muted)
        self.assertTrue(h.plugin.muted["111"]["manual"])
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    async def test_selfcheck_without_bot_is_noop(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=0))
        h.plugin._bots.clear()
        # 平台实例为空 -> 拿不到客户端,跳过自检
        changed = await h.plugin._self_check()
        self.assertEqual(changed, [])
        self.assertIn("111", h.plugin.muted)

    async def test_bot_for_ambiguous_multi_platform(self):
        # 有多个 aiocqhttp 平台实例且没记录过 -> 无法确定,返回 None
        h = await self._install()
        b1, b2 = FakeBot(), FakeBot()
        h.plugin.context.platform_manager.platform_insts = [
            FakePlatform("aiocqhttp", b1),
            FakePlatform("aiocqhttp", b2),
        ]
        self.assertIsNone(h.plugin._bot_for("111"))
        # 只有一个实例 -> 用它
        h.plugin.context.platform_manager.platform_insts = [
            FakePlatform("aiocqhttp", b1),
            FakePlatform("telegram", b2),
        ]
        self.assertIs(h.plugin._bot_for("111"), b1)
        # 通知里记过这个群 -> 优先用记录的那个
        h.plugin._bots["111"] = b2
        self.assertIs(h.plugin._bot_for("111"), b2)

    async def test_selfcheck_command(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=0))
        h.plugin._bots["111"] = FakeBot(shut_up_timestamp=0)
        results = []
        async for r in h.plugin.selfcheck_cmd(FakeEvent()):
            results.append(r)
        self.assertEqual(len(results), 1)
        self.assertIn("自检完成", results[0].text)
        self.assertIn("修正了 1 个群", results[0].text)
        self.assertNotIn("111", h.plugin.muted)

    # ------------------------------------------------------- 持久化
    async def test_persistence_across_restart(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=3600))
        self.assertTrue(h.state_file.exists())

        # 模拟重启:新实例读取同一状态文件
        h2 = _PluginHarness(data_dir=h.data_dir)
        self.assertEqual(h2.plugin.muted, h.plugin.muted)

    async def test_load_old_flat_format(self):
        # v1.0 的状态文件是 {群号: 时间戳或 null}
        h = _PluginHarness()
        old = {"111": time.time() + 3600, "222": None, "333": time.time() - 100}
        h.state_file.write_text(json.dumps(old), encoding="utf-8")
        h.plugin._load_state()
        # 333 已过期,加载即清理
        self.assertEqual(set(h.plugin.muted), {"111", "222"})
        self.assertAlmostEqual(h.plugin.muted["111"]["until"], old["111"], places=3)
        self.assertIsNone(h.plugin.muted["222"]["until"])
        self.assertFalse(h.plugin.muted["222"]["whole"])

    async def test_load_new_format(self):
        h = _PluginHarness()
        data = {
            "111": {
                "until": time.time() + 3600,
                "whole": False,
                "self_id": "999",
                "manual": False,
            },
            "222": {"until": None, "whole": True, "self_id": "999", "manual": False},
        }
        h.state_file.write_text(json.dumps(data), encoding="utf-8")
        h.plugin._load_state()
        self.assertEqual(set(h.plugin.muted), {"111", "222"})
        self.assertEqual(h.plugin.muted["111"]["self_id"], "999")
        self.assertTrue(h.plugin.muted["222"]["whole"])

    async def test_load_tolerates_v11_format(self):
        # v1.1 的文件没有 self_id / manual 字段,读取时补默认值
        h = _PluginHarness()
        data = {"111": {"until": time.time() + 3600, "whole": False}}
        h.state_file.write_text(json.dumps(data), encoding="utf-8")
        h.plugin._load_state()
        info = h.plugin.muted["111"]
        self.assertIsNone(info["self_id"])
        self.assertFalse(info["manual"])

    async def test_load_purges_expired_entry(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=3600))
        # 停机期间禁言早已到期:重启后不应继续拦截
        h.plugin.muted["111"]["until"] = time.time() - 100
        h.plugin._save_state()
        h2 = _PluginHarness(data_dir=h.data_dir)
        self.assertNotIn("111", h2.plugin.muted)

    async def test_load_keeps_manual_entry_past_until(self):
        # 手动闭嘴的条目不会因为 until 过期被清掉
        h = _PluginHarness()
        data = {
            "111": {
                "until": time.time() - 100,
                "whole": False,
                "self_id": "999",
                "manual": True,
            }
        }
        h.state_file.write_text(json.dumps(data), encoding="utf-8")
        h.plugin._load_state()
        self.assertIn("111", h.plugin.muted)

    async def test_state_file_falls_back_without_startools(self):
        # 老版本 AstrBot 没有 StarTools -> 退回插件目录
        import astrbot.api.star as star_mod

        saved = star_mod.StarTools
        del star_mod.StarTools
        try:
            plugin = ShutUpWhenMuted(FakeContext(), AstrBotConfig({}))
            self.assertEqual(
                plugin._state_file.parent,
                Path(main.__file__).resolve().parent,
            )
        finally:
            star_mod.StarTools = saved

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

    async def test_status_shows_manual(self):
        h = await self._install()
        async for _ in h.plugin.mute_cmd(FakeEvent(), "111"):
            pass
        results = []
        async for r in h.plugin.status_cmd(FakeEvent()):
            results.append(r)
        self.assertIn("手动闭嘴中", results[0].text)

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
        self.assertTrue(h.plugin.muted["111"]["manual"])
        self.assertIsNone(h.plugin.muted["111"]["until"])
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

    async def test_manual_mute_survives_lift_notice(self):
        # 手动 /闭嘴 的群,收到解禁通知也不该自动恢复(等 /闭嘴解除)
        h = await self._install()
        async for _ in h.plugin.mute_cmd(FakeEvent(self_id="999"), "111"):
            pass
        await self._notice(
            h.plugin, _ban_notice("111", "999", "999", duration=0, sub_type="lift_ban")
        )
        self.assertIn("111", h.plugin.muted)
        await self._send(session_id="111")
        self.assertEqual(FakeAiocqhttpMessageEvent.sent, [])

    async def test_expire_keeps_manual_mute(self):
        # 手动闭嘴的条目即使被写了 until,到期也不自动恢复
        h = await self._install()
        async for _ in h.plugin.mute_cmd(FakeEvent(), "111"):
            pass
        h.plugin.muted["111"]["until"] = time.time() - 1
        expired = h.plugin._expire_once()
        self.assertEqual(expired, [])
        self.assertIn("111", h.plugin.muted)

    async def test_missing_group_id_hint(self):
        h = await self._install()
        for coro in (h.plugin.mute_cmd(FakeEvent()), h.plugin.unmute_cmd(FakeEvent())):
            results = []
            async for r in coro:
                results.append(r)
            self.assertIn("请指定群号", results[0].text)

    # ------------------------------------------------------- LLM 抑制
    async def test_group_message_suppresses_llm_when_muted(self):
        h = await self._install()
        await self._notice(h.plugin, _ban_notice("111", "999", "999", duration=60))
        ev = FakeEvent(group_id="111", self_id="999")
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

    async def test_notice_without_self_id_uses_event_self_id(self):
        # 通知里没带 self_id 时,退回用 event.get_self_id()
        h = await self._install()
        raw = _ban_notice("111", "999", "999", duration=60)
        raw.pop("self_id")
        ev = FakeEvent(group_id="111", self_id="999", raw=raw)
        await h.plugin.on_group_ban_notice(ev)
        self.assertIn("111", h.plugin.muted)
        self.assertEqual(h.plugin.muted["111"]["self_id"], "999")

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