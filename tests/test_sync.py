import json
import sys
import threading

import pytest

from conftest import BUNDLE_ID, CALIBRATED_ID, PLAIN_ID, FakePreset, NotificationLevel, PluginResult

PLAIN = f"_local/{BUNDLE_ID}/Plain PLA"
CALIBRATED = f"_local/{BUNDLE_ID}/Calibrated PLA"


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
        app.db.filaments[PLAIN_ID]["settings"] = {"filament_ramming_parameters": [stored]}
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

    def test_an_empty_export_leaves_the_presets_alone(self, app):
        app.pull()
        app.db.filaments.clear()
        result = app.pull()
        assert result.status is PluginResult.Skipped
        assert "has no filaments" in result.message
        assert PLAIN in app.filaments.presets

    def test_an_empty_export_is_fine_on_a_first_pull(self, app):
        app.db.filaments.clear()
        assert app.pull().message.startswith("Pulled 0 filaments")

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
        assert plugin.preset_files([first]) == {"A-B": first}
        assert plugin.preset_files([added, first]) == {"A-B": first, "A-B (2)": added}

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
        path = plugin.data_dir_for_test / "user" / "Copy.json"
        path.write_text(json.dumps({"filament_id": "fdb_" + "c" * 24}))
        app.filaments.edited = FakePreset("Copy", path)
        assert "not been pulled" in app.send().message

    def test_a_filament_deleted_in_filament_db_is_reported(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        del app.db.filaments[PLAIN_ID]
        assert "no longer has this filament" in app.send().message

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
        assert app.send().message == ("Nothing was sent, because Filament DB holds other values for: "
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

    def test_a_filament_with_a_hot_plate_row_holds_the_bed_temperature_back(self, app):
        """The route would change the plain bed temperature, which the export does not even show."""
        app.db.filaments[PLAIN_ID]["bedTypeTemps"] = [{"bedType": "Hot Plate", "temperature": 70}]
        edit(app, PLAIN, hot_plate_temp="75")
        assert "cannot be sent: hot_plate_temp" in app.send().message
        assert not app.db.posts

    def test_the_record_is_only_read_for_a_bed_temperature(self, app):
        edit(app, PLAIN, nozzle_temperature="222")
        app.send()
        assert app.db.record_reads == 0
        edit(app, PLAIN, hot_plate_temp="65")
        assert app.send().message == "Sent 1 of 1 edits to Filament DB."
        assert app.db.record_reads == 1

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

        monkeypatch.setattr(plugin.urllib.request, "urlopen", denied)
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


class TestValues:
    @pytest.mark.parametrize("value, expected", [
        ("0.97", ["0.97"]),
        ("0.97,0.98", ["0.97", "0.98"]),
        ("95%", ["95%"]),
        ("-.5", ["-.5"]),
        ("#F7ADA6", ["#F7ADA6"]),
        ("plain,text", ["plain,text"]),
        ('"Bambu Lab"', ["Bambu Lab"]),
        ('"one";"two"', ["one", "two"]),
        ('"a\\nb\\t\\"c\\\\"', ['a\nb\t"c\\']),
        ('"unterminated', []),
        ("nil", ["nil"]),
    ])
    def test_decode_value(self, plugin, value, expected):
        assert plugin.decode_value(value) == expected

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
