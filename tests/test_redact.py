"""Secret masking at the vendor boundary, using invented content only."""

import json
import os
from unittest.mock import patch

from tests.support import IsolatedTest


class RedactTests(IsolatedTest):
    def test_legacy_switches_cannot_disable_or_replace_redaction(self):
        from memsift.vendor.redact import redact
        with patch.dict(os.environ, {
            "COGNEE_CAPTURE_REDACT": "false",
            "COGNEE_CAPTURE_REDACT_PATTERNS": "[",
        }):
            self.assertEqual(redact("PASSWORD=invented-value"), "[redacted:credential]")

    def test_nested_json_masks_credentials_and_keeps_numeric_metadata(self):
        from memsift.vendor.redact import redact
        value = {
            "nested": [{"API_KEY": "invented-value", "text": "Bearer invented-bearer"}],
            "authorization": "invented-authorization",
            "token_ratio": 3.2, "input_tokens": 42, "token_count": 12,
            "ok": True, "nothing": None,
        }
        result = redact(value)
        self.assertEqual(result["nested"], [{
            "API_KEY": "[redacted:credential]", "text": "[redacted:authorization]",
        }])
        self.assertEqual(result["authorization"], "[redacted:credential]")
        self.assertEqual((result["token_ratio"], result["input_tokens"], result["token_count"]), (3.2, 42, 12))
        self.assertIs(result["ok"], True)
        self.assertIsNone(result["nothing"])
        self.assertEqual(value["nested"][0]["API_KEY"], "invented-value")

    def test_supported_text_secret_patterns(self):
        from memsift.vendor.redact import redact
        cases = [
            ('"PASSWORD": "invented quoted value"', "invented quoted value"),
            ("'API_KEY'='invented-value'", "invented-value"),
            ("sk-proj-abcdefghijklmnop123456", "abcdefghijklmnop123456"),
            ("ghp_abcdefghijklmnop123456", "abcdefghijklmnop123456"),
            ("xoxb-abcdefghijklmnop123456", "abcdefghijklmnop123456"),
            ("Bearer invented-bearer", "invented-bearer"),
            ("Basic aW52ZW50ZWQ=", "aW52ZW50ZWQ="),
            ("postgres://acme:invented-value@db.invalid/shop", "invented-value"),
            ("-----BEGIN RSA PRIVATE KEY-----\ninvented-private\n-----END RSA PRIVATE KEY-----", "invented-private"),
            ("-----BEGIN PRIVATE KEY-----\ninvented-unfinished", "invented-unfinished"),
        ]
        for text, secret in cases:
            with self.subTest(text=text):
                self.assertNotIn(secret, redact(text))
                self.assertIn("[redacted:", redact(text))

    def test_redaction_precedes_clipping_and_serialization(self):
        from memsift.vendor.redact import redact
        value = {"text": "-----BEGIN PRIVATE KEY-----\n" + "invented-private" * 100 + "\n-----END PRIVATE KEY-----"}
        self.assertEqual(json.dumps(redact(value))[:100], '{"text": "[redacted:private-key]"}')


    def test_quoted_credentials_with_escaped_quotes_are_fully_masked(self):
        from memsift.vendor.redact import redact
        for text in (json.dumps({"PASSWORD": 'invented-head "invented-tail"'}),
                     r"API_KEY='invented-head \'invented-tail\' end'"):
            with self.subTest(text=text):
                result = redact(text)
                self.assertNotIn("invented-head", result)
                self.assertNotIn("invented-tail", result)
