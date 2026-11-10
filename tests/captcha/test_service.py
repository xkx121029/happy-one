"""人机验证判定逻辑单测（不依赖 fastapi）。"""

from __future__ import annotations

import unittest
from unittest import mock

from support import build_solution, fetch_challenge

from happyone.captcha import deps, service


class CaptchaServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        deps.reset_test_state()
        deps._SCHEMA_TEXT = None  # 内存库重建后需要重新读迁移文件

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def assert_rejected(self, code: str, payload: dict) -> service.CaptchaError:
        with self.assertRaises(service.CaptchaError) as ctx:
            service.submit_solution(payload)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        self.assertFalse(ctx.exception.to_dict()["ok"])
        return ctx.exception

    def solve_and_issue(self, challenge: dict, **kwargs) -> dict:
        return service.submit_solution(build_solution(challenge, **kwargs))

    # ------------------------------------------------------------------ #
    # 正常路径
    # ------------------------------------------------------------------ #
    def test_challenge_schema(self) -> None:
        challenge = fetch_challenge()
        self.assertEqual(challenge["type"], "sequence")
        self.assertEqual(len(challenge["items"]), service.ITEM_COUNT)
        self.assertEqual(len(challenge["slots"]), service.ITEM_COUNT)
        self.assertEqual(
            [slot["index"] for slot in challenge["slots"]],
            list(range(1, service.ITEM_COUNT + 1)),
        )
        self.assertEqual(len(challenge["target"]), service.ITEM_COUNT)
        self.assertEqual(set(challenge["target"]), {it["id"] for it in challenge["items"]})
        self.assertIn("tap", challenge["interaction"]["alt_modes"])
        self.assertEqual(challenge["expires_in"], service.CHALLENGE_TTL)
        # 目标顺序应按碎片标签数值升序
        labels = {it["id"]: int(it["label"]) for it in challenge["items"]}
        self.assertEqual(
            [labels[item_id] for item_id in challenge["target"]],
            sorted(labels.values()),
        )

    def test_valid_drag_trajectory_passes(self) -> None:
        challenge = fetch_challenge()
        result = self.solve_and_issue(challenge)
        self.assertTrue(result["ok"])
        self.assertTrue(result["ticket"].startswith("v1."))
        self.assertEqual(result["expires_in"], service.TICKET_TTL)

        verified = service.verify_ticket(result["ticket"])
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["purpose"], service.PURPOSE)

    def test_tap_mode_is_equivalent_channel(self) -> None:
        challenge = fetch_challenge()
        result = self.solve_and_issue(challenge, mode="tap")
        self.assertTrue(result["ok"])
        self.assertTrue(service.verify_ticket(result["ticket"])["ok"])

    def test_optional_path_accepted_when_consistent(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge)
        for step in payload["steps"]:
            step["path"] = [
                {"x": step["x"], "y": step["y"] - 30, "t": step["t"] - step["dt"]},
                {"x": step["x"] + 2, "y": step["y"] - 14, "t": step["t"] - step["dt"] / 2},
                {"x": step["x"], "y": step["y"], "t": step["t"]},
            ]
        self.assertTrue(service.submit_solution(payload)["ok"])

    # ------------------------------------------------------------------ #
    # 答案与结构
    # ------------------------------------------------------------------ #
    def test_wrong_order_rejected(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge, order=list(reversed(challenge["target"])))
        self.assert_rejected("wrong_order", payload)

    def test_step_numbering_must_be_sequential(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge)
        payload["steps"][1]["n"] = 5
        self.assert_rejected("invalid_request", payload)

    def test_step_count_must_match(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge)
        payload["steps"] = payload["steps"][:-1]
        self.assert_rejected("invalid_request", payload)

    def test_duplicate_items_rejected(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge)
        payload["steps"][2]["item"] = payload["steps"][0]["item"]
        self.assert_rejected("invalid_request", payload)

    # ------------------------------------------------------------------ #
    # 过程判定
    # ------------------------------------------------------------------ #
    def test_zero_duration_rejected(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge, dts=(0, 0, 0, 0))
        self.assert_rejected("timing_zero_step", payload)

    def test_uniform_timing_rejected(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge, gaps=(300, 300, 300), dts=(180, 180, 180, 180))
        self.assert_rejected("timing_uniform", payload)

    def test_too_fast_overall_rejected(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge, gaps=(120, 120, 120), dts=(60, 60, 60, 60))
        self.assert_rejected("timing_too_fast", payload)

    def test_teleport_between_steps_rejected(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge, gaps=(20, 20, 20), dts=(200, 200, 200, 200))
        self.assert_rejected("timing_too_fast", payload)

    def test_position_must_match_target_slot(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(
            challenge, coords=[(service.CANVAS_W - 5, 5)] * service.ITEM_COUNT
        )
        self.assert_rejected("position_invalid", payload)

    def test_path_jump_rejected(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge)
        step = payload["steps"][0]
        step["path"] = [
            {"x": 0, "y": 0, "t": 0},
            {"x": 0, "y": 0, "t": 10},
            {"x": service.CANVAS_W, "y": service.CANVAS_H, "t": 20},
        ]
        self.assert_rejected("track_invalid", payload)

    # ------------------------------------------------------------------ #
    # 时效 / 一次性 / 失败上限
    # ------------------------------------------------------------------ #
    def test_expired_challenge_rejected(self) -> None:
        with mock.patch.object(service, "CHALLENGE_TTL", -1):
            challenge = fetch_challenge()
        self.assert_rejected("challenge_expired", build_solution(challenge))

    def test_challenge_is_single_use(self) -> None:
        challenge = fetch_challenge()
        self.assertTrue(self.solve_and_issue(challenge)["ok"])
        self.assert_rejected("challenge_used", build_solution(challenge))

    def test_unknown_challenge_rejected(self) -> None:
        challenge = fetch_challenge()
        payload = build_solution(challenge)
        payload["challenge_id"] = "c_deadbeef"
        self.assert_rejected("challenge_not_found", payload)

    def test_attempt_limit_locks_challenge(self) -> None:
        challenge = fetch_challenge()
        bad = build_solution(challenge, order=list(reversed(challenge["target"])))
        for remaining in (2, 1):
            error = self.assert_rejected("wrong_order", bad)
            self.assertEqual(error.to_dict()["attempts_left"], remaining)
        error = self.assert_rejected("wrong_order", bad)
        self.assertEqual(error.to_dict()["attempts_left"], 0)
        self.assertIn("刷新挑战", error.message)

        # 超过 3 次后，即使轨迹正确也不放行
        self.assert_rejected("too_many_attempts", build_solution(challenge))

    # ------------------------------------------------------------------ #
    # 票据
    # ------------------------------------------------------------------ #
    def test_ticket_replay_rejected(self) -> None:
        challenge = fetch_challenge()
        ticket = self.solve_and_issue(challenge)["ticket"]
        self.assertTrue(service.verify_ticket(ticket)["ok"])
        with self.assertRaises(service.CaptchaError) as ctx:
            service.verify_ticket(ticket)
        self.assertEqual(ctx.exception.code, "ticket_used")

    def test_ticket_expired_rejected(self) -> None:
        challenge = fetch_challenge()
        with mock.patch.object(service, "TICKET_TTL", -1):
            ticket = self.solve_and_issue(challenge)["ticket"]
        with self.assertRaises(service.CaptchaError) as ctx:
            service.verify_ticket(ticket)
        self.assertEqual(ctx.exception.code, "ticket_expired")

    def test_tampered_ticket_rejected(self) -> None:
        challenge = fetch_challenge()
        ticket = self.solve_and_issue(challenge)["ticket"]
        version, body, signature = ticket.split(".")
        tampered = f"{version}.{body}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
        with self.assertRaises(service.CaptchaError) as ctx:
            service.verify_ticket(tampered)
        self.assertEqual(ctx.exception.code, "ticket_invalid")

    def test_ticket_purpose_must_match(self) -> None:
        challenge = fetch_challenge()
        ticket = self.solve_and_issue(challenge)["ticket"]
        with self.assertRaises(service.CaptchaError) as ctx:
            service.verify_ticket(ticket, purpose="other_purpose")
        self.assertEqual(ctx.exception.code, "ticket_purpose")

    def test_ticket_ip_binding(self) -> None:
        challenge = fetch_challenge()
        ticket = service.submit_solution(build_solution(challenge), client_ip="203.0.113.7")[
            "ticket"
        ]
        with self.assertRaises(service.CaptchaError) as ctx:
            service.verify_ticket(ticket, client_ip="198.51.100.9")
        self.assertEqual(ctx.exception.code, "ip_mismatch")

        # 同 /24 视为同一来源
        challenge = fetch_challenge()
        ticket = service.submit_solution(build_solution(challenge), client_ip="203.0.113.7")[
            "ticket"
        ]
        self.assertTrue(service.verify_ticket(ticket, client_ip="203.0.113.201")["ok"])


if __name__ == "__main__":
    unittest.main()
