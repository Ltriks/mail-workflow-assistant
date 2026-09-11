"""Approval security and lifecycle tests; no real messages or email are sent."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/agentmail-assistant/scripts"))
import feishu_review as review


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(review, "REVIEW", self.root),
                        patch.object(review, "update"),
                        patch.object(review, "send_approved", return_value={"status": "submitted"})]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.send = self.mocks[2]
        now = review.guard.now()
        self.ticket = dict(ticket_id="ticket1", email_id="msg_email", to="colleague@example.com",
                           mailbox="assistant@example.com", subject="[演示] T03", reason="003：审稿",
                           body="完整正文", body_sha256=review.digest("完整正文"), reviewer="ou_owner",
                           message_id="om_card", chat_id="oc_chat", version=1, nonce="nonce1",
                           created_at=now.isoformat(), expires_at=(now + timedelta(minutes=20)).isoformat(),
                           status="pending", events=[], revisions=[])

    def event(self, action="approve"):
        return dict(type="card.action.trigger", operator_id="ou_owner", message_id="om_card",
                    chat_id="oc_chat", host="im_message", action_tag="button", token="test-only",
                    event_id="event" + str(len(self.ticket["events"])), card_content=json.dumps(review.card(self.ticket)),
                    timestamp=str(int(review.guard.now().timestamp() * 1000)),
                    action_value=json.dumps(review.binding(self.ticket, action)))

    def test_only_verified_approval_sends_once(self):
        event = self.event()
        review.handle(self.ticket, event)
        self.assertEqual(self.ticket["status"], "submitted")
        self.assertEqual(self.ticket["approval"]["body_sha256"], review.digest("完整正文"))
        with self.assertRaises(review.guard.GuardError):
            review.handle(self.ticket, event)
        self.send.assert_called_once()

    def test_wrong_identity_card_chat_missing_content_and_old_event_blocked(self):
        for change in [{"operator_id": "ou_other"}, {"message_id": "om_other"},
                       {"chat_id": "oc_other"}, {"card_content": ""}, {"token": ""},
                       {"timestamp": "1"}, {"event_id": ""}]:
            with self.subTest(change=change):
                with self.assertRaises(review.guard.GuardError):
                    review.handle(self.ticket, {**self.event(), **change})
        self.send.assert_not_called()

    def test_changed_binding_and_body_blocked(self):
        for field, value in [("version", 0), ("nonce", "old"), ("body_sha256", "tampered"),
                             ("ticket_id", "other"), ("action", "send_all")]:
            event = self.event()
            payload = json.loads(event["action_value"])
            payload[field] = value
            event["action_value"] = json.dumps(payload)
            with self.assertRaises(review.guard.GuardError):
                review.handle(self.ticket, event)
        self.ticket["body"] = "被改动"
        with self.assertRaises(review.guard.GuardError):
            review.handle(self.ticket, self.event())
        self.send.assert_not_called()

    def test_expired_or_replayed_event_blocked(self):
        event = self.event()
        self.ticket["events"].append(event["event_id"])
        with self.assertRaises(review.guard.GuardError):
            review.handle(self.ticket, event)
        self.ticket["expires_at"] = (review.guard.now() - timedelta(seconds=1)).isoformat()
        with self.assertRaises(review.guard.GuardError):
            review.handle(self.ticket, self.event())
        self.send.assert_not_called()

    def test_epoch_precisions_preserve_actual_freshness_check(self):
        epoch = review.guard.now().timestamp()
        for scale in [1, 1000, 1000000, 1000000000]:
            event = self.event()
            event["timestamp"] = str(int(epoch * scale))
            self.assertEqual(review.validate(self.ticket, event)[0], "approve")
            event["timestamp"] = str(int((epoch - 3600) * scale))
            with self.assertRaises(review.guard.GuardError):
                review.validate(self.ticket, event)
            event["timestamp"] = str(int((epoch + 3600) * scale))
            with self.assertRaises(review.guard.GuardError):
                review.validate(self.ticket, event)
        for value in ["NaN", "Infinity", "-1", "", "abc", "1e100"]:
            with self.assertRaises(review.guard.GuardError):
                review.event_seconds(value)
        self.send.assert_not_called()

    def test_resuming_does_not_authorize_old_clicks(self):
        event = self.event()
        self.ticket["accept_after"] = (review.guard.now() + timedelta(seconds=1)).isoformat()
        with self.assertRaises(review.guard.GuardError):
            review.handle(self.ticket, event)
        self.send.assert_not_called()

    def test_edit_requires_fresh_approval_and_invalidates_old_button(self):
        old = self.event()
        review.handle(self.ticket, self.event("edit"))
        self.assertEqual(self.ticket["status"], "editing")
        event = self.event()
        event.update(action_name=review.edit_name(self.ticket), form_value=json.dumps({"revised_body": "新正文"}))
        review.handle(self.ticket, event)
        self.assertEqual(self.ticket["status"], "pending")
        self.assertEqual(self.ticket["version"], 2)
        self.send.assert_not_called()
        old["event_id"] = "oldbutton_newclick"
        with self.assertRaises(review.guard.GuardError):
            review.handle(self.ticket, old)
        review.handle(self.ticket, self.event())
        self.send.assert_called_once()
        self.assertEqual(self.ticket["approval"]["body_sha256"], review.digest("新正文"))

    def test_reject_never_sends(self):
        review.handle(self.ticket, self.event("reject"))
        self.assertEqual(self.ticket["status"], "rejected")
        self.send.assert_not_called()

    def test_failure_reserved_and_never_retried(self):
        self.send.side_effect = TimeoutError("模拟发送结果未知")
        review.handle(self.ticket, self.event())
        self.assertEqual(self.ticket["status"], "uncertain")
        saved = json.loads((self.root / "ticket1.json").read_text())
        self.assertEqual(saved["status"], "uncertain")
        with self.assertRaises(review.guard.GuardError):
            review.handle(self.ticket, self.event())
        self.send.assert_called_once()

    def test_card_full_plain_body_and_no_terminal_buttons(self):
        self.ticket["body"] = "<at id=all>原样文本</at>"
        rendered = review.card(self.ticket)
        text = rendered["body"]["elements"][1]["columns"][0]["elements"][1]["text"]
        self.assertEqual(text["tag"], "plain_text")
        self.assertEqual(text["content"], self.ticket["body"])
        self.assertNotIn("lines", text)
        self.ticket["status"] = "submitted"
        self.assertNotIn('"tag": "button"', json.dumps(review.card(self.ticket)))

    def test_expired_card_sync_removes_actions_and_never_sends_mail(self):
        self.ticket["status"] = "expired"
        with patch.object(review, "lark", return_value={}) as api:
            review.sync_card_state(self.ticket)
        args = api.call_args.args
        self.assertEqual(args[:3], ("api", "PATCH", "/open-apis/im/v1/messages/om_card"))
        content = json.loads(json.loads(args[-1])["content"])
        self.assertTrue(content["config"]["update_multi"])
        self.assertIn("审批已结束", content["header"]["title"]["content"])
        self.assertNotIn('"tag": "button"', json.dumps(content))
        self.assertEqual(self.ticket["display_status"], "expired")
        self.send.assert_not_called()

    def test_card_sync_failure_is_not_recorded_as_success(self):
        self.ticket["status"] = "expired"
        with patch.object(review, "lark", return_value={"code": 230027, "msg": "denied"}):
            with self.assertRaises(review.guard.GuardError):
                review.sync_card_state(self.ticket)
        self.assertNotIn("display_status", self.ticket)
        self.send.assert_not_called()

    def test_interaction_test_edit_save_approve_never_calls_email(self):
        self.ticket["interaction_test"] = True
        self.ticket["email_id"] = "ui-test:ticket1"
        review.handle(self.ticket, self.event("edit"))
        event = self.event()
        event.update(action_name=review.edit_name(self.ticket), form_value=json.dumps({"revised_body": "已修改，仅测试"}))
        review.handle(self.ticket, event)
        self.assertEqual(self.ticket["status"], "pending")
        self.assertNotIn("approval", self.ticket)
        review.handle(self.ticket, self.event())
        self.assertEqual(self.ticket["status"], "test_approved")
        self.assertEqual(self.ticket["execution"]["version"], 2)
        self.assertFalse(self.ticket["execution"]["email_sent"])
        self.send.assert_not_called()

    def test_interaction_test_cannot_enter_real_sender_directly(self):
        self.patches[2].stop()
        self.ticket.update(interaction_test=True, status="sending", approval={"body_sha256": self.ticket["body_sha256"]})
        with patch.object(review.guard, "execute") as mail:
            with self.assertRaises(review.guard.GuardError):
                review.send_approved(self.ticket)
            mail.assert_not_called()

    def test_real_guard_pipeline_uses_exact_approval_and_global_ledger(self):
        # Integrate the actual guard; replace only the external email CLI.
        self.patches[2].stop()
        state = self.root / "state"
        state.mkdir()
        policy_path = self.root / "policy.json"
        policy = dict(mailbox=self.ticket["mailbox"], allowed_senders=[self.ticket["to"]],
                      subject_prefix="[演示]", duration_minutes=20, max_replies=2,
                      auto_enabled=False, knowledge_file="knowledge.md", templates={})
        review.guard.save(policy_path, policy)
        (self.root / "knowledge.md").write_text("已核实的虚构资料")
        created = (review.guard.now() - timedelta(minutes=1)).isoformat()
        self.ticket.update(policy_path=str(policy_path), receive_after=created,
                           policy_digest=review.guard.load_policy(policy_path)[1])
        calls = []
        def fake_cli(*args):
            if args == ("+me",):
                return {"ok": True, "data": {"aliases": [{"email": policy["mailbox"]}]}}
            if args[:2] == ("message", "+read"):
                return {"ok": True, "data": {"message_id": "msg_email", "dir": "inbox",
                    "subject": self.ticket["subject"], "created_at": created,
                    "from": {"email": self.ticket["to"]}, "to": [{"email": policy["mailbox"]}]}}
            if args[:2] == ("message", "+reply"):
                calls.append(args)
                body_file = Path(args[args.index("--body-file") + 1])
                self.assertEqual(body_file.read_text(), "完整正文")
                return {"ok": True, "data": {"queued": True}}
            raise AssertionError(args)
        with patch.object(review, "STATE", state), patch.object(review.guard, "cli", side_effect=fake_cli):
            review.handle(self.ticket, self.event())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ticket["status"], "submitted")
        session = review.guard.read_json(state / "session.json")
        self.assertEqual(session["mode"], "preview")
        self.assertTrue(session["closed"])
        ledger = review.guard.read_json(state / "ledger.json")
        self.assertEqual(ledger[policy["mailbox"] + ":msg_email"]["body_sha256"], review.digest("完整正文"))


if __name__ == "__main__":
    unittest.main()
