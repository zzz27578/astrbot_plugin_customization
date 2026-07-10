from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any


def _install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_module = types.ModuleType("astrbot.core.utils.astrbot_path")

    class Logger:
        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: None

    class Filter:
        class EventMessageType:
            ALL = "all"

        class PlatformAdapterType:
            AIOCQHTTP = "aiocqhttp"

        @staticmethod
        def command(*_args, **_kwargs):
            return lambda function: function

        @staticmethod
        def event_message_type(*_args, **_kwargs):
            return lambda function: function

    class Component:
        pass

    class Star:
        def __init__(self, context: Any) -> None:
            self.context = context

    class Context:
        pass

    class PluginUploadFile:
        pass

    api.logger = Logger()
    event.AstrMessageEvent = object
    event.filter = Filter
    for name in ("At", "Face", "Forward", "Image", "Json", "Plain", "Reply"):
        setattr(components, name, type(name, (Component,), {}))
    star.Context = Context
    star.Star = Star
    web.PluginUploadFile = PluginUploadFile
    web.error_response = lambda *args, **kwargs: (args, kwargs)
    web.json_response = lambda value: value
    web.request = types.SimpleNamespace()
    path_module.get_astrbot_plugin_data_path = lambda: "."

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
        "astrbot.api.web": web,
        "astrbot.core": core,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": path_module,
    }
    sys.modules.update(modules)


class FakeNapCat:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        self.calls.append((action, params))
        if action == "get_forward_msg":
            return {
                "messages": [
                    {
                        "sender": {"user_id": 10001, "nickname": "甲"},
                        "message": [{"type": "text", "data": {"text": "第一条"}}],
                    },
                    {
                        "sender": {"user_id": 10002, "nickname": "乙"},
                        "message": [{"type": "image", "data": {"file": "https://example/image.jpg"}}],
                    },
                ],
            }
        if action == "send_private_forward_msg":
            return {"status": "ok", "retcode": 0, "message_id": 123}
        if action in {"send_private_msg", "send_msg"}:
            raise RuntimeError("发送转发消息（res_id：expired-res-id）失败")
        raise AssertionError(f"unexpected NapCat action: {action}")


class NestedNapCat(FakeNapCat):
    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        self.calls.append((action, params))
        if action == "get_forward_msg" and params["id"] == "outer-forward-id":
            return {
                "messages": [
                    {
                        "sender": {"user_id": 10001, "nickname": "甲"},
                        "message": [
                            {
                                "type": "forward",
                                "data": {"id": "inner-forward-id"},
                            },
                        ],
                    },
                ],
            }
        if action == "get_forward_msg" and params["id"] == "inner-forward-id":
            return {
                "messages": [
                    {
                        "sender": {"user_id": 10002, "nickname": "乙"},
                        "message": [{"type": "text", "data": {"text": "内层"}}],
                    },
                ],
            }
        if action == "send_private_forward_msg":
            return {"status": "ok", "retcode": 0, "message_id": 456}
        raise AssertionError(f"unexpected NapCat action: {action}")


class RecordDeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _install_astrbot_stubs()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        cls.module = importlib.import_module("main")

    def test_rebuilds_forward_from_nodes_when_saved_res_id_cannot_send(self) -> None:
        plugin = self.module.WelcomeCustomizationPlugin.__new__(
            self.module.WelcomeCustomizationPlugin,
        )
        plugin.store = {
            "settings": {"delivery_confirm_wait_seconds": 0},
            "records": {},
        }
        plugin._save = lambda: None
        bot = FakeNapCat()
        record = {
            "id": "record-1",
            "name": "聊天记录1",
            "mode": "forward_resource",
            "record_forward_id": "expired-res-id",
        }

        asyncio.run(plugin._send_added_record(bot, "397605468", record, {}, "765863601"))

        forward_calls = [
            params for action, params in bot.calls if action == "send_private_forward_msg"
        ]
        self.assertEqual(len(forward_calls), 1)
        self.assertEqual(
            forward_calls[0]["messages"][0]["data"]["content"][0]["data"]["text"],
            "第一条",
        )
        self.assertEqual(record["last_strategy"], "rebuilt_nodes")
        self.assertEqual(len(record["nodes"]), 2)

    def test_uses_cached_nodes_without_retrying_expired_res_id(self) -> None:
        plugin = self.module.WelcomeCustomizationPlugin.__new__(
            self.module.WelcomeCustomizationPlugin,
        )
        plugin.store = {"settings": {}, "records": {}}
        plugin._save = lambda: None
        bot = FakeNapCat()
        record = {
            "id": "record-2",
            "name": "聊天记录2",
            "mode": "forward_resource",
            "record_forward_id": "expired-res-id",
            "nodes": [
                {
                    "user_id": "10001",
                    "nickname": "甲",
                    "content": [{"type": "text", "data": {"text": "已缓存"}}],
                },
            ],
        }

        asyncio.run(plugin._send_added_record(bot, "397605468", record, {}, None))

        self.assertEqual([action for action, _params in bot.calls], ["send_private_forward_msg"])
        self.assertEqual(record["last_strategy"], "rebuilt_nodes")

    def test_rebuilds_nested_forward_as_nested_node_content(self) -> None:
        plugin = self.module.WelcomeCustomizationPlugin.__new__(
            self.module.WelcomeCustomizationPlugin,
        )
        plugin.store = {"settings": {}, "records": {}}
        plugin._save = lambda: None
        bot = NestedNapCat()

        nodes = asyncio.run(
            plugin._record_nodes_from_forward(bot, "outer-forward-id", {}),
        )
        asyncio.run(plugin._send_record_nodes(bot, "397605468", nodes, {}))

        send_params = bot.calls[-1][1]
        nested_content = send_params["messages"][0]["data"]["content"]
        self.assertEqual(nested_content[0]["type"], "node")
        self.assertEqual(
            nested_content[0]["data"]["content"][0]["data"]["text"],
            "内层",
        )

    def test_direct_record_migrates_expired_forward_resource(self) -> None:
        plugin = self.module.WelcomeCustomizationPlugin.__new__(
            self.module.WelcomeCustomizationPlugin,
        )
        plugin.store = {"settings": {}, "records": {}}
        plugin._save = lambda: None
        bot = FakeNapCat()
        record = {
            "id": "record-3",
            "name": "直转聊天记录",
            "mode": "direct_forward",
            "record_forward_id": "expired-res-id",
        }

        asyncio.run(plugin._send_direct_record(bot, "397605468", record, {}, None))

        self.assertEqual(bot.calls[-1][0], "send_private_forward_msg")
        self.assertEqual(record["last_strategy"], "rebuilt_nodes")
        self.assertEqual(len(record["nodes"]), 2)


if __name__ == "__main__":
    unittest.main()
