"""Self-test for PrintBot. No Slack, no printer. Run: python -m unittest test_printbot"""
import unittest

import printbot
from printbot import DONE, FAILED, PrintBot, PrintError


class FakeClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        def call(**kwargs):
            self.calls.append((method, kwargs))
            if method == "conversations_open":
                return {"channel": {"id": "D-" + kwargs["users"]}}
            return {"ts": f"status{len(self.calls)}"}
        return call

    def named(self, method):
        return [kw for m, kw in self.calls if m == method]


def full_file(ts, filetype="pdf"):
    return {"id": "F" + ts, "name": f"doc{ts}.pdf", "filetype": filetype, "size": 100,
            "url_private_download": "https://files.slack.com/x"}


def msg(ts="1.0", user="U1", filetype="pdf", reactions=None):
    return {"channel": "D1", "ts": ts, "user": user, "reactions": reactions or [],
            "files": [full_file(ts, filetype)]}


class PrintBotTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.printed = []
        self.bot = PrintBot(self.client, "xoxb-test", "UBOT", ["UADMIN", "UHOUSE"],
                            print_fn=self.printed.append)
        self.bot.download = lambda f: b"%PDF-1.4 test"

    def texts(self, method="chat_postMessage"):
        return [kw["text"] for kw in self.client.named(method)]

    def test_queue_positions_and_success(self):
        self.bot.handle_message(msg("1.0"))
        self.bot.handle_message(msg("2.0", user="U2"))
        self.assertEqual(self.texts(), ["doc1.0.pdf: you are #1 in the queue.",
                                        "doc2.0.pdf: you are #2 in the queue."])

        self.bot.process(self.bot.jobs.get())
        self.assertEqual(len(self.printed), 1)
        self.assertEqual(self.client.named("chat_delete")[0]["ts"], "status1")
        self.assertEqual(self.client.named("reactions_add")[0],
                         {"channel": "D1", "timestamp": "1.0", "name": DONE})
        self.assertIn("doc2.0.pdf: you are #1 in the queue.", self.texts("chat_update"))

    def test_failure_alerts_both_admins(self):
        def fail(path):
            raise PrintError("the printer is not reachable over Wi-Fi Direct")
        self.bot.print_fn = fail
        self.bot.handle_message(msg())
        self.bot.process(self.bot.jobs.get())

        self.assertTrue(any(t.startswith("Could not print doc1.0.pdf: the printer is not reachable")
                            for t in self.texts("chat_update")))
        self.assertEqual(self.client.named("reactions_add")[0]["name"], FAILED)
        self.assertEqual([kw["users"] for kw in self.client.named("conversations_open")],
                         ["UADMIN", "UHOUSE"])
        alerts = [kw for kw in self.client.named("chat_postMessage") if kw["channel"].startswith("D-")]
        self.assertEqual(len(alerts), 2)
        self.assertIn("<@U1>", alerts[0]["text"])
        self.assertEqual(self.bot.pending, [])

    def test_non_pdf_rejected_without_alert(self):
        self.bot.handle_message(msg(filetype="docx"))
        self.assertEqual(self.texts(), ["Cannot print doc1.0.pdf: only PDF files can be printed."])
        self.assertEqual(self.client.named("reactions_add")[0]["name"], FAILED)
        self.assertEqual(self.client.named("conversations_open"), [])
        self.assertTrue(self.bot.jobs.empty())

    def test_fake_pdf_content_rejected(self):
        self.bot.download = lambda f: b"<html>login</html>"
        self.bot.handle_message(msg())
        self.assertEqual(self.texts(), ["Cannot print doc1.0.pdf: the file is not a valid PDF."])

    def test_file_stub_is_resolved_with_files_info(self):
        self.client.files_info = lambda **kw: {"file": full_file("1.0")}
        m = msg()
        m["files"] = [{"id": "F1.0", "file_access": "check_file_info"}]
        self.bot.handle_message(m)
        self.assertEqual(self.texts(), ["doc1.0.pdf: you are #1 in the queue."])

    def test_catch_up_survives_one_bad_dm(self):
        def history(channel, **kw):
            if channel == "DBAD":
                raise RuntimeError("ratelimited")
            return {"messages": [msg("1.0")]}
        self.client.users_conversations = lambda **kw: {"channels": [{"id": "DBAD"}, {"id": "D1"}]}
        self.client.conversations_history = history
        self.bot.catch_up()
        self.assertEqual(self.bot.jobs.qsize(), 1)

    def test_already_printed_is_skipped(self):
        self.bot.handle_message(msg(reactions=[{"name": DONE, "users": ["UBOT"]}]))
        self.assertEqual(self.client.calls, [])

    def test_same_message_queued_once(self):
        self.bot.handle_message(msg())
        self.bot.handle_message(msg())  # e.g. catch-up and a live event at the same time
        self.assertEqual(self.bot.jobs.qsize(), 1)

    def test_bot_and_text_messages_ignored(self):
        self.bot.handle_message({"channel": "D1", "ts": "1.0", "user": "UBOT", "files": [{}]})
        self.bot.handle_message({"channel": "D1", "ts": "2.0", "user": "U1", "text": "hi"})
        self.assertEqual(self.client.calls, [])

    def test_daily_limit(self):
        for i in range(printbot.DAILY_LIMIT + 1):
            self.bot.handle_message(msg(f"{i}.0"))
        self.assertEqual(self.bot.jobs.qsize(), printbot.DAILY_LIMIT)
        self.assertIn("limit", self.texts()[-1])

    def test_catch_up_queues_unprinted_and_deletes_stale_status(self):
        history = [
            {"ts": "3.0", "user": "UBOT", "text": "old.pdf: printing…"},
            msg("2.0", reactions=[{"name": DONE, "users": ["UBOT"]}]),
            msg("1.0"),
        ]
        self.client.users_conversations = lambda **kw: {"channels": [{"id": "D1"}]}
        self.client.conversations_history = lambda **kw: {"messages": history}
        self.bot.catch_up()
        self.assertEqual(self.bot.jobs.qsize(), 1)
        self.assertEqual(self.bot.jobs.get()["ts"], "1.0")
        self.assertEqual(self.client.named("chat_delete"), [{"channel": "D1", "ts": "3.0"}])


if __name__ == "__main__":
    unittest.main()
