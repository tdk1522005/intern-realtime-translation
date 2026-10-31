from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import database  # noqa: E402
from latency_metrics import read_latency_report, record_provider_connection_sample  # noqa: E402
from live_agents import (  # noqa: E402
    LiveSessionOrchestrator,
    MemoryAgent,
    next_segment_id,
)


class LiveAgentReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        )
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "agent-tests.sqlite3"
        database.init_db()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO presentations (presentation_id, status, started_at) "
                "VALUES (?, ?, ?)",
                ("presentation-agent-test", "active", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, presentation_id, session_number, status, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "session-agent-test",
                    "presentation-agent-test",
                    1,
                    "active",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        self.orchestrator = LiveSessionOrchestrator("offline-test-model")

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_reconnect_reuses_persisted_pending_segment_key_and_sequence(self):
        first_context = self.orchestrator.prepare("session-agent-test", "vi")
        saved = self.orchestrator.memory.save_live_segment(
            first_context,
            "The meeting starts at nine.",
            "Cuộc họp bắt đầu lúc chín giờ.",
            segment_id=first_context.pending_segment_id,
        )

        reconnected_context = self.orchestrator.prepare(
            "session-agent-test",
            "vi",
        )

        self.assertEqual(saved["sequence_number"], 1)
        self.assertEqual(
            reconnected_context.pending_segment_id,
            next_segment_id("session-agent-test", first_context.pending_segment_id),
        )
        self.assertEqual(reconnected_context.persisted_segment_count, 1)
        self.assertEqual(reconnected_context.last_sequence_number, 1)

    def test_same_finalization_replayed_after_reconnect_is_stored_once(self):
        first_context = self.orchestrator.prepare("session-agent-test", "vi")
        original_id = first_context.pending_segment_id
        original = self.orchestrator.memory.save_live_segment(
            first_context,
            "Please take your seats.",
            "Vui lòng vào chỗ ngồi.",
            segment_id=original_id,
        )

        reconnected_context = self.orchestrator.prepare(
            "session-agent-test",
            "vi",
        )
        replay = MemoryAgent().save_live_segment(
            reconnected_context,
            "Please take your seats.",
            "Vui lòng vào chỗ ngồi.",
            segment_id=original_id,
        )
        final_context = self.orchestrator.prepare("session-agent-test", "vi")

        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM segments WHERE session_id = ?",
                ("session-agent-test",),
            ).fetchone()[0]

        self.assertEqual(replay["segment_id"], original["segment_id"])
        self.assertEqual(replay["sequence_number"], 1)
        self.assertEqual(count, 1)
        self.assertEqual(
            final_context.pending_segment_id,
            reconnected_context.pending_segment_id,
        )

    def test_provider_resume_handle_is_scoped_to_model_and_target_language(self):
        context = self.orchestrator.prepare("session-agent-test", "vi")
        saved = self.orchestrator.memory.save_resumption_update(
            context,
            SimpleNamespace(new_handle="provider-handle-1", resumable=True),
        )

        reconnected = self.orchestrator.prepare("session-agent-test", "vi")
        changed_direction = self.orchestrator.prepare("session-agent-test", "en")

        self.assertTrue(saved)
        self.assertEqual(reconnected.resumption_handle, "provider-handle-1")
        self.assertIsNone(changed_direction.resumption_handle)

    def test_expired_provider_handle_is_cleared_but_history_key_is_kept(self):
        context = self.orchestrator.prepare("session-agent-test", "vi")
        self.orchestrator.memory.save_resumption_update(
            context,
            SimpleNamespace(new_handle="expired-handle", resumable=True),
        )
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with database.get_connection() as connection:
            connection.execute(
                """
                UPDATE live_provider_context
                SET resumption_updated_at = ?
                WHERE session_id = ? AND model_name = ? AND target_language = ?
                """,
                (
                    old_time,
                    "session-agent-test",
                    "offline-test-model",
                    "vi",
                ),
            )

        reconnected = self.orchestrator.prepare("session-agent-test", "vi")
        with database.get_connection() as connection:
            row = connection.execute(
                """
                SELECT resumption_handle, pending_segment_id
                FROM live_provider_context
                WHERE session_id = ? AND model_name = ? AND target_language = ?
                """,
                ("session-agent-test", "offline-test-model", "vi"),
            ).fetchone()

        self.assertIsNone(reconnected.resumption_handle)
        self.assertIsNone(row["resumption_handle"])
        self.assertEqual(row["pending_segment_id"], context.pending_segment_id)

    def test_nonresumable_update_invalidates_previous_token(self):
        context = self.orchestrator.prepare("session-agent-test", "vi")
        self.orchestrator.memory.save_resumption_update(
            context,
            SimpleNamespace(new_handle="old-handle", resumable=True),
        )
        saved = self.orchestrator.memory.save_resumption_update(
            context,
            SimpleNamespace(new_handle="", resumable=False),
        )

        self.assertFalse(saved)
        self.assertIsNone(
            self.orchestrator.prepare("session-agent-test", "vi").resumption_handle
        )

    def test_stale_resume_handle_falls_back_to_fresh_provider_stream(self):
        context = self.orchestrator.prepare("session-agent-test", "vi")
        self.orchestrator.memory.save_resumption_update(
            context,
            SimpleNamespace(new_handle="rejected-handle", resumable=True),
        )
        context = self.orchestrator.prepare("session-agent-test", "vi")

        class FakeConnectContext:
            def __init__(self, owner, config):
                self.owner = owner
                self.config = config

            async def __aenter__(self):
                self.owner.configs.append(self.config)
                if self.config.session_resumption is not None:
                    raise RuntimeError("provider rejected saved handle")
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class FakeLive:
            def __init__(self):
                self.configs = []
                self.aio = SimpleNamespace(live=self)

            def connect(self, *, model, config):
                self.assert_model = model
                return FakeConnectContext(self, config)

        client = FakeLive()

        async def run_connect():
            async with self.orchestrator.translation.connect(
                client,
                context,
                self.orchestrator.memory,
            ) as (provider_session, mode):
                self.assertIsNotNone(provider_session)
                return mode

        mode = asyncio.run(run_connect())

        self.assertEqual(mode, "resume_fallback")
        self.assertEqual(len(client.configs), 2)
        self.assertIsNotNone(client.configs[0].session_resumption)
        self.assertIsNone(client.configs[1].session_resumption)
        self.assertIsNone(
            self.orchestrator.prepare("session-agent-test", "vi").resumption_handle
        )

    def test_successful_provider_resumption_is_reported_as_restored(self):
        context = self.orchestrator.prepare("session-agent-test", "vi")
        self.orchestrator.memory.save_resumption_update(
            context,
            SimpleNamespace(new_handle="valid-handle", resumable=True),
        )
        context = self.orchestrator.prepare("session-agent-test", "vi")

        class FakeProviderContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        fake_client = SimpleNamespace(
            aio=SimpleNamespace(
                live=SimpleNamespace(
                    connect=lambda **kwargs: FakeProviderContext()
                )
            )
        )

        async def run_connect():
            async with self.orchestrator.translation.connect(
                fake_client,
                context,
                self.orchestrator.memory,
            ) as (_, mode):
                return mode

        self.assertEqual(asyncio.run(run_connect()), "resumed")

    def test_connection_guard_rejects_parallel_stream_and_releases_cleanly(self):
        async def run_guard():
            self.assertTrue(
                await self.orchestrator.connections.try_acquire(
                    "connection-lock-test"
                )
            )
            self.assertFalse(
                await self.orchestrator.connections.try_acquire(
                    "connection-lock-test"
                )
            )
            self.orchestrator.connections.release("connection-lock-test")
            reacquired = await self.orchestrator.connections.try_acquire(
                "connection-lock-test"
            )
            self.orchestrator.connections.release("connection-lock-test")
            return reacquired

        self.assertTrue(asyncio.run(run_guard()))


class ProviderConnectionLatencyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        )
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "latency-agent-tests.sqlite3"
        database.init_db()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO presentations (presentation_id, status, started_at) "
                "VALUES (?, ?, ?)",
                ("presentation-agent-test", "active", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO sessions "
                "(session_id, presentation_id, session_number, status, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "session-agent-test",
                    "presentation-agent-test",
                    1,
                    "active",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_provider_connect_p50_p95_are_reported_by_connection_mode(self):
        self.assertTrue(
            record_provider_connection_sample(
                "session-agent-test", 100, "fresh"
            )
        )
        self.assertTrue(
            record_provider_connection_sample(
                "session-agent-test", 200, "resumed"
            )
        )

        report = read_latency_report()

        self.assertEqual(report["metrics"]["provider_connect_ms"]["sample_count"], 2)
        self.assertEqual(report["metrics"]["provider_connect_ms"]["p50_ms"], 100.0)
        self.assertEqual(report["metrics"]["provider_connect_ms"]["p95_ms"], 200.0)
        self.assertEqual(
            report["provider_connection_modes"]["resumed"]["sample_count"],
            1,
        )
        self.assertFalse(
            record_provider_connection_sample(
                "session-agent-test", -1, "fresh"
            )
        )


if __name__ == "__main__":
    unittest.main()
