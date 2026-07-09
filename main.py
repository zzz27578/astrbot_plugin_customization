from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Face, Forward, Image, Json, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.api.web import PluginUploadFile, error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_customization"
STORE_VERSION = 1
SEND_STEPS = {"card", "record", "image", "text"}
DEFAULT_SEND_ORDER = ["record", "card", "image", "text"]
LEGACY_DEFAULT_SEND_ORDER = ["card", "record", "image", "text"]
FORWARD_EXPAND_MAX_DEPTH = 6
DAILY_TEST_POLL_SECONDS = 30
DIRECT_RECORD_SETTLE_SECONDS = 5.0


class ForwardExpandError(RuntimeError):
    def __init__(self, unresolved_ids: list[str]) -> None:
        self.unresolved_ids = unresolved_ids
        preview = "、".join(unresolved_ids[:5])
        if len(unresolved_ids) > 5:
            preview += f" 等 {len(unresolved_ids)} 个"
        super().__init__(f"嵌套聊天记录无法展开：{preview}")


DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "mode": "all",
    "whitelist_groups": [],
    "blacklist_groups": [],
    "send_order": DEFAULT_SEND_ORDER,
    "send_interval_seconds": 1.5,
    "retry_enabled": True,
    "retry_count": 1,
    "retry_interval_seconds": 5.0,
    "delivery_compensation_enabled": True,
    "delivery_confirm_wait_seconds": 8.0,
    "delivery_compensation_count": 1,
    "delivery_compensation_interval_seconds": 15.0,
    "dedupe_enabled": True,
    "dedupe_minutes": 1440,
    "notify_admin_private": False,
    "notify_admin_group": False,
    "admin_qq_list": [],
    "notify_group_id": "",
    "notify_on_success": False,
    "group_fallback_enabled": False,
    "group_fallback_mode": "all_failed",
    "group_fallback_at": False,
    "group_fallback_template": "欢迎加入，请检查机器人私聊或查看群公告。",
    "card_fallback_enabled": False,
    "card_fallback_text": "",
    "record_fallback_enabled": False,
    "record_fallback_text": "",
    "image_fallback_enabled": False,
    "image_fallback_text": "",
    "text_content": "",
    "active_card_id": "",
    "active_record_id": "",
    "active_image_id": "",
    "test_receiver_qq": "",
    "daily_test_enabled": False,
    "daily_test_receiver_qq": "",
    "daily_test_time": "09:00",
    "daily_test_last_date": "",
    "max_logs": 100,
}


DEFAULT_STORE: dict[str, Any] = {
    "version": STORE_VERSION,
    "settings": DEFAULT_SETTINGS,
    "cards": {},
    "records": {},
    "images": {},
    "dedupe": {},
    "logs": [],
}


class WelcomeCustomizationPlugin(Star):
    """新人入群自动私聊欢迎素材：QQ 卡片、聊天记录和图片。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.image_dir = self.data_dir / "images"
        self.store_path = self.data_dir / "store.json"
        self.store: dict[str, Any] = self._load_store()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.pending_join_keys: set[str] = set()
        self.worker_task: asyncio.Task | None = None
        self.daily_test_task: asyncio.Task | None = None

        context.register_web_api(
            f"/{PLUGIN_NAME}/state",
            self.api_state,
            ["GET"],
            "Get welcome customization state",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings",
            self.api_save_settings,
            ["POST"],
            "Save welcome customization settings",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/activate",
            self.api_activate,
            ["POST"],
            "Activate a material",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/delete",
            self.api_delete,
            ["POST"],
            "Delete a material",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/rename",
            self.api_rename,
            ["POST"],
            "Rename a material",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/test",
            self.api_test,
            ["POST"],
            "Send a welcome test message",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/image/upload",
            self.api_upload_image,
            ["POST"],
            "Upload a welcome image",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/logs/clear",
            self.api_clear_logs,
            ["POST"],
            "Clear welcome delivery logs",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/astrbot/max-agent-step",
            self.api_save_max_agent_step,
            ["POST"],
            "Save AstrBot max agent step",
        )

    async def initialize(self) -> None:
        self._ensure_worker()
        self._ensure_daily_test_task()

    async def terminate(self) -> None:
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        if self.daily_test_task and not self.daily_test_task.done():
            self.daily_test_task.cancel()
            try:
                await self.daily_test_task
            except asyncio.CancelledError:
                pass

    def _load_store(self) -> dict[str, Any]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        if not self.store_path.exists():
            store = deepcopy(DEFAULT_STORE)
            self._write_store(store)
            return store
        try:
            with self.store_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            logger.exception("欢迎私聊插件数据读取失败，已使用默认空数据。")
            raw = {}
        store = deepcopy(DEFAULT_STORE)
        self._deep_update(store, raw)
        store["settings"] = self._normalize_settings(store.get("settings", {}))
        return store

    def _write_store(self, store: dict[str, Any] | None = None) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = store or self.store
        with self.store_path.open("w", encoding="utf-8") as f:
            json.dump(target, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _deep_update(base: dict[str, Any], incoming: dict[str, Any]) -> None:
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                WelcomeCustomizationPlugin._deep_update(base[key], value)
            else:
                base[key] = value

    def _normalize_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(DEFAULT_SETTINGS)
        if isinstance(settings, dict):
            for key in DEFAULT_SETTINGS:
                if key in settings:
                    normalized[key] = settings[key]
        normalized["mode"] = (
            normalized["mode"]
            if normalized["mode"] in {"all", "whitelist", "disabled"}
            else "all"
        )
        normalized["group_fallback_mode"] = (
            normalized["group_fallback_mode"]
            if normalized["group_fallback_mode"] in {"all_failed", "any_failed", "on_join"}
            else "all_failed"
        )
        for key in ("whitelist_groups", "blacklist_groups", "admin_qq_list"):
            normalized[key] = self._normalize_id_list(normalized.get(key, []))
        send_order = [
            item
            for item in normalized.get("send_order", [])
            if item in SEND_STEPS
        ]
        normalized["send_order"] = (
            DEFAULT_SEND_ORDER.copy()
            if not send_order or send_order == LEGACY_DEFAULT_SEND_ORDER
            else send_order
        )
        normalized["text_content"] = str(normalized.get("text_content") or "")
        normalized["send_interval_seconds"] = self._bounded_float(
            normalized.get("send_interval_seconds"),
            0,
            30,
            1.5,
        )
        normalized["retry_count"] = self._bounded_int(
            normalized.get("retry_count"),
            0,
            5,
            1,
        )
        normalized["retry_interval_seconds"] = self._bounded_float(
            normalized.get("retry_interval_seconds"),
            0,
            600,
            5,
        )
        normalized["delivery_confirm_wait_seconds"] = self._bounded_float(
            normalized.get("delivery_confirm_wait_seconds"),
            1,
            120,
            8,
        )
        normalized["delivery_compensation_count"] = self._bounded_int(
            normalized.get("delivery_compensation_count"),
            0,
            2,
            1,
        )
        normalized["delivery_compensation_interval_seconds"] = self._bounded_float(
            normalized.get("delivery_compensation_interval_seconds"),
            1,
            600,
            15,
        )
        normalized["dedupe_minutes"] = self._bounded_int(
            normalized.get("dedupe_minutes"),
            0,
            10080,
            1440,
        )
        normalized["max_logs"] = self._bounded_int(
            normalized.get("max_logs"),
            10,
            500,
            100,
        )
        normalized["test_receiver_qq"] = str(
            normalized.get("test_receiver_qq") or "",
        ).strip()
        normalized["daily_test_enabled"] = bool(normalized.get("daily_test_enabled"))
        normalized["daily_test_receiver_qq"] = str(
            normalized.get("daily_test_receiver_qq") or "",
        ).strip()
        normalized["daily_test_time"] = self._normalize_time_hhmm(
            normalized.get("daily_test_time"),
            "09:00",
        )
        normalized["daily_test_last_date"] = str(
            normalized.get("daily_test_last_date") or "",
        ).strip()
        return normalized

    @staticmethod
    def _normalize_id_list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = re.split(r"[\s,，;；]+", value)
        if not isinstance(value, list):
            return []
        ret: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in ret:
                ret.append(text)
        return ret

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
        try:
            ret = int(value)
        except (TypeError, ValueError):
            return fallback
        return max(minimum, min(maximum, ret))

    @staticmethod
    def _bounded_float(
        value: Any,
        minimum: float,
        maximum: float,
        fallback: float,
    ) -> float:
        try:
            ret = float(value)
        except (TypeError, ValueError):
            return fallback
        return max(minimum, min(maximum, ret))

    async def _prepare_record_node_for_send(
        self,
        bot: Any,
        node: dict[str, Any] | None,
        routing: dict[str, Any],
        depth: int,
        seen: set[str],
        unresolved: list[str],
    ) -> list[dict[str, Any]]:
        if not node:
            return []
        normalized = self._normalize_node_data(node)
        if isinstance(node.get("_record_meta"), dict):
            normalized["_record_meta"] = dict(node["_record_meta"])
        content, extra_nodes = await self._prepare_record_segments_for_send(
            bot,
            normalized.get("content", []),
            routing,
            depth,
            seen,
            unresolved,
        )
        result: list[dict[str, Any]] = []
        if content:
            normalized["content"] = content
            result.append(normalized)
        result.extend(extra_nodes)
        return result

    async def _prepare_record_segments_for_send(
        self,
        bot: Any,
        segments: list[dict[str, Any]],
        routing: dict[str, Any],
        depth: int,
        seen: set[str],
        unresolved: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        content: list[dict[str, Any]] = []
        extra_nodes: list[dict[str, Any]] = []
        for segment in self._normalize_raw_segments(segments):
            forward_id = self._forward_id_from_segment(segment)
            if forward_id:
                content.append({"type": "forward", "data": {"id": forward_id}})
                continue

            data = segment.get("data") if isinstance(segment, dict) else None
            nested_nodes = data.get("_nested_nodes") if isinstance(data, dict) else None
            if segment.get("type") == "forward" and isinstance(nested_nodes, list):
                for child in nested_nodes:
                    extra_nodes.extend(
                        await self._prepare_record_node_for_send(
                            bot,
                            deepcopy(child),
                            routing,
                            depth + 1,
                            seen,
                            unresolved,
                        ),
                    )
                continue

            embedded_nodes = self._embedded_forward_nodes_from_segment(segment)
            if embedded_nodes:
                for child in embedded_nodes:
                    extra_nodes.extend(
                        await self._prepare_record_node_for_send(
                            bot,
                            self._forward_message_to_node(child),
                            routing,
                            depth + 1,
                            seen,
                            unresolved,
                        ),
                    )
                continue

            content.append(self._strip_internal_segment_fields(segment))
        return content, extra_nodes

    async def _nodes_to_nested_node_segment(
        self,
        bot: Any,
        nodes: list[dict[str, Any]],
        routing: dict[str, Any],
        depth: int,
        seen: set[str],
        unresolved: list[str],
    ) -> dict[str, Any] | None:
        prepared: list[dict[str, Any]] = []
        for node in nodes:
            prepared.extend(
                await self._prepare_record_node_for_send(
                    bot,
                    deepcopy(node),
                    routing,
                    depth,
                    seen,
                    unresolved,
                ),
            )
        if not prepared:
            return None
        return {
            "type": "node",
            "data": {
                "content": [
                    {"type": "node", "data": self._node_for_send(child)}
                    for child in prepared
                ],
            },
        }

    async def _forward_segment_with_nested_backup(
        self,
        bot: Any,
        forward_id: str,
        routing: dict[str, Any],
        depth: int,
        seen: set[str],
        unresolved: list[str],
    ) -> dict[str, Any] | None:
        nested = await self._fetch_forward_nodes(
            bot,
            forward_id,
            routing,
            depth + 1,
            seen,
            unresolved,
            "",
        )
        if not nested:
            return None
        return await self._nodes_to_nested_node_segment(
            bot,
            nested,
            routing,
            depth + 1,
            seen,
            unresolved,
        )

    @staticmethod
    def _normalize_time_hhmm(value: Any, fallback: str) -> str:
        text = str(value or "").strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{1,2})", text)
        if not match:
            return fallback
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23 or minute > 59:
            return fallback
        return f"{hour:02d}:{minute:02d}"

    def _save(self) -> None:
        self.store["settings"] = self._normalize_settings(self.store["settings"])
        max_logs = self.store["settings"]["max_logs"]
        self.store["logs"] = self.store.get("logs", [])[-max_logs:]
        self._write_store()

    def _ensure_worker(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._process_join_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("欢迎私聊队列任务执行失败。")
            finally:
                join_key = str(job.get("join_key") or "")
                if join_key:
                    self.pending_join_keys.discard(join_key)
                self.queue.task_done()

    def _ensure_daily_test_task(self) -> None:
        if self.daily_test_task is None or self.daily_test_task.done():
            self.daily_test_task = asyncio.create_task(self._daily_test_worker())

    async def _daily_test_worker(self) -> None:
        while True:
            try:
                await self._run_daily_test_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Daily welcome test delivery failed.")
            await asyncio.sleep(DAILY_TEST_POLL_SECONDS)

    async def _run_daily_test_once(self) -> None:
        settings = self.store["settings"]
        if not settings.get("daily_test_enabled"):
            return
        target = str(settings.get("daily_test_receiver_qq") or "").strip()
        if not target:
            return
        today = time.strftime("%Y-%m-%d")
        if str(settings.get("daily_test_last_date") or "") == today:
            return
        if time.strftime("%H:%M") != str(settings.get("daily_test_time") or "09:00"):
            return

        settings["daily_test_last_date"] = today
        self._save()
        bot = self._get_aiocqhttp_bot()
        if bot is None:
            self._append_log(
                "failed",
                "daily-test",
                target,
                "daily_test",
                "aiocqhttp platform is not online",
            )
            return

        summary = await self._send_current_config_to_target(bot, target, {})
        failed = summary["failed"]
        self._append_log(
            "failed" if failed else "success",
            "daily-test",
            target,
            "daily_test",
            "; ".join(item["error"] for item in failed),
            summary["results"],
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_any_event(self, event: AstrMessageEvent):
        raw = self._raw_event_from_event(event)
        join = self._group_increase_payload(raw)
        if not join:
            return

        group_id = join["group_id"]
        user_id = join["user_id"]
        self_id = join["self_id"]
        join_key = self._join_key(group_id, user_id)
        if not group_id or not user_id or user_id == self_id:
            return
        if not self._group_enabled(group_id):
            return
        if join_key in self.pending_join_keys or self._is_deduped(group_id, user_id):
            return

        bot = getattr(event, "bot", None)
        if bot is None:
            self._append_log(
                "failed",
                group_id,
                user_id,
                "event",
                "当前事件没有 aiocqhttp bot 客户端，无法发送私聊。",
            )
            return

        self._ensure_worker()
        self.pending_join_keys.add(join_key)
        await self.queue.put(
            {
                "bot": bot,
                "group_id": group_id,
                "user_id": user_id,
                "self_id": self_id,
                "join_key": join_key,
                "source": "group_increase",
            },
        )

    @filter.command("帮助", alias={"help"})
    async def help_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        yield event.plain_result(self._help_text())

    @filter.command("状态", alias={"status"})
    async def status_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        yield event.plain_result(self._status_text())

    @filter.command("测试", alias={"test"})
    async def test_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"测试", "test"})
        target = args[0] if args else event.get_sender_id()
        yield event.plain_result(await self._test_send(event, target))

    @filter.command("启用", alias={"enable"})
    async def enable_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"启用", "enable"})
        yield event.plain_result(self._enable_group(event, args[0] if args else "当前群"))

    @filter.command("禁用", alias={"disable"})
    async def disable_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"禁用", "disable"})
        yield event.plain_result(self._disable_group(event, args[0] if args else "当前群"))

    @filter.command("模式", alias={"mode"})
    async def mode_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"模式", "mode"})
        if not args:
            yield event.plain_result("用法：/模式 all|whitelist|disabled")
            return
        yield event.plain_result(self._set_mode(args[0]))

    @filter.command("卡片", alias={"card"})
    async def card_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"卡片", "card"})
        yield event.plain_result(await self._card_command(event, args))

    @filter.command("记录", alias={"record", "forward"})
    async def record_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"记录", "record", "forward"})
        yield event.plain_result(await self._record_command(event, args))

    @filter.command("图片", alias={"image", "img"})
    async def image_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"图片", "image", "img"})
        yield event.plain_result(await self._image_command(event, args))

    @filter.command("管理员", alias={"admin"})
    async def admin_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"管理员", "admin"})
        yield event.plain_result(self._admin_command(args))

    @filter.command("通知", alias={"notify"})
    async def notify_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"通知", "notify"})
        yield event.plain_result(self._toggle_command("notify_admin_private", args))

    @filter.command("群内兜底", alias={"fallback", "group_fallback"})
    async def group_fallback_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"群内兜底", "fallback", "group_fallback"})
        yield event.plain_result(self._toggle_command("group_fallback_enabled", args))

    @filter.command("去重", alias={"dedupe"})
    async def dedupe_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return
        args = self._command_args(event, {"去重", "dedupe"})
        action = self._normalize_action_word(args[0]) if args else "状态"
        if action in {"清空", "clear"}:
            count = len(self.store.get("dedupe", {}))
            self.store["dedupe"] = {}
            self.pending_join_keys.clear()
            self._save()
            yield event.plain_result(f"已清空入群去重记录：{count} 条")
            return
        if action in {"状态", "status"}:
            yield event.plain_result(
                f"入群去重：{len(self.store.get('dedupe', {}))} 条；待发送队列：{self.queue.qsize()} 条",
            )
            return
        yield event.plain_result("用法：/去重 状态；/去重 清空")

    def _command_args(self, event: AstrMessageEvent, names: set[str]) -> list[str]:
        text = re.sub(r"\s+", " ", event.get_message_str().strip())
        for name in sorted(names, key=len, reverse=True):
            text = re.sub(rf"^[/!！。.]?{re.escape(name)}\b", "", text, count=1).strip()
        if not text:
            return []
        return self._split_args(text)

    @staticmethod
    def _split_args(text: str) -> list[str]:
        try:
            return shlex.split(text)
        except ValueError:
            return text.split()

    def _is_operator(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        admins = set(self.store["settings"].get("admin_qq_list", []))
        return sender in admins or event.is_admin()

    @staticmethod
    def _is_group_increase(raw: Any) -> bool:
        return bool(WelcomeCustomizationPlugin._group_increase_payload(raw))

    @staticmethod
    def _raw_event_from_event(event: AstrMessageEvent) -> Any:
        message_obj = getattr(event, "message_obj", None)
        candidates = [
            getattr(message_obj, "raw_message", None),
            getattr(message_obj, "raw_event", None),
            getattr(event, "raw_message", None),
            getattr(event, "raw_event", None),
            getattr(event, "event", None),
        ]
        for candidate in candidates:
            raw = WelcomeCustomizationPlugin._unwrap_raw_event(candidate)
            if raw is not None:
                return raw
        return None

    @staticmethod
    def _unwrap_raw_event(value: Any, depth: int = 0) -> Any:
        if value is None or depth > 4:
            return None
        if any(
            WelcomeCustomizationPlugin._raw_get(value, key)
            for key in ("notice_type", "noticeType", "post_type", "postType", "type")
        ):
            return value
        for key in ("raw_message", "raw_event", "event", "data", "payload"):
            nested = WelcomeCustomizationPlugin._raw_get(value, key)
            raw = WelcomeCustomizationPlugin._unwrap_raw_event(nested, depth + 1)
            if raw is not None:
                return raw
        return None

    @staticmethod
    def _raw_get(raw: Any, key: str, default: Any = "") -> Any:
        if raw is None:
            return default
        try:
            if hasattr(raw, "get"):
                value = raw.get(key, default)
                if value not in (None, ""):
                    return value
        except Exception:
            pass
        return getattr(raw, key, default)

    @staticmethod
    def _first_raw_value(raw: Any, keys: tuple[str, ...]) -> str:
        for key in keys:
            value = WelcomeCustomizationPlugin._raw_get(raw, key, "")
            if value not in (None, ""):
                return str(value).strip()
        return ""

    @staticmethod
    def _group_increase_payload(raw: Any) -> dict[str, str] | None:
        if raw is None:
            return None
        post_type = str(
            WelcomeCustomizationPlugin._raw_get(raw, "post_type")
            or WelcomeCustomizationPlugin._raw_get(raw, "postType")
            or "",
        ).strip()
        notice_type = str(
            WelcomeCustomizationPlugin._raw_get(raw, "notice_type")
            or WelcomeCustomizationPlugin._raw_get(raw, "noticeType")
            or WelcomeCustomizationPlugin._raw_get(raw, "type")
            or "",
        ).strip()
        normalized_notice = re.sub(r"[^a-z0-9]", "_", notice_type.lower()).strip("_")
        allowed = {
            "group_increase",
            "group_member_increase",
            "member_increase",
            "group_member_added",
            "member_added",
        }
        if post_type and post_type != "notice":
            return None
        if normalized_notice not in allowed and not (
            "group" in normalized_notice
            and any(word in normalized_notice for word in ("increase", "join", "add"))
        ):
            return None
        group_id = WelcomeCustomizationPlugin._first_raw_value(raw, ("group_id", "groupId", "group"))
        user_id = WelcomeCustomizationPlugin._first_raw_value(
            raw,
            ("user_id", "userId", "member_id", "memberId", "qq", "uin"),
        )
        self_id = WelcomeCustomizationPlugin._first_raw_value(raw, ("self_id", "selfId", "bot_id", "botId"))
        if not group_id or not user_id:
            return None
        return {"group_id": group_id, "user_id": user_id, "self_id": self_id}

    def _group_enabled(self, group_id: str) -> bool:
        settings = self.store["settings"]
        if not settings.get("enabled", True):
            return False
        if group_id in settings.get("blacklist_groups", []):
            return False
        mode = settings.get("mode", "all")
        if mode == "disabled":
            return False
        if mode == "whitelist":
            return group_id in settings.get("whitelist_groups", [])
        return True

    def _is_deduped(self, group_id: str, user_id: str) -> bool:
        settings = self.store["settings"]
        if not settings.get("dedupe_enabled", True):
            return False
        minutes = int(settings.get("dedupe_minutes", 0))
        if minutes <= 0:
            return False
        key = self._join_key(group_id, user_id)
        last = float(self.store.get("dedupe", {}).get(key, 0))
        return time.time() - last < minutes * 60

    def _mark_dedupe(self, group_id: str, user_id: str) -> None:
        self.store.setdefault("dedupe", {})[self._join_key(group_id, user_id)] = time.time()
        self._save()

    @staticmethod
    def _join_key(group_id: str, user_id: str) -> str:
        return f"{group_id}:{user_id}"

    async def _process_join_job(self, job: dict[str, Any]) -> None:
        bot = job["bot"]
        group_id = str(job["group_id"])
        user_id = str(job["user_id"])
        self_id = str(job.get("self_id", ""))
        routing = {"self_id": self_id} if self_id else {}
        step_results: list[dict[str, Any]] = []

        for step in self.store["settings"].get("send_order", []):
            result = await self._send_step_with_retry(
                bot,
                user_id,
                step,
                routing,
                origin_group_id=group_id,
            )
            step_results.append(result)
            if not result.get("skipped"):
                await asyncio.sleep(float(self.store["settings"]["send_interval_seconds"]))

        ok = all(item["ok"] for item in step_results)
        failed_steps = [item for item in step_results if not item["ok"]]
        self._append_log(
            "success" if ok else "failed",
            group_id,
            user_id,
            self._summarize_step_results(step_results, failed_steps),
            "" if ok else "；".join(item["error"] for item in failed_steps),
            step_results,
        )
        self._mark_dedupe(group_id, user_id)

        if ok:
            if (
                self.store["settings"].get("group_fallback_enabled")
                and self.store["settings"].get("group_fallback_mode") == "on_join"
            ):
                await self._send_group_fallback(bot, group_id, user_id, routing)
            if self.store["settings"].get("notify_on_success"):
                await self._notify_admins(
                    bot,
                    f"欢迎私聊发送成功\n群号：{group_id}\n新人：{user_id}",
                    routing,
                )
            return

        await self._run_fallbacks(
            bot,
            group_id,
            user_id,
            routing,
            failed_steps,
            step_results,
        )

    async def _send_step_with_retry(
        self,
        bot: Any,
        user_id: str,
        step: str,
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> dict[str, Any]:
        attempts = 1
        if self.store["settings"].get("retry_enabled", True):
            attempts += int(self.store["settings"].get("retry_count", 0))
        last_error = ""
        use_missing_fallback = False
        fallback_text = ""
        markers: list[dict[str, Any]] = []
        if step not in SEND_STEPS:
            return {
                "step": step,
                "ok": False,
                "attempts": 0,
                "error": f"{step}: 未知发送步骤",
            }
        if not self._step_has_config(step):
            fallback_text = self._fallback_text_for_step(step)
            if not fallback_text:
                return {
                    "step": step,
                    "ok": True,
                    "attempts": 0,
                    "error": "",
                    "skipped": True,
                }
            use_missing_fallback = True
            markers = [{"type": "text", "text": fallback_text}]
        else:
            markers = self._delivery_markers(step)
        started_at = int(time.time())
        for index in range(attempts):
            try:
                await self._send_step_or_fallback(
                    bot,
                    user_id,
                    step,
                    routing,
                    origin_group_id,
                    use_missing_fallback,
                    fallback_text,
                )
                return {"step": step, "ok": True, "attempts": index + 1, "error": ""}
            except Exception as e:
                last_error = str(e)
                if (
                    self.store["settings"].get("delivery_compensation_enabled", True)
                    and self._is_ambiguous_send_timeout(e)
                ):
                    return await self._handle_ambiguous_timeout(
                        bot,
                        user_id,
                        step,
                        routing,
                        origin_group_id,
                        use_missing_fallback,
                        fallback_text,
                        markers,
                        started_at,
                        index + 1,
                        last_error,
                    )
                if index + 1 < attempts:
                    await asyncio.sleep(
                        float(self.store["settings"].get("retry_interval_seconds", 5)),
                    )
        return {
            "step": step,
            "ok": False,
            "attempts": attempts,
            "error": f"{step}: {last_error}",
            "fallback": use_missing_fallback,
        }

    async def _send_step_or_fallback(
        self,
        bot: Any,
        user_id: str,
        step: str,
        routing: dict[str, Any],
        origin_group_id: str | None,
        use_missing_fallback: bool,
        fallback_text: str,
    ) -> None:
        if use_missing_fallback:
            await self._send_private_payload(
                bot,
                user_id,
                [{"type": "text", "data": {"text": fallback_text}}],
                routing,
                origin_group_id,
            )
            return
        await self._send_step(bot, user_id, step, routing, origin_group_id)

    async def _handle_ambiguous_timeout(
        self,
        bot: Any,
        user_id: str,
        step: str,
        routing: dict[str, Any],
        origin_group_id: str | None,
        use_missing_fallback: bool,
        fallback_text: str,
        markers: list[dict[str, Any]],
        started_at: int,
        attempts_done: int,
        last_error: str,
    ) -> dict[str, Any]:
        settings = self.store["settings"]
        wait_seconds = float(settings.get("delivery_confirm_wait_seconds", 8))
        await asyncio.sleep(wait_seconds)
        if await self._confirm_recent_private_delivery(
            bot,
            user_id,
            markers,
            started_at,
        ):
            return {
                "step": step,
                "ok": True,
                "attempts": attempts_done,
                "error": "",
                "verified": True,
                "ambiguous_timeout": True,
            }

        compensation_count = int(settings.get("delivery_compensation_count", 1))
        interval = float(settings.get("delivery_compensation_interval_seconds", 15))
        for offset in range(compensation_count):
            await asyncio.sleep(interval)
            try:
                await self._send_step_or_fallback(
                    bot,
                    user_id,
                    step,
                    routing,
                    origin_group_id,
                    use_missing_fallback,
                    fallback_text,
                )
                return {
                    "step": step,
                    "ok": True,
                    "attempts": attempts_done + offset + 1,
                    "error": "",
                    "compensated": True,
                    "ambiguous_timeout": True,
                }
            except Exception as e:
                last_error = str(e)
                if self._is_ambiguous_send_timeout(e):
                    await asyncio.sleep(wait_seconds)
                    if await self._confirm_recent_private_delivery(
                        bot,
                        user_id,
                        markers,
                        started_at,
                    ):
                        return {
                            "step": step,
                            "ok": True,
                            "attempts": attempts_done + offset + 1,
                            "error": "",
                            "verified": True,
                            "ambiguous_timeout": True,
                        }
        return {
            "step": step,
            "ok": False,
            "attempts": attempts_done + compensation_count,
            "error": f"{step}: {last_error}",
            "ambiguous_timeout": True,
            "compensation_exhausted": True,
            "fallback": use_missing_fallback,
        }

    async def _send_step(
        self,
        bot: Any,
        user_id: str,
        step: str,
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> None:
        if step == "card":
            card = self._active_item("cards", "active_card_id")
            if not card:
                raise ValueError("未设置启用卡片")
            await self._send_private_payload(
                bot,
                user_id,
                [{"type": "json", "data": {"data": card["raw_json"]}}],
                routing,
                origin_group_id,
            )
            return
        if step == "record":
            record = self._active_item("records", "active_record_id")
            if not record or not self._record_has_content(record):
                raise ValueError("未设置启用聊天记录")
            if self._record_is_direct(record):
                await self._send_direct_record(bot, user_id, record, routing, origin_group_id)
                await asyncio.sleep(DIRECT_RECORD_SETTLE_SECONDS)
                return
            root_forward_id = str(record.get("root_forward_id") or "").strip()
            if root_forward_id:
                try:
                    delivery_group_id = str(
                        origin_group_id
                        or record.get("root_forward_group_id")
                        or record.get("source_group_id")
                        or "",
                    )
                    await self._send_record_forward_id(
                        bot,
                        user_id,
                        root_forward_id,
                        routing,
                        delivery_group_id or None,
                    )
                    return
                except Exception:
                    if not record.get("nodes"):
                        raise
                    logger.exception("原始合并转发 id 发送失败，降级为本地节点备份。")
            await self._send_record_nodes(
                bot,
                user_id,
                record.get("nodes", []),
                routing,
                origin_group_id,
            )
            return
        if step == "image":
            image = self._active_item("images", "active_image_id")
            if not image:
                raise ValueError("未设置启用图片")
            await self._send_private_payload(
                bot,
                user_id,
                [{"type": "image", "data": {"file": self._image_payload(image)}}],
                routing,
                origin_group_id,
            )
            return
        if step == "text":
            segments = self._text_segments()
            if not segments:
                raise ValueError("未配置文字内容")
            errors = []
            for index, text in enumerate(segments):
                try:
                    await self._send_private_payload(
                        bot,
                        user_id,
                        [{"type": "text", "data": {"text": text}}],
                        routing,
                        origin_group_id,
                    )
                except Exception as e:
                    errors.append(str(e))
                if index + 1 < len(segments):
                    await asyncio.sleep(
                        float(self.store["settings"].get("send_interval_seconds", 1.5)),
                    )
            if errors:
                raise RuntimeError("；".join(errors))
            return
        raise ValueError(f"未知发送步骤：{step}")

    def _step_has_config(self, step: str) -> bool:
        if step == "card":
            return bool(self._active_item("cards", "active_card_id"))
        if step == "record":
            record = self._active_item("records", "active_record_id")
            return bool(record and self._record_has_content(record))
        if step == "image":
            return bool(self._active_item("images", "active_image_id"))
        if step == "text":
            return bool(self._text_segments())
        return False

    def _fallback_text_for_step(self, step: str) -> str:
        settings = self.store["settings"]
        mapping = {
            "card": ("card_fallback_enabled", "card_fallback_text"),
            "record": ("record_fallback_enabled", "record_fallback_text"),
            "image": ("image_fallback_enabled", "image_fallback_text"),
        }
        keys = mapping.get(step)
        if not keys:
            return ""
        enabled_key, text_key = keys
        if not settings.get(enabled_key):
            return ""
        return str(settings.get(text_key, "")).strip()

    def _text_segments(self) -> list[str]:
        return [
            item.strip()
            for item in str(self.store["settings"].get("text_content", "")).splitlines()
            if item.strip()
        ]

    def _delivery_markers(self, step: str) -> list[dict[str, Any]]:
        if step == "card":
            return [{"type": "json"}]
        if step == "record":
            return [{"type": "forward"}]
        if step == "image":
            return [{"type": "image"}]
        if step == "text":
            return [{"type": "text", "text": item} for item in self._text_segments()]
        return []

    @staticmethod
    def _is_ambiguous_send_timeout(error: Exception) -> bool:
        text = str(error)
        compact = re.sub(r"\s+", "", text)
        return (
            "Timeout: NTEvent" in text
            and "NodeIKernelMsgService/sendMsg" in text
            and '"result":0' in compact
            and '"errMsg":""' in compact
        )

    async def _confirm_recent_private_delivery(
        self,
        bot: Any,
        user_id: str,
        markers: list[dict[str, Any]],
        started_at: int,
        routing: dict[str, Any] | None = None,
    ) -> bool:
        if not markers:
            return False
        for action, params in self._history_probe_actions(user_id):
            try:
                data = await bot.call_action(action, **params, **(routing or {}))
            except Exception:
                continue
            messages = self._extract_history_messages(data)
            if self._history_contains_markers(messages, markers, started_at):
                return True
        return False

    @staticmethod
    def _history_probe_actions(user_id: str) -> list[tuple[str, dict[str, Any]]]:
        uid = int(user_id)
        return [
            ("get_friend_msg_history", {"user_id": uid, "count": 30}),
            ("get_private_msg_history", {"user_id": uid, "count": 30}),
            ("get_msg_history", {"message_type": "private", "user_id": uid, "count": 30}),
        ]

    def _extract_history_messages(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("messages", "msgs", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = self._extract_history_messages(value)
                if nested:
                    return nested
        return [data] if "message" in data or "raw_message" in data else []

    def _history_contains_markers(
        self,
        messages: list[dict[str, Any]],
        markers: list[dict[str, Any]],
        started_at: int,
    ) -> bool:
        if not messages:
            return False
        recent = []
        for message in messages:
            timestamp = self._message_timestamp(message)
            if timestamp and timestamp < started_at - 3:
                continue
            recent.append(message)
        return all(
            any(self._message_matches_marker(message, marker) for message in recent)
            for marker in markers
        )

    @staticmethod
    def _message_timestamp(message: dict[str, Any]) -> int:
        for key in ("time", "timestamp", "message_time"):
            try:
                value = int(message.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                return value
        return 0

    def _message_matches_marker(
        self,
        message: dict[str, Any],
        marker: dict[str, Any],
    ) -> bool:
        raw = str(message.get("raw_message") or "")
        segments = self._coerce_message_segments(message.get("message"))
        marker_type = marker.get("type")
        if marker_type == "text":
            text = str(marker.get("text") or "")
            if text and text in raw:
                return True
            return any(
                segment.get("type") == "text"
                and text in str((segment.get("data") or {}).get("text") or "")
                for segment in segments
            )
        if marker_type == "forward" and any(
            hint in raw for hint in ("[聊天记录]", "聊天记录", "转发消息", "forward")
        ):
            return True
        return any(segment.get("type") == marker_type for segment in segments) or (
            marker_type and f"[{marker_type}]" in raw.lower()
        )

    @staticmethod
    def _coerce_message_segments(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        if isinstance(value, str) and value:
            return [{"type": "text", "data": {"text": value}}]
        return []

    @staticmethod
    def _summarize_step_results(
        results: list[dict[str, Any]],
        failed_steps: list[dict[str, Any]],
    ) -> str:
        notes = []
        for item in results:
            step = item.get("step", "unknown")
            if item.get("skipped"):
                notes.append(f"{step}:跳过")
            elif item.get("verified"):
                notes.append(f"{step}:复核成功")
            elif item.get("compensated"):
                notes.append(f"{step}:补偿发送")
            elif item.get("ambiguous_timeout") and item.get("ok"):
                notes.append(f"{step}:疑似送达")
        if notes:
            return ",".join(notes)
        return ",".join(item["step"] for item in failed_steps) or "all"

    async def _send_private_payload(
        self,
        bot: Any,
        user_id: str,
        payload: list[dict[str, Any]],
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> None:
        await self._call_send_attempts(
            bot,
            self._private_payload_attempts(user_id, payload, routing, origin_group_id),
        )

    def _private_payload_attempts(
        self,
        user_id: str,
        payload: list[dict[str, Any]],
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        base = {
            "user_id": int(user_id),
            "message": payload,
            **routing,
        }
        if not origin_group_id:
            return [("send_private_msg", base)]
        group_id = int(origin_group_id)
        with_group = {**base, "group_id": group_id}
        return [
            ("send_private_msg", with_group),
            (
                "send_msg",
                {
                    **with_group,
                    "message_type": "private",
                    "sub_type": "group",
                },
            ),
        ]

    async def _call_send_attempts(
        self,
        bot: Any,
        attempts: list[tuple[str, dict[str, Any]]],
    ) -> Any:
        errors: list[str] = []
        for action, params in attempts:
            try:
                return await bot.call_action(action, **params)
            except Exception as e:
                errors.append(f"{action}: {e}")
        raise RuntimeError("; ".join(errors))

    def _record_is_direct(self, record: dict[str, Any]) -> bool:
        return str(record.get("mode") or "") == "direct_forward"

    def _record_has_content(self, record: dict[str, Any]) -> bool:
        if self._record_is_direct(record):
            return bool(record.get("source_message_id") or record.get("nodes"))
        return bool(record.get("root_forward_id") or record.get("nodes"))

    @staticmethod
    def _trusted_direct_record_strategy(strategy: str) -> bool:
        return strategy in {"forward_group_single_msg", "forward_friend_single_msg"}

    @staticmethod
    def _direct_record_routing(
        record: dict[str, Any],
        routing: dict[str, Any],
    ) -> dict[str, Any]:
        ret = dict(routing)
        source_self_id = str(record.get("source_self_id") or "").strip()
        if source_self_id:
            ret["self_id"] = source_self_id
        return ret

    async def _send_direct_record(
        self,
        bot: Any,
        user_id: str,
        record: dict[str, Any],
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> None:
        direct_routing = self._direct_record_routing(record, routing)
        try:
            attempts = await self._try_direct_record_strategies(
                bot,
                user_id,
                record,
                direct_routing,
                origin_group_id,
                stop_on_success=True,
                trusted_only=False,
                confirm_delivery=True,
            )
        except Exception as e:
            attempts = [{"strategy": "direct_source", "ok": False, "error": str(e)}]
        winner = next((item for item in attempts if item.get("ok")), None)
        if winner:
            strategy = winner.get("strategy", "")
            if strategy and self._trusted_direct_record_strategy(strategy):
                record["last_strategy"] = strategy
            record.pop("last_error", None)
            record["last_success_at"] = int(time.time())
            record["updated_at"] = int(time.time())
            self._save()
            return
        refresh_error = ""
        refreshed = False
        try:
            refreshed = await self._refresh_direct_record_source(bot, record, direct_routing)
        except Exception as e:
            refresh_error = str(e)
        if refreshed:
            try:
                retry_attempts = await self._try_direct_record_strategies(
                    bot,
                    user_id,
                    record,
                    direct_routing,
                    origin_group_id,
                    stop_on_success=True,
                    trusted_only=False,
                    confirm_delivery=True,
                )
            except Exception as e:
                retry_attempts = [{"strategy": "direct_source_retry", "ok": False, "error": str(e)}]
            attempts.extend(retry_attempts)
            winner = next((item for item in retry_attempts if item.get("ok")), None)
            if winner:
                strategy = winner.get("strategy", "")
                if strategy and self._trusted_direct_record_strategy(strategy):
                    record["last_strategy"] = strategy
                record.pop("last_error", None)
                record["last_success_at"] = int(time.time())
                record["updated_at"] = int(time.time())
                self._save()
                return

        fallback_nodes = record.get("nodes", [])
        if fallback_nodes:
            try:
                started_at = int(time.time())
                await self._send_record_nodes(
                    bot,
                    user_id,
                    fallback_nodes,
                    direct_routing,
                    origin_group_id,
                )
                wait_seconds = float(
                    self.store["settings"].get("delivery_confirm_wait_seconds", 8),
                )
                await asyncio.sleep(wait_seconds)
                if not await self._confirm_recent_private_delivery(
                    bot,
                    user_id,
                    [{"type": "forward"}],
                    started_at,
                    direct_routing,
                ):
                    raise RuntimeError("本地备份 API 返回成功，但最近私聊历史未确认收到聊天记录")
                record["last_strategy"] = "local_backup_nodes"
                record["last_backup_send_at"] = int(time.time())
                record.pop("last_error", None)
                record["updated_at"] = int(time.time())
                await self._notify_direct_record_degraded(
                    bot,
                    record,
                    user_id,
                    origin_group_id,
                    direct_routing,
                    attempts,
                )
                self._save()
                return
            except Exception as e:
                attempts.append({"strategy": "local_backup_nodes", "ok": False, "error": str(e)})
        errors = "；".join(
            f"{item.get('strategy')}: {item.get('error')}" for item in attempts
        )
        if refresh_error:
            errors = f"{errors}；refresh_source: {refresh_error}" if errors else refresh_error
        record["last_error"] = errors or "原消息直转失败"
        record["last_failed_at"] = int(time.time())
        record["updated_at"] = int(time.time())
        self._save()
        await self._notify_direct_record_failed(
            bot,
            record,
            user_id,
            origin_group_id,
            direct_routing,
            errors or "原消息直转失败",
        )
        raise RuntimeError(errors or "原消息直转失败")

    async def _notify_direct_record_degraded(
        self,
        bot: Any,
        record: dict[str, Any],
        user_id: str,
        origin_group_id: str | None,
        routing: dict[str, Any],
        attempts: list[dict[str, Any]],
    ) -> None:
        now = int(time.time())
        last_notify = int(record.get("last_degraded_notify_at") or 0)
        if now - last_notify < 86400:
            return
        record["last_degraded_notify_at"] = now
        errors = "；".join(
            f"{item.get('strategy')}: {item.get('error')}"
            for item in attempts
            if not item.get("ok")
        )
        await self._notify_admins(
            bot,
            "聊天记录直转已降级为本地备份发送\n"
            f"素材：{record.get('name', record.get('id', ''))}\n"
            f"目标 QQ：{user_id}\n"
            f"来源群：{origin_group_id or record.get('source_group_id') or '-'}\n"
            f"原消息：{record.get('source_message_id') or '-'}\n"
            f"错误：{errors or '直转缓存不可用'}",
            routing,
            force_private=True,
        )

    async def _notify_direct_record_failed(
        self,
        bot: Any,
        record: dict[str, Any],
        user_id: str,
        origin_group_id: str | None,
        routing: dict[str, Any],
        error: str,
    ) -> None:
        now = int(time.time())
        last_notify = int(record.get("last_failed_notify_at") or 0)
        if now - last_notify < 86400:
            return
        record["last_failed_notify_at"] = now
        self._save()
        await self._notify_admins(
            bot,
            "聊天记录直转发送失败\n"
            f"素材：{record.get('name', record.get('id', ''))}\n"
            f"目标 QQ：{user_id}\n"
            f"来源群：{origin_group_id or record.get('source_group_id') or '-'}\n"
            f"原消息：{record.get('source_message_id') or '-'}\n"
            f"本地备份：{len(record.get('nodes') or [])} 个节点\n"
            f"错误：{error}",
            routing,
            force_private=True,
        )

    async def _send_record_forward_id(
        self,
        bot: Any,
        user_id: str,
        forward_id: str,
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> None:
        await self._send_forward_id_private_confirmed(
            bot,
            user_id,
            forward_id,
            routing,
            origin_group_id,
        )

    async def _send_forward_id_private_confirmed(
        self,
        bot: Any,
        user_id: str,
        forward_id: str,
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> None:
        payload = [{"type": "forward", "data": {"id": forward_id}}]
        attempts = self._private_payload_attempts(user_id, payload, routing, origin_group_id)
        wait_seconds = float(self.store["settings"].get("delivery_confirm_wait_seconds", 8))
        errors: list[str] = []
        for action, params in attempts:
            started_at = int(time.time())
            try:
                await bot.call_action(action, **params)
            except Exception as e:
                errors.append(f"{action}: {e}")
                continue
            await asyncio.sleep(wait_seconds)
            if await self._confirm_recent_private_delivery(
                bot,
                user_id,
                [{"type": "forward"}],
                started_at,
                routing,
            ):
                return
            errors.append(f"{action}: API 返回成功，但最近私聊历史未确认收到聊天记录")
        raise RuntimeError("; ".join(errors) or "合并转发发送失败")

    async def _send_record_nodes(
        self,
        bot: Any,
        user_id: str,
        nodes: list[dict[str, Any]],
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> None:
        prepared = await self._prepare_record_nodes_for_send(bot, nodes, routing)
        params = {
            "user_id": int(user_id),
            "messages": [
                {"type": "node", "data": self._node_for_send(node)}
                for node in prepared
            ],
            **routing,
        }
        await self._call_send_attempts(
            bot,
            self._private_forward_attempts(params, origin_group_id),
        )

    def _private_forward_attempts(
        self,
        params: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        if not origin_group_id:
            return [("send_private_forward_msg", params)]
        group_id = int(origin_group_id)
        with_group = {**params, "group_id": group_id}
        return [
            (
                "send_forward_msg",
                {
                    **with_group,
                    "message_type": "private",
                    "sub_type": "group",
                    "message": with_group.get("messages", []),
                },
            ),
            ("send_private_forward_msg", with_group),
            (
                "send_msg",
                {
                    **with_group,
                    "message_type": "private",
                    "sub_type": "group",
                    "message": with_group.get("messages", []),
                },
            ),
        ]

    def _node_for_send(self, node: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_node_data(node)
        return {
            "user_id": normalized["user_id"],
            "nickname": normalized["nickname"],
            "content": [
                self._strip_internal_segment_fields(segment)
                for segment in normalized["content"]
            ],
        }

    @staticmethod
    def _strip_internal_segment_fields(segment: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(segment, dict):
            return segment
        cleaned = deepcopy(segment)
        data = cleaned.get("data")
        if isinstance(data, dict):
            data.pop("_nested_nodes", None)
            data.pop("_source_forward_id", None)
        cleaned.pop("_record_meta", None)
        return cleaned

    async def _refresh_direct_record_source(
        self,
        bot: Any,
        record: dict[str, Any],
        routing: dict[str, Any],
    ) -> bool:
        source_message_id = str(record.get("source_message_id") or "").strip()
        if not source_message_id:
            return False
        params = {**routing}
        source_self_id = str(record.get("source_self_id") or "").strip()
        if source_self_id:
            params["self_id"] = source_self_id
        ret = await bot.call_action("get_msg", message_id=int(source_message_id), **params)
        message = ret.get("message", []) if isinstance(ret, dict) else []
        forward_id = self._forward_id_from_raw(message)
        changed = False
        if forward_id and forward_id != str(record.get("source_forward_id") or ""):
            record["source_forward_id"] = forward_id
            changed = True
        if isinstance(ret, dict):
            group_id = str(ret.get("group_id") or "").strip()
            user_id = str(ret.get("user_id") or "").strip()
            if group_id and group_id != str(record.get("source_group_id") or ""):
                record["source_group_id"] = group_id
                changed = True
            if user_id and user_id != str(record.get("source_user_id") or ""):
                record["source_user_id"] = user_id
                changed = True
        record["last_refresh_at"] = int(time.time())
        if changed:
            record["updated_at"] = int(time.time())
        self._save()
        return bool(forward_id)

    async def _try_direct_record_strategies(
        self,
        bot: Any,
        user_id: str,
        record: dict[str, Any],
        routing: dict[str, Any],
        origin_group_id: str | None = None,
        stop_on_success: bool = False,
        trusted_only: bool = False,
        confirm_delivery: bool = False,
    ) -> list[dict[str, Any]]:
        source_message_id = str(record.get("source_message_id") or "").strip()
        if not source_message_id:
            raise ValueError("直转记录缺少原消息 message_id")
        source_group_id = str(record.get("source_group_id") or "").strip()
        source_forward_id = str(record.get("source_forward_id") or "").strip()
        delivery_group_id = str(origin_group_id or source_group_id or "").strip()
        preferred = str(record.get("last_strategy") or "").strip()
        strategies: list[str] = []
        if source_forward_id:
            strategies.append("forward_segment_private")
        if not source_group_id:
            strategies.append("forward_friend_single_msg")
        if delivery_group_id:
            strategies.append("forward_node_id_private")
            strategies.append("forward_node_id_private_without_group")
        else:
            strategies.append("forward_node_id_private")
            strategies.append("forward_node_id_private_without_group")
        if trusted_only:
            strategies = [
                strategy
                for strategy in strategies
                if self._trusted_direct_record_strategy(strategy)
            ]
        if self._trusted_direct_record_strategy(preferred) and preferred in strategies:
            strategies.remove(preferred)
            strategies.insert(0, preferred)

        results: list[dict[str, Any]] = []
        for strategy in strategies:
            started_at = int(time.time())
            try:
                if strategy == "forward_segment_private" and confirm_delivery:
                    await self._send_forward_id_private_confirmed(
                        bot,
                        user_id,
                        source_forward_id,
                        routing,
                        delivery_group_id or None,
                    )
                else:
                    await self._send_direct_record_by_strategy(
                        bot,
                        strategy,
                        user_id,
                        source_message_id,
                        source_forward_id,
                        source_group_id,
                        delivery_group_id,
                        routing,
                    )
                if confirm_delivery and strategy != "forward_segment_private":
                    wait_seconds = float(
                        self.store["settings"].get("delivery_confirm_wait_seconds", 8),
                    )
                    await asyncio.sleep(wait_seconds)
                    if not await self._confirm_recent_private_delivery(
                        bot,
                        user_id,
                        [{"type": "forward"}],
                        started_at,
                        routing,
                    ):
                        raise RuntimeError("直转 API 返回成功，但最近私聊历史未确认收到聊天记录")
                result = {"strategy": strategy, "ok": True, "error": ""}
                results.append(result)
                if stop_on_success:
                    break
            except Exception as e:
                results.append({"strategy": strategy, "ok": False, "error": str(e)})
        return results

    async def _send_direct_record_by_strategy(
        self,
        bot: Any,
        strategy: str,
        user_id: str,
        source_message_id: str,
        source_forward_id: str,
        source_group_id: str,
        delivery_group_id: str,
        routing: dict[str, Any],
    ) -> None:
        if strategy == "forward_node_id_private":
            params = {
                "user_id": int(user_id),
                "messages": [
                    {"type": "node", "data": {"id": source_message_id}},
                ],
                **routing,
            }
            await self._call_send_attempts(
                bot,
                self._private_forward_attempts(params, delivery_group_id or None),
            )
            return
        if strategy == "forward_node_id_private_without_group":
            await bot.call_action(
                "send_private_forward_msg",
                user_id=int(user_id),
                messages=[{"type": "node", "data": {"id": source_message_id}}],
                **routing,
            )
            return
        if strategy == "forward_friend_single_msg":
            await bot.call_action(
                "forward_friend_single_msg",
                user_id=int(user_id),
                message_id=source_message_id,
                **routing,
            )
            return
        if strategy == "forward_group_single_msg":
            if not source_group_id:
                raise ValueError("群原消息直转缺少来源群号")
            await bot.call_action(
                "forward_group_single_msg",
                group_id=int(source_group_id),
                user_id=int(user_id),
                message_id=source_message_id,
                **routing,
            )
            return
        if strategy == "forward_segment_private":
            if not source_forward_id:
                raise ValueError("直转记录缺少合并转发 id")
            await self._send_private_payload(
                bot,
                user_id,
                [{"type": "forward", "data": {"id": source_forward_id}}],
                routing,
                delivery_group_id or None,
            )
            return
        raise ValueError(f"未知直转策略：{strategy}")

    def _active_item(self, collection: str, active_key: str) -> dict[str, Any] | None:
        item_id = self.store["settings"].get(active_key)
        if not item_id:
            return None
        item = self.store.get(collection, {}).get(item_id)
        return item if isinstance(item, dict) else None

    def _image_payload(self, image: dict[str, Any]) -> str:
        source = str(image.get("source", ""))
        if image.get("kind") == "local":
            path = Path(source)
            if not path.exists():
                raise FileNotFoundError(f"图片文件不存在：{source}")
            with path.open("rb") as f:
                return "base64://" + base64.b64encode(f.read()).decode("ascii")
        return source

    async def _run_fallbacks(
        self,
        bot: Any,
        group_id: str,
        user_id: str,
        routing: dict[str, Any],
        failed_steps: list[dict[str, Any]],
        step_results: list[dict[str, Any]] | None = None,
    ) -> None:
        fallback_texts = []
        failed_names = {item["step"] for item in failed_steps}
        settings = self.store["settings"]
        if "card" in failed_names and settings.get("card_fallback_enabled"):
            fallback_texts.append(settings.get("card_fallback_text", ""))
        if "record" in failed_names and settings.get("record_fallback_enabled"):
            fallback_texts.append(settings.get("record_fallback_text", ""))
        if "image" in failed_names and settings.get("image_fallback_enabled"):
            fallback_texts.append(settings.get("image_fallback_text", ""))

        for text in [item.strip() for item in fallback_texts if item.strip()]:
            try:
                await self._send_private_payload(
                    bot,
                    user_id,
                    [{"type": "text", "data": {"text": text}}],
                    routing,
                    group_id,
                )
            except Exception:
                logger.exception("欢迎私聊降级文本发送失败。")

        await self._notify_admins(
            bot,
            "欢迎私聊发送失败\n"
            f"群号：{group_id}\n"
            f"新人：{user_id}\n"
            f"失败步骤：{', '.join(failed_names)}\n"
            f"错误：{'; '.join(item['error'] for item in failed_steps)}",
            routing,
        )

        if settings.get("group_fallback_enabled"):
            mode = settings.get("group_fallback_mode", "all_failed")
            active_results = [
                item for item in (step_results or []) if not item.get("skipped")
            ]
            should_send = mode in {"any_failed", "on_join"} or (
                bool(failed_steps) and len(failed_steps) >= len(active_results)
            )
            if should_send:
                await self._send_group_fallback(bot, group_id, user_id, routing)

    async def _notify_admins(
        self,
        bot: Any,
        message: str,
        routing: dict[str, Any],
        force_private: bool = False,
    ) -> None:
        settings = self.store["settings"]
        if force_private or settings.get("notify_admin_private"):
            for qq in settings.get("admin_qq_list", []):
                try:
                    await self._send_private_payload(
                        bot,
                        qq,
                        [{"type": "text", "data": {"text": message}}],
                        routing,
                    )
                except Exception:
                    logger.exception("欢迎私聊管理员 QQ 通知发送失败。")
        group_id = str(settings.get("notify_group_id", "")).strip()
        if settings.get("notify_admin_group") and group_id:
            try:
                await bot.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=[{"type": "text", "data": {"text": message}}],
                    **routing,
                )
            except Exception:
                logger.exception("欢迎私聊管理群通知发送失败。")

    async def _send_group_fallback(
        self,
        bot: Any,
        group_id: str,
        user_id: str,
        routing: dict[str, Any],
    ) -> None:
        settings = self.store["settings"]
        message = []
        if settings.get("group_fallback_at", True):
            message.append({"type": "at", "data": {"qq": user_id}})
            message.append({"type": "text", "data": {"text": " "}})
        text = str(settings.get("group_fallback_template", "")).strip()
        if text:
            message.append({"type": "text", "data": {"text": text}})
        if not message:
            return
        await bot.call_action(
            "send_group_msg",
            group_id=int(group_id),
            message=message,
            **routing,
        )

    def _append_log(
        self,
        status: str,
        group_id: str,
        user_id: str,
        step: str,
        error: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.store.setdefault("logs", []).append(
            {
                "id": uuid.uuid4().hex,
                "time": int(time.time()),
                "status": status,
                "group_id": group_id,
                "user_id": user_id,
                "step": step,
                "error": error,
                "details": details or [],
            },
        )
        self._save()

    async def _card_command(self, event: AstrMessageEvent, args: list[str]) -> str:
        if not args:
            return "用法：/卡片 添加 名称；/卡片 使用 名称；/卡片 列表；/卡片 删除 名称"
        action = self._normalize_action_word(args[0])
        if action == "添加":
            name = args[1] if len(args) > 1 else f"卡片{len(self.store['cards']) + 1}"
            raw_json = await self._extract_card_json(event)
            if not raw_json:
                if await self._reply_has_record_reference(event):
                    return "引用消息是聊天记录转发，不会作为 QQ JSON 卡片保存。请使用 /记录 添加 名称。"
                return "没有在引用消息中找到 QQ JSON 卡片。请回复卡片消息后再执行。"
            card_id = uuid.uuid4().hex
            summary = self._summarize_card(raw_json)
            self.store["cards"][card_id] = {
                "id": card_id,
                "name": name,
                "raw_json": raw_json,
                "created_at": int(time.time()),
                **summary,
            }
            self.store["settings"]["active_card_id"] = card_id
            self._save()
            return f"已添加并启用卡片：{name}"
        return self._material_command("cards", "active_card_id", "卡片", args)

    async def _record_command(self, event: AstrMessageEvent, args: list[str]) -> str:
        if not args:
            return "用法：/记录 添加 名称；/记录 直转 名称；/记录 探测 名称 QQ；/记录 使用 名称；/记录 列表；/记录 删除 名称"
        action = self._normalize_action_word(args[0])
        if action == "添加":
            name = args[1] if len(args) > 1 else f"聊天记录{len(self.store['records']) + 1}"
            root_forward_id = await self._extract_record_root_forward_id(event)
            try:
                nodes = await self._extract_record_nodes(event)
            except ForwardExpandError as e:
                return f"该聊天记录包含 NapCat 无法读取的内层消息，未保存。{e}\n可改用 /记录 直转 名称 保存原消息直转素材。"
            if not nodes and not root_forward_id:
                return "没有在引用消息中找到可保存的聊天记录内容。"
            record_id = self._find_material_id_by_name("records", name)
            if not record_id:
                record_id = uuid.uuid4().hex
                self.store["records"][record_id] = {
                    "id": record_id,
                    "name": name,
                    "nodes": [],
                    "created_at": int(time.time()),
                }
            self.store["records"][record_id]["mode"] = "nodes"
            if root_forward_id:
                self.store["records"][record_id]["root_forward_id"] = root_forward_id
                self.store["records"][record_id]["root_forward_group_id"] = str(
                    event.get_group_id() or "",
                )
            self.store["records"][record_id]["nodes"].extend(nodes)
            for key in (
                "source_message_id",
                "source_group_id",
                "source_user_id",
                "source_self_id",
                "source_forward_id",
                "last_strategy",
                "last_error",
            ):
                self.store["records"][record_id].pop(key, None)
            self._set_record_capture_meta(
                self.store["records"][record_id],
                self.store["records"][record_id].get("nodes", []),
            )
            self.store["records"][record_id]["updated_at"] = int(time.time())
            self.store["settings"]["active_record_id"] = record_id
            self._save()
            return self._record_save_message(name, nodes)
        if action in {"直转", "原转发", "direct", "raw"}:
            name = args[1] if len(args) > 1 else f"直转记录{len(self.store['records']) + 1}"
            source = await self._extract_direct_record_source(event)
            if not source:
                return "没有找到可直转的引用消息。请回复原始聊天记录消息后执行 /记录 直转 名称。"
            backup_nodes: list[dict[str, Any]] = []
            backup_error = ""
            try:
                backup_nodes = await self._extract_record_nodes(event)
            except Exception as e:
                backup_error = str(e)
            record_id = self._find_material_id_by_name("records", name)
            if not record_id:
                record_id = uuid.uuid4().hex
                self.store["records"][record_id] = {
                    "id": record_id,
                    "name": name,
                    "created_at": int(time.time()),
                }
            self.store["records"][record_id].update(
                {
                    "mode": "direct_forward",
                    "nodes": backup_nodes,
                    "backup_error": backup_error,
                    "backup_captured_at": int(time.time()) if backup_nodes else 0,
                    "updated_at": int(time.time()),
                    **source,
                },
            )
            self._set_record_capture_meta(self.store["records"][record_id], backup_nodes)
            self.store["settings"]["active_record_id"] = record_id
            self._save()
            backup_note = f"，已同时保存本地备份 {len(backup_nodes)} 个节点" if backup_nodes else "，未能保存本地备份"
            if backup_error and not backup_nodes:
                backup_note += f"：{backup_error}"
            return (
                f"已保存并启用原消息直转记录：{name}{backup_note}\n"
                "建议立即执行 /记录 探测 "
                f"{name} QQ号，确认当前 NapCat 能否原样转发嵌套聊天记录。"
            )
        if action in {"探测", "直转测试", "probe", "test_direct"}:
            if len(args) < 2:
                return "用法：/记录 探测 名称 QQ"
            name = args[1]
            target = args[2] if len(args) > 2 else event.get_sender_id()
            return await self._probe_direct_record(event, name, str(target))
        return self._material_command("records", "active_record_id", "聊天记录", args)

    async def _image_command(self, event: AstrMessageEvent, args: list[str]) -> str:
        if not args:
            return "用法：/图片 添加 名称；/图片 使用 名称；/图片 列表；/图片 删除 名称"
        action = self._normalize_action_word(args[0])
        if action == "添加":
            name = args[1] if len(args) > 1 else f"图片{len(self.store['images']) + 1}"
            source = await self._extract_image_source(event)
            if not source:
                return "没有在引用消息中找到图片。请回复图片消息后再执行。"
            image_id = uuid.uuid4().hex
            self.store["images"][image_id] = {
                "id": image_id,
                "name": name,
                "kind": "remote",
                "source": source,
                "created_at": int(time.time()),
            }
            self.store["settings"]["active_image_id"] = image_id
            self._save()
            return f"已添加并启用图片：{name}"
        return self._material_command("images", "active_image_id", "图片", args)

    def _material_command(
        self,
        collection: str,
        active_key: str,
        label: str,
        args: list[str],
    ) -> str:
        action = self._normalize_action_word(args[0])
        if action == "列表":
            items = self.store.get(collection, {})
            if not items:
                return f"还没有保存{label}。"
            active = self.store["settings"].get(active_key)
            lines = [
                f"{'*' if item_id == active else '-'} {item['name']}"
                for item_id, item in items.items()
            ]
            return "\n".join(lines)
        if action == "使用" and len(args) > 1:
            item_id = self._find_material_id_by_name(collection, args[1])
            if not item_id:
                return f"没有找到{label}：{args[1]}"
            self.store["settings"][active_key] = item_id
            self._save()
            return f"已启用{label}：{args[1]}"
        if action == "删除" and len(args) > 1:
            item_id = self._find_material_id_by_name(collection, args[1])
            if not item_id:
                return f"没有找到{label}：{args[1]}"
            del self.store[collection][item_id]
            if self.store["settings"].get(active_key) == item_id:
                self.store["settings"][active_key] = ""
            self._save()
            return f"已删除{label}：{args[1]}"
        return f"{label}命令格式不正确。"

    def _find_material_id_by_name(self, collection: str, name: str) -> str | None:
        for item_id, item in self.store.get(collection, {}).items():
            if item.get("name") == name or item_id == name:
                return item_id
        return None

    def _enable_group(self, event: AstrMessageEvent, group_arg: str) -> str:
        group_id = self._resolve_group_arg(event, group_arg)
        if not group_id:
            return "请在群内使用当前群，或提供群号。"
        settings = self.store["settings"]
        if group_id in settings["blacklist_groups"]:
            settings["blacklist_groups"].remove(group_id)
        if settings["mode"] == "whitelist" and group_id not in settings["whitelist_groups"]:
            settings["whitelist_groups"].append(group_id)
        self._save()
        return f"已启用群：{group_id}"

    def _disable_group(self, event: AstrMessageEvent, group_arg: str) -> str:
        group_id = self._resolve_group_arg(event, group_arg)
        if not group_id:
            return "请在群内使用当前群，或提供群号。"
        settings = self.store["settings"]
        if group_id not in settings["blacklist_groups"]:
            settings["blacklist_groups"].append(group_id)
        if group_id in settings["whitelist_groups"]:
            settings["whitelist_groups"].remove(group_id)
        self._save()
        return f"已禁用群：{group_id}"

    def _resolve_group_arg(self, event: AstrMessageEvent, group_arg: str) -> str:
        if group_arg in {"当前群", "current", "this"}:
            return str(event.get_group_id() or "")
        return str(group_arg).strip()

    def _set_mode(self, mode: str) -> str:
        aliases = {"全部": "all", "白名单": "whitelist", "关闭": "disabled"}
        mode = aliases.get(mode, mode)
        if mode not in {"all", "whitelist", "disabled"}:
            return "模式只能是 all、whitelist、disabled。"
        self.store["settings"]["mode"] = mode
        self._save()
        return f"已切换启用模式：{mode}"

    def _admin_command(self, args: list[str]) -> str:
        if args:
            args[0] = self._normalize_action_word(args[0])
        if len(args) < 2 or args[0] not in {"添加", "删除"}:
            return "用法：/管理员 添加 QQ；/管理员 删除 QQ"
        admins = self.store["settings"]["admin_qq_list"]
        qq = str(args[1]).strip()
        if args[0] == "添加" and qq not in admins:
            admins.append(qq)
        if args[0] == "删除" and qq in admins:
            admins.remove(qq)
        self._save()
        return f"管理员 QQ 列表：{', '.join(admins) or '空'}"

    def _toggle_command(self, key: str, args: list[str]) -> str:
        if not args or args[0] not in {"开", "关", "on", "off", "true", "false"}:
            return "用法：开 / 关"
        value = args[0] in {"开", "on", "true"}
        self.store["settings"][key] = value
        self._save()
        return f"{key} 已{'开启' if value else '关闭'}。"

    @staticmethod
    def _normalize_action_word(word: str) -> str:
        aliases = {
            "add": "添加",
            "set": "添加",
            "use": "使用",
            "select": "使用",
            "list": "列表",
            "ls": "列表",
            "delete": "删除",
            "del": "删除",
            "remove": "删除",
        }
        return aliases.get(word.lower(), word)

    async def _test_send(self, event: AstrMessageEvent, target: str) -> str:
        if not target:
            return "缺少测试接收 QQ。"
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前平台没有 aiocqhttp bot 客户端，无法测试发送。"
        routing = {}
        raw = getattr(event.message_obj, "raw_message", None)
        if hasattr(raw, "get") and raw.get("self_id"):
            routing["self_id"] = raw.get("self_id")
        origin_group_id = str(event.get_group_id() or "").strip()
        summary = await self._send_current_config_to_target(
            bot,
            target,
            routing,
            origin_group_id or None,
        )
        failed = summary["failed"]
        if failed:
            return "测试发送失败：" + "；".join(item["error"] for item in failed)
        sent_count = summary["sent_count"]
        if sent_count <= 0:
            return "当前没有可发送项，已跳过测试发送。"
        return f"已向 {target} 发送当前配置，共 {sent_count} 项。"

    async def _send_current_config_to_target(
        self,
        bot: Any,
        target: str,
        routing: dict[str, Any],
        origin_group_id: str | None = None,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for step in self.store["settings"]["send_order"]:
            result = await self._send_step_with_retry(
                bot,
                target,
                step,
                routing,
                origin_group_id,
            )
            results.append(result)
            if not result.get("skipped"):
                await asyncio.sleep(float(self.store["settings"]["send_interval_seconds"]))
        failed = [item for item in results if not item["ok"]]
        sent_count = len([item for item in results if not item.get("skipped")])
        return {
            "results": results,
            "failed": failed,
            "sent_count": sent_count,
            "skipped_count": len(results) - sent_count,
        }

    def _status_text(self) -> str:
        settings = self.store["settings"]
        return "\n".join(
            [
                "欢迎私聊状态",
                f"总开关：{'开启' if settings['enabled'] else '关闭'}",
                f"启用模式：{settings['mode']}",
                f"卡片：{self._active_name('cards', 'active_card_id')}",
                f"聊天记录：{self._active_name('records', 'active_record_id')}",
                f"图片：{self._active_name('images', 'active_image_id')}",
                f"文字段落：{len(self._text_segments())}",
                f"白名单群：{', '.join(settings['whitelist_groups']) or '空'}",
                f"黑名单群：{', '.join(settings['blacklist_groups']) or '空'}",
                f"管理员 QQ：{', '.join(settings['admin_qq_list']) or '空'}",
            ],
        )

    def _active_name(self, collection: str, active_key: str) -> str:
        item = self._active_item(collection, active_key)
        return item.get("name", "未设置") if item else "未设置"

    def _help_text(self) -> str:
        return (
            "欢迎私聊内置指令\n"
            "/状态\n"
            "/测试 [QQ]\n"
            "/模式 all|whitelist|disabled\n"
            "/启用 当前群|群号\n"
            "/禁用 当前群|群号\n"
            "/卡片 添加 名称（回复 QQ 卡片）\n"
            "/卡片 使用/列表/删除 名称\n"
            "/记录 添加 名称（回复消息，追加为合并转发节点）\n"
            "/记录 使用/列表/删除 名称\n"
            "/图片 添加 名称（回复图片）\n"
            "/图片 使用/列表/删除 名称\n"
            "/管理员 添加/删除 QQ\n"
            "/通知 开|关\n"
            "/群内兜底 开|关\n"
            "/去重 状态/清空"
        )

    async def _extract_card_json(self, event: AstrMessageEvent) -> str:
        raw_segments = await self._raw_reply_segments(event)
        for segment in raw_segments:
            if segment.get("type") == "json":
                value = segment.get("data", {}).get("data")
                if self._looks_like_forward_json_payload(value):
                    continue
                return self._json_segment_to_raw(value)

        for component in self._reply_chain(event):
            if isinstance(component, Json) and not self._looks_like_forward_json_payload(
                component.data,
            ):
                return self._json_segment_to_raw(component.data)
        return ""

    async def _reply_has_forward_json_card(self, event: AstrMessageEvent) -> bool:
        raw_segments = await self._raw_reply_segments(event)
        for segment in raw_segments:
            if segment.get("type") == "json" and self._looks_like_forward_json_payload(
                segment.get("data", {}).get("data"),
            ):
                return True
        return any(
            isinstance(component, Json)
            and self._looks_like_forward_json_payload(component.data)
            for component in self._reply_chain(event)
        )

    async def _reply_has_record_reference(self, event: AstrMessageEvent) -> bool:
        raw_segments = await self._raw_reply_segments(event)
        if any(self._forward_id_from_segment(segment) for segment in raw_segments):
            return True
        if await self._reply_has_forward_json_card(event):
            return True
        return any(isinstance(component, Forward) for component in self._reply_chain(event))

    @staticmethod
    def _json_segment_to_raw(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("data"), str):
            return value["data"]
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _looks_like_forward_json_payload(value: Any) -> bool:
        data = WelcomeCustomizationPlugin._parse_json_card_payload(value)
        return WelcomeCustomizationPlugin._looks_like_forward_json_card(data)

    async def _extract_image_source(self, event: AstrMessageEvent) -> str:
        raw_segments = await self._raw_reply_segments(event)
        for segment in raw_segments:
            if segment.get("type") == "image":
                data = segment.get("data", {})
                return str(data.get("url") or data.get("file") or "")
        for component in self._reply_chain(event):
            if isinstance(component, Image):
                return str(component.url or component.file or "")
        return ""

    async def _extract_record_root_forward_id(self, event: AstrMessageEvent) -> str:
        raw = getattr(event.message_obj, "raw_message", None)
        if hasattr(raw, "get"):
            reply_id = self._reply_id_from_raw(raw.get("message"))
            if reply_id and hasattr(event, "bot"):
                forward_id = await self._forward_id_from_message_id(event, reply_id, raw)
                if forward_id:
                    return forward_id

        for component in self._reply_chain(event, include_reply=True):
            if isinstance(component, Reply) and component.chain:
                for segment in self._components_to_segments(component.chain):
                    forward_id = self._forward_id_from_segment(segment)
                    if forward_id:
                        return forward_id
        return ""

    async def _extract_record_nodes(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        snapshot = await self._reply_snapshot(event)
        if not snapshot:
            return []
        if snapshot.get("nodes"):
            return snapshot["nodes"]
        content = snapshot.get("content", [])
        if not content:
            return []
        unresolved_ids = []
        for segment in content:
            forward_id = self._forward_id_from_segment(segment)
            if forward_id and forward_id not in unresolved_ids:
                unresolved_ids.append(forward_id)
        if unresolved_ids:
            raise ForwardExpandError(unresolved_ids)
        return [
            {
                "user_id": str(snapshot.get("user_id") or event.get_sender_id()),
                "nickname": str(snapshot.get("nickname") or "QQ 用户"),
                "content": content,
            },
        ]

    def _set_record_capture_meta(
        self,
        record: dict[str, Any],
        nodes: list[dict[str, Any]],
    ) -> None:
        external_ids = self._record_external_forward_ids(nodes)
        source_ids, max_depth = self._record_source_meta(nodes)
        record["node_count"] = len(nodes)
        record["capture_depth_limit"] = FORWARD_EXPAND_MAX_DEPTH
        record["source_forward_ids"] = source_ids
        record["max_captured_depth"] = max_depth
        record["external_forward_ids"] = external_ids
        record["capture_mode"] = (
            "partial_external" if external_ids else "complete_local"
        ) if nodes else "empty"
        record["captured_at"] = int(time.time())

    def _record_save_message(self, name: str, nodes: list[dict[str, Any]]) -> str:
        external_ids = self._record_external_forward_ids(nodes)
        message = f"已保存 {len(nodes)} 个聊天记录节点到模板：{name}"
        if external_ids:
            message += (
                f"\n其中 {len(external_ids)} 个内层转发仍依赖协议端缓存；"
                "发送时会继续尝试展开，失效时请重新引用原消息采集。"
            )
        else:
            message += "\n已尽量展开并本地保存嵌套聊天记录内容。"
        return message

    def _record_external_forward_ids(self, nodes: list[dict[str, Any]]) -> list[str]:
        ret: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                data = value.get("data") if isinstance(value.get("data"), dict) else {}
                has_local_nested_backup = isinstance(data.get("_nested_nodes"), list)
                forward_id = self._forward_id_from_segment(value)
                if forward_id and not has_local_nested_backup and forward_id not in ret:
                    ret.append(forward_id)
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(nodes)
        return ret

    @staticmethod
    def _record_source_meta(nodes: list[dict[str, Any]]) -> tuple[list[str], int]:
        source_ids: list[str] = []
        max_depth = 0

        def visit(value: Any) -> None:
            nonlocal max_depth
            if isinstance(value, dict):
                meta = value.get("_record_meta")
                if isinstance(meta, dict):
                    source_id = str(meta.get("source_forward_id") or "")
                    if source_id and source_id not in source_ids:
                        source_ids.append(source_id)
                    try:
                        max_depth = max(max_depth, int(meta.get("depth") or 0))
                    except (TypeError, ValueError):
                        pass
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(nodes)
        return source_ids, max_depth

    async def _extract_direct_record_source(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, Any] | None:
        raw = getattr(event.message_obj, "raw_message", None)
        if hasattr(raw, "get"):
            reply_id = self._reply_id_from_raw(raw.get("message"))
            if reply_id:
                source: dict[str, Any] = {
                    "source_message_id": str(reply_id),
                    "source_group_id": str(raw.get("group_id") or event.get_group_id() or ""),
                    "source_user_id": str(raw.get("user_id") or event.get_sender_id() or ""),
                    "source_self_id": str(raw.get("self_id") or ""),
                }
                forward_id = await self._forward_id_from_message_id(event, reply_id, raw)
                if forward_id:
                    source["source_forward_id"] = forward_id
                return source

        for component in self._reply_chain(event, include_reply=True):
            if isinstance(component, Reply):
                reply_id = str(component.id or "")
                if reply_id:
                    return {
                        "source_message_id": reply_id,
                        "source_group_id": str(event.get_group_id() or ""),
                        "source_user_id": str(component.sender_id or component.qq or ""),
                        "source_self_id": "",
                    }
        return None

    async def _forward_id_from_message_id(
        self,
        event: AstrMessageEvent,
        message_id: str,
        raw: Any,
    ) -> str:
        bot = getattr(event, "bot", None)
        if bot is None:
            return ""
        try:
            ret = await bot.call_action(
                "get_msg",
                message_id=int(message_id),
                **({"self_id": raw.get("self_id")} if raw.get("self_id") else {}),
            )
        except Exception:
            logger.exception("读取直转引用消息失败。")
            return ""
        return self._forward_id_from_raw(ret.get("message", []))

    async def _probe_direct_record(
        self,
        event: AstrMessageEvent,
        name: str,
        target: str,
    ) -> str:
        record_id = self._find_material_id_by_name("records", name)
        if not record_id:
            return f"没有找到聊天记录：{name}"
        record = self.store.get("records", {}).get(record_id)
        if not isinstance(record, dict) or not self._record_is_direct(record):
            return "该聊天记录不是原消息直转素材。请先回复原始聊天记录执行 /记录 直转 名称。"
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前平台没有 aiocqhttp bot 客户端，无法探测。"
        raw = getattr(event.message_obj, "raw_message", None)
        routing = {}
        if hasattr(raw, "get") and raw.get("self_id"):
            routing["self_id"] = raw.get("self_id")
        direct_routing = self._direct_record_routing(record, routing)
        try:
            results = await self._try_direct_record_strategies(
                bot,
                target,
                record,
                direct_routing,
                str(event.get_group_id() or record.get("source_group_id") or ""),
                stop_on_success=True,
                trusted_only=False,
                confirm_delivery=True,
            )
        except Exception as e:
            results = [{"strategy": "direct_source", "ok": False, "error": str(e)}]
        winner = next((item for item in results if item.get("ok")), None)
        trusted_winner = None
        if winner:
            strategy = winner.get("strategy", "")
            if self._trusted_direct_record_strategy(strategy):
                trusted_winner = winner
                record["last_strategy"] = strategy
                record["updated_at"] = int(time.time())
                self._save()
        lines = [
            f"原消息直转探测：{record.get('name', name)}",
            f"目标 QQ：{target}",
            f"原消息：{record.get('source_message_id')}",
        ]
        for item in results:
            status = "成功" if item.get("ok") else "失败"
            detail = item.get("error") or ""
            lines.append(f"{item.get('strategy')}：{status}{(' - ' + detail) if detail else ''}")
        if trusted_winner:
            lines.append(f"已记录优先策略：{trusted_winner.get('strategy')}")
        elif winner:
            lines.append(
                "该成功策略只是兜底路径，NapCat 可能返回成功但 QQ 显示 0 条；"
                "请以接收端实际展开结果为准，插件不会把它记为优先策略。",
            )
        else:
            refreshed = False
            try:
                refreshed = await self._refresh_direct_record_source(bot, record, direct_routing)
            except Exception as e:
                lines.append(f"原消息缓存刷新失败：{e}")
            if refreshed:
                lines.append("已从 QQ 上下文重新读取原消息信息，可再次执行探测确认直转。")
            if record.get("nodes"):
                try:
                    started_at = int(time.time())
                    await self._send_record_nodes(
                        bot,
                        target,
                        record.get("nodes", []),
                        direct_routing,
                        str(event.get_group_id() or record.get("source_group_id") or ""),
                    )
                    wait_seconds = float(
                        self.store["settings"].get("delivery_confirm_wait_seconds", 8),
                    )
                    await asyncio.sleep(wait_seconds)
                    if not await self._confirm_recent_private_delivery(
                        bot,
                        target,
                        [{"type": "forward"}],
                        started_at,
                        direct_routing,
                    ):
                        raise RuntimeError("本地备份 API 返回成功，但最近私聊历史未确认收到聊天记录")
                    lines.append(f"直转失败，但本地备份 {len(record.get('nodes', []))} 个节点发送成功。")
                except Exception as e:
                    lines.append(f"本地备份发送也失败：{e}")
            else:
                lines.append("没有策略成功，且该直转素材没有本地备份。需要重新引用原始聊天记录再采集。")
        return "\n".join(lines)

    async def _reply_snapshot(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        raw = getattr(event.message_obj, "raw_message", None)
        if hasattr(raw, "get"):
            reply_id = self._reply_id_from_raw(raw.get("message"))
            if reply_id and hasattr(event, "bot"):
                try:
                    ret = await event.bot.call_action(
                        "get_msg",
                        message_id=int(reply_id),
                        **({"self_id": raw.get("self_id")} if raw.get("self_id") else {}),
                    )
                    forward_id = self._forward_id_from_raw(ret.get("message", []))
                    if forward_id:
                        nodes = await self._fetch_forward_nodes(
                            event.bot,
                            forward_id,
                            {"self_id": raw.get("self_id")} if raw.get("self_id") else {},
                        )
                        if nodes:
                            return {"nodes": nodes}
                    sender = ret.get("sender") or {}
                    return {
                        "user_id": sender.get("user_id") or ret.get("user_id"),
                        "nickname": sender.get("card")
                        or sender.get("nickname")
                        or str(sender.get("user_id") or ""),
                        "time": ret.get("time"),
                        "content": self._normalize_raw_segments(ret.get("message", [])),
                    }
                except ForwardExpandError:
                    raise
                except Exception:
                    logger.exception("读取引用消息失败，尝试使用 AstrBot Reply 组件。")

        for component in self._reply_chain(event, include_reply=True):
            if isinstance(component, Reply) and component.chain:
                for seg in self._components_to_segments(component.chain):
                    forward_id = self._forward_id_from_segment(seg)
                    if forward_id:
                        try:
                            nodes = await self._fetch_forward_nodes(
                                event.bot,
                                forward_id,
                                {},
                            )
                            if nodes:
                                return {"nodes": nodes}
                        except ForwardExpandError:
                            raise
                        except Exception:
                            logger.exception("读取引用合并转发消息失败。")
                return {
                    "user_id": component.sender_id or component.qq,
                    "nickname": component.sender_nickname or str(component.sender_id),
                    "time": component.time,
                    "content": self._components_to_segments(component.chain),
                }
        return None

    async def _fetch_forward_nodes(
        self,
        bot: Any,
        forward_id: str,
        routing: dict[str, Any],
        depth: int = 0,
        seen: set[str] | None = None,
        unresolved: list[str] | None = None,
        parent_forward_id: str = "",
    ) -> list[dict[str, Any]]:
        unresolved = unresolved if unresolved is not None else []
        if depth >= FORWARD_EXPAND_MAX_DEPTH:
            logger.warning("合并转发嵌套层级过深，停止展开：%s", forward_id)
            if forward_id not in unresolved:
                unresolved.append(forward_id)
            return []
        seen = seen if seen is not None else set()
        if forward_id in seen:
            logger.warning("合并转发出现循环引用，停止展开：%s", forward_id)
            if forward_id not in unresolved:
                unresolved.append(forward_id)
            return []
        seen.add(forward_id)
        try:
            try:
                ret = await bot.call_action("get_forward_msg", id=forward_id, **routing)
            except Exception:
                logger.exception("读取合并转发消息失败：%s", forward_id)
                if forward_id not in unresolved:
                    unresolved.append(forward_id)
                return []
            messages = self._extract_forward_messages(ret)
            nodes: list[dict[str, Any]] = []
            for item in messages:
                node = self._forward_message_to_node(item)
                if node:
                    self._attach_record_node_meta(
                        node,
                        forward_id,
                        parent_forward_id,
                        depth,
                    )
                    nodes.extend(
                        await self._expand_record_node(
                            bot,
                            node,
                            routing,
                            depth,
                            seen,
                            unresolved,
                            forward_id,
                        ),
                    )
            return nodes
        finally:
            seen.discard(forward_id)

    async def _prepare_record_nodes_for_send(
        self,
        bot: Any,
        nodes: list[dict[str, Any]],
        routing: dict[str, Any],
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        unresolved: list[str] = []
        for node in nodes:
            prepared.extend(
                await self._prepare_record_node_for_send(
                    bot,
                    deepcopy(node),
                    routing,
                    0,
                    seen,
                    unresolved,
                ),
            )
        if not prepared:
            raise ValueError("聊天记录内容为空")
        return prepared

    @staticmethod
    def _attach_record_node_meta(
        node: dict[str, Any],
        forward_id: str,
        parent_forward_id: str,
        depth: int,
    ) -> None:
        node["_record_meta"] = {
            "source_forward_id": forward_id,
            "parent_forward_id": parent_forward_id,
            "depth": depth,
        }

    async def _expand_record_node(
        self,
        bot: Any,
        node: dict[str, Any] | None,
        routing: dict[str, Any],
        depth: int,
        seen: set[str],
        unresolved: list[str],
        current_forward_id: str,
    ) -> list[dict[str, Any]]:
        if not node:
            return []
        normalized = self._normalize_node_data(node)
        if isinstance(node.get("_record_meta"), dict):
            normalized["_record_meta"] = dict(node["_record_meta"])
        content, nested_nodes = await self._expand_forward_segments(
            bot,
            normalized.get("content", []),
            routing,
            depth,
            seen,
            unresolved,
            current_forward_id,
        )
        result: list[dict[str, Any]] = []
        if content:
            normalized["content"] = content
            result.append(normalized)
        return result

    async def _expand_forward_segments(
        self,
        bot: Any,
        segments: list[dict[str, Any]],
        routing: dict[str, Any],
        depth: int,
        seen: set[str],
        unresolved: list[str],
        current_forward_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        content: list[dict[str, Any]] = []
        nested_nodes: list[dict[str, Any]] = []
        for segment in self._normalize_raw_segments(segments):
            forward_id = self._forward_id_from_segment(segment)
            if forward_id:
                content.append({"type": "forward", "data": {"id": forward_id}})
                continue
            content.append(segment)
        return content, nested_nodes

    @staticmethod
    def _extract_forward_messages(ret: Any) -> list[Any]:
        if isinstance(ret, list):
            return ret
        if not isinstance(ret, dict):
            return []
        for key in ("messages", "message", "data"):
            value = ret.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = value.get("messages") or value.get("message")
                if isinstance(nested, list):
                    return nested
        return []

    @staticmethod
    def _embedded_forward_nodes_from_segment(segment: Any) -> list[Any]:
        if not isinstance(segment, dict) or segment.get("type") not in (
            "forward",
            "forward_msg",
            "nodes",
        ):
            return []
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            return []
        for key in ("content", "messages", "message", "nodes"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = value.get("content") or value.get("messages") or value.get("message")
                if isinstance(nested, list):
                    return nested
        return []

    def _forward_message_to_node(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        if item.get("type") == "node" and isinstance(item.get("data"), dict):
            return self._normalize_node_data(item["data"])

        sender = item.get("sender") or {}
        content = item.get("content", item.get("message", []))
        node = {
            "user_id": str(
                item.get("user_id")
                or item.get("uin")
                or sender.get("user_id")
                or sender.get("uin")
                or "0",
            ),
            "nickname": str(
                item.get("nickname")
                or sender.get("card")
                or sender.get("nickname")
                or sender.get("name")
                or "QQ 用户",
            ),
            "content": self._normalize_raw_segments(content),
        }
        return node if node["content"] else None

    def _normalize_node_data(self, data: dict[str, Any]) -> dict[str, Any]:
        content = data.get("content", [])
        return {
            "user_id": str(data.get("user_id") or data.get("uin") or "0"),
            "nickname": str(data.get("nickname") or data.get("name") or "QQ 用户"),
            "content": self._normalize_raw_segments(content),
        }

    async def _raw_reply_segments(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        snapshot = await self._reply_snapshot(event)
        return snapshot.get("content", []) if snapshot else []

    @staticmethod
    def _reply_id_from_raw(message: Any) -> str:
        if not isinstance(message, list):
            return ""
        for segment in message:
            if segment.get("type") == "reply":
                return str(segment.get("data", {}).get("id") or "")
        return ""

    @staticmethod
    def _forward_id_from_raw(message: Any) -> str:
        if not isinstance(message, list):
            return ""
        for segment in message:
            forward_id = WelcomeCustomizationPlugin._forward_id_from_segment(segment)
            if forward_id:
                return forward_id
        return ""

    @staticmethod
    def _forward_id_from_segment(segment: Any) -> str:
        if not isinstance(segment, dict):
            return ""
        if segment.get("type") == "json":
            data = segment.get("data") or {}
            forward_segment = WelcomeCustomizationPlugin._json_card_to_forward_segment(
                data.get("data") if isinstance(data, dict) and "data" in data else data,
            )
            if forward_segment:
                return WelcomeCustomizationPlugin._forward_id_from_segment(forward_segment)
            return ""
        if segment.get("type") != "forward":
            return ""
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            return ""
        direct = WelcomeCustomizationPlugin._valid_forward_id_candidate(
            data.get("id")
            or data.get("res_id")
            or data.get("resId")
            or data.get("forward_id")
            or data.get("forwardId")
            or data.get("file")
            or "",
        )
        if direct:
            return direct
        nested = WelcomeCustomizationPlugin._extract_forward_id_from_json_card(data)
        if nested:
            return nested
        return ""

    @staticmethod
    def _json_card_to_forward_segment(value: Any) -> dict[str, Any] | None:
        data = WelcomeCustomizationPlugin._parse_json_card_payload(value)
        if not WelcomeCustomizationPlugin._looks_like_forward_json_card(data):
            return None
        forward_id = WelcomeCustomizationPlugin._extract_forward_id_from_json_card(data)
        if not forward_id:
            return None
        return {"type": "forward", "data": {"id": forward_id}}

    @staticmethod
    def _parse_json_card_payload(value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("{", "[")):
                try:
                    return WelcomeCustomizationPlugin._parse_json_card_payload(
                        json.loads(text),
                        depth + 1,
                    )
                except Exception:
                    return value
            return value
        if isinstance(value, dict):
            return {
                key: WelcomeCustomizationPlugin._parse_json_card_payload(item, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                WelcomeCustomizationPlugin._parse_json_card_payload(item, depth + 1)
                for item in value
            ]
        return value

    @staticmethod
    def _looks_like_forward_json_card(data: Any) -> bool:
        haystack = "\n".join(
            str(item).lower()
            for item in WelcomeCustomizationPlugin._walk_json_values(data)
            if isinstance(item, str)
        )
        resource_keys = {
            "mresid",
            "resid",
            "forwardid",
            "forwardmsgid",
            "multimsgid",
        }

        def has_forward_resource_key(value: Any) -> bool:
            if isinstance(value, dict):
                for item_key, item_value in value.items():
                    normalized_key = re.sub(r"[^a-z0-9]", "", str(item_key).lower())
                    if normalized_key in resource_keys:
                        return True
                    if has_forward_resource_key(item_value):
                        return True
            if isinstance(value, list):
                return any(has_forward_resource_key(item) for item in value)
            return False

        if not has_forward_resource_key(data):
            return False
        strong_hints = (
            "com.tencent.multimsg",
            "multimsg",
            "multi_msg",
        )
        if any(hint.lower() in haystack for hint in strong_hints):
            return True
        text_hints = (
            "[聊天记录]",
            "聊天记录",
            "群聊的聊天记录",
            "转发消息",
        )
        return any(hint.lower() in haystack for hint in text_hints)

    @staticmethod
    def _extract_forward_id_from_json_card(data: Any) -> str:
        preferred_keys = {
            "mresid",
            "resid",
            "forwardid",
            "forwardmsgid",
            "multimsgid",
        }
        candidates: list[tuple[int, str]] = []

        def normalize_key(value: Any) -> str:
            return re.sub(r"[^a-z0-9]", "", str(value).lower())

        def rank_key(value: Any) -> int:
            key = normalize_key(value)
            if key in {"mresid", "resid"}:
                return 0
            if key in {"forwardid", "forwardmsgid", "multimsgid"}:
                return 1
            return 9

        def visit(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for item_key, item_value in value.items():
                    if normalize_key(item_key) in preferred_keys:
                        candidate = WelcomeCustomizationPlugin._valid_forward_id_candidate(item_value)
                        if candidate:
                            candidates.append((rank_key(item_key), candidate))
                    visit(item_value, str(item_key))
            elif isinstance(value, list):
                for item in value:
                    visit(item, key)

        visit(data)
        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    @staticmethod
    def _valid_forward_id_candidate(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        candidate = value.strip()
        if len(candidate) < 12 or len(candidate) > 512:
            return ""
        lowered = candidate.lower()
        if lowered.startswith(("http://", "https://", "{", "[")):
            return ""
        if re.search(r"[\u4e00-\u9fff]", candidate):
            return ""
        if re.search(r"\s", candidate):
            return ""
        return candidate

    @staticmethod
    def _walk_json_values(data: Any) -> list[Any]:
        values: list[Any] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for item_key, item_value in value.items():
                    values.append(str(item_key))
                    visit(item_value)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            else:
                values.append(value)

        visit(data)
        return values

    @staticmethod
    def _reply_chain(
        event: AstrMessageEvent,
        include_reply: bool = False,
    ) -> list[Any]:
        components = getattr(event.message_obj, "message", []) or []
        ret: list[Any] = []
        for component in components:
            if isinstance(component, Reply):
                if include_reply:
                    ret.append(component)
                if component.chain:
                    ret.extend(component.chain)
        return ret

    def _normalize_raw_segments(self, segments: Any) -> list[dict[str, Any]]:
        if not isinstance(segments, list):
            return [{"type": "text", "data": {"text": str(segments)}}]
        ret = []
        for segment in segments:
            if not isinstance(segment, dict) or "type" not in segment:
                continue
            if segment.get("type") == "reply":
                continue
            data = segment.get("data") or {}
            if segment.get("type") == "json":
                if not isinstance(data.get("data"), str):
                    data = dict(data)
                    data["data"] = self._json_segment_to_raw(data.get("data"))
            ret.append({"type": segment["type"], "data": data})
        return ret

    def _components_to_segments(self, components: list[Any]) -> list[dict[str, Any]]:
        ret = []
        for component in components:
            if isinstance(component, Plain):
                ret.append({"type": "text", "data": {"text": component.text}})
            elif isinstance(component, Image):
                ret.append(
                    {
                        "type": "image",
                        "data": {"file": component.url or component.file or ""},
                    },
                )
            elif isinstance(component, Json):
                ret.append(
                    {
                        "type": "json",
                        "data": {"data": self._json_segment_to_raw(component.data)},
                    },
                )
            elif isinstance(component, Forward):
                ret.append({"type": "forward", "data": {"id": str(component.id)}})
            elif isinstance(component, At):
                ret.append({"type": "at", "data": {"qq": str(component.qq)}})
            elif isinstance(component, Face):
                ret.append({"type": "face", "data": {"id": str(component.id)}})
            else:
                try:
                    ret.append(component.toDict())
                except Exception:
                    ret.append({"type": "text", "data": {"text": str(component)}})
        return ret

    def _summarize_card(self, raw_json: str) -> dict[str, str]:
        try:
            data = json.loads(raw_json)
        except Exception:
            return {"title": "QQ JSON 卡片", "desc": "", "url": "", "image": ""}
        strings = self._collect_json_strings(data)
        title = self._first_matching(strings, ("title", "name", "prompt")) or "QQ JSON 卡片"
        desc = self._first_matching(strings, ("desc", "summary", "text")) or ""
        url = next((v for k, v in strings if k in {"url", "jumpUrl", "jump_url"}), "")
        image = next(
            (v for k, v in strings if k in {"preview", "cover", "image", "pic", "icon"}),
            "",
        )
        return {"title": title[:80], "desc": desc[:160], "url": url, "image": image}

    def _collect_json_strings(self, data: Any, prefix: str = "") -> list[tuple[str, str]]:
        ret: list[tuple[str, str]] = []
        if isinstance(data, dict):
            for key, value in data.items():
                ret.extend(self._collect_json_strings(value, str(key)))
        elif isinstance(data, list):
            for item in data:
                ret.extend(self._collect_json_strings(item, prefix))
        elif isinstance(data, str) and data.strip():
            ret.append((prefix, data.strip()))
        return ret

    @staticmethod
    def _first_matching(strings: list[tuple[str, str]], keys: tuple[str, ...]) -> str:
        for key, value in strings:
            if key in keys:
                return value
        return ""

    async def api_state(self):
        return json_response(
            {
                "settings": self.store["settings"],
                "cards": list(self.store.get("cards", {}).values()),
                "records": list(self.store.get("records", {}).values()),
                "images": list(self.store.get("images", {}).values()),
                "logs": list(reversed(self.store.get("logs", [])[-100:])),
                "queue_size": self.queue.qsize(),
                "worker_running": bool(self.worker_task and not self.worker_task.done()),
                "astrbot": {
                    "max_agent_step": self._get_max_agent_step(),
                },
            },
        )

    async def api_save_settings(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid json body", status_code=400)
        incoming = payload.get("settings", payload)
        if not isinstance(incoming, dict):
            return error_response("settings must be an object", status_code=400)
        for key in DEFAULT_SETTINGS:
            if key in incoming:
                self.store["settings"][key] = incoming[key]
        self._save()
        return json_response({"saved": True, "settings": self.store["settings"]})

    async def api_activate(self):
        payload = await request.json(default={})
        kind = payload.get("kind")
        item_id = str(payload.get("id", ""))
        mapping = {
            "card": ("cards", "active_card_id"),
            "record": ("records", "active_record_id"),
            "image": ("images", "active_image_id"),
        }
        if kind not in mapping:
            return error_response("invalid kind", status_code=400)
        collection, active_key = mapping[kind]
        if item_id not in self.store.get(collection, {}):
            return error_response("item not found", status_code=404)
        self.store["settings"][active_key] = item_id
        self._save()
        return json_response({"activated": True})

    async def api_delete(self):
        payload = await request.json(default={})
        kind = payload.get("kind")
        item_id = str(payload.get("id", ""))
        mapping = {
            "card": ("cards", "active_card_id"),
            "record": ("records", "active_record_id"),
            "image": ("images", "active_image_id"),
        }
        if kind not in mapping:
            return error_response("invalid kind", status_code=400)
        collection, active_key = mapping[kind]
        item = self.store.get(collection, {}).pop(item_id, None)
        if not item:
            return error_response("item not found", status_code=404)
        if self.store["settings"].get(active_key) == item_id:
            self.store["settings"][active_key] = ""
        if kind == "image" and item.get("kind") == "local":
            try:
                Path(item["source"]).unlink(missing_ok=True)
            except Exception:
                logger.exception("删除本地图片文件失败。")
        self._save()
        return json_response({"deleted": True})

    async def api_rename(self):
        payload = await request.json(default={})
        kind = payload.get("kind")
        item_id = str(payload.get("id", ""))
        name = str(payload.get("name", "")).strip()
        mapping = {"card": "cards", "record": "records", "image": "images"}
        if kind not in mapping or not name:
            return error_response("invalid request", status_code=400)
        item = self.store.get(mapping[kind], {}).get(item_id)
        if not item:
            return error_response("item not found", status_code=404)
        item["name"] = name
        item["updated_at"] = int(time.time())
        self._save()
        return json_response({"renamed": True})

    async def api_test(self):
        payload = await request.json(default={})
        target = str(payload.get("qq") or self.store["settings"].get("test_receiver_qq") or "")
        if not target:
            return error_response("missing qq", status_code=400)
        bot = self._get_aiocqhttp_bot()
        if bot is None:
            return error_response("aiocqhttp platform is not online", status_code=503)
        summary = await self._send_current_config_to_target(bot, target, {})
        failed = summary["failed"]
        if failed:
            return error_response(
                "；".join(item["error"] for item in failed),
                status_code=500,
            )
        return json_response(
            {
                "sent": summary["sent_count"] > 0,
                "sent_count": summary["sent_count"],
                "skipped_count": summary["skipped_count"],
                "results": summary["results"],
            },
        )

    async def api_upload_image(self):
        files = await request.files()
        upload: PluginUploadFile | None = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("missing file", status_code=400)
        suffix = Path(upload.filename or "image.png").suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            return error_response("unsupported image type", status_code=400)
        image_id = uuid.uuid4().hex
        target = self.image_dir / f"{image_id}{suffix}"
        await upload.save(target)
        item = {
            "id": image_id,
            "name": Path(upload.filename or image_id).stem,
            "kind": "local",
            "source": str(target.resolve()),
            "created_at": int(time.time()),
        }
        self.store["images"][image_id] = item
        self.store["settings"]["active_image_id"] = image_id
        self._save()
        return json_response({"image": item})

    async def api_clear_logs(self):
        self.store["logs"] = []
        self._save()
        return json_response({"cleared": True})

    async def api_save_max_agent_step(self):
        payload = await request.json(default={})
        value = self._bounded_int(payload.get("max_agent_step"), 1, 200, 30)
        cfg = self.context.get_config()
        try:
            provider_settings = dict(cfg.get("provider_settings", {}) or {})
            provider_settings["max_agent_step"] = value
            cfg["provider_settings"] = provider_settings
        except Exception:
            return error_response("current AstrBot config cannot be updated", status_code=500)
        save_config = getattr(cfg, "save_config", None)
        if not callable(save_config):
            return error_response("current AstrBot config cannot be saved", status_code=500)
        try:
            save_config()
        except Exception:
            logger.exception("AstrBot 工具调用轮数上限保存失败。")
            return error_response("current AstrBot config save failed", status_code=500)
        return json_response({"saved": True, "max_agent_step": value})

    def _get_max_agent_step(self) -> int:
        try:
            cfg = self.context.get_config()
            return int(cfg.get("provider_settings", {}).get("max_agent_step", 30))
        except Exception:
            return 30

    def _get_aiocqhttp_bot(self) -> Any | None:
        get_platform = getattr(self.context, "get_platform", None)
        if callable(get_platform):
            try:
                platform = get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                get_client = getattr(platform, "get_client", None)
                if callable(get_client):
                    return get_client()
            except Exception:
                pass

        platform_manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(platform_manager, "platform_insts", []) or []
        for platform in platforms:
            meta = platform.meta() if hasattr(platform, "meta") else None
            if getattr(meta, "name", "") == "aiocqhttp":
                get_client = getattr(platform, "get_client", None)
                if callable(get_client):
                    return get_client()
        return None
