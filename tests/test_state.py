import unittest

from voice_terminal.core.state import RuntimeState, TerminalState


class StateTests(unittest.TestCase):
    def test_runtime_is_not_startable_until_all_services_are_ready(self) -> None:
        state = RuntimeState()
        self.assertFalse(state.can_start)
        for name in state.services:
            state.set_service(name, "READY")
        state.set_state(TerminalState.IDLE, listening=False)
        self.assertTrue(state.can_start)
        state.set_state(TerminalState.IDLE, listening=True)
        self.assertFalse(state.can_start)

    def test_disabled_kws_does_not_block_manual_session(self) -> None:
        state = RuntimeState()
        for name in state.services:
            state.set_service(name, "READY")
        state.set_service("kws", "DISABLED")
        state.set_state(TerminalState.IDLE, listening=False)
        self.assertTrue(state.can_start)

    def test_loading_capability_reports_starting_reason_not_failure(self) -> None:
        """加载中的能力属于瞬态：原因码必须是加载中，界面才能显示“启动中”。"""
        state = RuntimeState()
        for name in state.services:
            state.set_service(name, "READY")
        state.set_service("kws", "DISABLED")
        state.set_state(TerminalState.IDLE, listening=False)
        state.set_service("tts", "STARTING")
        self.assertFalse(state.can_start)
        self.assertEqual(state.can_start_reason["code"], "CAPABILITY_LOADING")
        self.assertEqual(state.can_start_reason["services"], ["tts"])
        self.assertEqual(state.can_start_reason["loading"], ["tts"])
        self.assertIn("TTS（语音合成）", state.can_start_reason["message"])

    def test_failed_capability_reports_failure_reason(self) -> None:
        state = RuntimeState()
        for name in state.services:
            state.set_service(name, "READY")
        state.set_service("kws", "DISABLED")
        state.set_state(TerminalState.IDLE, listening=False)
        state.set_service("tts", "ERROR")
        self.assertEqual(state.can_start_reason["code"], "CAPABILITY_FAILED")
        self.assertEqual(state.can_start_reason["loading"], [])

    def test_mixed_loading_and_missing_capability_is_failure(self) -> None:
        """只要有一项不是加载中，就不能把整体判断成“稍后会自己好”。"""
        state = RuntimeState()
        for name in state.services:
            state.set_service(name, "READY")
        state.set_service("kws", "DISABLED")
        state.set_state(TerminalState.IDLE, listening=False)
        state.set_service("llm", "STARTING")
        state.set_service("tts", "OFFLINE")
        self.assertEqual(state.can_start_reason["code"], "CAPABILITY_FAILED")

    def test_reason_distinguishes_active_session_and_runtime_error(self) -> None:
        state = RuntimeState()
        for name in state.services:
            state.set_service(name, "READY")
        state.set_service("kws", "DISABLED")
        state.set_state(TerminalState.IDLE, listening=True)
        self.assertEqual(state.can_start_reason["code"], "SESSION_ACTIVE")
        state.set_state(TerminalState.ERROR, listening=False)
        self.assertEqual(state.can_start_reason["code"], "RUNTIME_ERROR")


if __name__ == "__main__":
    unittest.main()
