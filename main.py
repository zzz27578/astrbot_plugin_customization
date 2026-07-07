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
from astrbot.api.message_components import At, Face, Image, Json, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.api.web import PluginUploadFile, error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_customization"
STORE_VERSION = 1


DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "mode": "all",
    "whitelist_groups": [],
    "blacklist_groups": [],
    "send_order": ["card", "record", "image"],
    "send_interval_seconds": 1.5,
    "retry_enabled": True,
    "retry_count": 1,
    "retry_interval_seconds": 5.0,
    "dedupe_enabled": True,
    "dedupe_minutes": 1440,
    "notify_admin_private": False,
    "notify_admin_group": False,
    "admin_qq_list": [],
    "notify_group_id": "",
    "notify_on_success": False,
    "group_fallback_enabled": False,
    "group_fallback_mode": "all_failed",
    "group_fallback_at": True,
    "group_fallback_template": "欢迎加入，请检查机器人私聊或查看群公告。",
    "card_fallback_enabled": False,
    "card_fallback_text": "",
    "record_fallback_enabled": False,
    "record_fallback_text": "",
    "image_fallback_enabled": False,
    "image_fallback_text": "",
    "active_card_id": "",
    "active_record_id": "",
    "active_image_id": "",
    "test_receiver_qq": "",
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
        self.worker_task: asyncio.Task | None = None

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

    async def initialize(self) -> None:
        self._ensure_worker()

    async def terminate(self) -> None:
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            try:
                await self.worker_task
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
            if normalized["group_fallback_mode"] in {"all_failed", "any_failed"}
            else "all_failed"
        )
        for key in ("whitelist_groups", "blacklist_groups", "admin_qq_list"):
            normalized[key] = self._normalize_id_list(normalized.get(key, []))
        normalized["send_order"] = [
            item
            for item in normalized.get("send_order", [])
            if item in {"card", "record", "image"}
        ] or ["card", "record", "image"]
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
                self.queue.task_done()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_event(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", None)
        if not self._is_group_increase(raw):
            return

        group_id = str(raw.get("group_id", "")).strip()
        user_id = str(raw.get("user_id", "")).strip()
        self_id = str(raw.get("self_id", "")).strip()
        if not group_id or not user_id or user_id == self_id:
            return
        if not self._group_enabled(group_id):
            return
        if self._is_deduped(group_id, user_id):
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

        self._mark_dedupe(group_id, user_id)
        self._ensure_worker()
        await self.queue.put(
            {
                "bot": bot,
                "group_id": group_id,
                "user_id": user_id,
                "self_id": self_id,
                "source": "group_increase",
            },
        )

    @filter.command("欢迎", alias={"welcome"})
    async def welcome_command(self, event: AstrMessageEvent):
        if not self._is_operator(event):
            return event.plain_result("没有权限使用欢迎私聊管理命令。")

        args = self._command_args(event)
        if not args or args[0] in {"帮助", "help", "?"}:
            return event.plain_result(self._help_text())

        try:
            text = await self._dispatch_command(event, args)
        except Exception as e:
            logger.exception("欢迎私聊命令执行失败。")
            text = f"执行失败：{e}"
        return event.plain_result(text)

    async def _dispatch_command(self, event: AstrMessageEvent, args: list[str]) -> str:
        cmd = args[0]
        if cmd == "状态":
            return self._status_text()
        if cmd == "测试":
            target = args[1] if len(args) > 1 else event.get_sender_id()
            return await self._test_send(event, target)
        if cmd == "启用":
            return self._enable_group(event, args[1] if len(args) > 1 else "当前群")
        if cmd == "禁用":
            return self._disable_group(event, args[1] if len(args) > 1 else "当前群")
        if cmd == "模式" and len(args) > 1:
            return self._set_mode(args[1])
        if cmd == "卡片":
            return await self._card_command(event, args[1:])
        if cmd == "记录":
            return await self._record_command(event, args[1:])
        if cmd == "图片":
            return await self._image_command(event, args[1:])
        if cmd == "管理员":
            return self._admin_command(args[1:])
        if cmd == "通知":
            return self._toggle_command("notify_admin_private", args[1:])
        if cmd == "群内兜底":
            return self._toggle_command("group_fallback_enabled", args[1:])
        return "未知命令。发送 /欢迎 帮助 查看内置指令。"

    def _command_args(self, event: AstrMessageEvent) -> list[str]:
        text = re.sub(r"\s+", " ", event.get_message_str().strip())
        text = re.sub(r"^[/!！。.]?欢迎\b", "", text, count=1).strip()
        if not text:
            return []
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
        return (
            hasattr(raw, "get")
            and raw.get("post_type") == "notice"
            and raw.get("notice_type") == "group_increase"
        )

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
        key = f"{group_id}:{user_id}"
        last = float(self.store.get("dedupe", {}).get(key, 0))
        return time.time() - last < minutes * 60

    def _mark_dedupe(self, group_id: str, user_id: str) -> None:
        self.store.setdefault("dedupe", {})[f"{group_id}:{user_id}"] = time.time()
        self._save()

    async def _process_join_job(self, job: dict[str, Any]) -> None:
        bot = job["bot"]
        group_id = str(job["group_id"])
        user_id = str(job["user_id"])
        self_id = str(job.get("self_id", ""))
        routing = {"self_id": self_id} if self_id else {}
        step_results: list[dict[str, Any]] = []

        for step in self.store["settings"].get("send_order", []):
            result = await self._send_step_with_retry(bot, user_id, step, routing)
            step_results.append(result)
            await asyncio.sleep(float(self.store["settings"]["send_interval_seconds"]))

        ok = all(item["ok"] for item in step_results)
        failed_steps = [item for item in step_results if not item["ok"]]
        self._append_log(
            "success" if ok else "failed",
            group_id,
            user_id,
            ",".join(item["step"] for item in failed_steps) or "all",
            "" if ok else "；".join(item["error"] for item in failed_steps),
            step_results,
        )

        if ok:
            if self.store["settings"].get("notify_on_success"):
                await self._notify_admins(
                    bot,
                    f"欢迎私聊发送成功\n群号：{group_id}\n新人：{user_id}",
                    routing,
                )
            return

        await self._run_fallbacks(bot, group_id, user_id, routing, failed_steps)

    async def _send_step_with_retry(
        self,
        bot: Any,
        user_id: str,
        step: str,
        routing: dict[str, Any],
    ) -> dict[str, Any]:
        attempts = 1
        if self.store["settings"].get("retry_enabled", True):
            attempts += int(self.store["settings"].get("retry_count", 0))
        last_error = ""
        for index in range(attempts):
            try:
                await self._send_step(bot, user_id, step, routing)
                return {"step": step, "ok": True, "attempts": index + 1, "error": ""}
            except Exception as e:
                last_error = str(e)
                if index + 1 < attempts:
                    await asyncio.sleep(
                        float(self.store["settings"].get("retry_interval_seconds", 5)),
                    )
        return {
            "step": step,
            "ok": False,
            "attempts": attempts,
            "error": f"{step}: {last_error}",
        }

    async def _send_step(
        self,
        bot: Any,
        user_id: str,
        step: str,
        routing: dict[str, Any],
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
            )
            return
        if step == "record":
            record = self._active_item("records", "active_record_id")
            if not record or not record.get("nodes"):
                raise ValueError("未设置启用聊天记录")
            await bot.call_action(
                "send_private_forward_msg",
                user_id=int(user_id),
                messages=[
                    {"type": "node", "data": node} for node in record.get("nodes", [])
                ],
                **routing,
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
            )
            return
        raise ValueError(f"未知发送步骤：{step}")

    async def _send_private_payload(
        self,
        bot: Any,
        user_id: str,
        payload: list[dict[str, Any]],
        routing: dict[str, Any],
    ) -> None:
        await bot.call_action(
            "send_private_msg",
            user_id=int(user_id),
            message=payload,
            **routing,
        )

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
            should_send = mode == "any_failed" or len(failed_steps) >= len(
                settings.get("send_order", []),
            )
            if should_send:
                await self._send_group_fallback(bot, group_id, user_id, routing)

    async def _notify_admins(
        self,
        bot: Any,
        message: str,
        routing: dict[str, Any],
    ) -> None:
        settings = self.store["settings"]
        if settings.get("notify_admin_private"):
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
            return "用法：/欢迎 卡片 添加 名称；/欢迎 卡片 使用 名称；/欢迎 卡片 列表；/欢迎 卡片 删除 名称"
        action = args[0]
        if action == "添加":
            name = args[1] if len(args) > 1 else f"卡片{len(self.store['cards']) + 1}"
            raw_json = await self._extract_card_json(event)
            if not raw_json:
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
            return "用法：/欢迎 记录 添加 名称；/欢迎 记录 使用 名称；/欢迎 记录 列表；/欢迎 记录 删除 名称"
        action = args[0]
        if action == "添加":
            name = args[1] if len(args) > 1 else f"聊天记录{len(self.store['records']) + 1}"
            node = await self._extract_record_node(event)
            if not node:
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
            self.store["records"][record_id]["nodes"].append(node)
            self.store["records"][record_id]["updated_at"] = int(time.time())
            self.store["settings"]["active_record_id"] = record_id
            self._save()
            return f"已保存聊天记录节点到模板：{name}"
        return self._material_command("records", "active_record_id", "聊天记录", args)

    async def _image_command(self, event: AstrMessageEvent, args: list[str]) -> str:
        if not args:
            return "用法：/欢迎 图片 添加 名称；/欢迎 图片 使用 名称；/欢迎 图片 列表；/欢迎 图片 删除 名称"
        action = args[0]
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
        action = args[0]
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
        if len(args) < 2 or args[0] not in {"添加", "删除"}:
            return "用法：/欢迎 管理员 添加 QQ；/欢迎 管理员 删除 QQ"
        admins = self.store["settings"]["admin_qq_list"]
        qq = str(args[1]).strip()
        if args[0] == "添加" and qq not in admins:
            admins.append(qq)
        if args[0] == "删除" and qq in admins:
            admins.remove(qq)
        self._save()
        return f"管理员 QQ 列表：{', '.join(admins) or '空'}"

    def _toggle_command(self, key: str, args: list[str]) -> str:
        if not args or args[0] not in {"开", "关", "on", "off"}:
            return "用法：开 / 关"
        value = args[0] in {"开", "on"}
        self.store["settings"][key] = value
        self._save()
        return f"{key} 已{'开启' if value else '关闭'}。"

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
        for step in self.store["settings"]["send_order"]:
            await self._send_step(bot, target, step, routing)
            await asyncio.sleep(float(self.store["settings"]["send_interval_seconds"]))
        return f"已向 {target} 发送测试欢迎私聊。"

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
            "/欢迎 状态\n"
            "/欢迎 测试 [QQ]\n"
            "/欢迎 模式 all|whitelist|disabled\n"
            "/欢迎 启用 当前群|群号\n"
            "/欢迎 禁用 当前群|群号\n"
            "/欢迎 卡片 添加 名称（回复 QQ 卡片）\n"
            "/欢迎 卡片 使用/列表/删除 名称\n"
            "/欢迎 记录 添加 名称（回复消息，追加为合并转发节点）\n"
            "/欢迎 记录 使用/列表/删除 名称\n"
            "/欢迎 图片 添加 名称（回复图片）\n"
            "/欢迎 图片 使用/列表/删除 名称\n"
            "/欢迎 管理员 添加/删除 QQ\n"
            "/欢迎 通知 开|关\n"
            "/欢迎 群内兜底 开|关"
        )

    async def _extract_card_json(self, event: AstrMessageEvent) -> str:
        raw_segments = await self._raw_reply_segments(event)
        for segment in raw_segments:
            if segment.get("type") == "json":
                return self._json_segment_to_raw(segment.get("data", {}).get("data"))

        for component in self._reply_chain(event):
            if isinstance(component, Json):
                return self._json_segment_to_raw(component.data)
        return ""

    @staticmethod
    def _json_segment_to_raw(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("data"), str):
            return value["data"]
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

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

    async def _extract_record_node(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        snapshot = await self._reply_snapshot(event)
        if not snapshot:
            return None
        content = snapshot.get("content", [])
        if not content:
            return None
        return {
            "user_id": str(snapshot.get("user_id") or event.get_sender_id()),
            "nickname": str(snapshot.get("nickname") or "QQ 用户"),
            "content": content,
        }

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
                    sender = ret.get("sender") or {}
                    return {
                        "user_id": sender.get("user_id") or ret.get("user_id"),
                        "nickname": sender.get("card")
                        or sender.get("nickname")
                        or str(sender.get("user_id") or ""),
                        "time": ret.get("time"),
                        "content": self._normalize_raw_segments(ret.get("message", [])),
                    }
                except Exception:
                    logger.exception("读取引用消息失败，尝试使用 AstrBot Reply 组件。")

        for component in self._reply_chain(event, include_reply=True):
            if isinstance(component, Reply) and component.chain:
                return {
                    "user_id": component.sender_id or component.qq,
                    "nickname": component.sender_nickname or str(component.sender_id),
                    "time": component.time,
                    "content": self._components_to_segments(component.chain),
                }
        return None

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
            if segment.get("type") == "json" and not isinstance(data.get("data"), str):
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
        for step in self.store["settings"]["send_order"]:
            await self._send_step(bot, target, step, {})
            await asyncio.sleep(float(self.store["settings"]["send_interval_seconds"]))
        return json_response({"sent": True})

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

    def _get_aiocqhttp_bot(self) -> Any | None:
        platform_manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(platform_manager, "platform_insts", []) or []
        for platform in platforms:
            meta = platform.meta() if hasattr(platform, "meta") else None
            if getattr(meta, "name", "") == "aiocqhttp":
                get_client = getattr(platform, "get_client", None)
                if callable(get_client):
                    return get_client()
        return None
