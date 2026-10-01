#!/usr/bin/env python3
"""Smart GTK feature picker for ChatGPT Community Linux features."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Iterable

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,63}$")
DEFAULT_PACKAGE_NAME = "codex-desktop"
RESERVED_PACKAGE_NAMES = {"chatgpt", "codex", "codex-update-manager"}
ISOLATION_FEATURE_ID = "community-profile-isolation"
ISOLATION_DEFAULTS = {
    "codexHome": "~/.codex-community",
    "electronUserDataPath": "~/.config/Codex-Community",
}


class SelectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    description: str
    requires: tuple[str, ...]
    conflicts: tuple[str, ...]
    local: bool = False


def _normalize_ids(value, *, field: str, manifest: pathlib.Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SelectionError(f"{manifest}: {field} must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not ID_RE.fullmatch(item):
            raise SelectionError(f"{manifest}: invalid {field} feature id: {item!r}")
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def discover_features(root: pathlib.Path) -> dict[str, Feature]:
    root = root.resolve()
    candidates: list[tuple[bool, pathlib.Path]] = []
    reserved = {"local", "README.md", "features.example.json", "features.json", "compatibility.json"}

    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if child.name.startswith(".") or child.name in reserved or not child.is_dir():
                continue
            manifest = child / "feature.json"
            if manifest.is_file():
                candidates.append((False, manifest))

    local_root = root / "local"
    if local_root.is_dir():
        for child in sorted(local_root.iterdir(), key=lambda path: path.name):
            if child.name.startswith(".") or not child.is_dir():
                continue
            manifest = child / "feature.json"
            if manifest.is_file():
                candidates.append((True, manifest))

    features: dict[str, Feature] = {}
    for local, manifest in candidates:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SelectionError(f"Could not read {manifest}: {exc}") from exc
        if not isinstance(data, dict):
            raise SelectionError(f"{manifest}: manifest must be a JSON object")

        feature_id = data.get("id")
        if not isinstance(feature_id, str) or not ID_RE.fullmatch(feature_id):
            raise SelectionError(f"{manifest}: invalid feature id")
        if data.get("internal") is True:
            continue
        if feature_id in features:
            raise SelectionError(f"Duplicate feature id: {feature_id}")

        readme = manifest.parent / "README.md"
        if not readme.is_file():
            raise SelectionError(f"{manifest}: README.md is required")

        features[feature_id] = Feature(
            id=feature_id,
            title=str(data.get("title") or data.get("name") or feature_id),
            description=str(data.get("description") or ""),
            requires=_normalize_ids(data.get("requires"), field="requires", manifest=manifest),
            conflicts=_normalize_ids(data.get("conflicts"), field="conflicts", manifest=manifest),
            local=local,
        )

    for feature in features.values():
        for required in feature.requires:
            if required not in features:
                raise SelectionError(
                    f"Feature '{feature.id}' requires unavailable feature '{required}'"
                )
        for conflict in feature.conflicts:
            if conflict not in features:
                raise SelectionError(
                    f"Feature '{feature.id}' conflicts with unavailable feature '{conflict}'"
                )
    return features


def read_current_selection(config_path: pathlib.Path, features: dict[str, Feature]) -> list[str]:
    if not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SelectionError(f"Could not read {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SelectionError(f"{config_path}: config must be a JSON object")
    enabled = data.get("enabled", [])
    if not isinstance(enabled, list):
        raise SelectionError(f"{config_path}: enabled must be an array")

    result: list[str] = []
    seen: set[str] = set()
    for item in enabled:
        if not isinstance(item, str) or not ID_RE.fullmatch(item):
            raise SelectionError(f"{config_path}: invalid enabled feature id: {item!r}")
        if item in features and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def read_current_feature_settings(config_path: pathlib.Path) -> dict[str, dict]:
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SelectionError(f"Could not read {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SelectionError(f"{config_path}: config must be a JSON object")
    raw_settings = data.get("settings", {})
    if raw_settings is None:
        return {}
    if not isinstance(raw_settings, dict):
        raise SelectionError(f"{config_path}: settings must be an object")
    result: dict[str, dict] = {}
    for feature_id, value in raw_settings.items():
        if isinstance(feature_id, str) and isinstance(value, dict):
            result[feature_id] = dict(value)
    return result


def read_current_installer_options(config_path: pathlib.Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SelectionError(f"Could not read {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SelectionError(f"{config_path}: config must be a JSON object")
    installer = data.get("installer", {})
    if installer is None:
        return {}
    if not isinstance(installer, dict):
        raise SelectionError(f"{config_path}: installer must be an object")
    return dict(installer)


def validate_profile_path_spec(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise SelectionError(f"{label} must not be empty")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise SelectionError(f"{label} contains unsupported control characters")
    if value == "~" or value.startswith("~/") or value.startswith("/"):
        return value
    raise SelectionError(f"{label} must start with ~/ or /")


def validate_installation_name(value: str) -> str:
    value = value.strip()
    if not PACKAGE_NAME_RE.fullmatch(value):
        raise SelectionError(
            "Installation name must use lowercase letters, numbers, '.', '+', or '-' "
            "and must start with a letter or number"
        )
    if value in RESERVED_PACKAGE_NAMES:
        raise SelectionError(
            f"Installation name '{value}' is reserved and may conflict with official "
            "ChatGPT/Codex commands"
        )
    return value


def write_feature_config(
    config_path: pathlib.Path,
    selected: Iterable[str],
    incoming_settings: dict[str, dict],
    installer_options: dict | None = None,
) -> None:
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SelectionError(f"Could not read {config_path}: {exc}") from exc
    else:
        data = {}
    if not isinstance(data, dict):
        raise SelectionError(f"{config_path}: config must be a JSON object")

    data["enabled"] = list(selected)
    settings = data.get("settings", {})
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise SelectionError(f"{config_path}: settings must be an object")
    for feature_id, value in incoming_settings.items():
        if not isinstance(feature_id, str) or not isinstance(value, dict):
            raise SelectionError("Invalid installer feature settings")
        settings[feature_id] = dict(value)
    if settings:
        data["settings"] = settings
    if installer_options is not None:
        if not isinstance(installer_options, dict):
            raise SelectionError("Installer options must be an object")
        data["installer"] = dict(installer_options)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_name(config_path.name + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, config_path)


def os_release_tokens(path: pathlib.Path = pathlib.Path("/etc/os-release")) -> set[str]:
    tokens: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return tokens
    for line in lines:
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if key not in {"ID", "ID_LIKE"}:
            continue
        value = raw.strip().strip('"').strip("'").lower()
        tokens.update(part for part in value.split() if part)
    return tokens


def detect_package_format() -> str:
    tokens = os_release_tokens()
    if tokens & {"arch", "manjaro", "endeavouros"}:
        return "pacman"
    if tokens & {"fedora", "rhel", "centos", "suse", "opensuse"}:
        return "rpm"
    if tokens & {"debian", "ubuntu", "linuxmint", "pop"}:
        return "deb"
    if shutil.which("dpkg-deb"):
        return "deb"
    if shutil.which("rpm"):
        return "rpm"
    if shutil.which("pacman"):
        return "pacman"
    raise SelectionError("No supported native package manager was detected")


def build_install_stages(options: dict) -> list[tuple[str, list[str]]]:
    package_name = validate_installation_name(str(options["packageName"]))
    updater = bool(options["withUpdater"])
    if updater and package_name != DEFAULT_PACKAGE_NAME:
        raise SelectionError(
            "Automatic updates require installation name 'codex-desktop'"
        )
    stages: list[tuple[str, list[str]]] = []
    if bool(options.get("installDependencies", True)):
        stages.append(
            ("Installing build dependencies", ["bash", "scripts/install-deps.sh"])
        )
    stages.extend([
        ("Building feature helpers", ["make", "build-native-feature-helpers"]),
        ("Building ChatGPT Community", ["make", "build-app"]),
        (
            "Building native package",
            [
                "make",
                "package",
                f"PACKAGE_WITH_UPDATER={1 if updater else 0}",
                f"PACKAGE_NAME={package_name}",
            ],
        ),
    ])
    return stages


def latest_package(repo_root: pathlib.Path, package_name: str, package_format: str) -> pathlib.Path:
    dist = repo_root / "dist"
    if package_format == "deb":
        candidates = list(dist.glob(f"{package_name}_*.deb"))
    elif package_format == "rpm":
        candidates = list(dist.glob(f"{package_name}-*.rpm"))
    else:
        candidates = [
            path
            for path in dist.glob(f"{package_name}-*.pkg.tar.*")
            if not path.name.endswith((".sig", ".sha256", ".part"))
            and "latest" not in path.name
        ]
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        raise SelectionError(f"No built {package_format} package found for {package_name}")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


class SelectionModel:
    def __init__(self, features: dict[str, Feature], current: Iterable[str] = ()) -> None:
        self.features = dict(features)
        self.order = list(features)
        self.requested = {feature_id for feature_id in current if feature_id in features}
        self._conflicts = {feature_id: set() for feature_id in features}
        for feature in features.values():
            for other in feature.conflicts:
                self._conflicts[feature.id].add(other)
                self._conflicts[other].add(feature.id)
        self._validate_graph()
        self._validate_selection(self.requested)

    def _closure_for_root(self, root: str) -> set[str]:
        if root not in self.features:
            raise SelectionError(f"Unknown feature: {root}")
        result: set[str] = set()
        visiting: list[str] = []

        def visit(feature_id: str) -> None:
            if feature_id in result:
                return
            if feature_id in visiting:
                cycle = visiting[visiting.index(feature_id):] + [feature_id]
                raise SelectionError(f"Dependency cycle: {' -> '.join(cycle)}")
            visiting.append(feature_id)
            for required in self.features[feature_id].requires:
                visit(required)
            visiting.pop()
            result.add(feature_id)

        visit(root)
        return result

    def _closure(self, roots: Iterable[str]) -> set[str]:
        result: set[str] = set()
        for root in roots:
            result.update(self._closure_for_root(root))
        return result

    def _validate_graph(self) -> None:
        for feature_id in self.features:
            closure = self._closure_for_root(feature_id)
            for selected in closure:
                overlap = closure & self._conflicts[selected]
                if overlap:
                    other = sorted(overlap)[0]
                    raise SelectionError(
                        f"Feature '{feature_id}' has an impossible dependency/conflict "
                        f"combination involving '{selected}' and '{other}'"
                    )

    def _validate_selection(self, roots: Iterable[str]) -> None:
        selected = self._closure(roots)
        for feature_id in selected:
            overlap = selected & self._conflicts[feature_id]
            if overlap:
                raise SelectionError(
                    f"Conflicting selection: '{feature_id}' and '{sorted(overlap)[0]}'"
                )

    @property
    def selected(self) -> set[str]:
        return self._closure(self.requested)

    def ordered_selected(self) -> list[str]:
        selected = self.selected
        return [feature_id for feature_id in self.order if feature_id in selected]

    def requested_roots_requiring(self, target: str) -> set[str]:
        result: set[str] = set()
        for root in self.requested:
            if target in self._closure_for_root(root):
                result.add(root)
        return result

    def required_by(self, feature_id: str) -> list[str]:
        roots = self.requested_roots_requiring(feature_id) - {feature_id}
        return [feature_id_ for feature_id_ in self.order if feature_id_ in roots]

    def blocked_by(self, feature_id: str) -> list[str]:
        if feature_id in self.selected:
            return []
        blocked: set[str] = set()
        selected = self.selected
        for candidate in self._closure_for_root(feature_id):
            blocked.update(selected & self._conflicts[candidate])
        return [item for item in self.order if item in blocked]

    def enable(self, feature_id: str) -> dict[str, list[str]]:
        if feature_id not in self.features:
            raise SelectionError(f"Unknown feature: {feature_id}")

        new_closure = self._closure_for_root(feature_id)
        conflicting_selected: set[str] = set()
        selected_before = self.selected
        for candidate in new_closure:
            conflicting_selected.update(selected_before & self._conflicts[candidate])

        removed_roots: set[str] = set()
        for conflict in conflicting_selected:
            removed_roots.update(self.requested_roots_requiring(conflict))

        self.requested.difference_update(removed_roots)
        self.requested.add(feature_id)
        self._validate_selection(self.requested)

        selected_after = self.selected
        auto_added = selected_after - selected_before - {feature_id}
        return {
            "disabled": [item for item in self.order if item in removed_roots],
            "required": [item for item in self.order if item in auto_added],
        }

    def disable(self, feature_id: str) -> dict[str, list[str]]:
        if feature_id not in self.features:
            raise SelectionError(f"Unknown feature: {feature_id}")
        removed_roots = self.requested_roots_requiring(feature_id)
        self.requested.difference_update(removed_roots)
        self._validate_selection(self.requested)
        return {
            "disabled": [item for item in self.order if item in removed_roots],
            "required": [],
        }


def _display_names(model: SelectionModel, ids: Iterable[str]) -> str:
    return ", ".join(model.features[item].title for item in ids)


def run_gtk_picker(
    model: SelectionModel,
    current_settings: dict[str, dict],
    current_installer_options: dict,
    *,
    install_mode: bool = False,
    repo_root: pathlib.Path | None = None,
    config_path: pathlib.Path | None = None,
) -> dict | None:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    if install_mode and (repo_root is None or config_path is None):
        raise SelectionError("Installer mode requires repo_root and config_path")
    if repo_root is not None:
        repo_root = repo_root.resolve()
    if config_path is not None:
        config_path = config_path.resolve()

    result: dict[str, object] = {"selected": None, "settings": {}, "installOptions": {}}

    class PickerWindow(Gtk.ApplicationWindow):
        def __init__(self, app: Gtk.Application) -> None:
            super().__init__(application=app)
            self.set_title("ChatGPT Community Installer")
            self.set_default_size(820, 680)
            self.refreshing = False
            self.install_in_progress = False
            self.rows: dict[str, tuple[Gtk.ListBoxRow, Gtk.CheckButton, Gtk.Label]] = {}
            self.dim_targets: dict[str, tuple[Gtk.Widget, ...]] = {}

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            outer.set_margin_top(20)
            outer.set_margin_bottom(20)
            outer.set_margin_start(24)
            outer.set_margin_end(24)
            self.feature_page = outer
            self.set_child(outer)

            heading = Gtk.Label()
            heading.set_markup("<span size='x-large' weight='bold'>Choose optional Linux features</span>")
            heading.set_xalign(0)
            outer.append(heading)

            intro = Gtk.Label(
                label=(
                    "Dependencies are selected automatically. When you select a feature, "
                    "features that conflict with it are turned off and become unavailable "
                    "until the selected feature is disabled."
                )
            )
            intro.set_wrap(True)
            intro.set_xalign(0)
            outer.append(intro)

            scroller = Gtk.ScrolledWindow()
            scroller.set_vexpand(True)
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            outer.append(scroller)

            list_box = Gtk.ListBox()
            list_box.set_selection_mode(Gtk.SelectionMode.NONE)
            list_box.set_activate_on_single_click(True)
            list_box.connect("row-activated", self._on_row_activated)
            list_box.add_css_class("boxed-list")
            scroller.set_child(list_box)

            for feature_id in model.order:
                feature = model.features[feature_id]
                row = Gtk.ListBoxRow()
                row.set_activatable(True)
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
                box.set_margin_top(12)
                box.set_margin_bottom(12)
                box.set_margin_start(14)
                box.set_margin_end(14)
                row.set_child(box)

                check = Gtk.CheckButton()
                check.set_valign(Gtk.Align.START)
                check.connect("toggled", self._on_toggled, feature_id)
                box.append(check)

                text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                text_box.set_hexpand(True)
                box.append(text_box)

                title = Gtk.Label()
                suffix = "  [local]" if feature.local else ""
                safe_title = GLib.markup_escape_text(feature.title)
                safe_suffix = GLib.markup_escape_text(suffix)
                title.set_markup(f"<b>{safe_title}</b>{safe_suffix}")
                title.set_xalign(0)
                text_box.append(title)
                dim_targets: list[Gtk.Widget] = [title]

                if feature.description:
                    description = Gtk.Label(label=feature.description)
                    description.set_wrap(True)
                    description.set_xalign(0)
                    description.add_css_class("dim-label")
                    text_box.append(description)
                    dim_targets.append(description)

                relations: list[str] = []
                if feature.requires:
                    relations.append(
                        "Requires: " + _display_names(model, feature.requires)
                    )
                if feature.conflicts:
                    relations.append(
                        "Conflicts: " + _display_names(model, feature.conflicts)
                    )
                if relations:
                    relation = Gtk.Label(label=" • ".join(relations))
                    relation.set_wrap(True)
                    relation.set_xalign(0)
                    relation.add_css_class("dim-label")
                    text_box.append(relation)
                    dim_targets.append(relation)

                status = Gtk.Label()
                status.set_wrap(True)
                status.set_xalign(0)
                text_box.append(status)
                self.rows[feature_id] = (row, check, status)
                self.dim_targets[feature_id] = tuple(dim_targets)
                row.feature_id = feature_id
                list_box.append(row)

            self.notice = Gtk.Label()
            self.notice.set_wrap(True)
            self.notice.set_xalign(0)
            outer.append(self.notice)

            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            outer.append(footer)

            self.count_label = Gtk.Label()
            self.count_label.set_xalign(0)
            self.count_label.set_hexpand(True)
            footer.append(self.count_label)

            cancel = Gtk.Button(label="Cancel")
            cancel.connect("clicked", self._cancel)
            footer.append(cancel)

            apply_button = Gtk.Button(label="Continue")
            apply_button.add_css_class("suggested-action")
            apply_button.connect("clicked", self._apply)
            footer.append(apply_button)

            self.connect("close-request", self._close_requested)
            self._refresh()

        def _on_row_activated(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
            feature_id = getattr(row, "feature_id", None)
            if not isinstance(feature_id, str):
                return
            _row, check, _status = self.rows[feature_id]
            if not check.get_sensitive():
                return
            check.set_active(not check.get_active())

        def _on_toggled(self, check: Gtk.CheckButton, feature_id: str) -> None:
            if self.refreshing:
                return
            try:
                if check.get_active():
                    changes = model.enable(feature_id)
                    notes: list[str] = []
                    if changes["disabled"]:
                        notes.append(
                            "Disabled because of conflicts: "
                            + _display_names(model, changes["disabled"])
                        )
                    if changes["required"]:
                        notes.append(
                            "Enabled required features: "
                            + _display_names(model, changes["required"])
                        )
                    self.notice.set_text(" ".join(notes))
                else:
                    changes = model.disable(feature_id)
                    disabled = [item for item in changes["disabled"] if item != feature_id]
                    if disabled:
                        self.notice.set_text(
                            "Also disabled dependent features: "
                            + _display_names(model, disabled)
                        )
                    else:
                        self.notice.set_text("")
            except SelectionError as exc:
                self.notice.set_text(str(exc))
            self._refresh()

        def _refresh(self) -> None:
            self.refreshing = True
            selected = model.selected
            for feature_id, (row, check, status) in self.rows.items():
                active = feature_id in selected
                blocked = model.blocked_by(feature_id)
                required_by = model.required_by(feature_id)
                auto_required = active and feature_id not in model.requested and bool(required_by)

                check.set_active(active)
                check.set_sensitive(not blocked and not auto_required)
                check.set_opacity(0.38 if blocked else 1.0)
                row.set_sensitive(True)
                row.set_activatable(not blocked and not auto_required)
                row.set_opacity(1.0)
                for widget in self.dim_targets[feature_id]:
                    widget.set_opacity(0.30 if blocked else 1.0)
                status.set_opacity(1.0)

                status.remove_css_class("error")
                if blocked:
                    status.add_css_class("error")
                    status.set_text(
                        "Unavailable — conflicts with: " + _display_names(model, blocked)
                    )
                elif auto_required:
                    status.set_text(
                        "Required by: " + _display_names(model, required_by)
                    )
                elif active and required_by:
                    status.set_text(
                        "Selected • also required by: " + _display_names(model, required_by)
                    )
                elif active:
                    status.set_text("Selected")
                else:
                    status.set_text("Available")

            self.count_label.set_text(f"{len(selected)} feature(s) selected")
            self.refreshing = False

        def _apply(self, _button: Gtk.Button) -> None:
            if ISOLATION_FEATURE_ID in model.selected:
                self._show_profile_paths()
                return
            self._after_profile({})

        def _show_profile_paths(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)

            heading = Gtk.Label()
            heading.set_markup(
                "<span size='x-large' weight='bold'>Community profile paths</span>"
            )
            heading.set_xalign(0)
            page.append(heading)

            intro = Gtk.Label(
                label=(
                    "Community Profile Isolation is enabled. These are the private "
                    "state locations used by this Community installation. Keep the "
                    "defaults or choose different roots."
                )
            )
            intro.set_wrap(True)
            intro.set_xalign(0)
            page.append(intro)

            settings = dict(ISOLATION_DEFAULTS)
            pending = getattr(self, "pending_settings", {})
            existing = pending.get(
                ISOLATION_FEATURE_ID,
                current_settings.get(ISOLATION_FEATURE_ID, {}),
            )
            for key in ISOLATION_DEFAULTS:
                value = existing.get(key)
                if isinstance(value, str) and value.strip():
                    settings[key] = value.strip()

            codex_label = Gtk.Label(label="Codex state root")
            codex_label.set_xalign(0)
            codex_label.add_css_class("heading")
            page.append(codex_label)
            self.codex_home_entry = Gtk.Entry()
            self.codex_home_entry.set_text(settings["codexHome"])
            self.codex_home_entry.set_hexpand(True)
            page.append(self.codex_home_entry)

            codex_hint = Gtk.Label(
                label="Default: ~/.codex-community  •  Accepts ~/... or an absolute path"
            )
            codex_hint.set_xalign(0)
            codex_hint.add_css_class("dim-label")
            page.append(codex_hint)

            electron_label = Gtk.Label(label="Electron profile root")
            electron_label.set_xalign(0)
            electron_label.add_css_class("heading")
            page.append(electron_label)
            self.electron_profile_entry = Gtk.Entry()
            self.electron_profile_entry.set_text(settings["electronUserDataPath"])
            self.electron_profile_entry.set_hexpand(True)
            page.append(self.electron_profile_entry)

            electron_hint = Gtk.Label(
                label="Default: ~/.config/Codex-Community  •  Accepts ~/... or an absolute path"
            )
            electron_hint.set_xalign(0)
            electron_hint.add_css_class("dim-label")
            page.append(electron_hint)

            install_note = Gtk.Label(
                label=(
                    "Native application root: /opt/codex-desktop. A custom installation "
                    "name/root is configured in the installation-options step because "
                    "automatic updates currently require the default package identity."
                )
            )
            install_note.set_wrap(True)
            install_note.set_xalign(0)
            install_note.add_css_class("dim-label")
            page.append(install_note)

            self.profile_error = Gtk.Label()
            self.profile_error.set_wrap(True)
            self.profile_error.set_xalign(0)
            self.profile_error.add_css_class("error")
            page.append(self.profile_error)

            spacer = Gtk.Box()
            spacer.set_vexpand(True)
            page.append(spacer)

            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            page.append(footer)

            back = Gtk.Button(label="Back")
            back.connect("clicked", self._back_to_features)
            footer.append(back)

            filler = Gtk.Box()
            filler.set_hexpand(True)
            footer.append(filler)

            apply_button = Gtk.Button(label="Apply selection")
            apply_button.add_css_class("suggested-action")
            apply_button.connect("clicked", self._finish_profile)
            footer.append(apply_button)

            self.set_child(page)

        def _back_to_features(self, _button: Gtk.Button) -> None:
            self.set_child(self.feature_page)

        def _finish_profile(self, _button: Gtk.Button) -> None:
            try:
                codex_home = validate_profile_path_spec(
                    self.codex_home_entry.get_text(),
                    "Codex state root",
                )
                electron_profile = validate_profile_path_spec(
                    self.electron_profile_entry.get_text(),
                    "Electron profile root",
                )
            except SelectionError as exc:
                self.profile_error.set_text(str(exc))
                return

            self._after_profile({
                ISOLATION_FEATURE_ID: {
                    "codexHome": codex_home,
                    "electronUserDataPath": electron_profile,
                }
            })

        def _after_profile(self, settings: dict[str, dict]) -> None:
            self.pending_settings = settings
            if install_mode:
                self._show_install_options()
                return
            self._finish(settings)

        def _show_install_options(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)
            self.install_options_page = page

            heading = Gtk.Label()
            heading.set_markup(
                "<span size='x-large' weight='bold'>Installation options</span>"
            )
            heading.set_xalign(0)
            page.append(heading)

            intro = Gtk.Label(
                label=(
                    "Choose update behavior and the native package identity. "
                    "The installation root is derived as /opt/<installation-name>."
                )
            )
            intro.set_wrap(True)
            intro.set_xalign(0)
            page.append(intro)

            self.updater_check = Gtk.CheckButton(label="Include automatic updater")
            saved_updater = current_installer_options.get("withUpdater", True)
            self.updater_check.set_active(
                saved_updater if isinstance(saved_updater, bool) else True
            )
            self.updater_check.connect("toggled", self._sync_install_options)
            page.append(self.updater_check)

            self.deps_check = Gtk.CheckButton(
                label="Install or refresh build dependencies before building"
            )
            saved_deps = current_installer_options.get("installDependencies", True)
            self.deps_check.set_active(
                saved_deps if isinstance(saved_deps, bool) else True
            )
            page.append(self.deps_check)

            name_label = Gtk.Label(label="Installation name")
            name_label.set_xalign(0)
            name_label.add_css_class("heading")
            page.append(name_label)

            self.install_name_entry = Gtk.Entry()
            saved_name = current_installer_options.get(
                "packageName", DEFAULT_PACKAGE_NAME
            )
            if not isinstance(saved_name, str) or not PACKAGE_NAME_RE.fullmatch(saved_name):
                saved_name = DEFAULT_PACKAGE_NAME
            self.install_name_entry.set_text(saved_name)
            self.install_name_entry.connect("changed", self._sync_install_root)
            page.append(self.install_name_entry)

            self.install_root_label = Gtk.Label()
            self.install_root_label.set_xalign(0)
            self.install_root_label.add_css_class("dim-label")
            page.append(self.install_root_label)

            self.updater_note = Gtk.Label()
            self.updater_note.set_wrap(True)
            self.updater_note.set_xalign(0)
            page.append(self.updater_note)

            self.options_error = Gtk.Label()
            self.options_error.set_wrap(True)
            self.options_error.set_xalign(0)
            self.options_error.add_css_class("error")
            page.append(self.options_error)

            spacer = Gtk.Box()
            spacer.set_vexpand(True)
            page.append(spacer)

            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            page.append(footer)
            back = Gtk.Button(label="Back")
            back.connect("clicked", self._back_from_options)
            footer.append(back)
            filler = Gtk.Box()
            filler.set_hexpand(True)
            footer.append(filler)
            review = Gtk.Button(label="Review")
            review.add_css_class("suggested-action")
            review.connect("clicked", self._show_review)
            footer.append(review)

            self._sync_install_options()
            self.set_child(page)

        def _sync_install_options(self, _widget=None) -> None:
            updater = self.updater_check.get_active()
            if updater:
                self.install_name_entry.set_text(DEFAULT_PACKAGE_NAME)
                self.install_name_entry.set_sensitive(False)
                self.updater_note.set_text(
                    "Automatic updates currently require the default package identity "
                    "'codex-desktop'. Disable automatic updates to choose another "
                    "installation name/root."
                )
            else:
                self.install_name_entry.set_sensitive(True)
                current_name = self.install_name_entry.get_text().strip()
                custom_note = (
                    " A custom package name installs alongside any existing "
                    "'codex-desktop' package; it does not rename or remove it."
                    if current_name and current_name != DEFAULT_PACKAGE_NAME
                    else ""
                )
                self.updater_note.set_text(
                    "Manual-update mode: a custom installation name is allowed. "
                    "Rerun this installer when you want to rebuild from a newer "
                    "official package." + custom_note
                )
            self._sync_install_root()

        def _sync_install_root(self, _widget=None) -> None:
            name = self.install_name_entry.get_text().strip() or DEFAULT_PACKAGE_NAME
            self.install_root_label.set_text(f"Native application root: /opt/{name}")
            if not self.updater_check.get_active():
                custom_note = (
                    " A custom package name installs alongside any existing "
                    "'codex-desktop' package; it does not rename or remove it."
                    if name != DEFAULT_PACKAGE_NAME
                    else ""
                )
                self.updater_note.set_text(
                    "Manual-update mode: a custom installation name is allowed. "
                    "Rerun this installer when you want to rebuild from a newer "
                    "official package." + custom_note
                )

        def _back_from_options(self, _button: Gtk.Button) -> None:
            if ISOLATION_FEATURE_ID in model.selected:
                self._show_profile_paths()
            else:
                self.set_child(self.feature_page)

        def _show_review(self, _button: Gtk.Button) -> None:
            try:
                package_name = validate_installation_name(
                    self.install_name_entry.get_text()
                )
            except SelectionError as exc:
                self.options_error.set_text(str(exc))
                return

            updater = self.updater_check.get_active()
            if updater and package_name != DEFAULT_PACKAGE_NAME:
                self.options_error.set_text(
                    "Automatic updates require installation name 'codex-desktop'."
                )
                return

            self.install_options = {
                "packageName": package_name,
                "withUpdater": updater,
                "installDependencies": self.deps_check.get_active(),
                "packageFormat": detect_package_format(),
            }

            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)

            heading = Gtk.Label()
            heading.set_markup("<span size='x-large' weight='bold'>Review installation</span>")
            heading.set_xalign(0)
            page.append(heading)

            selected_names = [
                model.features[item].title for item in model.ordered_selected()
            ]
            review_lines = [
                "Features: " + (", ".join(selected_names) if selected_names else "none"),
                f"Package: {package_name}",
                f"Install root: /opt/{package_name}",
                "Updates: " + ("automatic" if updater else "manual"),
                f"Package format: {self.install_options['packageFormat']}",
            ]
            if ISOLATION_FEATURE_ID in model.selected:
                isolation = self.pending_settings.get(
                    ISOLATION_FEATURE_ID, ISOLATION_DEFAULTS
                )
                review_lines.extend([
                    f"Codex state root: {isolation.get('codexHome', ISOLATION_DEFAULTS['codexHome'])}",
                    "Electron profile root: "
                    + isolation.get(
                        "electronUserDataPath",
                        ISOLATION_DEFAULTS["electronUserDataPath"],
                    ),
                ])

            for line in review_lines:
                label = Gtk.Label(label=line)
                label.set_xalign(0)
                label.set_wrap(True)
                page.append(label)

            spacer = Gtk.Box()
            spacer.set_vexpand(True)
            page.append(spacer)
            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            page.append(footer)
            back = Gtk.Button(label="Back")
            back.connect("clicked", lambda _button: self.set_child(self.install_options_page))
            footer.append(back)
            filler = Gtk.Box()
            filler.set_hexpand(True)
            footer.append(filler)
            install = Gtk.Button(label="Build and install")
            install.add_css_class("suggested-action")
            install.connect("clicked", self._start_install)
            footer.append(install)
            self.set_child(page)

        def _start_install(self, _button: Gtk.Button) -> None:
            assert config_path is not None
            assert repo_root is not None
            try:
                write_feature_config(
                    config_path,
                    model.ordered_selected(),
                    self.pending_settings,
                    self.install_options,
                )
            except SelectionError as exc:
                self._show_install_error(str(exc))
                return

            result["selected"] = model.ordered_selected()
            result["settings"] = self.pending_settings
            result["installOptions"] = dict(self.install_options)
            self.install_in_progress = True
            self._show_progress_page()
            threading.Thread(target=self._run_install, daemon=True).start()

        def _show_progress_page(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)

            heading = Gtk.Label()
            heading.set_markup("<span size='x-large' weight='bold'>Installing ChatGPT Community</span>")
            heading.set_xalign(0)
            page.append(heading)

            self.progress_status = Gtk.Label(label="Preparing…")
            self.progress_status.set_xalign(0)
            page.append(self.progress_status)

            self.progress_bar = Gtk.ProgressBar()
            self.progress_bar.set_show_text(True)
            self.progress_bar.set_fraction(0.0)
            page.append(self.progress_bar)

            scroller = Gtk.ScrolledWindow()
            scroller.set_vexpand(True)
            page.append(scroller)
            self.log_view = Gtk.TextView()
            self.log_view.set_editable(False)
            self.log_view.set_monospace(True)
            self.log_view.set_wrap_mode(Gtk.WrapMode.CHAR)
            scroller.set_child(self.log_view)

            self.progress_close = Gtk.Button(label="Close")
            self.progress_close.set_sensitive(False)
            self.progress_close.connect("clicked", self._close_after_install)
            page.append(self.progress_close)
            self.set_child(page)

        def _append_log(self, text: str) -> bool:
            buffer = self.log_view.get_buffer()
            end = buffer.get_end_iter()
            buffer.insert(end, text)
            mark = buffer.create_mark(None, buffer.get_end_iter(), False)
            self.log_view.scroll_mark_onscreen(mark)
            return False

        def _set_progress(self, fraction: float, label: str) -> bool:
            self.progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
            self.progress_bar.set_text(f"{round(fraction * 100)}%")
            self.progress_status.set_text(label)
            return False

        def _set_install_done(self, ok: bool, message: str) -> bool:
            self.install_in_progress = False
            result["installOk"] = ok
            if ok:
                self.progress_bar.set_fraction(1.0)
                self.progress_bar.set_text("100%")
            self.progress_status.set_text(message)
            self.progress_close.set_sensitive(True)
            return False

        def _run_process(self, command: list[str], env: dict[str, str]) -> None:
            GLib.idle_add(self._append_log, "$ " + " ".join(command) + "\n")
            process = subprocess.Popen(
                command,
                cwd=str(repo_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                GLib.idle_add(self._append_log, line)
            status = process.wait()
            if status != 0:
                raise RuntimeError(
                    f"Command failed with exit code {status}: {' '.join(command)}"
                )

        def _install_artifact_command(
            self, package: pathlib.Path, package_format: str
        ) -> list[str]:
            privilege = shutil.which("pkexec") or shutil.which("sudo")
            if privilege is None:
                raise SelectionError("pkexec or sudo is required to install the package")
            if package_format == "deb":
                return [privilege, "dpkg", "-i", str(package)]
            if package_format == "rpm":
                if shutil.which("dnf"):
                    return [privilege, "dnf", "install", "-y", str(package)]
                return [privilege, "rpm", "-Uvh", str(package)]
            return [privilege, "pacman", "-U", "--noconfirm", str(package)]

        def _run_install(self) -> None:
            assert repo_root is not None
            assert config_path is not None
            options = self.install_options
            package_name = options["packageName"]
            updater = options["withUpdater"]
            package_format = options["packageFormat"]

            env = os.environ.copy()
            env["PATH"] = str(pathlib.Path.home() / ".cargo" / "bin") + os.pathsep + env.get("PATH", "")
            env["CODEX_LINUX_FEATURES_CONFIG"] = str(config_path)
            env["CODEX_PRIVILEGE_HELPER"] = "pkexec"

            stages = build_install_stages(options)
            total = len(stages) + 1

            try:
                for index, (label, command) in enumerate(stages):
                    GLib.idle_add(self._set_progress, index / total, label)
                    self._run_process(command, env)

                package = latest_package(repo_root, package_name, package_format)
                GLib.idle_add(
                    self._set_progress,
                    (total - 1) / total,
                    f"Installing {package.name}",
                )
                self._run_process(
                    self._install_artifact_command(package, package_format),
                    env,
                )
                GLib.idle_add(
                    self._set_install_done,
                    True,
                    "Installation complete.",
                )
            except Exception as exc:
                GLib.idle_add(self._append_log, f"\nERROR: {exc}\n")
                GLib.idle_add(
                    self._set_install_done,
                    False,
                    "Installation failed. Review the log below.",
                )

        def _show_install_error(self, message: str) -> None:
            self.options_error.set_text(message)
            self.set_child(self.install_options_page)

        def _finish(self, settings: dict[str, dict]) -> None:
            result["selected"] = model.ordered_selected()
            result["settings"] = settings
            self.get_application().quit()

        def _close_after_install(self, _button: Gtk.Button) -> None:
            self.get_application().quit()

        def _cancel(self, _button: Gtk.Button) -> None:
            result["selected"] = None
            self.get_application().quit()

        def _close_requested(self, _window: Gtk.Window) -> bool:
            if self.install_in_progress:
                self.progress_status.set_text(
                    "Installation is still running. Wait for it to finish before closing."
                )
                return True
            result["selected"] = None
            self.get_application().quit()
            return False

    app = Gtk.Application(application_id=None)

    def activate(application: Gtk.Application) -> None:
        PickerWindow(application).present()

    app.connect("activate", activate)
    app.run([])
    return None if result["selected"] is None else result


def gtk_probe() -> int:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except Exception:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-root", type=pathlib.Path)
    parser.add_argument("--config", type=pathlib.Path)
    parser.add_argument("--repo-root", type=pathlib.Path)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    if args.probe:
        return gtk_probe()
    if args.features_root is None or args.config is None:
        parser.error("--features-root and --config are required")

    try:
        features = discover_features(args.features_root)
        current = read_current_selection(args.config, features)
        current_settings = read_current_feature_settings(args.config)
        current_installer_options = read_current_installer_options(args.config)
        model = SelectionModel(features, current)
        selection_result = run_gtk_picker(
            model,
            current_settings,
            current_installer_options,
            install_mode=args.install,
            repo_root=args.repo_root,
            config_path=args.config,
        )
    except SelectionError as exc:
        print(f"[setup][ERROR] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[setup][ERROR] Smart feature picker failed: {exc}", file=sys.stderr)
        return 1

    if selection_result is None:
        return 2
    if args.install:
        return 0 if selection_result.get("installOk") is True else 1
    print(json.dumps(selection_result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
