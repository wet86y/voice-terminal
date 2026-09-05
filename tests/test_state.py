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


if __name__ == "__main__":
    unittest.main()
