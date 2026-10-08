import copy
import unittest

from agent_relay import envelope


class EnvelopeTests(unittest.TestCase):
    def message(self, **changes):
        value = envelope.make("request-1", "codex:worker", "claude:lead", "한글 결과")
        value.update(changes)
        return value

    def test_make_supplies_schema_identity_and_utc_time(self):
        message = self.message()
        self.assertEqual(message["schema"], 1)
        self.assertRegex(message["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(message["kind"], "result")
        self.assertEqual(message["hop"], 0)
        self.assertTrue(message["created_utc"].endswith("+00:00"))
        self.assertIs(envelope.validate(message), message)

    def test_explicit_id_and_hops_are_preserved(self):
        message = envelope.make("r", "a", "b", "hello", kind="request", hop=4,
                                message_id="a" * 32)
        self.assertEqual(message["id"], "a" * 32)
        self.assertEqual(message["hop"], 4)

    def test_invalid_fields_are_rejected(self):
        invalid = [
            {"schema": True}, {"schema": 2}, {"id": "../escape"},
            {"id": "A" * 32}, {"request_id": " "}, {"sender": None},
            {"recipient": ""}, {"body": []}, {"kind": "unknown"},
            {"created_utc": "today"}, {"created_utc": "2026-10-07T00:00:00"},
            {"created_utc": "2026-10-07T00:00:00+09:00"}, {"extra": "field"},
            {"body": "\ud800"},
        ]
        for changes in invalid:
            with self.subTest(changes=repr(changes)), self.assertRaises(ValueError):
                envelope.validate(self.message(**changes))
        value = self.message()
        del value["sender"]
        with self.assertRaises(ValueError):
            envelope.validate(value)
        with self.assertRaises(ValueError):
            envelope.validate([])

    def test_hop_limit_rejects_loops_and_nonintegers(self):
        for hop in (-1, 5, True, 1.5, "1"):
            with self.subTest(hop=hop), self.assertRaises(ValueError):
                envelope.validate(self.message(hop=hop))
        message = self.message(hop=2)
        with self.assertRaises(ValueError):
            envelope.validate(message, max_hops=1)
        self.assertIs(envelope.validate(message, max_hops=2), message)
        for limit in (-1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                envelope.validate(message, max_hops=limit)

    def test_exact_utf8_size_boundary(self):
        message = self.message(body="")
        remaining = envelope.MAX_BYTES - len(envelope.encode(message))
        message["body"] = "x" * remaining
        self.assertEqual(len(envelope.encode(message)), envelope.MAX_BYTES)
        envelope.validate(message)
        message["body"] += "x"
        with self.assertRaises(ValueError):
            envelope.validate(message)
        with self.assertRaises(ValueError):
            envelope.validate(self.message(body="가" * (envelope.MAX_BYTES // 2)))

    def test_validation_never_mutates_message(self):
        message = self.message(created_utc="2026-10-07T00:00:00Z")
        original = copy.deepcopy(message)
        envelope.validate(message)
        self.assertEqual(message, original)


if __name__ == "__main__":
    unittest.main()
