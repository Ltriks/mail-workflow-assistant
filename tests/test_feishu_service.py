"""Persistent queue invariants; all external APIs are mocked."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/agentmail-assistant/scripts"))
import feishu_service as service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for target, value in [("REVIEW", self.root), ("STATE", self.root)]:
            p = patch.object(service.review, target, value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(service, "config", return_value={"reviewer": "ou_owner", "allow_real_mail": False})
        p.start(); self.addCleanup(p.stop)

    def test_enqueue_never_sends_and_real_mail_disabled(self):
        with patch.object(service.review, "lark") as api:
            result = service.enqueue(interaction_test=True, minutes=1)
            self.assertEqual(result["status"], "queued")
            with self.assertRaises(service.guard.GuardError):
                service.enqueue("state/source.json", "draft")
            api.assert_not_called()

    def test_offline_does_not_publish_and_expiry_removes_buttons(self):
        service.enqueue(interaction_test=True, minutes=1)
        with patch.object(service.review, "lark") as api:
            service.tick(False)
            api.assert_not_called()
        t = next(service.tickets())
        t.update(status="pending", message_id="om_card", expires_at=(service.guard.now() - timedelta(seconds=1)).isoformat())
        service.review.persist(t)
        with patch.object(service.review, "sync_card_state") as sync:
            service.tick(True)
            self.assertEqual(sync.call_args.args[0]["status"], "expired")
        self.assertEqual(next(service.tickets())["status"], "expired")

    def test_publish_failure_is_durable_and_not_retried(self):
        service.enqueue(interaction_test=True)
        with patch.object(service.review, "lark", side_effect=TimeoutError("test timeout")) as api:
            service.tick(True)
            service.tick(True)
            api.assert_called_once()
        self.assertEqual(next(service.tickets())["status"], "publish_uncertain")

    def test_crash_during_send_never_retries(self):
        service.enqueue(interaction_test=True)
        t = next(service.tickets())
        t["status"] = "sending"
        service.review.persist(t)
        with patch.object(service.review, "handle") as handle:
            service.recover()
            service.tick(True)
            handle.assert_not_called()
        self.assertEqual(next(service.tickets())["status"], "uncertain")

    def test_wrong_reviewer_cannot_be_published(self):
        service.enqueue(interaction_test=True)
        t = next(service.tickets())
        t["reviewer"] = "ou_other"
        service.review.persist(t)
        with patch.object(service.review, "lark") as api:
            service.tick(True)
            api.assert_not_called()
        self.assertEqual(next(service.tickets())["status"], "blocked")

    def test_callback_routes_only_exact_card(self):
        service.enqueue(interaction_test=True)
        with patch.object(service.review, "lark", return_value={"message_id": "om_card", "chat_id": "oc_chat"}):
            service.tick(True)
        with patch.object(service.review, "handle") as handle:
            service.process({"message_id": "om_other"})
            handle.assert_not_called()
            service.process({"message_id": "om_card"})
            handle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
