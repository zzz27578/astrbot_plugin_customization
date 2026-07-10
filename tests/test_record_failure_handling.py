from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.test_record_delivery import _install_astrbot_stubs


class RecordFailureHandlingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _install_astrbot_stubs()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        cls.module = importlib.import_module("main")

    def _plugin(self, settings: dict[str, Any] | None = None):
        plugin = self.module.WelcomeCustomizationPlugin.__new__(
            self.module.WelcomeCustomizationPlugin,
        )
        plugin.store = {
            "settings": settings or {},
            "records": {},
        }
        plugin._save = lambda: None
        return plugin

    def test_forward_capture_failure_returns_no_source(self) -> None:
        # Given a reply whose root forward id exists but whose nodes are unreadable.
        plugin = self._plugin()

        async def extract_direct(_event: Any) -> dict[str, str]:
            return {
                "source_forward_id": "expired-inner-forward",
                "source_message_id": "123",
                "source_group_id": "456",
                "source_user_id": "789",
                "source_self_id": "10000",
            }

        async def load_nodes(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("消息已过期或者为内层消息，无法获取转发消息")

        plugin._extract_direct_record_source = extract_direct
        plugin._record_nodes_from_forward = load_nodes
        event = SimpleNamespace(bot=SimpleNamespace())

        # When the plugin captures the forward record.
        source = asyncio.run(plugin._extract_forward_record_source(event))

        # Then the failed resource is rejected instead of becoming saveable data.
        self.assertIsNone(source)

    def test_record_add_failure_does_not_save_or_replace_active_record(self) -> None:
        # Given an existing active record and a new capture that failed.
        plugin = self._plugin({"active_record_id": "existing-record"})
        plugin.store["records"]["existing-record"] = {
            "id": "existing-record",
            "name": "现有记录",
            "record_forward_id": "working-forward",
        }

        async def capture_failed(_event: Any) -> None:
            return None

        plugin._extract_forward_record_source = capture_failed

        # When `/记录 添加` handles the failed capture.
        message = asyncio.run(
            plugin._record_command(SimpleNamespace(), ["添加", "失败记录"]),
        )

        # Then no false success is reported and the active record is untouched.
        self.assertNotIn("已保存并启用", message)
        self.assertEqual(plugin.store["settings"]["active_record_id"], "existing-record")
        self.assertEqual(list(plugin.store["records"]), ["existing-record"])

    def test_temp_session_ambiguous_timeout_is_unverifiable_without_compensation(self) -> None:
        # Given an ambiguous send timeout in a non-friend group temporary session.
        plugin = self._plugin(
            {
                "delivery_confirm_wait_seconds": 0,
                "delivery_compensation_count": 1,
                "delivery_compensation_interval_seconds": 0,
            },
        )
        compensation_calls = 0

        async def cannot_confirm(*_args: Any, **_kwargs: Any) -> bool:
            return False

        async def compensate(*_args: Any, **_kwargs: Any) -> None:
            nonlocal compensation_calls
            compensation_calls += 1

        plugin._confirm_recent_private_delivery = cannot_confirm
        plugin._send_step_or_fallback = compensate

        # When ambiguous-timeout handling knows the source group context.
        result = asyncio.run(
            plugin._handle_ambiguous_timeout(
                SimpleNamespace(),
                "3032158374",
                "record",
                {},
                "765863601",
                True,
                False,
                "",
                [{"type": "forward"}],
                1,
                1,
                'Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg '
                'EventRet: {"result":0,"errMsg":""}',
            ),
        )

        # Then it remains a failure, is labelled unverified, and is not resent.
        self.assertFalse(result["ok"])
        self.assertTrue(result["ambiguous_timeout"])
        self.assertTrue(result["unverifiable"])
        self.assertEqual(compensation_calls, 0)

    def test_ambiguous_send_attempt_does_not_try_additional_routes(self) -> None:
        # Given several routes where the first one returns an ambiguous timeout.
        plugin = self._plugin()

        class AmbiguousBot:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def call_action(self, action: str, **_params: Any) -> dict[str, Any]:
                self.calls.append(action)
                raise RuntimeError(
                    "Timeout: NTEvent "
                    "serviceAndMethod:NodeIKernelMsgService/sendMsg "
                    'EventRet: {"result":0,"errMsg":""}',
                )

        bot = AmbiguousBot()
        attempts = [
            ("send_msg", {"user_id": 1}),
            ("send_private_msg", {"user_id": 1, "group_id": 2}),
            ("send_private_msg", {"user_id": 1}),
        ]

        # When the shared attempt runner receives that indeterminate result.
        with self.assertRaises(self.module.AmbiguousSendTimeout) as raised:
            asyncio.run(plugin._call_send_attempts(bot, attempts))

        # Then it stops immediately rather than risking duplicate delivery.
        self.assertEqual(bot.calls, ["send_msg"])
        self.assertFalse(raised.exception.is_temporary_session)

    def test_friend_ambiguous_timeout_keeps_existing_compensation_behavior(self) -> None:
        # Given the same timeout without group temporary-session context.
        plugin = self._plugin(
            {
                "delivery_confirm_wait_seconds": 0,
                "delivery_compensation_count": 1,
                "delivery_compensation_interval_seconds": 0,
            },
        )
        compensation_calls = 0

        async def cannot_confirm(*_args: Any, **_kwargs: Any) -> bool:
            return False

        async def compensate(*_args: Any, **_kwargs: Any) -> None:
            nonlocal compensation_calls
            compensation_calls += 1

        plugin._confirm_recent_private_delivery = cannot_confirm
        plugin._send_step_or_fallback = compensate

        # When ambiguous-timeout handling runs for a normal private session.
        result = asyncio.run(
            plugin._handle_ambiguous_timeout(
                SimpleNamespace(),
                "2950506809",
                "record",
                {},
                None,
                False,
                False,
                "",
                [{"type": "forward"}],
                1,
                1,
                "ambiguous",
            ),
        )

        # Then the existing single compensation attempt still determines success.
        self.assertTrue(result["ok"])
        self.assertTrue(result["compensated"])
        self.assertEqual(compensation_calls, 1)


if __name__ == "__main__":
    unittest.main()
