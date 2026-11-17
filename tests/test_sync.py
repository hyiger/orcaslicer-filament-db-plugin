import io
import json
import sys
import urllib.request
import threading

import pytest

from conftest import (BUNDLE_ID, CALIBRATED_ID, MOVED_PREFIX, PLAIN_ID, PLATE_ROWS, FakePreset, NotificationLevel,
                      PluginResult)

PLAIN = f"_local/{BUNDLE_ID}/Plain PLA"
CALIBRATED = f"_local/{BUNDLE_ID}/Calibrated PLA"
REMOVED_TEXT = ("Filament DB no longer has this filament. Discard the edits to it, "
                "then run Pull from Filament DB to remove its synced preset.")


def bundle_preset(app, name):
    return json.loads((app.bundle_dir / "filament" / f"{name}.json").read_text(encoding="utf-8"))


def edit(app, name, **edits):
    app.pull()
    app.filaments.select(name, edits)


class TestPull:
    def test_writes_a_preset_per_filament_and_reloads(self, app):
        result = app.pull()
        assert result.status is PluginResult.Success
        assert result.message == "Pulled 2 filaments from Filament DB for a 0.4 mm nozzle."
        assert {PLAIN, CALIBRATED} <= set(app.filaments.presets)
        assert app.notifications[-1] == (NotificationLevel.RegularNotificationLevel, result.message)

    def test_a_reload_accepts_what_the_export_needs_changed(self, app):
        app.pull()
        preset = bundle_preset(app, "Plain PLA")
        assert preset["version"]
        assert preset["default_filament_colour"] == ["#112233"]
        assert "filament_colour" not in preset

    def test_keeps_the_identity_of_the_filament(self, app):
        app.pull()
        preset = bundle_preset(app, "Plain PLA")
        assert (preset["name"], preset["type"], preset["from"]) == ("Plain PLA", "filament", "filament_db")
        assert preset["filament_id"] == f"fdb_{PLAIN_ID}"

    def test_adds_the_calibration_of_the_active_nozzle(self, app):
        app.pull()
        preset = bundle_preset(app, "Calibrated PLA")
        assert preset["filament_flow_ratio"] == ["0.97"]
        assert preset["pressure_advance"] == ["0.04"]
        assert preset["enable_pressure_advance"] == ["1"]

    def test_pulling_on_another_nozzle_changes_the_calibration(self, app):
        app.state.nozzle = "0.6,0.6"
        assert app.pull().message.endswith("for a 0.6 mm nozzle.")
        assert bundle_preset(app, "Calibrated PLA")["filament_flow_ratio"] == ["0.93"]

    def test_a_filament_without_calibration_has_none(self, app):
        app.pull()
        assert "pressure_advance" not in bundle_preset(app, "Plain PLA")

    @pytest.mark.parametrize("key, value", [
        ("inherits", ["Prusament PC Blend @COREONE"]),  # a parent that only exists in PrusaSlicer
        ("compatible_printers", [""]),
        ("compatible_printers_condition", ["printer_model!~/COREONE_INDX8T/"]),
        ("compatible_prints", [""]),
        ("compatible_prints_condition", [""]),
        ("temperature", ["215"]),  # a known setting that does not belong in a filament preset
        ("openprinttag_slug", ["x"]),
    ])
    def test_leaves_out_what_a_preset_cannot_hold(self, app, key, value):
        app.db.filaments[PLAIN_ID]["settings"] = {key: value}
        assert app.pull().status is PluginResult.Success
        assert key not in bundle_preset(app, "Plain PLA")

    def test_leaves_out_nil_which_many_settings_refuse(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"fan_max_speed": ["nil"], "filament_notes": ["nil"]}
        assert app.pull().status is PluginResult.Success
        assert "fan_max_speed" not in bundle_preset(app, "Plain PLA")

    @pytest.mark.parametrize("prusaslicer_key, orca_key", [
        ("disable_fan_first_layers", "close_fan_the_first_x_layers"),
        ("extrusion_multiplier", "filament_flow_ratio"),
        ("fan_below_layer_time", "fan_cooling_layer_time"),
        ("filament_deretract_speed", "filament_deretraction_speed"),
        ("filament_retract_length", "filament_retraction_length"),
        ("filament_retract_lift", "filament_z_hop"),
        ("filament_retract_speed", "filament_retraction_speed"),
        ("max_fan_speed", "fan_max_speed"),
        ("min_fan_speed", "fan_min_speed"),
        ("min_print_speed", "slow_down_min_speed"),
        ("slowdown_below_layer_time", "slow_down_layer_time"),
    ])
    def test_translates_prusaslicer_names(self, app, prusaslicer_key, orca_key):
        app.db.filaments[PLAIN_ID]["settings"] = {prusaslicer_key: ["1.0114"]}
        app.pull()
        preset = bundle_preset(app, "Plain PLA")
        assert preset[orca_key] == ["1.0114"]
        assert prusaslicer_key not in preset

    def test_an_orcaslicer_value_wins_over_a_prusaslicer_one(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"extrusion_multiplier": ["1.05"], "filament_flow_ratio": ["0.98"]}
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_flow_ratio"] == ["0.98"]

    def test_calibration_wins_over_a_prusaslicer_value(self, app):
        app.db.filaments[CALIBRATED_ID]["settings"] = {"extrusion_multiplier": ["1.05"]}
        app.pull()
        assert bundle_preset(app, "Calibrated PLA")["filament_flow_ratio"] == ["0.97"]

    @pytest.mark.parametrize("shrinkage, expected", [
        (0.2, ["99.8%"]), (0, ["100%"]), ("0.28%", ["99.72%"]), (100, ["100%"]), ("99.5%", ["99.5%"]),
    ])
    def test_z_shrinkage_becomes_orcas_remaining_size(self, app, shrinkage, expected):
        """Orca divides by this value, so a 0-based value scales a print by hundreds of times."""
        app.db.filaments[PLAIN_ID]["shrinkageZ"] = shrinkage
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_shrinkage_compensation_z"] == expected

    def test_a_max_volumetric_speed_of_zero_is_left_out(self, app):
        """It means no limit in PrusaSlicer, and OrcaSlicer resets it to 0.5 as an unmade edit."""
        app.db.filaments[PLAIN_ID]["maxVolumetricSpeed"] = 0
        app.pull()
        assert "filament_max_volumetric_speed" not in bundle_preset(app, "Plain PLA")

    def test_a_first_layer_bed_temperature_follows_the_bed_temperature(self, app):
        app.pull()
        preset = bundle_preset(app, "Plain PLA")
        assert preset["hot_plate_temp_initial_layer"] == ["60"]
        assert preset["textured_plate_temp_initial_layer"] == ["60"]

    def test_an_unset_z_shrinkage_stays_unset(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_shrinkage_compensation_z": ["nil"]}
        assert app.pull().status is PluginResult.Success
        assert "filament_shrinkage_compensation_z" not in bundle_preset(app, "Plain PLA")

    def test_every_plate_gets_the_bed_temperature(self, app):
        app.db.filaments[PLAIN_ID]["temperatures"]["bedFirstLayer"] = 65
        app.pull()
        preset = bundle_preset(app, "Plain PLA")
        for plate in ("cool", "eng", "supertack", "textured_cool", "textured"):
            assert preset[f"{plate}_plate_temp"] == ["60"]
            assert preset[f"{plate}_plate_temp_initial_layer"] == ["65"]

    def test_a_plate_with_its_own_temperature_keeps_it(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"textured_plate_temp": ["75"]}
        app.pull()
        assert bundle_preset(app, "Plain PLA")["textured_plate_temp"] == ["75"]

    def test_a_calibrated_bed_temperature_reaches_every_plate(self, app):
        app.db.calibrations[(PLAIN_ID, "0.4")] = [{"keys": {"hot_plate_temp": ["80"]}}]
        app.pull()
        assert bundle_preset(app, "Plain PLA")["textured_plate_temp"] == ["80"]

    @pytest.mark.parametrize("stored, plain", [('"250 100 42.4| 0.05 42.4"', "250 100 42.4| 0.05 42.4"), ('""', "")])
    def test_ini_quotes_from_a_prusaslicer_import_are_removed(self, app, stored, plain):
        """Filament DB stores the text of a single INI value as a string, not as a list."""
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_ramming_parameters": stored}
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_ramming_parameters"] == [plain]

    def test_calibration_for_another_printer_is_left_out(self, app):
        app.db.calibrations[(PLAIN_ID, "0.4")] = [{"printer": "Bambu Labs H2D", "keys": {"filament_flow_ratio": ["0.8"]}}]
        app.pull()
        assert "filament_flow_ratio" not in bundle_preset(app, "Plain PLA")

    def test_calibration_for_the_configured_printer_is_used(self, app):
        app.config["printer"] = "Prusa Core One INDX"
        app.db.calibrations[(PLAIN_ID, "0.4")] = [
            {"printer": "Bambu Labs H2D", "keys": {"filament_flow_ratio": ["0.8"]}},
            {"printer": "Prusa Core One INDX", "keys": {"filament_flow_ratio": ["0.9"]}},
        ]
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_flow_ratio"] == ["0.9"]

    def test_calibration_for_any_printer_is_used_with_a_configured_printer(self, app):
        app.config["printer"] = "Prusa Core One INDX"
        app.pull()
        assert bundle_preset(app, "Calibrated PLA")["filament_flow_ratio"] == ["0.97"]

    def test_reports_a_removed_filament_that_was_in_use(self, app):
        app.pull()
        app.filaments.select(PLAIN)
        del app.db.filaments[PLAIN_ID]
        assert "chose other presets for them: Plain PLA." in app.pull().message

    def test_a_case_only_rename_reaches_the_file_name(self, app):
        """The file name is the preset name, and replacing a file on macOS keeps its old capitals."""
        app.pull()
        app.db.filaments[PLAIN_ID]["name"] = "PLAIN PLA"
        app.pull()
        stems = [p.stem for p in (app.bundle_dir / "filament").glob("*.json") if p.stem.casefold() == "plain pla"]
        assert stems == ["PLAIN PLA"]

    def test_a_deleted_filament_loses_its_preset(self, app):
        app.pull()
        del app.db.filaments[PLAIN_ID]
        app.pull()
        assert not (app.bundle_dir / "filament" / "Plain PLA.json").exists()
        assert PLAIN not in app.filaments.presets

    def test_a_change_in_filament_db_arrives(self, app):
        app.pull()
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 235
        app.pull()
        assert app.filaments.presets[PLAIN].config_value("nozzle_temperature") == "235"

    @pytest.mark.parametrize("name, stem", [
        ("Spike/Weird: PLA? é", "Spike-Weird- PLA- é"),
        ("  ...  ", "Filament"),
        ("Trailing dot.", "Trailing dot"),
    ])
    def test_a_name_that_cannot_be_a_file_name_is_cleaned(self, app, name, stem):
        app.db.filaments[PLAIN_ID]["name"] = name
        app.pull()
        assert (app.bundle_dir / "filament" / f"{stem}.json").exists()

    def test_names_that_clean_up_alike_stay_apart(self, app):
        app.db.filaments[PLAIN_ID]["name"] = "A/B"
        app.db.filaments[CALIBRATED_ID]["name"] = "a:b"
        app.pull()
        assert sorted(path.stem for path in (app.bundle_dir / "filament").glob("*.json")) == ["A-B", "a-b (2)"]

    @pytest.mark.parametrize("name, stem", [
        ("NUL", "NUL_"),
        ("con.PLA", "con_.PLA"),
        ("Com1 . ", "Com1_"),
        ("LPT9 .old", "LPT9_ .old"),
        ("aux", "aux_"),
        ("COM²", "COM²_"),
        ("CONIN$", "CONIN$_"),
        ("NULL", "NULL"),
        ("COM10", "COM10"),
        ("COM1 PLA", "COM1 PLA"),
        ("Console.PLA", "Console.PLA"),
    ])
    def test_a_windows_device_name_is_not_used_as_a_file_name(self, app, name, stem):
        """Windows opens the device for such a name, even with an extension after it."""
        app.db.filaments[PLAIN_ID]["name"] = name
        app.pull()
        assert (app.bundle_dir / "filament" / f"{stem}.json").exists()

    def test_a_device_name_pulled_before_is_moved_once(self, app):
        """Earlier versions wrote "AUX" as it is, which works outside Windows. The next pull moves it to "AUX_",
        and OrcaSlicer chooses another preset for a slot that had the old one."""
        app.db.filaments[PLAIN_ID]["name"] = "AUX"
        app.pull()
        filament_dir = app.bundle_dir / "filament"
        (filament_dir / "AUX_.json").rename(filament_dir / "AUX.json")
        app.bundles.reload_local(BUNDLE_ID)
        app.filaments.select(f"_local/{BUNDLE_ID}/AUX")
        assert "chose other presets for them: AUX." in app.pull().message
        assert preset_stems(app) == ["AUX_", "Calibrated PLA"]

    def test_unsent_edits_on_a_device_name_pulled_before_are_kept(self, app):
        app.db.filaments[PLAIN_ID]["name"] = "AUX"
        app.pull()
        filament_dir = app.bundle_dir / "filament"
        (filament_dir / "AUX_.json").rename(filament_dir / "AUX.json")
        app.bundles.reload_local(BUNDLE_ID)
        app.filaments.select(f"_local/{BUNDLE_ID}/AUX", {"filament_vendor": "Maker"})
        assert "has unsent edits" in app.pull().message
        assert (filament_dir / "AUX.json").exists()

    def test_finds_the_active_users_folder_and_leaves_no_trace_in_others(self, app, plugin):
        app.pull()
        assert app.bundle_dir.is_dir()
        assert not (plugin.data_dir_for_test / "user" / "default" / "_local").exists()
        assert sorted(path.name for path in app.bundle_dir.parent.iterdir()) == [BUNDLE_ID]

    @pytest.mark.parametrize("active, other", [("default", "cloud-user"), ("cloud-user", "default")])
    def test_every_pull_reaches_the_active_user_wherever_it_sorts(self, app, plugin, active, other):
        users = plugin.data_dir_for_test / "user"
        app.bundles.activate(active)
        app.pull()
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 235
        app.pull()
        assert app.filaments.presets[PLAIN].config_value("nozzle_temperature") == "235"
        assert not (users / other / "_local").exists()

    def test_a_bundle_in_another_users_folder_is_left_alone(self, app, plugin):
        other = plugin.data_dir_for_test / "user" / "a-user-checked-first" / "_local" / BUNDLE_ID
        other.mkdir(parents=True)
        app.pull()
        assert sorted(path.name for path in other.iterdir()) == []

    def test_the_probe_is_removed_when_the_host_fails(self, app, orca, plugin):
        def closing():
            raise RuntimeError("OrcaSlicer is closing")

        orca.host.preset.bundles.list_ids = closing
        assert "closing" in app.pull().message
        assert not list((plugin.data_dir_for_test / "user").glob("*/_local"))

    def test_no_user_folder_is_reported(self, app, orca):
        orca.host.preset.bundles.list_ids = lambda: []
        assert app.pull().message == "Could not find the active user's preset folder."

    def test_an_unreadable_nozzle_diameter_is_reported(self, app):
        app.state.nozzle = None
        assert "nozzle diameter" in app.pull().message

    def test_writes_nothing_when_the_export_cannot_be_read(self, app):
        app.db.fail_with = (500, {"error": "boom"})
        assert app.pull().status is PluginResult.Skipped
        assert not (app.bundle_dir / "filament").exists()

    def test_unsent_edits_are_kept_by_a_pull(self, app):
        edit(app, PLAIN, filament_density="1.3")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 235
        app.pull()
        assert app.filaments.edited.config_value("filament_density") == "1.3"
        assert app.filaments.selected.config_value("nozzle_temperature") == "235"

    @pytest.mark.parametrize("change", ["rename", "delete"])
    def test_unsent_edits_on_a_filament_that_moved_stop_the_pull(self, app, change):
        edit(app, PLAIN, nozzle_temperature="240")
        before = bundle_preset(app, "Plain PLA")
        if change == "rename":
            app.db.filaments[PLAIN_ID]["name"] = "Renamed PLA"
        else:
            del app.db.filaments[PLAIN_ID]
        assert f'"Plain PLA" has unsent edits, and it was {"renamed in" if change == "rename" else "removed from"}' in app.pull().message
        assert bundle_preset(app, "Plain PLA") == before

    def test_unsent_edits_never_reach_a_filament_that_took_the_name(self, app):
        edit(app, PLAIN, nozzle_temperature="240")
        app.db.filaments[PLAIN_ID]["name"] = "Old PLA"
        app.db.filaments["c" * 24] = {"name": "Plain PLA", "type": "PLA", "vendor": "Spike", "temperatures": {"nozzle": 250}}
        assert "has unsent edits" in app.pull().message
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.filaments["c" * 24]["temperatures"]["nozzle"] == 250
        assert app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] == "240"

    def test_adding_a_filament_does_not_move_an_existing_preset(self, app, plugin):
        first = {"name": "A:B", "filament_id": "fdb_1"}
        added = {"name": "A/B", "filament_id": "fdb_2"}  # sorts first by name, but it came later
        assert plugin.preset_files([first], app.bundle_dir, {}, set()) == {"A-B": first}
        assert plugin.preset_files([added, first], app.bundle_dir, {}, set()) == {"A-B": first, "A-B (2)": added}

    def test_a_refused_reload_puts_the_old_files_back(self, app):
        """The files are the conflict check's ancestor, so they must match what OrcaSlicer loaded."""
        app.pull()
        before = bundle_preset(app, "Plain PLA")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 230
        del app.db.filaments[CALIBRATED_ID]
        app.bundles.refuse = True
        assert "invalid preset" in app.pull().message
        assert bundle_preset(app, "Plain PLA") == before
        assert (app.bundle_dir / "filament" / "Calibrated PLA.json").exists()

    def test_a_failed_write_puts_the_old_files_back(self, app, plugin, monkeypatch):
        """Stale files are deleted before the new ones are written."""
        app.pull()
        before = bundle_preset(app, "Plain PLA")
        del app.db.filaments[CALIBRATED_ID]

        def no_space(path, content):
            raise OSError("No space left on device")

        monkeypatch.setattr(plugin, "write_json", no_space)
        assert "No space left on device" in app.pull().message
        assert bundle_preset(app, "Plain PLA") == before
        assert (app.bundle_dir / "filament" / "Calibrated PLA.json").exists()

    def test_a_locked_file_does_not_cost_the_others(self, app, plugin, monkeypatch):
        app.pull()
        before = bundle_preset(app, "Plain PLA")
        app.db.filaments["c" * 24] = {"name": "New PLA", "type": "PLA", "vendor": "Spike", "temperatures": {"nozzle": 230}}
        app.bundles.refuse = True
        locked = app.bundle_dir / "filament" / "Calibrated PLA.json"
        real_replace, real_unlink = plugin.os.replace, type(locked).unlink
        reloads = app.bundles.reloads

        def replace(source, target):
            if str(target) == str(locked) and app.bundles.reloads > reloads:
                raise PermissionError("in use")
            real_replace(source, target)

        def unlink(path):
            if path.name in ("New PLA.json", locked.name):
                raise PermissionError("in use")
            real_unlink(path)

        monkeypatch.setattr(plugin.os, "replace", replace)
        monkeypatch.setattr(type(locked), "unlink", unlink)
        message = app.pull().message
        assert message.startswith("Could not put these synced presets back: Calibrated PLA.json, New PLA.json.")
        assert bundle_preset(app, "Plain PLA") == before

    def test_a_conflict_is_still_seen_after_a_refused_reload(self, app):
        edit(app, PLAIN, nozzle_temperature="240")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 230
        app.bundles.refuse = True
        app.pull()
        app.bundles.refuse = False
        assert "also changed in Filament DB" in app.send().message

    def test_never_opens_a_file_the_sandbox_cannot_see(self, app):
        """The host prompts the user for any open() whose path it cannot read, such as a descriptor."""
        seen, main = [], threading.get_ident()
        active = [True]

        def hook(event, args):
            if active[0] and threading.get_ident() == main:
                if event == "open" and not isinstance(args[0], (str, bytes, type(app.bundle_dir))):
                    seen.append(args)
                elif event.startswith("tempfile.") or event == "shutil.rmtree":
                    seen.append(event)

        sys.addaudithook(hook)
        app.pull()
        active[0] = False
        assert not seen


def preset_stems(app):
    return sorted(path.stem for path in (app.bundle_dir / "filament").glob("*.json"))


class TestAnEmptyFilamentDb:
    """An empty export can mean the last filament was deleted, or that the plugin points at the wrong copy."""

    def emptied(self, app):
        app.pull()
        app.db.filaments.clear()
        app.orca.host.ui.dialogs.clear()

    def test_asks_before_removing_the_presets(self, app):
        self.emptied(app)
        app.pull()
        assert app.orca.host.ui.dialogs == [
            f"Filament DB at {app.db.url} has no filaments. Remove all 2 synced presets from OrcaSlicer?\n\n"
            "Press Cancel if the plugin is pointing at the wrong copy of Filament DB."]

    def test_cancel_leaves_the_presets_alone(self, app):
        self.emptied(app)
        app.orca.host.ui.answer = "cancel"
        result = app.pull()
        assert result.status is PluginResult.Skipped
        assert result.message == "Filament DB has no filaments, and the synced presets were left alone."
        assert PLAIN in app.filaments.presets
        assert preset_stems(app) == ["Calibrated PLA", "Plain PLA"]
        assert app.bundles.reloads == 1

    def test_ok_removes_the_presets(self, app):
        self.emptied(app)
        result = app.pull()
        assert result.status is PluginResult.Success
        assert result.message == "Pulled 0 filaments from Filament DB for a 0.4 mm nozzle."
        assert preset_stems(app) == []
        assert not {PLAIN, CALIBRATED} & set(app.filaments.presets)

    def test_names_a_single_preset_as_one(self, app):
        app.pull()
        del app.db.filaments[PLAIN_ID]
        app.pull()
        app.db.filaments.clear()
        app.orca.host.ui.answer = "cancel"
        app.pull()
        assert "Remove the 1 synced preset from OrcaSlicer?" in app.orca.host.ui.dialogs[-1]

    def test_a_first_pull_asks_nothing(self, app):
        app.db.filaments.clear()
        assert app.pull().message.startswith("Pulled 0 filaments")
        assert not app.orca.host.ui.dialogs

    def test_counts_and_removes_a_file_it_cannot_read(self, app):
        self.emptied(app)
        (app.bundle_dir / "filament" / "Plain PLA.json").write_text("{", encoding="utf-8")
        app.pull()
        assert "Remove all 2 synced presets" in app.orca.host.ui.dialogs[0]
        assert preset_stems(app) == []

    def test_unsent_edits_still_stop_the_removal_once_confirmed(self, app):
        """The question comes first, so that edits are not discarded over a pull from the wrong copy."""
        self.emptied(app)
        app.filaments.select(PLAIN, {"nozzle_temperature": "222"})
        result = app.pull()
        assert len(app.orca.host.ui.dialogs) == 1
        assert result.message == ('"Plain PLA" has unsent edits, and it was removed from Filament DB. '
                                  "Discard them, then pull again. Nothing was changed.")
        assert preset_stems(app) == ["Calibrated PLA", "Plain PLA"]
        app.filaments.select(PLAIN)
        app.pull()
        assert preset_stems(app) == []

    def test_cancel_with_unsent_edits_keeps_them(self, app):
        self.emptied(app)
        app.filaments.select(PLAIN, {"nozzle_temperature": "222"})
        app.orca.host.ui.answer = "cancel"
        assert app.pull().message == "Filament DB has no filaments, and the synced presets were left alone."
        assert app.filaments.edited.config_value("nozzle_temperature") == "222"


class TestLongNames:
    """A file name holds at most 255 bytes on common file systems, and the preset is written to its
    name plus .tmp first. Windows counts UTF-16 units instead, of which a name never has more than bytes."""

    @staticmethod
    def assert_fits(app):
        for path in (app.bundle_dir / "filament").glob("*.json"):
            assert len((path.name + ".tmp").encode("utf-8")) <= 255

    # The leading x puts the cut in the middle of a character.
    @pytest.mark.parametrize("name", ["P" * 256, "xé" + "é" * 200, "x" + "日本" * 50])
    def test_a_long_name_does_not_stop_the_pull(self, app, name):
        app.pull()
        app.db.filaments[PLAIN_ID]["name"] = name
        app.db.filaments[CALIBRATED_ID]["temperatures"]["nozzle"] = 235
        assert app.pull().status is PluginResult.Success
        assert app.filaments.presets[CALIBRATED].config_value("nozzle_temperature") == "235"
        self.assert_fits(app)
        [stem] = [stem for stem in preset_stems(app) if stem != "Calibrated PLA"]
        # The cut keeps whole characters, and marks the name as cut short.
        assert name.startswith(stem.rpartition("… ")[0])
        assert len(stem.encode("utf-8")) > 200

    def test_a_name_that_fits_is_kept_whole(self, app):
        name = "é" * 123  # 246 bytes, and 255 with .json.tmp
        app.db.filaments[PLAIN_ID]["name"] = name
        app.pull()
        assert name in preset_stems(app)

    def test_long_names_sharing_a_start_stay_apart_and_keep_their_file_names(self, app):
        app.db.filaments[PLAIN_ID]["name"] = "P" * 300 + " red"
        app.db.filaments[CALIBRATED_ID]["name"] = "P" * 300 + " blue"
        app.pull()
        stems = preset_stems(app)
        assert len(stems) == 2 and not any(stem.endswith(")") for stem in stems)
        app.pull()
        assert preset_stems(app) == stems
        self.assert_fits(app)

    def test_the_same_long_name_twice_is_numbered_and_still_fits(self, app):
        app.db.filaments[PLAIN_ID]["name"] = app.db.filaments[CALIBRATED_ID]["name"] = "P" * 300
        app.pull()
        first, second = preset_stems(app)
        assert second.endswith(" (2)") and second.removesuffix(" (2)").endswith(first[-10:])
        self.assert_fits(app)

    # The emoji are 4 bytes and 2 UTF-16 units each, so this name fits in 255 bytes but not in the room left.
    @pytest.mark.parametrize("name", ["P" * 200, "x" + "😀" * 60])
    def test_on_windows_the_folder_takes_room_from_the_file_name(self, app, monkeypatch, name):
        """OrcaSlicer is not marked as long path aware, so Windows holds it to 259 units in a whole path."""
        filament_dir = app.bundle_dir / "filament"
        monkeypatch.setattr(app.plugin, "MAX_PATH_UNITS", len(str(filament_dir)) + 1 + 100)
        app.db.filaments[PLAIN_ID]["name"] = name
        assert app.pull().status is PluginResult.Success
        for path in filament_dir.glob("*.json"):
            assert len(str(path).encode("utf-16-le")) // 2 + len(".tmp") <= app.plugin.MAX_PATH_UNITS
        [stem] = [stem for stem in preset_stems(app) if stem != "Calibrated PLA"]
        assert name.startswith(stem.rpartition("… ")[0]) and len(stem.encode("utf-16-le")) // 2 > 80

    def test_on_windows_a_file_name_that_no_longer_fits_the_folder_is_cut_again(self, app, monkeypatch):
        filament_dir = app.bundle_dir / "filament"
        monkeypatch.setattr(app.plugin, "MAX_PATH_UNITS", len(str(filament_dir)) + 1 + 300)  # room for 255
        app.db.filaments[PLAIN_ID]["name"] = "P" * 300
        app.pull()
        [before] = [stem for stem in preset_stems(app) if stem != "Calibrated PLA"]
        monkeypatch.setattr(app.plugin, "MAX_PATH_UNITS", len(str(filament_dir)) + 1 + 100)
        assert app.pull().status is PluginResult.Success
        [after] = [stem for stem in preset_stems(app) if stem != "Calibrated PLA"]
        assert after != before
        for path in filament_dir.glob("*.json"):
            assert len(str(path).encode("utf-16-le")) // 2 + len(".tmp") <= app.plugin.MAX_PATH_UNITS

    def test_a_long_name_can_be_edited_and_sent(self, app):
        app.db.filaments[PLAIN_ID]["name"] = "P" * 300
        app.pull()
        [stem] = [stem for stem in preset_stems(app) if stem != "Calibrated PLA"]
        edit(app, f"_local/{BUNDLE_ID}/{stem}", filament_vendor="Maker")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"vendor": "Maker"}]


NEW_ID = "c" * 24
FIRST_ID = "0" * 24  # sorts before every other id


def bundle_ids(app):
    return {stem: bundle_preset(app, stem)["filament_id"].removeprefix("fdb_") for stem in preset_stems(app)}


def put_in_slot(app, slot, stem):
    """Put the synced preset *stem* in the selected filament slot, or in one of the others."""
    name = f"_local/{BUNDLE_ID}/{stem}"
    if slot == "selected":
        app.filaments.select(name)
    else:
        app.state.slots.append(name)


def add_filament(app, filament_id, name):
    app.db.filaments[filament_id] = {"name": name, "type": "PLA", "vendor": "Spike", "temperatures": {"nozzle": 250}}


class TestAFileNameStaysWithItsFilament:
    """OrcaSlicer restores the preset of every filament slot by name, and a preset's name is its file name.
    A file name handed to another filament would quietly put that filament in the slot."""

    @pytest.fixture(params=["selected", "secondary"])
    def slot(self, request):
        return request.param

    @pytest.fixture
    def alike(self, app):
        app.db.filaments[PLAIN_ID]["name"] = "A/B"
        app.db.filaments[CALIBRATED_ID]["name"] = "A:B"
        app.pull()
        assert bundle_ids(app) == {"A-B": PLAIN_ID, "A-B (2)": CALIBRATED_ID}

    def test_deleting_the_first_of_two_names_alike_does_not_move_the_second(self, app, alike, slot):
        put_in_slot(app, slot, "A-B")
        del app.db.filaments[PLAIN_ID]
        assert "chose other presets for them: A-B." in app.pull().message
        assert bundle_ids(app) == {"A-B (2)": CALIBRATED_ID}
        assert f"_local/{BUNDLE_ID}/A-B" not in app.filaments.presets

    def test_a_new_filament_does_not_take_a_file_name_a_slot_holds(self, app, alike, slot):
        put_in_slot(app, slot, "A-B")
        del app.db.filaments[PLAIN_ID]
        add_filament(app, NEW_ID, "A/B")
        assert "chose other presets for them: A-B." in app.pull().message
        assert bundle_ids(app) == {"A-B (2)": CALIBRATED_ID, "A-B (3)": NEW_ID}

    def test_a_new_filament_keeps_the_file_name_it_got_once_the_slot_moves_on(self, app, alike, slot):
        put_in_slot(app, slot, "A-B")
        del app.db.filaments[PLAIN_ID]
        add_filament(app, NEW_ID, "A/B")
        app.pull()
        app.state.slots.clear()
        app.filaments.select(f"_local/{BUNDLE_ID}/A-B (2)")
        app.pull()
        assert bundle_ids(app) == {"A-B (2)": CALIBRATED_ID, "A-B (3)": NEW_ID}

    def test_a_file_name_no_slot_holds_can_go_to_a_new_filament(self, app, alike):
        del app.db.filaments[PLAIN_ID]
        add_filament(app, NEW_ID, "A/B")
        assert "chose other presets" not in app.pull().message
        assert bundle_ids(app) == {"A-B": NEW_ID, "A-B (2)": CALIBRATED_ID}

    def test_a_new_filament_that_sorts_first_does_not_take_an_existing_file_name(self, app, slot):
        app.db.filaments[PLAIN_ID]["name"] = "A:B"
        app.pull()
        put_in_slot(app, slot, "A-B")
        add_filament(app, FIRST_ID, "A/B")
        assert app.pull().message == "Pulled 3 filaments from Filament DB for a 0.4 mm nozzle."
        assert bundle_ids(app) == {"A-B": PLAIN_ID, "A-B (2)": FIRST_ID, "Calibrated PLA": CALIBRATED_ID}

    def test_a_new_filament_does_not_take_the_file_name_of_a_renamed_one_in_a_slot(self, app, slot):
        app.pull()
        put_in_slot(app, slot, "Plain PLA")
        app.db.filaments[PLAIN_ID]["name"] = "Old PLA"
        add_filament(app, NEW_ID, "Plain PLA")
        assert "chose other presets for them: Plain PLA." in app.pull().message
        assert bundle_ids(app) == {"Calibrated PLA": CALIBRATED_ID, "Old PLA": PLAIN_ID, "Plain PLA (2)": NEW_ID}

    def test_a_filament_in_a_slot_can_take_its_own_name_in_other_capitals(self, app, slot):
        app.pull()
        put_in_slot(app, slot, "Plain PLA")
        app.db.filaments[PLAIN_ID]["name"] = "PLAIN PLA"
        app.pull()
        assert bundle_ids(app) == {"Calibrated PLA": CALIBRATED_ID, "PLAIN PLA": PLAIN_ID}

    def test_a_renamed_filament_leaves_its_number_behind(self, app, alike):
        app.db.filaments[CALIBRATED_ID]["name"] = "C:D"
        app.pull()
        assert bundle_ids(app) == {"A-B": PLAIN_ID, "C-D": CALIBRATED_ID}

    def test_a_filament_whose_own_name_loses_its_number_loses_it_from_the_file_name(self, app):
        app.db.filaments[PLAIN_ID]["name"] = "PLA (2)"
        app.pull()
        app.db.filaments[PLAIN_ID]["name"] = "PLA"
        app.pull()
        assert bundle_ids(app) == {"Calibrated PLA": CALIBRATED_ID, "PLA": PLAIN_ID}

    def test_a_filament_whose_own_name_has_a_number_keeps_it(self, app):
        app.db.filaments[PLAIN_ID]["name"] = "PLA (2)"
        app.pull()
        app.pull()
        assert bundle_ids(app) == {"Calibrated PLA": CALIBRATED_ID, "PLA (2)": PLAIN_ID}

    # A half-saved hand edit, a file that is not a preset, and a preset that lost its name.
    @pytest.mark.parametrize("content", ["{", "[]", f'{{"filament_id": "fdb_{PLAIN_ID}"}}'])
    def test_a_pull_repairs_a_file_it_cannot_read(self, app, content):
        app.pull()
        (app.bundle_dir / "filament" / "Plain PLA.json").write_text(content, encoding="utf-8")
        assert app.pull().message == "Pulled 2 filaments from Filament DB for a 0.4 mm nozzle."
        assert bundle_ids(app) == {"Calibrated PLA": CALIBRATED_ID, "Plain PLA": PLAIN_ID}

    def test_a_file_it_cannot_read_still_asks_when_filament_db_is_empty(self, app):
        app.pull()
        (app.bundle_dir / "filament" / "Plain PLA.json").write_text("{", encoding="utf-8")
        (app.bundle_dir / "filament" / "Calibrated PLA.json").unlink()
        app.db.filaments.clear()
        app.orca.host.ui.answer = "cancel"
        assert "Filament DB has no filaments" in app.pull().message
        assert preset_stems(app) == ["Plain PLA"]

    def test_a_numbered_long_name_keeps_its_file_name(self, app):
        app.db.filaments[PLAIN_ID]["name"] = app.db.filaments[CALIBRATED_ID]["name"] = "P" * 300
        app.pull()
        numbered = next(stem for stem, filament_id in bundle_ids(app).items() if filament_id == CALIBRATED_ID)
        assert numbered.endswith(" (2)")
        del app.db.filaments[PLAIN_ID]
        app.pull()
        assert bundle_ids(app) == {numbered: CALIBRATED_ID}

    def test_an_edit_to_a_filament_that_kept_its_number_is_sent_to_it(self, app, alike):
        del app.db.filaments[PLAIN_ID]
        edit(app, f"_local/{BUNDLE_ID}/A-B (2)", filament_vendor="Maker")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.filaments[CALIBRATED_ID]["vendor"] == "Maker"


class TestSend:
    def test_sends_only_what_changed_under_filament_db_names(self, app):
        edit(app, PLAIN, nozzle_temperature="222", filament_retraction_length="1.2")
        result = app.send()
        assert result.message == "Sent 2 of 2 edits to Filament DB."
        assert app.db.posts == [{"temperatures": {"nozzle": "222"}, "filament_retraction_length": ["1.2"]}]

    @pytest.mark.parametrize("key, value, stored", [
        ("default_filament_colour", "#FF0000", "#FF0000"),
        ("filament_cost", "25", "25"),
        ("filament_diameter", "2.85", "2.85"),
        ("filament_max_volumetric_speed", "12", "12"),
        ("nozzle_temperature_initial_layer", "230", "230"),
    ])
    def test_a_structured_field_is_kept(self, app, key, value, stored):
        edit(app, PLAIN, **{key: value})
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."

    def test_only_the_edited_values_change_in_filament_db(self, app):
        edit(app, PLAIN, filament_density="1.3")
        before = dict(app.db.filaments[PLAIN_ID]["temperatures"])
        app.send()
        assert app.db.posts == [{"density": "1.3"}]
        assert app.db.filaments[PLAIN_ID]["temperatures"] == before

    def test_an_edit_to_an_imported_setting_goes_under_its_stored_name(self, app):
        """A second key would leave Filament DB's form and PrusaSlicer on the old value."""
        app.db.filaments[PLAIN_ID]["settings"] = {"extrusion_multiplier": ["1.05"]}
        edit(app, PLAIN, filament_flow_ratio="1.02")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.filaments[PLAIN_ID]["settings"] == {"extrusion_multiplier": ["1.02"]}
        app.db.filaments[PLAIN_ID]["settings"]["extrusion_multiplier"] = ["0.99"]
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_flow_ratio"] == ["0.99"]

    def test_an_edit_goes_under_the_orca_name_when_both_exist(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"extrusion_multiplier": ["1.05"], "filament_flow_ratio": ["1.01"]}
        edit(app, PLAIN, filament_flow_ratio="1.02")
        app.send()
        assert app.db.posts == [{"filament_flow_ratio": ["1.02"]}]

    def test_structured_fields_get_their_filament_db_names(self, app, plugin):
        body = plugin.request_body({
            "default_filament_colour": ["#FF0000"], "filament_cost": ["25"], "filament_density": ["1.3"],
            "filament_diameter": ["1.75"], "filament_max_volumetric_speed": ["12"], "filament_type": ["PETG"],
            "filament_vendor": ["Acme"], "hot_plate_temp": ["70"], "hot_plate_temp_initial_layer": ["75"],
            "nozzle_temperature_initial_layer": ["230"], "nozzle_temperature_range_high": ["250"],
            "nozzle_temperature_range_low": ["200"],
        }, {})
        assert body == {
            "color": "#FF0000", "cost": "25", "density": "1.3", "diameter": "1.75", "maxVolumetricSpeed": "12",
            "type": "PETG", "vendor": "Acme", "temperatures": {
                "bed": "70", "bedFirstLayer": "75", "nozzleFirstLayer": "230", "nozzleRangeMax": "250",
                "nozzleRangeMin": "200",
            },
        }

    def test_posts_the_bare_id_which_the_route_requires(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        assert app.send().status is PluginResult.Success
        assert app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] == "222"

    def test_the_edit_comes_back_as_the_saved_preset(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.send()
        assert app.filaments.selected.config_value("nozzle_temperature") == "222"
        assert app.filaments.edited.config_value("nozzle_temperature") == "222"

    def test_ids_and_scope_are_never_sent(self, app):
        edit(app, PLAIN, nozzle_temperature="222", filament_settings_id='"Renamed"', inherits="Other",
             compatible_printers="X", compatible_printers_condition="Y", compatible_prints="Z",
             compatible_prints_condition="W")
        app.send()
        assert app.db.posts == [{"temperatures": {"nozzle": "222"}}]

    def test_no_edits_sends_nothing(self, app):
        edit(app, PLAIN)
        result = app.send()
        assert result.status is PluginResult.Skipped
        assert result.message == "There are no edits to send."
        assert not app.db.posts

    def test_a_data_folder_reached_through_a_link_still_finds_the_filament(self, app, plugin):
        """OrcaSlicer's paths keep the link, while the plugin's own path resolves it."""
        link = plugin.data_dir_for_test.with_name("linked")
        link.symlink_to(plugin.data_dir_for_test)
        edit(app, PLAIN, nozzle_temperature="222")
        linked = link / app.bundle_dir.relative_to(plugin.data_dir_for_test) / "filament" / "Plain PLA.json"
        app.filaments.edited.file = str(linked)
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."

    def test_a_preset_from_elsewhere_is_refused(self, app, plugin):
        app.pull()
        path = plugin.data_dir_for_test / "system" / "system.json"
        path.write_text(json.dumps({"filament_id": "GFL99"}))
        app.filaments.edited = FakePreset("Generic PLA", path)
        assert app.send().message == '"Generic PLA" did not come from Filament DB, so there is nowhere to send it.'

    def test_a_preset_without_a_file_is_refused(self, app):
        app.pull()
        assert "did not come from Filament DB" in app.send().message

    def test_a_filament_that_was_never_pulled_is_refused(self, app, plugin):
        app.pull()
        app.db.filaments["c" * 24] = {**app.db.filaments[PLAIN_ID], "name": "New PLA"}
        path = plugin.data_dir_for_test / "user" / "Copy.json"
        path.write_text(json.dumps({"filament_id": "fdb_" + "c" * 24}))
        app.filaments.edited = FakePreset("Copy", path)
        assert "not been pulled" in app.send().message

    def test_a_filament_deleted_in_filament_db_is_reported(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        del app.db.filaments[PLAIN_ID]
        assert app.send().message == REMOVED_TEXT

    @pytest.mark.parametrize("emptied", [False, True])
    def test_the_advice_for_a_deleted_filament_removes_its_preset(self, app, emptied):
        """Pull refuses a preset with unsent edits, and asks first when Filament DB has nothing left."""
        edit(app, PLAIN, nozzle_temperature="222")
        app.db.filaments.pop(PLAIN_ID)
        if emptied:
            app.db.filaments.clear()
        app.send()
        app.filaments.select(PLAIN)
        assert app.pull().status is PluginResult.Success
        assert "Plain PLA" not in preset_stems(app)

    def saved_copy(self, app, plugin, **differences):
        """A preset saved under a new name, which keeps the filament's id and nothing else in common."""
        copy_file = plugin.data_dir_for_test / "user" / "cloud-user" / "Copy.json"
        copy_file.write_text(json.dumps({"filament_id": f"fdb_{PLAIN_ID}"}))
        base = app.filaments.presets[PLAIN]
        app.filaments.add(FakePreset("Edited Plain PLA", copy_file, {**base.values, **differences}))

    def test_a_saved_copy_with_no_unsaved_edits_sends_nothing(self, app, plugin):
        app.pull()
        self.saved_copy(app, plugin, filament_density="1.35")
        app.filaments.select("Edited Plain PLA")
        assert app.send().message == "Only unsaved edits can be sent. Make the change on the synced preset instead."
        assert not app.db.posts

    def test_a_saved_copy_sends_its_unsaved_edits(self, app, plugin):
        app.pull()
        self.saved_copy(app, plugin)
        app.filaments.select("Edited Plain PLA", {"filament_density": "1.35"})
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"density": "1.35"}]

    def test_a_saved_copy_of_a_removed_filament_is_not_sent_back_to_pull(self, app, plugin):
        """Following the advice to discard and pull removes the synced preset, and pulling again cannot help."""
        app.pull()
        self.saved_copy(app, plugin)
        app.filaments.select("Edited Plain PLA", {"filament_density": "1.35"})
        del app.db.filaments[PLAIN_ID]
        assert app.send().message == REMOVED_TEXT
        assert app.pull().message.startswith("Pulled 1 filaments")
        app.filaments.select("Edited Plain PLA", {"filament_density": "1.35"})
        assert app.send().message == ('"Edited Plain PLA" came from a filament that Filament DB no longer has, '
                                      "so there is nowhere to send it.")
        assert not app.db.posts

    def test_a_synced_preset_is_judged_by_its_file_not_its_loaded_value(self, app):
        """OrcaSlicer's form of a value can differ from the file's, and only a copy needs this check."""
        edit(app, PLAIN, nozzle_temperature="222")
        app.filaments.selected.values["nozzle_temperature"] = "216"
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."

    def test_an_edit_on_an_out_of_date_copy_cannot_overwrite_a_newer_value(self, app, plugin):
        """The bundle moved on with a pull, so it matches Filament DB and shows no conflict."""
        app.pull()
        self.saved_copy(app, plugin)
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 230
        app.pull()
        app.filaments.select("Edited Plain PLA", {"nozzle_temperature": "220"})
        assert app.send().message == ("Nothing was sent, because the synced preset shows other values for: "
                                      "nozzle_temperature. Make the change on the synced preset instead.")
        assert not app.db.posts

    def test_a_stale_saved_copy_cannot_revert_filament_db(self, app, plugin):
        app.pull()
        self.saved_copy(app, plugin)
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 230
        app.pull()
        app.filaments.select("Edited Plain PLA", {"filament_density": "1.35"})
        app.send()
        assert app.db.posts == [{"density": "1.35"}]
        assert app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] == 230

    def test_a_copy_cannot_bring_back_a_setting_removed_in_filament_db(self, app, plugin):
        """With the setting gone from both Filament DB and the bundle, the two agree and show no conflict."""
        app.db.filaments[PLAIN_ID]["settings"] = {"fan_max_speed": ["65"]}
        app.pull()
        self.saved_copy(app, plugin)
        del app.db.filaments[PLAIN_ID]["settings"]["fan_max_speed"]
        app.pull()
        app.filaments.select("Edited Plain PLA", {"fan_max_speed": "70"})
        assert app.send().message == ("Nothing was sent, because the synced preset shows other values for: "
                                      "fan_max_speed. Make the change on the synced preset instead.")
        assert not app.db.posts

    def test_a_copy_saved_with_its_own_value_for_a_setting_filament_db_leaves_out_sends_nothing(self, app, plugin):
        """The copy's saved value may be an override Filament DB has since removed, so it is not trusted."""
        app.pull()
        self.saved_copy(app, plugin, fan_max_speed="50")
        app.filaments.select("Edited Plain PLA", {"fan_max_speed": "70"})
        assert app.send().message == ("Nothing was sent, because the synced preset shows other values for: "
                                      "fan_max_speed. Make the change on the synced preset instead.")
        assert not app.db.posts

    def test_a_copy_cannot_be_checked_when_orcaslicer_has_not_loaded_the_synced_preset(self, app, plugin):
        app.pull()
        self.saved_copy(app, plugin)
        del app.filaments.presets[PLAIN]
        app.filaments.select("Edited Plain PLA", {"fan_max_speed": "70"})
        assert app.send().message == ("Nothing was sent, because OrcaSlicer has not loaded this filament's synced "
                                      "preset. Run Pull from Filament DB, then send again.")
        assert not app.db.posts

    def test_a_copys_edit_to_a_setting_filament_db_exports_needs_no_loaded_synced_preset(self, app, plugin):
        app.pull()
        self.saved_copy(app, plugin)
        del app.filaments.presets[PLAIN]
        app.filaments.select("Edited Plain PLA", {"nozzle_temperature": "222"})
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"temperatures": {"nozzle": "222"}}]

    def test_a_copys_held_back_edit_needs_no_loaded_synced_preset(self, app, plugin):
        app.db.filaments[PLAIN_ID]["bedTypeTemps"] = [{"bedType": "Cool Plate", "temperature": 45}]
        app.pull()
        self.saved_copy(app, plugin)
        del app.filaments.presets[PLAIN]
        app.filaments.select("Edited Plain PLA", {"cool_plate_temp": "50"})
        assert app.send().message == ("Sent 0 of 1 edits to Filament DB. Change these in Filament DB, "
                                      "as they cannot be sent: cool_plate_temp.")
        assert not app.db.posts

    def test_a_copys_edit_already_in_filament_db_needs_no_loaded_synced_preset(self, app, plugin):
        app.pull()
        self.saved_copy(app, plugin)
        del app.filaments.presets[PLAIN]
        app.filaments.select("Edited Plain PLA", {"fan_max_speed": "70"})
        app.db.filaments[PLAIN_ID]["settings"] = {"fan_max_speed": ["70"]}
        assert app.send().message == ("Sent 0 of 1 edits to Filament DB. "
                                      "These were in Filament DB already: fan_max_speed.")
        assert not app.db.posts

    def test_a_copy_showing_orcaslicers_own_value_can_set_one_in_filament_db(self, app, plugin):
        app.pull()
        self.saved_copy(app, plugin)
        app.filaments.select("Edited Plain PLA", {"fan_max_speed": "70"})
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"fan_max_speed": ["70"]}]

    def test_a_copys_conflict_is_checked_against_the_bundle(self, app, plugin):
        app.pull()
        self.saved_copy(app, plugin)
        app.filaments.select("Edited Plain PLA", {"nozzle_temperature": "240"})
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 230
        assert "also changed in Filament DB" in app.send().message

    @pytest.mark.parametrize("file", ["", "/somewhere/project.3mf", "/outside/preset.json"])
    def test_a_preset_outside_the_data_folder_is_not_read(self, app, monkeypatch, file):
        def unexpected(*args):
            raise AssertionError("read a file the sandbox would ask about")

        monkeypatch.setattr(app.plugin.Path, "read_text", unexpected)
        assert app.plugin.preset_filament_id(FakePreset("x", file)) is None

    def test_a_preset_file_that_cannot_be_parsed_is_not_a_synced_preset(self, app, plugin):
        path = plugin.data_dir_for_test / "user" / "broken.json"
        path.write_text("{")
        assert plugin.preset_filament_id(FakePreset("x", path)) is None

    def test_field_only_settings_are_held_back(self, app):
        edit(app, PLAIN, filament_shrink="99.5%", filament_notes='"note"', filament_density="1.3")
        message = app.send().message
        assert message == ("Sent 1 of 3 edits to Filament DB. Change these in Filament DB, as they cannot be sent: "
                           "filament_notes, filament_shrink.")
        assert app.db.posts == [{"density": "1.3"}]

    def test_send_shows_what_will_change_first(self, app):
        edit(app, PLAIN, nozzle_temperature="222", filament_density="1.3")
        app.send()
        assert app.orca.host.ui.dialogs == [
            'Send these changes to "Plain PLA" in Filament DB?\n\n'
            "filament_density: 1.24 -> 1.3\nnozzle_temperature: 215 -> 222"
        ]

    def test_an_ampersand_shows_in_the_confirmation(self, app):
        """OrcaSlicer's message dialog would take a lone & for a shortcut key and drop it."""
        edit(app, PLAIN, filament_end_gcode="M117 A&B")
        app.send()
        assert app.orca.host.ui.dialogs[0].endswith("filament_end_gcode: not set -> M117 A&&B")
        assert app.db.posts == [{"filament_end_gcode": ["M117 A&B"]}]

    def test_declining_sends_nothing_and_keeps_the_edits(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.orca.host.ui.answer = "cancel"
        assert app.send().message == "Nothing was sent."
        assert not app.db.posts
        assert app.filaments.edited.is_dirty

    def test_nothing_to_post_asks_nothing(self, app):
        edit(app, CALIBRATED, filament_flow_ratio="0.9")
        app.send()
        assert not app.orca.host.ui.dialogs

    def test_a_long_value_and_a_long_list_are_cut_short(self, app):
        edit(app, PLAIN, filament_end_gcode="x" * 60, **{f"filament_extra_{n:02}": "1" for n in range(20)})
        app.send()
        text = app.orca.host.ui.dialogs[0]
        assert text.endswith("and 6 more")
        assert "not set -> 1" in text

    def test_a_long_value_is_shortened(self, plugin):
        assert plugin.shown_value(["x" * 60]) == "x" * 40 + "..."

    def test_an_edit_of_one_extruder_variant_is_held_back(self, app):
        """These fields hold one value, so only the first variant's would survive."""
        edit(app, PLAIN, nozzle_temperature="220,235")
        assert "cannot be sent: nozzle_temperature" in app.send().message
        assert not app.db.posts

    def test_a_list_setting_is_sent_whole(self, app):
        edit(app, PLAIN, filament_retraction_length="1.2,1.4")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts[0] == {"filament_retraction_length": ["1.2", "1.4"]}

    def test_z_shrinkage_is_never_sent(self, app):
        """Filament DB and PrusaSlicer read this key on a different scale from Orca."""
        edit(app, PLAIN, filament_shrinkage_compensation_z="99.5%")
        assert "Sent 0 of 1" in app.send().message
        assert not app.db.posts


class TestPlateRows:
    """Filament DB exports a bed type row over the settings bag and the plain bed temperature, and the
    sync route cannot write a row, so an edit sent for one would be stored out of sight."""

    @pytest.mark.parametrize("bed_type, key", [
        (bed_type, key) for bed_type, keys in PLATE_ROWS.items() for key in keys
    ])
    def test_an_edit_to_a_plate_with_a_row_is_held_back(self, app, bed_type, key):
        app.db.filaments[PLAIN_ID]["bedTypeTemps"] = [
            {"bedType": bed_type, "temperature": 70, "firstLayerTemperature": 70}]
        edit(app, PLAIN, **{key: "75"})
        assert app.send().message == (f"Sent 0 of 1 edits to Filament DB. Change these in Filament DB, "
                                      f"as they cannot be sent: {key}.")
        assert not app.db.posts

    def test_a_held_back_edit_does_not_turn_up_once_the_row_is_removed(self, app):
        """The issue as reported: the value would wait in the settings bag behind the row."""
        app.db.filaments[PLAIN_ID]["bedTypeTemps"] = [{"bedType": "Textured PEI Plate", "temperature": 70}]
        edit(app, PLAIN, textured_plate_temp="75")
        app.send()
        assert bundle_preset(app, "Plain PLA")["textured_plate_temp"] == ["70"]
        del app.db.filaments[PLAIN_ID]["bedTypeTemps"]
        app.pull()
        assert bundle_preset(app, "Plain PLA")["textured_plate_temp"] == ["60"]

    def test_the_other_edits_are_still_sent(self, app):
        app.db.filaments[PLAIN_ID]["bedTypeTemps"] = [{"bedType": "Cool Plate", "temperature": 45}]
        edit(app, PLAIN, cool_plate_temp="50", nozzle_temperature="222")
        assert app.send().message == ("Sent 1 of 2 edits to Filament DB. Change these in Filament DB, "
                                      "as they cannot be sent: cool_plate_temp.")
        assert app.db.posts == [{"temperatures": {"nozzle": "222"}}]

    def test_a_value_the_row_leaves_empty_is_sent(self, app):
        """The export skips an empty value, so the setting comes from the settings bag."""
        app.db.filaments[PLAIN_ID]["bedTypeTemps"] = [
            {"bedType": "Textured PEI Plate", "temperature": 70, "firstLayerTemperature": None}]
        edit(app, PLAIN, textured_plate_temp_initial_layer="72")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert bundle_preset(app, "Plain PLA")["textured_plate_temp_initial_layer"] == ["72"]

    def test_the_first_layer_bed_temperature_is_sent_beside_a_hot_plate_row_without_one(self, app):
        app.db.filaments[PLAIN_ID]["bedTypeTemps"] = [{"bedType": "Hot Plate", "temperature": 70}]
        edit(app, PLAIN, hot_plate_temp_initial_layer="68")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"temperatures": {"bedFirstLayer": "68"}}]

    def test_a_row_for_a_bed_type_the_export_does_not_know_is_ignored(self, app):
        """Rows are free text, and one named for PrusaSlicer, such as Textured PEI, never reaches Orca."""
        app.db.filaments[PLAIN_ID]["bedTypeTemps"] = [{"bedType": "Textured PEI", "temperature": 70}]
        edit(app, PLAIN, textured_plate_temp="75")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."

    def test_a_row_inherited_from_the_parent_holds_the_edit_back(self, app):
        app.db.filaments["c" * 24] = {"name": "PLA Template", "type": "PLA", "vendor": "Spike",
                                      "bedTypeTemps": [{"bedType": "Engineering Plate", "temperature": 65,
                                                        "firstLayerTemperature": 70}]}
        app.db.filaments[PLAIN_ID]["parentId"] = "c" * 24
        edit(app, PLAIN, eng_plate_temp_initial_layer="72")
        assert "cannot be sent: eng_plate_temp_initial_layer" in app.send().message
        assert not app.db.posts

    def test_a_variant_with_rows_of_its_own_does_not_inherit_the_parents(self, app):
        """Filament DB inherits the rows as a whole list, not one bed type at a time."""
        app.db.filaments["c" * 24] = {"name": "PLA Template", "type": "PLA", "vendor": "Spike",
                                      "bedTypeTemps": [{"bedType": "Engineering Plate", "temperature": 65}]}
        app.db.filaments[PLAIN_ID].update(parentId="c" * 24,
                                          bedTypeTemps=[{"bedType": "Cool Plate", "temperature": 45}])
        edit(app, PLAIN, eng_plate_temp="70")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."

    def test_the_record_is_only_read_for_a_plate_temperature(self, app):
        edit(app, PLAIN, nozzle_temperature="222", supertack_plate_temp="55")
        app.send()
        assert app.db.record_reads == 0
        edit(app, PLAIN, textured_cool_plate_temp_initial_layer="65")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        # Once to ask, and once more after the confirmation in case a row was added meanwhile.
        assert app.db.record_reads == 2


class TestConflicts:
    def test_a_value_changed_on_both_sides_is_refused_and_nothing_is_sent(self, app):
        edit(app, PLAIN, nozzle_temperature="240", filament_density="1.3")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 230
        result = app.send()
        assert result.status is PluginResult.Skipped
        assert "also changed in Filament DB: nozzle_temperature" in result.message
        assert not app.db.posts
        assert app.db.filaments[PLAIN_ID]["density"] == 1.24

    @pytest.mark.parametrize("key, value, change", [
        ("filament_density", "1.3", lambda record: record.update(density=1.5)),
        ("default_filament_colour", "#FF0000", lambda record: record.update(color="#00FF00")),
        ("filament_retraction_length", "1.2", lambda record: record.update(settings={"filament_retraction_length": ["0.5"]})),
    ])
    def test_every_kind_of_setting_is_checked(self, app, key, value, change):
        edit(app, PLAIN, **{key: value})
        change(app.db.filaments[PLAIN_ID])
        assert f"also changed in Filament DB: {key}" in app.send().message
        assert not app.db.posts

    def test_a_setting_newly_added_in_filament_db_is_a_conflict(self, app):
        edit(app, PLAIN, filament_retraction_length="1.2")
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_retraction_length": ["0.5"]}
        assert "also changed" in app.send().message

    def test_every_conflict_is_listed(self, app):
        edit(app, PLAIN, filament_density="1.3", nozzle_temperature="240")
        app.db.filaments[PLAIN_ID].update(density=1.5, temperatures={"nozzle": 230, "bed": 60})
        assert "filament_density, nozzle_temperature" in app.send().message

    def test_the_same_value_written_differently_is_not_a_conflict(self, app):
        edit(app, PLAIN, nozzle_temperature="240")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 215.0
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."

    def test_a_change_to_another_setting_is_not_a_conflict(self, app):
        edit(app, PLAIN, nozzle_temperature="240")
        app.db.filaments[PLAIN_ID]["density"] = 1.5
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."

    def test_pulling_clears_the_conflict_and_keeps_the_edit(self, app):
        edit(app, PLAIN, nozzle_temperature="240")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 230
        app.send()
        app.pull()
        assert app.filaments.edited.config_value("nozzle_temperature") == "240"
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."


class TestChangesWhileConfirming:
    """The confirmation stays open for as long as the user likes, and Filament DB can change meanwhile."""

    def test_a_value_changed_in_filament_db_while_confirming_is_not_overwritten(self, app):
        """The issue as reported."""
        edit(app, PLAIN, nozzle_temperature="222")
        app.orca.host.ui.while_open = lambda: app.db.filaments[PLAIN_ID]["temperatures"].update(nozzle=230)
        result = app.send()
        assert result.status is PluginResult.Skipped
        assert result.message == ("Nothing was sent, because these changed in Filament DB while you were deciding: "
                                  "nozzle_temperature. Run Pull from Filament DB to see its values, then send again.")
        assert not app.db.posts
        assert app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] == 230

    def test_the_local_edit_is_kept_for_review(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.orca.host.ui.while_open = lambda: app.db.filaments[PLAIN_ID]["temperatures"].update(nozzle=230)
        app.send()
        assert app.filaments.edited.is_dirty
        assert app.filaments.edited.config_value("nozzle_temperature") == "222"
        assert bundle_preset(app, "Plain PLA")["nozzle_temperature"] == ["215"]

    def test_none_of_the_edits_is_sent(self, app):
        edit(app, PLAIN, nozzle_temperature="222", filament_density="1.3")
        app.orca.host.ui.while_open = lambda: app.db.filaments[PLAIN_ID]["temperatures"].update(nozzle=230)
        assert "while you were deciding: nozzle_temperature." in app.send().message
        assert not app.db.posts
        assert app.db.filaments[PLAIN_ID]["density"] == 1.24

    def test_a_change_to_another_setting_while_confirming_does_not_stop_the_send(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.orca.host.ui.while_open = lambda: app.db.filaments[PLAIN_ID].update(density=1.5)
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.filaments[PLAIN_ID]["density"] == 1.5
        assert bundle_preset(app, "Plain PLA")["filament_density"] == ["1.5"]

    def test_calibration_changed_while_confirming_is_what_the_synced_preset_gets(self, app):
        """The synced preset is the ancestor for the next send, so an old value would show up as a conflict."""
        edit(app, CALIBRATED, nozzle_temperature="225")
        app.orca.host.ui.while_open = lambda: app.db.calibrations.update(
            {(CALIBRATED_ID, "0.4"): [{"keys": {"filament_flow_ratio": ["0.97"], "pressure_advance": ["0.05"]}}]})
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert bundle_preset(app, "Calibrated PLA")["pressure_advance"] == ["0.05"]

    def test_a_setting_renamed_while_confirming_goes_under_its_new_name(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_flow_ratio": ["1.05"]}
        edit(app, PLAIN, filament_flow_ratio="1.02")
        app.orca.host.ui.while_open = lambda: app.db.filaments[PLAIN_ID].update(
            settings={"extrusion_multiplier": ["1.05"]})
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.filaments[PLAIN_ID]["settings"] == {"extrusion_multiplier": ["1.02"]}

    def test_calibration_added_while_confirming_stops_the_send(self, app):
        """The edit would now be held back, which is not what the user agreed to, even at the same value."""
        edit(app, PLAIN, nozzle_temperature="222")
        app.orca.host.ui.while_open = lambda: app.db.calibrations.update(
            {(PLAIN_ID, "0.4"): [{"keys": {"nozzle_temperature": ["215"]}}]})
        assert "while you were deciding: nozzle_temperature." in app.send().message
        assert not app.db.posts

    def test_calibration_removed_while_confirming_sends_nothing_unconfirmed(self, app):
        """The held back edit would become sendable without the user having seen it."""
        edit(app, CALIBRATED, filament_flow_ratio="0.9", nozzle_temperature="225")
        app.orca.host.ui.while_open = lambda: app.db.calibrations.pop((CALIBRATED_ID, "0.4"))
        assert "while you were deciding: filament_flow_ratio." in app.send().message
        assert not app.db.posts

    def test_a_plate_row_added_while_confirming_stops_the_send(self, app):
        edit(app, PLAIN, textured_plate_temp="75")
        app.orca.host.ui.while_open = lambda: app.db.filaments[PLAIN_ID].update(
            bedTypeTemps=[{"bedType": "Textured PEI Plate", "temperature": 60}])
        assert "while you were deciding: textured_plate_temp." in app.send().message
        assert not app.db.posts

    def test_a_filament_deleted_while_confirming_is_reported(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.orca.host.ui.while_open = lambda: app.db.filaments.pop(PLAIN_ID)
        assert app.send().message == REMOVED_TEXT
        assert not app.db.posts


class TestARefreshThatFailsAfterTheSend:
    """Once the post succeeds the edit is in Filament DB, whatever happens to the read back or the reload."""

    SAVED = ("These edits were saved in Filament DB, but OrcaSlicer could not load them back: nozzle_temperature. "
             "Run Pull from Filament DB to bring OrcaSlicer up to date before you change them again. ")

    def test_a_refused_reload_says_the_edit_was_saved(self, app):
        """The issue as reported."""
        edit(app, PLAIN, nozzle_temperature="222")
        app.bundles.refuse = True
        result = app.send()
        assert result.status is PluginResult.Skipped
        assert result.message == self.SAVED + "The error was: Local bundle contains an invalid preset"
        assert app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] == "222"

    def test_a_failed_read_back_says_the_edit_was_saved(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.db.after_post = lambda: setattr(app.db, "fail_with", (503, {"error": "Database is restarting"}))
        result = app.send()
        assert result.status is PluginResult.Skipped
        assert result.message == self.SAVED + "The error was: Filament DB returned an error: Database is restarting"
        assert app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] == "222"

    def test_the_bundle_still_matches_what_orcaslicer_loaded(self, app):
        """The files are the conflict check's ancestor, so a refused reload must leave them alone."""
        edit(app, PLAIN, nozzle_temperature="222")
        app.bundles.refuse = True
        app.send()
        assert bundle_preset(app, "Plain PLA")["nozzle_temperature"] == ["215"]
        assert app.filaments.edited.config_value("nozzle_temperature") == "222"

    def test_sending_again_is_not_a_conflict_and_posts_nothing(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.bundles.refuse = True
        app.send()
        app.bundles.refuse = False
        result = app.send()
        assert result.status is PluginResult.Success
        assert result.message == ("Sent 0 of 1 edits to Filament DB. "
                                  "These were in Filament DB already: nozzle_temperature.")
        assert len(app.db.posts) == 1
        assert bundle_preset(app, "Plain PLA")["nozzle_temperature"] == ["222"]
        assert app.filaments.selected.config_value("nozzle_temperature") == "222"

    def test_sending_again_asks_nothing_when_there_is_nothing_to_post(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.bundles.refuse = True
        app.send()
        app.bundles.refuse = False
        app.orca.host.ui.dialogs.clear()
        app.send()
        assert not app.orca.host.ui.dialogs

    def test_pulling_brings_orcaslicer_up_to_date(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.bundles.refuse = True
        app.send()
        app.bundles.refuse = False
        app.pull()
        assert bundle_preset(app, "Plain PLA")["nozzle_temperature"] == ["222"]
        assert app.send().message == "There are no edits to send."

    def test_a_further_edit_waits_for_a_pull(self, app):
        """The bundle cannot remember what was posted, so the earlier value reads as Filament DB's own."""
        edit(app, PLAIN, nozzle_temperature="222")
        app.bundles.refuse = True
        app.send()
        app.bundles.refuse = False
        app.filaments.edited.values["nozzle_temperature"] = "225"
        assert "also changed in Filament DB: nozzle_temperature" in app.send().message
        app.pull()
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] == "225"

    def test_a_saved_copy_can_be_sent_again(self, app, plugin):
        """The synced preset still shows the old value, which must not read as a stale copy."""
        app.pull()
        TestSend.saved_copy(None, app, plugin)
        app.filaments.select("Edited Plain PLA", {"filament_density": "1.35"})
        app.bundles.refuse = True
        assert "were saved in Filament DB" in app.send().message
        app.bundles.refuse = False
        assert app.send().message == ("Sent 0 of 1 edits to Filament DB. "
                                      "These were in Filament DB already: filament_density.")
        assert len(app.db.posts) == 1

    def test_a_failure_before_the_post_still_says_nothing_was_saved(self, app):
        edit(app, CALIBRATED, filament_flow_ratio="0.9")
        app.bundles.refuse = True
        assert app.send().message == "Filament DB Sync failed: Local bundle contains an invalid preset"
        assert not app.db.posts


class TestAValueAlreadyInFilamentDb:
    """Filament DB can hold the edit already, from a send whose refresh failed or from someone else."""

    def test_is_not_a_conflict_and_the_other_edits_are_posted(self, app):
        edit(app, PLAIN, nozzle_temperature="222", filament_density="1.3")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 222
        assert app.send().message == ("Sent 1 of 2 edits to Filament DB. "
                                      "These were in Filament DB already: nozzle_temperature.")
        assert app.db.posts == [{"density": "1.3"}]

    def test_is_left_out_of_the_confirmation(self, app):
        edit(app, PLAIN, nozzle_temperature="222", filament_density="1.3")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 222
        app.send()
        assert app.orca.host.ui.dialogs == ['Send these changes to "Plain PLA" in Filament DB?\n\n'
                                            "filament_density: 1.24 -> 1.3"]

    def test_changed_while_confirming_stops_the_send(self, app):
        """The user agreed to leave it alone because Filament DB had it, which no longer holds."""
        edit(app, PLAIN, nozzle_temperature="222", filament_density="1.3")
        app.db.filaments[PLAIN_ID]["temperatures"]["nozzle"] = 222
        app.orca.host.ui.while_open = lambda: app.db.filaments[PLAIN_ID]["temperatures"].update(nozzle=230)
        assert "while you were deciding: nozzle_temperature." in app.send().message
        assert not app.db.posts


class TestCalibration:
    def test_a_calibrated_value_is_held_back_and_the_rest_is_sent(self, app):
        edit(app, CALIBRATED, filament_flow_ratio="0.9", nozzle_temperature="225")
        message = app.send().message
        assert message == ("Sent 1 of 2 edits to Filament DB. Change these in Filament DB, as they cannot be "
                           "sent: filament_flow_ratio.")
        assert app.db.posts == [{"temperatures": {"nozzle": "225"}}]

    def test_the_held_back_edit_stays_in_orcaslicer(self, app):
        edit(app, CALIBRATED, filament_flow_ratio="0.9", nozzle_temperature="225")
        app.send()
        assert app.filaments.edited.config_value("filament_flow_ratio") == "0.9"

    def test_only_calibrated_edits_post_nothing(self, app):
        edit(app, CALIBRATED, pressure_advance="0.05", enable_pressure_advance="0")
        message = app.send().message
        assert message.startswith("Sent 0 of 2 edits")
        assert "enable_pressure_advance, pressure_advance" in message
        assert not app.db.posts

    def test_a_nozzle_change_is_not_a_conflict(self, app):
        edit(app, CALIBRATED, filament_flow_ratio="0.9")
        app.state.nozzle = "0.6"
        assert "cannot be sent: filament_flow_ratio" in app.send().message

    def test_a_filament_without_calibration_sends_these_values(self, app):
        edit(app, PLAIN, filament_flow_ratio="0.95", pressure_advance="0.05", enable_pressure_advance="1")
        assert app.send().message == "Sent 3 of 3 edits to Filament DB."

    def test_every_calibrated_setting_is_held_back_not_just_flow(self, app):
        app.db.calibrations[(PLAIN_ID, "0.4")] = [{"keys": {"nozzle_temperature": ["230"]}}]
        edit(app, PLAIN, nozzle_temperature="235")
        assert "cannot be sent: nozzle_temperature" in app.send().message
        assert not app.db.posts

    def test_the_calibration_of_the_active_nozzle_decides(self, app):
        edit(app, CALIBRATED, nozzle_temperature="225")
        del app.db.calibrations[(CALIBRATED_ID, "0.6")]
        app.state.nozzle = "0.6"
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."

    def test_enable_pressure_advance_is_only_held_back_with_a_calibrated_value(self, app):
        app.db.calibrations[(PLAIN_ID, "0.4")] = [{"keys": {"filament_flow_ratio": ["0.97"]}}]
        edit(app, PLAIN, enable_pressure_advance="1", pressure_advance="0.05")
        assert app.send().message == "Sent 2 of 2 edits to Filament DB."

    def test_pull_says_when_it_left_calibration_out(self, app):
        app.db.calibrations[(PLAIN_ID, "0.4")] = [{"printer": "Bambu Labs H2D", "keys": {"filament_flow_ratio": ["0.8"]}}]
        assert app.pull().message.endswith("Calibration tied to other printers was left out: Bambu Labs H2D.")

    @pytest.mark.parametrize("typed", ["bambu labs h2d", " Bambu Labs H2D "])
    def test_the_printer_setting_ignores_case_and_spaces(self, app, typed):
        app.db.calibrations[(PLAIN_ID, "0.4")] = [{"printer": "Bambu Labs H2D", "keys": {"filament_flow_ratio": ["0.8"]}}]
        app.config["printer"] = typed
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_flow_ratio"] == ["0.8"]

    def test_another_printers_calibration_does_not_hold_an_edit_back(self, app):
        app.db.calibrations[(PLAIN_ID, "0.4")] = [{"printer": "Bambu Labs H2D", "keys": {"filament_flow_ratio": ["0.8"]}}]
        edit(app, PLAIN, filament_flow_ratio="0.95")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."


class TestStandardAndHighFlowNozzles:
    """One printer with a standard and a high-flow 0.4 mm nozzle, each with its own calibration."""

    STANDARD = {"printer": "Bambu Labs H2D", "highFlow": False, "keys": {"filament_flow_ratio": ["0.95"]}}
    HIGH_FLOW = {"printer": "Bambu Labs H2D", "highFlow": True, "keys": {"pressure_advance": ["0.02"]}}

    @pytest.fixture(autouse=True)
    def two_nozzles(self, app):
        app.config["printer"] = "Bambu Labs H2D"
        # As OrcaSlicer serializes the H2D's list: each extruder's variants, quoted for their commas.
        app.state.variants = ('"Direct Drive Standard,Direct Drive High Flow";'
                              '"Direct Drive Standard,Direct Drive High Flow,Direct Drive TPU High Flow"')
        app.db.calibrations[(PLAIN_ID, "0.4")] = [self.STANDARD, self.HIGH_FLOW]

    @pytest.mark.parametrize("high_flow_first", [False, True])
    def test_a_standard_nozzle_gets_the_standard_row(self, app, high_flow_first):
        if high_flow_first:
            app.db.calibrations[(PLAIN_ID, "0.4")].reverse()
        app.pull()
        preset = bundle_preset(app, "Plain PLA")
        assert preset["filament_flow_ratio"] == ["0.95"]
        assert "pressure_advance" not in preset

    @pytest.mark.parametrize("volume_type", ["High Flow", "TPU High Flow", "E3D High Flow", "Extra High Flow"])
    def test_switching_to_a_high_flow_nozzle_gets_the_high_flow_row(self, app, volume_type):
        app.pull()
        app.state.volume_type = volume_type
        assert app.pull().message.endswith("for a 0.4 mm high-flow nozzle.")
        preset = bundle_preset(app, "Plain PLA")
        assert preset["pressure_advance"] == ["0.02"]
        assert "filament_flow_ratio" not in preset

    def test_the_first_extruder_decides(self, app):
        app.state.volume_type = "High Flow,Standard"
        app.pull()
        assert bundle_preset(app, "Plain PLA")["pressure_advance"] == ["0.02"]

    def test_a_high_flow_nozzle_never_gets_a_standard_only_calibration(self, app):
        app.state.volume_type = "High Flow"
        app.pull()
        preset = bundle_preset(app, "Calibrated PLA")
        assert "filament_flow_ratio" not in preset
        assert "pressure_advance" not in preset

    def test_send_holds_back_the_standard_rows_settings_on_a_standard_nozzle(self, app):
        edit(app, PLAIN, filament_flow_ratio="0.9")
        assert "cannot be sent: filament_flow_ratio" in app.send().message
        assert not app.db.posts

    def test_send_holds_back_the_high_flow_rows_settings_on_a_high_flow_nozzle(self, app):
        app.state.volume_type = "High Flow"
        edit(app, PLAIN, filament_flow_ratio="0.9", pressure_advance="0.03")
        message = app.send().message
        assert message == ("Sent 1 of 2 edits to Filament DB. Change these in Filament DB, as they cannot be "
                           "sent: pressure_advance.")
        assert app.db.posts == [{"filament_flow_ratio": ["0.9"]}]

    def test_a_standard_nozzle_never_gets_a_high_flow_only_calibration(self, app):
        app.db.calibrations[(PLAIN_ID, "0.4")] = [self.HIGH_FLOW]
        app.pull()
        assert "pressure_advance" not in bundle_preset(app, "Plain PLA")

    def test_a_standard_nozzle_gets_a_row_whose_nozzle_has_no_high_flow_flag(self, app):
        """Filament DB counts a nozzle saved without the flag as standard, but high_flow=0 does not match it."""
        app.db.calibrations[(PLAIN_ID, "0.4")] = [{"printer": "Bambu Labs H2D", "keys": {"pressure_advance": ["0.05"]}}]
        app.pull()
        assert bundle_preset(app, "Plain PLA")["pressure_advance"] == ["0.05"]
        edit(app, PLAIN, pressure_advance="0.06")
        assert "cannot be sent: pressure_advance" in app.send().message
        assert not app.db.posts

    @pytest.mark.parametrize("variants", ["Direct Drive Standard", "Bowden Standard",
                                          'Direct Drive Standard;"Direct Drive Standard,Direct Drive High Flow"',
                                          None, '"unclosed'])
    def test_a_printer_that_offers_no_high_flow_variant_gets_either_row(self, app, variants):
        """Prusa's HF nozzle profiles list no high-flow variant, so their volume type stays Standard."""
        app.state.variants = variants
        app.db.calibrations[(PLAIN_ID, "0.4")] = [self.HIGH_FLOW]
        assert app.pull().message.endswith("for a 0.4 mm nozzle.")
        assert bundle_preset(app, "Plain PLA")["pressure_advance"] == ["0.02"]
        edit(app, PLAIN, pressure_advance="0.03")
        assert "cannot be sent: pressure_advance" in app.send().message
        assert not app.db.posts

    def test_a_high_flow_volume_type_counts_without_a_high_flow_variant(self, app):
        app.state.variants = "Direct Drive Standard"
        app.state.volume_type = "High Flow"
        app.pull()
        assert bundle_preset(app, "Plain PLA")["pressure_advance"] == ["0.02"]

    @pytest.mark.parametrize("volume_type", ["Hybrid", None, "", "Something New"])
    def test_a_nozzle_that_is_not_clearly_one_or_the_other_stops_the_pull(self, app, volume_type):
        app.state.volume_type = volume_type
        result = app.pull()
        assert result.status is PluginResult.Skipped
        assert result.message == (f"The printer's nozzle volume type is {volume_type or ''!r}, so it is not "
                                  "clear whether its standard or its high-flow calibration applies. Nothing "
                                  "was changed. Set the volume type to Standard or to a high-flow type, then "
                                  "run this again.")
        assert not (app.bundle_dir / "filament").exists()

    def test_a_hybrid_nozzle_stops_a_send(self, app):
        edit(app, PLAIN, nozzle_temperature="225")
        app.state.volume_type = "Hybrid"
        assert "Nothing was changed" in app.send().message
        assert not app.db.posts


class TestVerification:
    def test_reports_a_value_filament_db_did_not_keep(self, app):
        """Filament DB's export overrides its settings bag with structured fields such as shrinkage."""
        edit(app, PLAIN, filament_vendor='"New Vendor"')
        original = app.db.export

        def export(filament_id):
            profile = original(filament_id)
            profile["filament_vendor"] = ["Spike"]
            return profile

        app.db.export = export
        assert app.send().message == "Sent 0 of 1 edits to Filament DB. Filament DB did not keep: filament_vendor."

    def test_clearing_an_override_counts_as_kept(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_retraction_length": ["0.8"]}
        edit(app, PLAIN, filament_retraction_length="nil")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."


class TestQuotes:
    """Filament DB keeps a setting imported from PrusaSlicer as the text of its INI line, where quotes can
    wrap the value. Its own fields and the lists OrcaSlicer sends hold the value itself, quotes and all."""

    @pytest.mark.parametrize("settings", [{}, {"filament_vendor": '"Old"'}])
    def test_a_field_keeps_its_quotes(self, app, settings):
        """The field wins over INI text for the same setting in the settings bag."""
        app.db.filaments[PLAIN_ID].update(vendor='"Maker"', settings=settings)
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_vendor"] == ['"Maker"']

    @pytest.mark.parametrize("stored", [['"M117 hi"'], ['"M117 hi"', "M400"]])
    def test_a_list_from_orcaslicer_keeps_its_quotes(self, app, stored):
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_end_gcode": stored}
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_end_gcode"] == stored

    @pytest.mark.parametrize("stored, pulled", [
        ('"say \\"hi\\" C:\\\\x"', ['say "hi" C:\\x']),
        ("120 100", ["120 100"]),
        # The export decodes a value that runs over several lines itself, and its quotes are then the text's.
        ('"\\"a\\nb\\""', ['"a\nb"']),
    ])
    def test_a_prusaslicer_import_loses_the_quotes_of_its_ini_text(self, app, stored, pulled):
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_ramming_parameters": stored}
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_ramming_parameters"] == pulled

    def test_a_prusaslicer_name_is_translated_without_its_quotes(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"extrusion_multiplier": '"1.05"'}
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_flow_ratio"] == ["1.05"]

    def test_the_record_is_only_read_for_a_quoted_value(self, app):
        app.pull()
        assert app.db.record_reads == 0
        app.db.filaments[PLAIN_ID]["vendor"] = '"Maker"'
        app.pull()
        assert app.db.record_reads == 1

    def test_a_quoted_field_is_sent_and_kept(self, app):
        edit(app, PLAIN, filament_vendor='"\\"Maker\\""')
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"vendor": '"Maker"'}]
        assert bundle_preset(app, "Plain PLA")["filament_vendor"] == ['"Maker"']

    def test_a_quoted_list_value_is_sent_and_kept(self, app):
        edit(app, PLAIN, filament_end_gcode='"\\"M117 hi\\""')
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"filament_end_gcode": ['"M117 hi"']}]
        assert bundle_preset(app, "Plain PLA")["filament_end_gcode"] == ['"M117 hi"']

    def test_an_edit_over_a_prusaslicer_import_is_no_conflict(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_ramming_parameters": '"120 100"'}
        edit(app, PLAIN, filament_ramming_parameters='"130 100"')
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"filament_ramming_parameters": ["130 100"]}]

    def test_a_one_line_value_from_a_json_import_loses_its_quotes(self, app):
        """Filament DB's OrcaSlicer and Bambu Studio .json import (unwrap in bambuStudioImport.ts) stores a
        one-line value as a plain string, which its settings bag cannot tell apart from INI text."""
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_notes": '"Keep dry"'}
        app.pull()
        assert bundle_preset(app, "Plain PLA")["filament_notes"] == ["Keep dry"]

    def test_the_readback_after_a_send_removes_ini_quotes(self, app):
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_ramming_parameters": '"120 100"'}
        edit(app, PLAIN, filament_vendor='"Maker"')
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert bundle_preset(app, "Plain PLA")["filament_ramming_parameters"] == ["120 100"]

    def test_a_quoted_field_beside_a_prusaslicer_import_keeps_its_quotes(self, app):
        app.db.filaments[PLAIN_ID]["vendor"] = '"Maker"'
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_notes": '"a note"'}
        app.pull()
        preset = bundle_preset(app, "Plain PLA")
        assert (preset["filament_vendor"], preset["filament_notes"]) == (['"Maker"'], ["a note"])


class TestListValues:
    """OrcaSlicer writes a setting with a value per extruder variant as one line, in a form that depends
    on the setting's type, so each value has to come back out on its own."""

    @pytest.mark.parametrize("key, stored, edited, sent", [
        ("filament_retraction_length", ["1", "2"], "nil,2", ["nil", "2"]),
        ("filament_z_hop_types", ["Auto Lift", "Auto Lift"], "Normal Lift,nil", ["Normal Lift", "nil"]),
        ("filament_end_gcode", ["M400"], 'M400;"M117 one";M401', ["M400", "M117 one", "M401"]),
        ("filament_end_gcode", ["M400"], '"M117 one";M400', ["M117 one", "M400"]),
        ("filament_end_gcode", ["M400"], '"G1 X1, Y2; wipe";M400', ["G1 X1, Y2; wipe", "M400"]),
        ("filament_end_gcode", ["M400"], "G1,X1;M400", ["G1,X1", "M400"]),
        ("filament_end_gcode", ["M400"], '"M117 \\"hi\\" C:\\\\x";"one\\ntwo"', ['M117 "hi" C:\\x', "one\ntwo"]),
        ("filament_end_gcode", ["M400"], "M400;", ["M400", ""]),
    ])
    def test_each_variant_is_kept_in_filament_db_and_the_preset(self, app, key, stored, edited, sent):
        app.db.filaments[PLAIN_ID]["settings"] = {key: stored}
        edit(app, PLAIN, **{key: edited})
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{key: sent}]
        assert app.db.filaments[PLAIN_ID]["settings"][key] == sent
        assert bundle_preset(app, "Plain PLA")[key] == sent
        assert app.filaments.selected.config_value(key) == edited

    @pytest.mark.parametrize("key, edited, fused", [
        ("filament_retraction_length", "nil,2", ["nil,2"]),
        ("filament_end_gcode", 'M400;"M117 one";M401', ['M400;"M117 one";M401']),
    ])
    def test_values_run_together_in_filament_db_are_not_reported_as_kept(self, app, key, edited, fused):
        edit(app, PLAIN, **{key: edited})
        original = app.db.export

        def export(filament_id):
            # Only the read back after the post runs the values together.
            return {**original(filament_id), key: fused} if app.db.posts else original(filament_id)

        app.db.export = export
        assert app.send().message == f"Sent 0 of 1 edits to Filament DB. Filament DB did not keep: {key}."

    @pytest.mark.parametrize("edited", ['"unterminated', '"M117 one"M400', 'M400;M117 "one"'])
    def test_a_value_that_cannot_be_read_sends_nothing(self, app, edited):
        edit(app, PLAIN, filament_end_gcode=edited)
        result = app.send()
        assert result.message == "Nothing was sent, because OrcaSlicer's value for filament_end_gcode could not be read."
        assert app.db.posts == []


    def test_plugin_settings_on_the_preset_stay_local_and_do_not_stop_the_send(self, app):
        edit(app, PLAIN, filament_plugin_config_overrides='[{"name":"x","enabled":true}]', filament_retraction_length="1.2")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.posts == [{"filament_retraction_length": ["1.2"]}]


class TestErrors:
    def test_an_unreachable_server_is_explained(self, app):
        app.db.server.shutdown()
        app.db.server.server_close()
        message = app.pull().message
        assert message == f"Could not reach Filament DB at {app.db.url}. Is it running?"
        assert app.notifications[-1][0] is NotificationLevel.WarningNotificationLevel

    def test_a_wrong_api_key_is_explained(self, app):
        app.db.api_key = "secret"
        assert "Check the API key" in app.pull().message

    def test_the_api_key_is_sent_as_a_bearer_token(self, app):
        app.db.api_key = "secret"
        app.config["api_key"] = "secret"
        assert app.pull().status is PluginResult.Success

    def test_a_server_error_shows_its_message(self, app):
        app.db.fail_with = (400, {"error": "density must be a finite number"})
        assert app.pull().message == "Filament DB returned an error: density must be a finite number"

    def test_a_server_error_without_a_message_shows_the_status(self, app):
        app.db.fail_with = (502, b"Bad Gateway", True)
        assert app.pull().message == "Filament DB returned an error: HTTP 502"

    def test_a_response_that_is_not_json_is_explained(self, app):
        app.db.fail_with = (200, b"<html>", True)
        assert "not JSON" in app.pull().message

    def test_a_dropped_connection_is_explained(self, app, plugin, monkeypatch):
        """urllib wraps only errors in sending the request, not in reading the reply."""
        def dropped(*args, **kwargs):
            raise plugin.http.client.RemoteDisconnected("Remote end closed connection without response")

        monkeypatch.setattr(plugin.urllib.request.OpenerDirector, "open", dropped)
        assert app.pull().message == f"Filament DB at {app.db.url} closed the connection before it answered."

    def test_a_url_with_a_bad_port_is_explained(self, app):
        app.config["url"] = "http://localhost:abc"
        assert app.pull().message == ("The Filament DB URL http://localhost:abc is not valid. "
                                      "Check it in the plugin's config.")

    def test_a_url_that_is_not_http_is_refused(self, app):
        app.config["url"] = "file:///etc/passwd"
        assert "must start with http" in app.pull().message

    def test_a_trailing_slash_in_the_url_is_fine(self, app):
        app.config["url"] += "/"
        assert app.pull().status is PluginResult.Success

    def test_the_default_url_applies_without_config(self, app, plugin, monkeypatch):
        monkeypatch.setattr(plugin, "DEFAULT_URL", app.db.url)  # never the real Filament DB on this machine
        app.config.clear()
        assert app.pull().status is PluginResult.Success

    def test_denied_network_access_asks_for_a_retry(self, app, monkeypatch, plugin):
        def denied(*args, **kwargs):
            raise PermissionError("audit permission required")

        monkeypatch.setattr(plugin.urllib.request.OpenerDirector, "open", denied)
        assert "needs your permission" in app.pull().message

    def test_a_file_permission_error_is_not_mistaken_for_a_network_prompt(self, app, plugin, monkeypatch):
        def denied(path, content):
            raise PermissionError("Access is denied")

        monkeypatch.setattr(plugin, "write_json", denied)
        assert app.pull().message == "Filament DB Sync failed: Access is denied"

    def test_a_second_command_waits_its_turn(self, app, plugin):
        """OrcaSlicer starts a thread per click, and two runs would undo each other's files."""
        assert plugin.RUN_LOCK.acquire(blocking=False)
        result = app.pull()
        plugin.RUN_LOCK.release()
        assert result.status is PluginResult.Skipped
        assert result.message == "The last Filament DB command is still running."
        assert app.bundles.reloads == 0
        assert app.pull().status is PluginResult.Success

    def test_a_host_failure_is_reported_instead_of_raised(self, app, orca):
        """OrcaSlicer disables a plugin whose script raises, which removes its commands."""
        def refuse(bundle_id):
            raise RuntimeError("Local bundle contains an invalid preset")

        orca.host.preset.bundles.reload_local = refuse
        result = app.pull()
        assert result.status is PluginResult.Skipped
        assert result.message == "Filament DB Sync failed: Local bundle contains an invalid preset"


DUMMY_KEY = "dummy-key-not-a-secret"


def with_key(app):
    app.db.api_key = DUMMY_KEY
    app.config["api_key"] = DUMMY_KEY


def redirect(plugin, configured, target, key=DUMMY_KEY, response=None):
    """Ask the plugin's redirect policy what it would send to *target*, without needing TLS."""
    request = urllib.request.Request(configured + "/api/filaments/orcaslicer",
                                     headers={"Authorization": f"Bearer {key}"} if key else {})
    handler = plugin.SameOriginRedirects(configured)
    return handler.redirect_request(request, response or io.BytesIO(), 302, "Found", {}, target)


class TestRedirects:
    """The API key goes only to the scheme, host and port it was configured for."""

    def test_a_redirect_within_the_origin_keeps_the_api_key(self, app):
        with_key(app)
        app.config["url"] = app.db.url + MOVED_PREFIX
        assert app.pull().status is PluginResult.Success
        assert app.db.authorizations and set(app.db.authorizations) == {f"Bearer {DUMMY_KEY}"}

    def test_a_chain_of_redirects_within_the_origin_keeps_the_api_key(self, app):
        with_key(app)
        app.config["url"] = app.db.url + MOVED_PREFIX + MOVED_PREFIX
        assert app.pull().status is PluginResult.Success

    def test_a_redirect_to_another_port_is_not_followed(self, app, foreign):
        with_key(app)
        foreign.api_key = DUMMY_KEY
        app.config["url"] = app.db.url + MOVED_PREFIX
        app.db.moved_to = foreign.url
        message = app.pull().message
        assert message.startswith(f"Filament DB at {app.config['url']} redirected to {foreign.url}, which was not followed")
        assert foreign.authorizations == []

    def test_a_redirect_to_another_host_name_is_not_followed(self, app):
        """localhost and 127.0.0.1 reach the same server here, but are different origins."""
        with_key(app)
        app.config["url"] = app.db.url + MOVED_PREFIX
        app.db.moved_to = app.db.url.replace("127.0.0.1", "localhost")
        assert "which was not followed" in app.pull().message
        assert app.db.authorizations == []

    def test_a_hop_back_to_the_origin_is_never_reached(self, app, foreign):
        """Once off the origin, nothing that follows can be trusted to send the key home again."""
        with_key(app)
        app.config["url"] = app.db.url + MOVED_PREFIX
        app.db.moved_to = foreign.url + MOVED_PREFIX
        foreign.moved_to = app.db.url
        assert "which was not followed" in app.pull().message
        assert foreign.authorizations == app.db.authorizations == []

    def test_a_redirect_to_a_port_that_is_not_a_number_is_not_followed(self, app):
        with_key(app)
        app.config["url"] = app.db.url + MOVED_PREFIX
        app.db.moved_to = "http://127.0.0.1:abc"
        assert "redirected to http://127.0.0.1:abc, which was not followed" in app.pull().message

    def test_without_an_api_key_a_redirect_elsewhere_is_still_followed(self, app, foreign):
        app.config["url"] = app.db.url + MOVED_PREFIX
        app.db.moved_to = foreign.url
        assert app.pull().status is PluginResult.Success
        assert foreign.authorizations and set(foreign.authorizations) == {None}

    @pytest.mark.parametrize("configured, target", [
        ("https://db.example", "http://db.example/api"),  # a downgrade would send the key in the clear
        ("http://db.example", "https://db.example/api"),  # an upgrade is still another origin
        ("https://db.example", "https://db.example:8443/api"),
        ("https://db.example", "https://other.example/api"),
        # urllib would dial "x@127.0.0.1", and on some networks any machine can answer to that name
        ("http://127.0.0.1:3456", "http://x@127.0.0.1:3456/api"),
        ("http://127.0.0.1:3456", "http://x%2f@127.0.0.1:3456/api"),
        ("http://x@127.0.0.1:3456", "http://x@127.0.0.1:3456/api"),  # unknown never matches unknown
    ])
    def test_another_origin_never_gets_the_api_key(self, plugin, configured, target):
        response = io.BytesIO(b"Moved")
        with pytest.raises(plugin.SyncError, match="which was not followed"):
            redirect(plugin, configured, target, response=response)
        assert response.closed

    @pytest.mark.parametrize("configured, target", [
        ("https://db.example", "https://DB.Example:443/api"),
        ("http://db.example:80", "http://db.example/api"),
        ("http://127.0.0.1:3456", "http://127.0.0.1:3456/api"),
    ])
    def test_the_same_origin_keeps_the_api_key(self, plugin, configured, target):
        assert redirect(plugin, configured, target).get_header("Authorization") == f"Bearer {DUMMY_KEY}"

    def test_a_request_without_a_key_may_change_origin(self, plugin):
        assert redirect(plugin, "https://db.example", "http://other.example/api", key="").full_url == (
            "http://other.example/api")

    def test_the_refusal_shows_neither_the_path_nor_credentials_of_the_target(self, plugin):
        with pytest.raises(plugin.SyncError) as refusal:
            redirect(plugin, "https://db.example", "https://user:hunter2@evil.example/collect?token=abc")
        assert "redirected to https://evil.example," in str(refusal.value)
        assert "hunter2" not in str(refusal.value) and "token" not in str(refusal.value)


class TestValues:
    @pytest.mark.parametrize("key, value, expected", [
        ("filament_flow_ratio", "0.97", ["0.97"]),
        ("filament_flow_ratio", "0.97,0.98", ["0.97", "0.98"]),
        ("filament_shrink", "95%", ["95%"]),
        ("filament_z_hop", "-.5", ["-.5"]),
        ("filament_z_hop", "nil", ["nil"]),
        ("filament_z_hop", "nil,0.4", ["nil", "0.4"]),
        ("filament_z_hop_types", "Normal Lift,nil", ["Normal Lift", "nil"]),
        ("default_filament_colour", "#F7ADA6", ["#F7ADA6"]),
        ("filament_vendor", "plain,text", ["plain,text"]),
        ("filament_vendor", "1,2", ["1,2"]),
        ("filament_vendor", '"Bambu Lab"', ["Bambu Lab"]),
        ("filament_vendor", '"one";"two"', ["one", "two"]),
        ("filament_vendor", 'one;"two, three";four', ["one", "two, three", "four"]),
        ("filament_vendor", '"";x', ["", "x"]),
        ("filament_vendor", ";x", ["", "x"]),
        ("filament_vendor", '""', [""]),
        # OrcaSlicer only turns \\n and \\r back into other characters, and never writes \\t.
        ("filament_notes", '"a\\nb\\r\\t\\"c\\\\"', ['a\nb\rt"c\\']),
        # A setting missing from the plugin's list still reads as strings when only strings could hold this.
        ("filament_mixed_components", 'a;"b c"', ["a", "b c"]),
        # OrcaSlicer never writes a list of strings like these, so they are single strings.
        ("filament_mixed_components", 'say "hi"', ['say "hi"']),
        ("filament_mixed_components", '[{"name":"x"}];a\\nb', ['[{"name":"x"}];a\nb']),
        ("filament_end_gcode", 'say "hi"', None),
    ])
    def test_decode_value(self, plugin, key, value, expected):
        if expected is None:
            with pytest.raises(plugin.SyncError):
                plugin.decode_value(key, value)
        else:
            assert plugin.decode_value(key, value) == expected

    @pytest.mark.parametrize("first, second, same", [
        (["0.9"], ["0.90"], True),
        (["95%"], ["95"], True),
        (["a"], ["a"], True),
        (["a"], ["b"], False),
        (["1"], ["1", "1"], False),
        (["1"], ["2"], False),
        (["1", "2"], ["1", "3"], False),
        (None, None, True),
        (None, ["1"], False),
        (["nil"], None, True),
    ])
    def test_same_value(self, plugin, first, second, same):
        assert plugin.same_value(first, second) is same


class TestPackage:
    def test_registers_both_commands_and_asks_for_network_access(self, plugin, orca):
        plugin.FilamentDbSync().register_capabilities()
        assert [capability().get_name() for capability in orca.registered] == [plugin.PULL_NAME, plugin.SEND_NAME]
        assert orca.requested == {"network": ["http://localhost:3456", "http://127.0.0.1"]}

    def test_default_config_lists_the_settings(self, plugin):
        assert plugin.PullFromFilamentDb().get_default_config() == {
            "url": "http://localhost:3456", "api_key": "", "printer": "",
        }

    def test_a_missing_data_folder_is_reported(self, plugin, monkeypatch, tmp_path):
        monkeypatch.setattr(plugin, "__file__", str(tmp_path / "x" / "plugin.py"))
        with pytest.raises(plugin.SyncError):
            plugin.data_dir()
