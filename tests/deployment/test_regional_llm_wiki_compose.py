import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TARGET = "/mnt/regional-llm-wiki-runtime"


class RegionalWikiComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load(
            (ROOT / "docker/docker-compose.yaml").read_text()
        )

    def test_gateway_mount_is_read_only_and_fail_closed(self):
        mounts = self.compose["services"]["gateway"]["volumes"]
        matches = [
            item for item in mounts
            if isinstance(item, dict) and item.get("target") == TARGET
        ]
        self.assertEqual(len(matches), 1)
        mount = matches[0]
        self.assertTrue(mount["read_only"])
        self.assertEqual(mount["type"], "bind")
        self.assertFalse(mount["bind"]["create_host_path"])
        self.assertTrue(mount["source"].startswith("${REGIONAL_LLM_WIKI_COMPAT_ROOT:?"))

    def test_legacy_mount_is_unchanged(self):
        self.assertIn(
            "/opt/regional_wiki_kb:/mnt/knowledge:ro",
            self.compose["services"]["gateway"]["volumes"],
        )

    def test_no_other_service_gets_wiki_mount(self):
        for name, service in self.compose["services"].items():
            if name == "gateway":
                continue
            for mount in service.get("volumes", []):
                value = str(mount)
                self.assertNotIn(TARGET, value, name)
                self.assertNotIn("REGIONAL_LLM_WIKI_COMPAT_ROOT", value, name)

    def test_removed_overlay_has_no_active_references(self):
        old_name = "docker-compose.regional-llm-wiki.yaml"
        self.assertFalse((ROOT / "docker" / old_name).exists())
        for path in [
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "docs/regional-llm-wiki-schema1-compat-deploy.md",
        ]:
            self.assertNotIn(old_name, path.read_text(), str(path))

    def test_path_variable_is_documented(self):
        self.assertIn("REGIONAL_LLM_WIKI_COMPAT_ROOT=", (ROOT / ".env.example").read_text())
        guide = (ROOT / "docs/regional-llm-wiki-schema1-compat-deploy.md").read_text()
        self.assertIn("up -d --no-deps --force-recreate gateway", guide)
        self.assertIn("previous base Compose", guide)


if __name__ == "__main__":
    unittest.main()
