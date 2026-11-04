"""AI expense-type selection with synthetic responses and isolated storage."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from classification.service import choose_expense_type_with_ai
from expense_lines_cache import load_vendor_expense_types_flat
from web.service import ExpenseService


class AIExpenseTypeTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.client.responses.create.return_value = SimpleNamespace(
            output_text='{"expense_type":"Parking"}',
        )
        self.factory = patch("browser_automation.build_openai_client", return_value=self.client)
        self.build_client = self.factory.start()
        self.addCleanup(self.factory.stop)

    def choose(self, **kwargs):
        return choose_expense_type_with_ai(
            "City parking", ["Parking", "Meals"], api_key="synthetic-key",
            model="configured-model", **kwargs,
        )

    def test_exact_choice_and_shared_prompt(self):
        self.assertEqual(self.choose(http_verify_preferred="/synthetic/ca.pem"), "Parking")
        self.build_client.assert_called_once_with(
            "synthetic-key", http_verify_preferred="/synthetic/ca.pem",
        )
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "configured-model")
        prompt = request["input"][0]["content"][0]["text"]
        self.assertIn("Merchant: City parking", prompt)
        self.assertIn('Options: ["Parking", "Meals"]', prompt)
        self.client.__exit__.assert_called_once()

    def test_fenced_json(self):
        self.client.responses.create.return_value.output_text = '```json\n{"expense_type":"Meals"}\n```'
        self.assertEqual(self.choose(), "Meals")

    def test_invalid_responses_are_not_fallback_choices(self):
        for raw in (
            "", "Meals", "{}", "null", "[]", '{"expense_type":42}',
            '{"expense_type":"Invented"}', '{"expense_type":"Park"}',
        ):
            with self.subTest(raw=raw):
                self.client.responses.create.return_value.output_text = raw
                with self.assertRaisesRegex(ValueError, "AI"):
                    self.choose()

    def test_missing_inputs_do_not_call_api(self):
        for merchant, options, key in (
            ("", ["Meals"], "key"), ("Cafe", [], "key"), ("Cafe", ["Meals"], ""),
        ):
            with self.subTest(merchant=merchant, options=options, key=key):
                with self.assertRaises(ValueError):
                    choose_expense_type_with_ai(merchant, options, api_key=key, model="model")
        self.build_client.assert_not_called()

    def test_network_failure_is_explicit(self):
        self.client.responses.create.side_effect = TimeoutError("synthetic timeout")
        with self.assertRaisesRegex(RuntimeError, "OpenAI expense type request failed"):
            self.choose()
        self.client.__exit__.assert_called_once()


class VendorClassificationServiceTests(unittest.TestCase):
    def test_uses_settings_and_replaces_only_requested_vendor(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ExpenseService(Path(temp))
            service.set_vendor_classification("city parking", "Meals")
            service.set_vendor_classification("cafe", "Meals")
            with (
                patch.object(service, "_load_settings_raw", return_value={
                    "openai_model": "chosen-model", "openai_http_verify": "/synthetic/ca.pem",
                }),
                patch.object(service, "_get_openai_key", return_value="synthetic-key"),
                patch("web.service.get_expense_type_options", return_value=["Parking", "Meals"]),
                patch("web.service.choose_expense_type_with_ai", return_value="Parking") as choose,
            ):
                choice = service.suggest_vendor_classification("city parking")
            choose.assert_called_once_with(
                "city parking", ["Parking", "Meals"], api_key="synthetic-key",
                model="chosen-model", http_verify_preferred="/synthetic/ca.pem",
            )
            self.assertEqual(load_vendor_expense_types_flat(Path(temp))["city parking"], "Meals")
            service.set_vendor_classification("city parking", choice)
            self.assertEqual(load_vendor_expense_types_flat(Path(temp)), {
                "city parking": "Parking", "cafe": "Meals",
            })

    def test_missing_key_and_request_errors_preserve_existing_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ExpenseService(Path(temp))
            service.set_vendor_classification("cafe", "Meals")
            path = Path(temp) / "vendor_expense_types.json"
            original = path.read_bytes()
            with patch.object(service, "_get_openai_key", return_value=""):
                with self.assertRaisesRegex(ValueError, "API key"):
                    service.suggest_vendor_classification("cafe")
            with (
                patch.object(service, "_get_openai_key", return_value="key"),
                patch("web.service.choose_expense_type_with_ai", side_effect=RuntimeError("AI failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "AI failed"):
                    service.suggest_vendor_classification("cafe")
            self.assertEqual(path.read_bytes(), original)

    def test_environment_model_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ExpenseService(Path(temp))
            with (
                patch.object(service, "_load_settings_raw", return_value={}),
                patch.object(service, "_get_openai_key", return_value="key"),
                patch.dict(os.environ, {"OPENAI_MODEL": "env-model"}),
                patch("web.service.choose_expense_type_with_ai", return_value="Meals") as choose,
            ):
                service.suggest_vendor_classification("cafe")
            self.assertEqual(choose.call_args.kwargs["model"], "env-model")

    def test_corrupt_storage_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ExpenseService(Path(temp))
            path = Path(temp) / "vendor_expense_types.json"
            for content in ("broken json", "[]", '{"vendors":[]}'):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(ValueError):
                        service.set_vendor_classification("cafe", "Meals")
                    self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
