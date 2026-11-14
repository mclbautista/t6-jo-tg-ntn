"""Configuration reference: defaults, validation, and the example files."""

import re
import tempfile
import unittest
from pathlib import Path

import yaml
from pydantic import ValidationError

from jo_watchdog.app.config import (
    LIVE_REQUIRED_KEYS,
    SECRET_KEYS,
    Config,
    load_config,
    placeholder_keys,
    require_live_keys,
)

ROOT = Path(__file__).resolve().parent.parent

MINIMAL = dict(
    intake_chat_id=-100123,
    intake_topic_id=5,
    ops_chat_id=-100999,
    ops_topic_id=9,
    job_orders_data_source_id="ds-1",
)


def make(**overrides) -> Config:
    return Config(**{**MINIMAL, **overrides})


class DefaultsTests(unittest.TestCase):
    def test_reference_defaults(self):
        config = make()
        self.assertEqual(config.deepseek_base_url, "https://api.deepseek.com")
        self.assertEqual(config.deepseek_timeout_seconds, 60)
        self.assertEqual(config.department_aliases, {"TO": "TOC"})
        self.assertEqual(config.cache_refresh_hours, 6)
        self.assertEqual(config.cache_stale_alert_hours, 24)
        self.assertEqual(config.fuzzy_min_score, 90)
        self.assertEqual(config.fuzzy_min_lead, 8)
        self.assertEqual(config.honorifics, ["ate", "kuya", "sir"])
        self.assertEqual(config.header_date_tolerance_days, 1)
        self.assertEqual(config.retry_schedule_seconds, [30, 120, 300, 600])
        self.assertEqual(config.deadman_stale_minutes, 10)
        self.assertEqual(config.heartbeat_time, "07:00")
        self.assertEqual(config.timezone, "Asia/Manila")
        self.assertEqual(config.jev_url, "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(config.jev_model, "~typesafe/jev-latest")
        self.assertEqual(config.jev_timeout_seconds, 30)
        self.assertEqual(config.t_link, 0.90)
        self.assertEqual(config.t_hint, 0.50)

    def test_rollout_controls_default_off_independently(self):
        config = make()
        self.assertFalse(config.dry_run)
        self.assertFalse(config.deliverable_link_enabled)
        self.assertFalse(make(dry_run=True).deliverable_link_enabled)
        self.assertFalse(make(deliverable_link_enabled=True).dry_run)


class ValidationTests(unittest.TestCase):
    def test_bad_values_fail(self):
        bad = [
            {"timezone": "Mars/Olympus"},
            {"heartbeat_time": "7am"},
            {"heartbeat_time": "24:00"},
            {"retry_schedule_seconds": []},
            {"retry_schedule_seconds": [30, 0]},
            {"t_link": 1.5},
            {"t_hint": 0.95, "t_link": 0.90},
            {"fuzzy_min_score": 101},
            {"cache_refresh_hours": 0},
            {"cache_refresh_hours": 30, "cache_stale_alert_hours": 24},
            {"owner_telegram_handle": "has space"},
            {"captured_reaction": " "},
            {"flagged_reaction": " "},
            {"department_aliases": {"TO": ""}},
            {"unknown_key": 1},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                make(**overrides)

    def test_handles_are_normalized_with_at_sign(self):
        config = make(owner_telegram_handle="cyril_owner", standing_cc_telegram_handle="@priya")
        self.assertEqual(config.owner_telegram_handle, "@cyril_owner")
        self.assertEqual(config.standing_cc_telegram_handle, "@priya")

    def test_live_keys_are_required_at_boot(self):
        with self.assertRaises(ValueError) as ctx:
            require_live_keys(make())
        for key in LIVE_REQUIRED_KEYS:
            self.assertIn(key, str(ctx.exception))


LIVE = dict(
    standing_cc_contact_page_id="c0000000-0000-4000-8000-0000000000cc",
    standing_cc_telegram_handle="@priya",
    owner_telegram_handle="@cyril_owner",
    job_orders_data_source_id="3d8fb4d5-9e59-8073-a0d0-000b77632947",
)


def live(**overrides) -> Config:
    return make(**{**LIVE, **overrides})


class ProductionReadinessTests(unittest.TestCase):
    def test_valid_configuration_boots(self):
        require_live_keys(live())
        require_live_keys(live(standing_cc_contact_page_id="c00000000000400080000000000000cc"))
        require_live_keys(live(deepseek_base_url="http://localhost:8080"))
        require_live_keys(live(notion_version="2027-01-31"))

    def test_negative_group_chat_ids_are_legitimate(self):
        require_live_keys(live(intake_chat_id=-1001234567890, ops_chat_id=-4242))

    def test_audited_invalid_values_are_rejected(self):
        bad = {
            "job_orders_data_source_id": ["", "ds-1", "not-a-uuid"],
            "contacts_data_source_id": ["", "xyz"],
            "projects_data_source_id": [""],
            "deliverables_data_source_id": [""],
            "standing_cc_contact_page_id": ["", "short"],
            "intake_topic_id": [-5],
            "ops_topic_id": [-1],
            "deepseek_base_url": ["", "ftp://x.example", "api.deepseek.com", "https://"],
            "jev_url": ["", "file:///etc/passwd"],
            "deepseek_model": ["", "  "],
            "jev_model": [""],
            "ledger_path": ["", " "],
            "notion_version": ["", "latest", "2026-3-1"],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError) as ctx:
                        require_live_keys(live(**{key: value}))
                    self.assertIn(key, str(ctx.exception))

    def test_all_problems_are_reported_without_values(self):
        with self.assertRaises(ValueError) as ctx:
            require_live_keys(live(intake_topic_id=-7, jev_url="secret-looking-value"))
        message = str(ctx.exception)
        self.assertIn("intake_topic_id", message)
        self.assertIn("jev_url", message)
        self.assertNotIn("secret-looking-value", message)

    def test_placeholders_are_distinct_from_readiness(self):
        example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        config = Config(**example)
        require_live_keys(config)  # structurally valid...
        self.assertEqual(  # ...yet still not production-ready
            set(placeholder_keys(config)),
            {
                "intake_chat_id",
                "ops_chat_id",
                "standing_cc_contact_page_id",
                "standing_cc_telegram_handle",
                "owner_telegram_handle",
            },
        )
        self.assertEqual(placeholder_keys(live()), [])


class ExampleFilesTests(unittest.TestCase):
    def test_config_example_lists_every_key_and_boots(self):
        data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(data), set(Config.model_fields))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            require_live_keys(load_config(path))

    def test_env_example_lists_every_secret(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        names = set(re.findall(r"^([A-Z_]+)=", text, re.MULTILINE))
        self.assertEqual(names, set(SECRET_KEYS))


if __name__ == "__main__":
    unittest.main()
