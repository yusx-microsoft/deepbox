import asyncio
import json
import unittest
import uuid

import pyte

from connector.transport import heartbeat_loop
from server.app.live import LiveSession, serialize_screen


class RecordingWebSocket:
    def __init__(self):
        self.frames = []

    async def send(self, data):
        self.frames.append(data)


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_connection_emits_protocol_heartbeat(self):
        socket = RecordingWebSocket()
        task = asyncio.create_task(heartbeat_loop(socket, interval=0.001))
        for _ in range(20):
            if socket.frames:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(socket.frames)
        self.assertEqual(json.loads(socket.frames[0]), {"type": "heartbeat"})


class ScreenRestoreTests(unittest.TestCase):
    def test_restore_contains_scrollback_and_current_screen(self):
        screen = pyte.HistoryScreen(20, 3, history=100)
        stream = pyte.ByteStream(screen)
        stream.feed(b"first\r\nsecond\r\nthird\r\nfourth")

        restored = serialize_screen(screen)

        self.assertIn("first", restored)
        self.assertIn("fourth", restored)
        self.assertIn("\x1b[2J\x1b[H", restored)

    def test_live_session_records_output_and_restores_it(self):
        sid = "test-" + uuid.uuid4().hex
        live = LiveSession(sid, 40, 5)
        try:
            live.feed_output("persistent hello")
            self.assertIn("persistent hello", live.restore_bytes())
            self.assertIn("persistent hello", live.cast_path.read_text(encoding="utf-8"))
        finally:
            live.mark_ended(0)
            live.cast_path.unlink(missing_ok=True)

    def test_input_is_recorded_once_only_after_delivery_ack(self):
        sid = "test-" + uuid.uuid4().hex
        live = LiveSession(sid, 40, 5)
        try:
            live.queue_input("input-1", "echo durable\r")
            before = live.cast_path.read_text(encoding="utf-8")
            self.assertNotIn("echo durable", before)

            self.assertTrue(live.acknowledge_input("input-1"))
            self.assertFalse(live.acknowledge_input("input-1"))
            live.queue_input("rejected", "must not be recorded")
            self.assertFalse(live.acknowledge_input("rejected", "received"))
            self.assertIn("rejected", live.pending_inputs)
            self.assertFalse(live.acknowledge_input("rejected", "rejected"))
            self.assertNotIn("rejected", live.pending_inputs)
            self.assertFalse(live.acknowledge_input("rejected", "delivered"))
            after = live.cast_path.read_text(encoding="utf-8")
            input_events = [
                json.loads(line) for line in after.splitlines()[1:]
                if line.strip() and json.loads(line)[1] == "i"
            ]
            self.assertEqual([event[2] for event in input_events], ["echo durable\r"])
        finally:
            live.mark_ended(0)
            live.cast_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
