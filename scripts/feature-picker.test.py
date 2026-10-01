#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("feature-picker.py")
SPEC = importlib.util.spec_from_file_location("feature_picker", MODULE_PATH)
feature_picker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
import sys
sys.modules[SPEC.name] = feature_picker
SPEC.loader.exec_module(feature_picker)


class FeaturePickerModelTests(unittest.TestCase):
    def feature(self, feature_id, *, requires=(), conflicts=()):
        return feature_picker.Feature(
            id=feature_id,
            title=feature_id.upper(),
            description="",
            requires=tuple(requires),
            conflicts=tuple(conflicts),
        )

    def test_last_selection_wins_and_blocks_conflict(self):
        features = {
            "isolation": self.feature("isolation", conflicts=("socket",)),
            "socket": self.feature("socket"),
        }
        model = feature_picker.SelectionModel(features, ["socket"])
        changes = model.enable("isolation")

        self.assertEqual(model.ordered_selected(), ["isolation"])
        self.assertEqual(changes["disabled"], ["socket"])
        self.assertEqual(model.blocked_by("socket"), ["isolation"])

    def test_dependency_is_automatic_and_locked(self):
        features = {
            "base": self.feature("base"),
            "consumer": self.feature("consumer", requires=("base",)),
        }
        model = feature_picker.SelectionModel(features)
        changes = model.enable("consumer")

        self.assertEqual(model.ordered_selected(), ["base", "consumer"])
        self.assertEqual(changes["required"], ["base"])
        self.assertEqual(model.required_by("base"), ["consumer"])
        self.assertNotIn("base", model.requested)

    def test_disabling_dependency_disables_dependents(self):
        features = {
            "base": self.feature("base"),
            "consumer": self.feature("consumer", requires=("base",)),
        }
        model = feature_picker.SelectionModel(features, ["consumer"])
        changes = model.disable("base")

        self.assertEqual(model.ordered_selected(), [])
        self.assertEqual(changes["disabled"], ["consumer"])

    def test_conflict_with_dependency_disables_requesting_root(self):
        features = {
            "base": self.feature("base"),
            "consumer": self.feature("consumer", requires=("base",)),
            "exclusive": self.feature("exclusive", conflicts=("base",)),
        }
        model = feature_picker.SelectionModel(features, ["consumer"])
        changes = model.enable("exclusive")

        self.assertEqual(model.ordered_selected(), ["exclusive"])
        self.assertEqual(changes["disabled"], ["consumer"])

    def test_conflicts_are_symmetric_for_ui(self):
        features = {
            "a": self.feature("a", conflicts=("b",)),
            "b": self.feature("b"),
        }
        model = feature_picker.SelectionModel(features, ["a"])
        self.assertEqual(model.blocked_by("b"), ["a"])

    def test_dependency_conflict_blocks_feature_row(self):
        features = {
            "a": self.feature("a", conflicts=("base",)),
            "base": self.feature("base"),
            "consumer": self.feature("consumer", requires=("base",)),
        }
        model = feature_picker.SelectionModel(features, ["a"])
        self.assertEqual(model.blocked_by("consumer"), ["a"])

    def test_impossible_dependency_conflict_is_rejected(self):
        features = {
            "a": self.feature("a", requires=("b",), conflicts=("b",)),
            "b": self.feature("b"),
        }
        with self.assertRaises(feature_picker.SelectionError):
            feature_picker.SelectionModel(features)

    def test_manifest_discovery_and_current_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            for feature_id in ("a", "b"):
                directory = root / feature_id
                directory.mkdir()
                (directory / "README.md").write_text("# feature\n")
                manifest = {
                    "id": feature_id,
                    "title": feature_id.upper(),
                    "description": f"{feature_id} description",
                    "defaultEnabled": False,
                }
                if feature_id == "a":
                    manifest["conflicts"] = ["b"]
                (directory / "feature.json").write_text(json.dumps(manifest))

            config = root / "features.json"
            config.write_text(json.dumps({"enabled": ["b"]}))

            features = feature_picker.discover_features(root)
            current = feature_picker.read_current_selection(config, features)

            self.assertEqual(list(features), ["a", "b"])
            self.assertEqual(current, ["b"])


if __name__ == "__main__":
    unittest.main()
