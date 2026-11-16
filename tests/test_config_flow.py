"""Real HA form serialization and schema-validation regressions.

Run with ``python -m unittest tests.test_config_flow -v`` in an HA environment.
These skip in the lightweight client environment. No SP network calls are made.
"""

from __future__ import annotations

import importlib
import importlib.util
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, PropertyMock, patch


@unittest.skipUnless(importlib.util.find_spec("homeassistant"), "requires HA runtime")
class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module("custom_components.sp_group.config_flow")
        self.vol = importlib.import_module("voluptuous")
        self.cv = importlib.import_module("homeassistant.helpers.config_validation")
        # Follow the backend HA actually uses, not packages merely installed.
        self.serializer = (
            self.cv.probatio
            if hasattr(self.cv, "probatio")
            else self.cv.voluptuous_serialize
        )
        self.flow = self.module.SpGroupConfigFlow()
        self.flow.async_show_form = Mock(side_effect=lambda **kwargs: kwargs)
        self.entry = SimpleNamespace(
            data={"username": "fixture@example.com"},
            options={"electricity_price": 0.29, "unrelated": "preserved"},
        )
        self.flow._get_reauth_entry = Mock(return_value=self.entry)
        self.flow._get_reconfigure_entry = Mock(return_value=self.entry)

    def serialize(self, schema: Any) -> list[dict[str, Any]]:
        if hasattr(self.serializer, "to_field_list"):
            return list(
                self.serializer.to_field_list(
                    schema, custom_serializer=self.cv.custom_serializer
                )
            )
        return list(
            self.serializer.convert(schema, custom_serializer=self.cv.custom_serializer)
        )

    async def test_all_credential_forms_serialize(self) -> None:
        for step in ("user", "reauth_confirm", "reconfigure"):
            with self.subTest(step=step):
                form = await getattr(self.flow, f"async_step_{step}")()
                fields = self.serialize(form["data_schema"])
                self.assertEqual(
                    [field["name"] for field in fields], ["username", "password"]
                )
                self.assertIn("text", fields[0]["selector"])
                self.assertEqual(fields[1]["type"], "string")
        self.assertEqual(
            self.serialize(self.flow._mfa_form()["data_schema"])[0]["type"], "string"
        )

    async def test_selector_uses_native_presentation_but_retains_server_rule(
        self,
    ) -> None:
        selector = importlib.import_module("homeassistant.helpers.selector")
        fields = importlib.import_module("custom_components.sp_group.form_fields")
        schema = self.vol.Schema({"value": fields.ValidatedTextSelector(str.upper)})
        presentation = self.serialize(schema)[0]["selector"]
        native = selector.selector(presentation)
        self.assertIsInstance(native, selector.TextSelector)
        self.assertEqual(native("fixture"), "fixture")
        self.assertEqual(schema({"value": "fixture"})["value"], "FIXTURE")

    async def test_username_rules_still_run_in_every_credentials_schema(self) -> None:
        for step in ("user", "reauth_confirm", "reconfigure"):
            form = await getattr(self.flow, f"async_step_{step}")()
            schema = form["data_schema"]
            for username in ("fixture@example.com", "a" * 254, "\U0001f600" * 62):
                with self.subTest(step=step, valid=username):
                    result = schema({"username": username, "password": "fixture"})
                    self.assertEqual(result["username"], username)
            for invalid_username in (
                "",
                "a" * 255,
                "\U0001f600" * 64,
                "user\ud800@example.com",
                True,
                None,
            ):
                with (
                    self.subTest(step=step, invalid=repr(invalid_username)),
                    self.assertRaises(self.vol.Invalid),
                ):
                    schema({"username": invalid_username, "password": "fixture"})
            for password in ("", "a" * 1025):
                with (
                    self.subTest(step=step, password_length=len(password)),
                    self.assertRaises(self.vol.Invalid),
                ):
                    schema({"username": "fixture@example.com", "password": password})

    async def test_price_schema_and_submission_preserve_existing_behavior(self) -> None:
        flow = self.module.SpGroupOptionsFlow()
        flow.async_show_form = Mock(side_effect=lambda **kwargs: kwargs)
        flow.async_create_entry = Mock(side_effect=lambda **kwargs: kwargs)
        for current in (None, 0.29):
            options: dict[str, object] = {"unrelated": "preserved"}
            if current is not None:
                options["electricity_price"] = current
            entry = SimpleNamespace(options=options)
            with patch.object(
                type(flow),
                "config_entry",
                new_callable=PropertyMock,
                return_value=entry,
            ):
                form = await flow.async_step_init()
                schema = form["data_schema"]
                field = self.serialize(schema)[0]
                self.assertIn("text", field["selector"])
                self.assertEqual(
                    field["description"]["suggested_value"],
                    str(current) if current is not None else "",
                )
                for raw, expected in (
                    (0.29, 0.29),
                    (1, 1.0),
                    ("0.31", 0.31),
                    (" 0.31 ", 0.31),
                    ("", ""),
                    ("   ", ""),
                    ("0", 0.0),
                    (0, 0.0),
                ):
                    with self.subTest(current=current, raw=raw):
                        validated = schema({"electricity_price": raw})
                        self.assertEqual(validated["electricity_price"], expected)
                        result = await flow.async_step_init(validated)
                        self.assertEqual(
                            result["data"],
                            {"electricity_price": expected, "unrelated": "preserved"},
                        )
                for invalid in (
                    "free",
                    "-1",
                    -1,
                    "nan",
                    float("inf"),
                    True,
                    False,
                    None,
                ):
                    with (
                        self.subTest(current=current, invalid=invalid),
                        self.assertRaises(self.vol.Invalid),
                    ):
                        schema({"electricity_price": invalid})
                result = await flow.async_step_init(schema({}))
                self.assertEqual(result["data"], {"unrelated": "preserved"})


if __name__ == "__main__":
    unittest.main()
