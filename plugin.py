"""麦麦炒外汇 — 插件主入口（P0）。

职责：
- 生命周期 on_load / on_unload / on_config_update；
- 组装服务：SQLite 账本、新浪行情源、交易编排器（scheduler.TradingEngine）；
- 读取**宿主人设**（WebUI「身份与人格」页 ``personality.personality``）：on_load 时读一次，
  原样注入决策提示词；插件不定义人设、不做性格数值化、也没有人设设置/刷新命令；
- 把最近群聊作为**参考材料**注入决策上下文（``influence.collect``，默认关闭），
  不评分、不设权重、不落盘；
- 注册 Command ``/外汇``：查询/控制（控制类仅管理员）；
- 播报：合并转发（失败回退纯文本），不写宿主消息库；
- LLM 决策与事件播报由编排器产生，本层负责发送。

明确不做：真实交易通道、EventHandler/MessageGateway、自然语言触发下单、图片。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from maibot_sdk import Command, MaiBotPlugin

# P0 兼容：部分模块使用绝对导入（from models import ...）。宿主按包加载时插件目录
# 不在 sys.path，因此这里补一次（仅追加本插件目录，避免影响宿主其余模块）。
# 注意：本项目已不再有与标准库同名的模块（旧 calendar.py 已改名 econ_calendar.py），
# 所以不需要「预热标准库 calendar」这类防遮蔽补丁。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from . import decision, influence, report
from .commands import CommandServices, handle as handle_command
from .config import CONFIG_VERSION, MaiFxConfig
from .config_ui import build_config_ui_schema
from .ledger import Ledger
from .market_clock import now_utc
from .quotes import QuoteSource
from .scheduler import TradingEngine

PLUGIN_VERSION = "0.3.4"


class MaiFxSimPlugin(MaiBotPlugin):
    """麦麦炒外汇插件实例。"""

    config_model = MaiFxConfig

    @classmethod
    def build_config_schema(cls, **plugin_info: Any):
        return build_config_ui_schema(super().build_config_schema(**plugin_info))

    def __init__(self) -> None:
        super().__init__()
        self._engine: TradingEngine | None = None
        self._ledger: Ledger | None = None
        self._source: QuoteSource | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._setup_lock = asyncio.Lock()
        self._outbox: dict[str, list[str]] = {}
        self._outbox_task: asyncio.Task[None] | None = None
        self._outbox_lock = asyncio.Lock()
        self._allowed_streams: set[str] = set()
        self._chat_streams: set[str] = set()
        # 宿主人设（on_load 时读一次；插件不定义人设，读不到就是空串）
        self._persona: str = ""

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        async with self._setup_lock:
            await self._stop_services()
            await self._start_services()
        engine = self._engine
        self.ctx.logger.info(
            "麦麦炒外汇 %s 已加载（config_version=%s，账户=%s，自动交易=%s，品种=%d，宿主人设=%s，群聊上下文=%s，财经日历=%s/%s）",
            PLUGIN_VERSION,
            CONFIG_VERSION,
            f"{int(self.config.account.initial_balance):,} 元 / {int(self.config.account.leverage)} 倍",
            "开启" if self.config.decision.auto_trade else "关闭",
            len(engine.pairs) if engine else 0,
            f"{len(self._persona)} 字" if self._persona else "未读取到",
            "开启" if self.config.influence.enabled else "关闭",
            "开启" if self.config.calendar.enabled else "关闭",
            str(getattr(self.config.calendar, "source", "auto")),
        )

    async def on_unload(self) -> None:
        async with self._setup_lock:
            await self._stop_services()
        self.ctx.logger.info("麦麦炒外汇已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data
        if scope != "self":
            return
        async with self._setup_lock:
            await self._stop_services()
            await self._start_services()
        self.ctx.logger.info("麦麦炒外汇配置已更新（version=%s），引擎已重建", version)

    # ---------- 服务装配 ----------

    def _data_dir(self) -> Any:
        paths = getattr(self.ctx, "paths", None)
        data_dir = getattr(paths, "data_dir", None)
        if data_dir is None:
            raise RuntimeError("宿主未提供 ctx.paths.data_dir")
        return data_dir

    async def _start_services(self) -> None:
        data_dir = self._data_dir()
        self._ledger = Ledger(data_dir / "ledger.sqlite3")
        if self._ledger.backup_path is not None:
            self.ctx.logger.warning(
                "账本：检测到旧版（日元）库，已备份到 %s 并重建空库（不做金额静默迁移）",
                self._ledger.backup_path,
            )
        self._ledger.init(int(self.config.account.initial_balance))
        self._source = QuoteSource(
            timeout_seconds=int(self.config.market.quote_timeout_seconds),
            include_usdcny=bool(getattr(self.config.market, "usdcny_enabled", True)),
            include_cny_reference=bool(
                getattr(self.config.market, "cny_reference_enabled", True)
            ),
        )
        # 宿主人设：读不到就空着（不阻塞加载，也不伪造性格）
        self._persona = await self._load_persona()
        self._engine = TradingEngine(
            ctx=self.ctx,
            config=self.config,
            quote_source=self._source,
            ledger=self._ledger,
            log=lambda msg: self.ctx.logger.warning(msg),
            persona=self._persona,
            chat_context_fetcher=self._fetch_chat_context,
        )
        await self._engine.start()
        self._event_task = asyncio.create_task(self._event_loop(), name="mai-fx-events")
        await self._refresh_streams()

    async def _stop_services(self) -> None:
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._event_task = None
        await self._flush_outbox()
        if self._engine:
            await self._engine.stop()
            self._engine = None
        if self._ledger:
            self._ledger.close()
            self._ledger = None
        self._source = None

    async def _event_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.2)
                if self._engine is None:
                    return
                for text in self._engine.drain_events():
                    await self._send_text(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("事件发送循环异常：%s", exc)

    # ---------- 宿主人设 ----------

    async def _load_persona(self) -> str:
        """从宿主读取人设字符串（``personality.personality``）；读不到就空串。"""

        try:
            response = await self.ctx.call_capability(
                "config.get",
                key="personality.personality",
                default="",
            )
        except Exception as exc:  # noqa: BLE001 - 人设缺失不影响交易
            self.ctx.logger.warning("读取宿主人设失败：%s", exc)
            return ""
        # SDK 会把 config.get 的返回值归一化成 value 本身（字符串），也可能返回字典
        if isinstance(response, str):
            persona = response.strip()
        elif isinstance(response, dict):
            if response.get("success") is False:
                return ""
            persona = str(response.get("value") or "").strip()
        else:
            persona = ""
        if persona:
            self.ctx.logger.info("已读取宿主人设（%d 字）", len(persona))
        return persona

    # ---------- 聊天流 / 发送 ----------

    async def _resolve_group_streams(self, group_ids: list[Any], platform: str = "qq") -> set[str]:
        """把群号解析成会话 id（宿主 ``chat.get_stream_by_group_id`` 返回 ``{"stream": {...}}``）。"""

        streams: set[str] = set()
        for group_id in group_ids:
            try:
                result = await self.ctx.call_capability(
                    "chat.get_stream_by_group_id",
                    group_id=str(group_id),
                    platform=str(platform or "qq"),
                )
            except Exception:  # noqa: BLE001 - 解析失败只是不播报/不读群
                continue
            stream_id: Any = None
            if isinstance(result, dict):
                stream = result.get("stream")
                if isinstance(stream, dict):
                    stream_id = stream.get("stream_id") or stream.get("session_id")
                stream_id = stream_id or result.get("stream_id") or result.get("session_id")
            elif result is not None and not isinstance(result, (list, tuple)):
                stream_id = getattr(result, "stream_id", None) or result
            if stream_id:
                streams.add(str(stream_id))
        return streams

    async def _refresh_streams(self) -> None:
        platform = str(getattr(self.config.notify, "platform", "qq") or "qq")
        self._allowed_streams = await self._resolve_group_streams(
            list(getattr(self.config.notify, "group_ids", []) or []), platform
        )
        chat_cfg = getattr(self.config, "influence", None)
        chat_groups = list(getattr(chat_cfg, "group_ids", []) or [])
        if chat_groups:
            self._chat_streams = await self._resolve_group_streams(chat_groups, platform)
            if not self._chat_streams:
                self.ctx.logger.warning("群聊上下文：配置的群号没有解析到聊天流，本功能按没有群聊处理")
        else:
            self._chat_streams = set(self._allowed_streams)
        if self._engine is not None:
            self._engine.chat_context_chat_ids = sorted(self._chat_streams)

    async def _fetch_chat_context(self, now: Any = None) -> Any:
        """调度器回调：读取最近群聊原样上下文（失败一律退化为空上下文）。"""

        del now  # 宿主能力按「当前时间 − hours」自行取数
        engine = self._engine
        if engine is None:
            return influence.empty_context(note=influence.NOTE_FAILED)
        chat_ids = list(engine.chat_context_chat_ids or sorted(self._chat_streams))
        return await influence.collect(
            self.ctx,
            self.config,
            chat_ids,
            log=lambda msg: self.ctx.logger.warning(msg),
        )

    async def _send_text(self, text: str, *, stream_id: str | None = None) -> None:
        if not text:
            return
        targets = [stream_id] if stream_id else sorted(self._allowed_streams)
        if not targets:
            self.ctx.logger.warning("没有可用播报聊天流：%s", text[:60])
            return
        if not bool(getattr(self.config.notify, "merge_messages", True)):
            await self._send_plain(text, targets)
            return
        async with self._outbox_lock:
            for target in targets:
                self._outbox.setdefault(target, []).append(text)
            if self._outbox_task is None or self._outbox_task.done():
                delay = max(0.1, float(getattr(self.config.notify, "merge_window_ms", 800)) / 1000.0)
                self._outbox_task = asyncio.create_task(self._flush_after(delay), name="mai-fx-outbox")

    async def _flush_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._flush_outbox()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("合并消息任务异常：%s", exc)

    async def _flush_outbox(self) -> None:
        async with self._outbox_lock:
            if not self._outbox:
                return
            batches = self._outbox
            self._outbox = {}
            self._outbox_task = None
        for target, texts in batches.items():
            if texts:
                await self._send_forward(target, texts)

    async def _send_plain(self, text: str, targets: list[str]) -> None:
        for target in targets:
            try:
                sent = await self.ctx.send.text(text, target, storage_message=False)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("播报异常 stream=%s：%s", target, exc)
                continue
            if sent is False:
                self.ctx.logger.warning("播报发送失败 stream=%s", target)

    async def _send_forward(self, target: str, texts: list[str]) -> None:
        nodes = [
            {"nickname": "麦麦外汇", "segments": [{"type": "text", "content": text}]}
            for text in texts
        ]
        try:
            result = await self.ctx.send.forward(
                nodes,
                target,
                return_details=True,
                storage_message=False,
                processed_plain_text="",
            )
            sent = bool(result.get("sent")) if isinstance(result, dict) else bool(result)
        except Exception as exc:  # noqa: BLE001 - 失败回退纯文本
            self.ctx.logger.warning("合并转发异常，回退纯文本 stream=%s：%s", target, exc)
            sent = False
        if sent:
            return
        await self._send_plain("\n\n".join(texts), [target])

    # ---------- 服务视图 ----------

    def _services(self) -> CommandServices:
        engine = self._engine
        assert engine is not None
        actions = {
            "pause": engine.pause,
            "resume": engine.resume,
            "reset": engine.reset,
            "manual": engine.manual_order,
            "close": engine.close_position,
            "cancel": engine.cancel_order,
            "dry_run": self._dry_run,
        }
        return CommandServices(
            cfg=self.config,
            account=engine.account,
            positions=lambda: list(engine.positions),
            quotes=lambda: dict(engine.quotes),
            pairs=dict(engine.pairs),
            ledger=self._ledger,
            engine=engine,
            actions=actions,
        )

    async def _dry_run(self) -> str:
        engine = self._engine
        if engine is None:
            return "引擎未就绪"
        chat_context_lines, calendar_lines = await engine.collect_context(now_utc())
        budget = decision.CallBudget("dry-run", 1)
        intent = await decision.decide(
            self.ctx,
            engine.account,
            engine.positions,
            engine.quotes,
            engine.pairs,
            [],
            self.config,
            budget,
            persona=self._persona,
            chat_context_lines=chat_context_lines,
            calendar_lines=calendar_lines,
        )
        engine.last_intent = intent
        return report.decision_report(intent)

    # ---------- 命令 ----------

    def _command_allowed(self, *, group_id: str, platform: str, user_id: str, is_local_operator: bool) -> bool:
        if is_local_operator:
            return True
        config = self.config
        if str(platform or "").strip().lower() != str(config.notify.platform).strip().lower():
            return False
        group_text = str(group_id or "").strip()
        if group_text and group_text in {str(item).strip() for item in config.notify.group_ids}:
            return True
        if bool(config.notify.private_enabled) and str(user_id or "").strip():
            return True
        return False

    @Command(
        "外汇",
        description="麦麦炒外汇模拟盘：状态/持仓/挂单/行情/保证金/隔夜/帮助；控制命令仅管理员",
        pattern=r"^/(?:外汇|fx)(?:\s+.*)?$",
        timeout_ms=180000,
    )
    async def handle_fx(
        self,
        stream_id: str = "",
        text: str = "",
        group_id: str = "",
        platform: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        del kwargs
        if not text:
            return False, "", 0
        if not self._command_allowed(
            group_id=group_id, platform=platform, user_id=user_id, is_local_operator=is_local_operator
        ):
            return False, "", 0
        if self._engine is None or self._ledger is None:
            await self._send_text("外汇模拟盘尚未就绪，请稍后再试。", stream_id=stream_id or None)
            return False, "", 0
        response = await handle_command(
            self._services(),
            platform=platform,
            user_id=user_id,
            group_id=group_id or None,
            text=text,
        )
        if response:
            await self._send_text(response, stream_id=stream_id or None)
        await self._flush_outbox()
        return True, "", 0


def create_plugin() -> MaiFxSimPlugin:
    """宿主加载入口：不接受参数，返回插件实例。"""

    return MaiFxSimPlugin()
