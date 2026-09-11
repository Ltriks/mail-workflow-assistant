"""Offline invariants: never calls the actual mail CLI."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from datetime import timedelta

SCRIPT = Path(__file__).resolve().parents[1] / "skills/agentmail-assistant/scripts/mail_guard.py"
spec = importlib.util.spec_from_file_location("mail_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.policy_path = self.root / "policy.json"
        self.policy = dict(mailbox="assistant@example.com", allowed_senders=["teacher@example.com"],
                           subject_prefix="[演示]", duration_minutes=20, max_replies=2,
                           auto_enabled=True, knowledge_file="knowledge.md",
                           templates={"location": "虚构地点 A302"})
        guard.save(self.policy_path, self.policy)
        (self.root / "knowledge.md").write_text("虚构业务资料")
        self.mailbox = self.policy["mailbox"]
        self.message = dict(message_id="msg_test", dir="inbox", subject="[演示] 地点",
                            to=[{"email": self.mailbox}],
                            **{"from": {"email": "teacher@example.com"}})
        self.sends = []
        self.result = {"ok": True, "data": {"queued": True}}
        self.mock = patch.object(guard, "cli", side_effect=self.fake_cli)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.start()
        self.message["created_at"] = guard.now().isoformat()

    def fake_cli(self, *args):
        if args == ("+me",):
            return {"ok": True, "data": {"aliases": [{"email": self.mailbox}]}}
        if args[:2] == ("message", "+read"):
            return {"ok": True, "data": self.message.copy()}
        if args[:2] == ("message", "+reply"):
            self.sends.append(args)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        if args[:2] == ("message", "+list"):
            return {"ok": True, "data": {"data": [self.message.copy()], "pagination": {"has_more": False}}}
        raise AssertionError(args)

    def run_guard(self, command, **kwargs):
        return guard.execute(SimpleNamespace(command=command, **kwargs), self.state)

    def start(self, mode="auto", since=None):
        return self.run_guard("start", policy=str(self.policy_path), mode=mode, authorized=True, since=since)

    def reply(self, **kwargs):
        options = dict(id="msg_test", template="location", draft=None, approved=False)
        options.update(kwargs)
        return self.run_guard("reply", **options)

    def test_reply_success_then_duplicate_blocked_across_restart(self):
        self.assertEqual(self.reply()["status"], "submitted")
        self.run_guard("finish")
        self.start(since=self.message["created_at"])
        with self.assertRaises(guard.GuardError):
            self.reply()
        self.assertEqual(len(self.sends), 1)
        self.assertTrue(list((self.state / "history").glob("*.json")))

    def test_wrong_account_blocks_send(self):
        self.mailbox = "other@example.com"
        with self.assertRaises(guard.GuardError):
            self.reply()
        self.assertFalse(self.sends)

    def test_live_directory_object_accepts_only_inbox(self):
        self.message['dir'] = {'dir_id': 1, 'dir_name': 'inbox'}
        self.assertEqual(self.reply()['status'], 'submitted')

    def test_directory_object_cannot_bypass_inbox_guard(self):
        for value in ({'dir_id': 1}, {'dir_id': 1, 'dir_name': 'sent'}, {'dir_name': None}):
            with self.subTest(directory=value):
                self.message['dir'] = value
                with self.assertRaises(guard.GuardError):
                    self.reply()
        self.assertFalse(self.sends)

    def test_scope_and_old_mail_blocked(self):
        changes = [({"from": {"email": "stranger@example.com"}}),
                   {"subject": "普通邮件"}, {"dir": "sent"}, {"to": []},
                   {"created_at": (guard.now() - timedelta(days=1)).isoformat()}]
        for change in changes:
            with self.subTest(change=change):
                original = self.message.copy()
                self.message.update(change)
                with self.assertRaises(guard.GuardError):
                    self.reply()
                self.message = original
        self.assertFalse(self.sends)

    def test_preview_blocks_auto_but_allows_approved_exact_draft(self):
        self.run_guard("finish")
        self.start("preview", since=self.message["created_at"])
        with self.assertRaises(guard.GuardError):
            self.reply()
        body_file = self.root / "draft.txt"
        body_file.write_text("尚未批准改期。")
        draft = self.run_guard("draft", id="msg_test", body_file=str(body_file), reason="需要决定")
        with self.assertRaises(guard.GuardError):
            self.reply(template=None, draft=draft["draft_id"])
        self.assertEqual(self.reply(template=None, draft=draft["draft_id"], approved=True)["status"], "submitted")

    def test_uncertain_send_never_retried(self):
        self.result = guard.GuardError("模拟请求超时")
        with self.assertRaises(guard.GuardError):
            self.reply()
        self.result = {"ok": True, "data": {"queued": True}}
        with self.assertRaises(guard.GuardError):
            self.reply()
        self.assertEqual(len(self.sends), 1)
        ledger = guard.read_json(self.state / "ledger.json")
        self.assertEqual(next(iter(ledger.values()))["status"], "uncertain")

    def test_confirmation_or_missing_queue_is_not_success(self):
        self.result = {"ok": True, "data": {"confirmation_required": True}}
        with self.assertRaises(guard.GuardError):
            self.reply()
        self.assertEqual(next(iter(guard.read_json(self.state / "ledger.json").values()))["status"], "uncertain")

    def test_expiry_and_quota_block_sends(self):
        path = self.state / "session.json"
        state = guard.read_json(path)
        state["expires_at"] = (guard.now() - timedelta(seconds=1)).isoformat()
        guard.save(path, state)
        with self.assertRaises(guard.GuardError):
            self.reply()
        state["expires_at"] = (guard.now() + timedelta(minutes=5)).isoformat()
        state["attempts"] = 2
        guard.save(path, state)
        with self.assertRaises(guard.GuardError):
            self.reply()
        self.assertFalse(self.sends)

    def test_changed_knowledge_blocks_sends(self):
        (self.root / "knowledge.md").write_text("未经本次授权的新安排")
        with self.assertRaises(guard.GuardError):
            self.reply()
        self.assertFalse(self.sends)

    def test_scan_excludes_pending_and_processed(self):
        self.assertEqual(len(self.run_guard("scan")["messages"]), 1)
        self.reply()
        self.assertEqual(self.run_guard("scan")["messages"], [])

    def test_empty_allowlist_cannot_start(self):
        self.run_guard("finish")
        self.policy["allowed_senders"] = []
        guard.save(self.policy_path, self.policy)
        with self.assertRaises(guard.GuardError):
            self.start()


if __name__ == "__main__":
    unittest.main()
