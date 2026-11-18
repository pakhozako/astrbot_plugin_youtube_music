"""Public guides, executable parameter examples and packaged screenshots."""

import json
import re
import struct
import tempfile
import unittest
from pathlib import Path

from core.ai_tools import YouTubeMusicTool
from core.settings import Settings
from scripts.build_release import FILES, ROOT


def json_examples(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    return [json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)]


class DocumentationTests(unittest.TestCase):
    def test_user_guides_are_packaged_and_linked_from_the_documentation_index(self):
        index = (ROOT / "docs/index.md").read_text(encoding="utf-8")
        for name in (
            "INSTALLATION.md",
            "USAGE.md",
            "CONFIGURATION.md",
            "DEPENDENCIES.md",
            "TROUBLESHOOTING.md",
        ):
            self.assertIn(f"]({name})", index)
            self.assertIn(f"docs/{name}", FILES)
        self.assertIn("docs/index.md", FILES)

    def test_configuration_example_uses_actual_schema_and_runtime_types(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        examples = json_examples("docs/CONFIGURATION.md")
        self.assertEqual(len(examples), 1)
        for example in examples:
            self.assertTrue(set(example) <= set(schema))
            settings = Settings.from_config(example, data_dir=Path(tempfile.gettempdir()))
            self.assertEqual(settings.max_duration_seconds, example["max_duration_seconds"])
            self.assertEqual(settings.max_wav_size_mb, example["max_wav_size_mb"])
            self.assertEqual(
                settings.search_result_recall_delay_seconds,
                example["search_result_recall_delay_seconds"],
            )

    def test_ai_tool_reference_and_examples_match_registered_parameter_schema(self):
        parameters = YouTubeMusicTool().parameters
        properties = parameters["properties"]
        text = (ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
        actions = re.findall(r"^\| `(\w+)` \|", text, re.MULTILINE)
        self.assertCountEqual(actions, properties["action"]["enum"])
        examples = json_examples("docs/USAGE.md")
        self.assertEqual(len(examples), 2)
        for example in examples:
            self.assertTrue(set(example) <= set(properties))
            self.assertTrue(set(parameters["required"]) <= set(example))
            self.assertIn(example["action"], properties["action"]["enum"])
            if "query" in example:
                self.assertIsInstance(example["query"], str)
                self.assertLessEqual(len(example["query"]), properties["query"]["maxLength"])
            if "index" in example:
                self.assertIs(type(example["index"]), int)
                self.assertGreaterEqual(example["index"], properties["index"]["minimum"])
                self.assertLessEqual(example["index"], properties["index"]["maximum"])

    def test_readme_screenshots_are_packaged_pngs_at_documented_viewport_sizes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        images = re.findall(r"!\[[^\]]+\]\((docs/images/[^)]+)\)", readme)
        expected = {
            "docs/images/desktop-search.png": (1440, 900),
            "docs/images/desktop-library.png": (1440, 900),
            "docs/images/tablet-maintenance.png": (834, 1112),
            "docs/images/mobile-queue.png": (390, 844),
        }
        self.assertCountEqual(images, expected)
        for name, dimensions in expected.items():
            self.assertIn(name, FILES)
            data = (ROOT / name).read_bytes()
            self.assertGreater(len(data), 10000)
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(data[12:16], b"IHDR")
            self.assertEqual(struct.unpack(">II", data[16:24]), dimensions)
