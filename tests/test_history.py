import unittest

from voice_terminal.server.history import InMemoryHistory


class InMemoryHistoryTests(unittest.TestCase):
    def test_request_context_does_not_commit_user_until_exchange_completes(self) -> None:
        history = InMemoryHistory()

        request = history.messages_for_user("打开浏览器")

        self.assertEqual(len(history.messages), 1)
        self.assertEqual(request[-1], {"role": "user", "content": "打开浏览器"})

        history.append_exchange("打开浏览器", "好的，我来帮你打开浏览器。")

        self.assertEqual(
            history.messages[-2:],
            [
                {"role": "user", "content": "打开浏览器"},
                {"role": "assistant", "content": "好的，我来帮你打开浏览器。"},
            ],
        )

    def test_next_request_contains_complete_committed_exchanges(self) -> None:
        history = InMemoryHistory()
        history.append_exchange("第一句话", "第一句回答")

        request = history.messages_for_user("第二句话")

        self.assertEqual(
            request,
            [
                history.messages[0],
                {"role": "user", "content": "第一句话"},
                {"role": "assistant", "content": "第一句回答"},
                {"role": "user", "content": "第二句话"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
