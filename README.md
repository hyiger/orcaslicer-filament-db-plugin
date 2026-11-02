# Filament DB Sync for OrcaSlicer

Keeps OrcaSlicer's filament presets in step with [Filament DB](https://github.com/hyiger/filament-db), in both directions. Requires OrcaSlicer 2.5.0-rc or later and a running copy of Filament DB.

## Using it

1. Install `orca_filament_db_sync_any.py` from OrcaSlicer's Plugins window. OrcaSlicer asks once for permission to reach Filament DB.
2. Run **Pull from Filament DB**. Your filaments appear in the filament dropdown.
3. Change a setting in OrcaSlicer and run **Send to Filament DB**. Do not press Save.

**Pull from Filament DB** replaces the synced presets with what Filament DB holds, including the calibration for the nozzle on the active printer. Run it again after you change something in Filament DB or switch to another nozzle size. Unsent edits in OrcaSlicer are kept.

**Send to Filament DB** shows your unsaved changes to the selected filament, old value and new, and sends them once you press OK. It then pulls that filament back. It sends only those settings, so a variant keeps inheriting everything else from its parent.

## Settings

These are in the Config tab of **Pull from Filament DB**.

| Setting | Use |
| --- | --- |
| `printer` | Your printer's name in Filament DB, such as `Prusa Core One INDX`. Set this if your calibrations are tied to printers: without it, calibration for a named printer is left out, so tuning for one machine never lands on another. Pull names any printer whose calibration it left out. |
| `url` | Where Filament DB runs, if not `http://localhost:3456`. Keep it on `localhost` or `127.0.0.1`: elsewhere, OrcaSlicer asks for permission for each individual address the plugin reads, and each prompt stops the command, so a Pull would need one run per filament. |
| `api_key` | The key, if your Filament DB sets `FILAMENTDB_API_KEY`. |

## Good to know

- **Do not press Save on a synced preset.** OrcaSlicer cannot overwrite a preset from a bundle, so Save makes you pick a new name and leaves a separate copy. Changes already saved in a copy are not sent, because the plugin cannot tell them from values that have since moved on in Filament DB. Unsaved changes made on top of a copy are sent, to the filament it came from, unless Filament DB holds a newer value for that setting than the copy shows.
- **Check the list before pressing OK, especially after opening a project.** OrcaSlicer loads a project's filament values on top of the preset as unsaved edits, so the plugin cannot tell an old project value from a change you made. Press Cancel if a value is one you did not mean to change.
- **Calibration, shrinkage and notes cannot be sent.** Filament DB offers no way to write them through this route, so a changed flow ratio, pressure advance or calibrated temperature is held back, and the notification lists it. The edit stays in OrcaSlicer until you discard it, or change the value in Filament DB to match and pull. A filament with no calibration for the active nozzle and printer sends flow ratio and pressure advance normally, as a value for the whole filament.
- **A bed temperature held in a Hot Plate row in Filament DB is also held back,** and so is an edit to one of several extruder variants. Change those in Filament DB too.
- **Nothing is sent if the same setting also changed in Filament DB.** The notification lists the settings. Pull, then send again: your edit is kept, and it wins.
- **Pull stops if a filament with unsent edits was renamed or removed in Filament DB,** so the edits cannot land on another filament. For a rename, send or discard them, then pull again. For a removal, discard them.
- **Settings Filament DB does not hold use OrcaSlicer's defaults,** not the ones of the material's generic preset. A filament imported from PrusaSlicer or Bambu Studio is the most complete. This includes a max volumetric speed of 0, which means no limit in PrusaSlicer and which OrcaSlicer does not accept.
- **Every bed type gets the filament's bed temperature,** unless Filament DB has one for that bed type.
- **The calibration comes from the first nozzle of the active printer.** A toolchanger with mixed nozzle sizes gets the first tool's values.
- **Syncing is by command.** Nothing happens until you run one, and a change made in Filament DB does not appear by itself.
- **Filaments deleted in OrcaSlicer are not deleted in Filament DB,** and a new filament made in OrcaSlicer cannot be sent. Create it in Filament DB.
- **Filament DB's template filaments are not pulled,** because its export leaves them out.

## How filaments imported from PrusaSlicer are handled

Filament DB keeps settings from a PrusaSlicer import under PrusaSlicer's names. The plugin changes them as follows, and a value under OrcaSlicer's own name always wins:

| In Filament DB | In the preset |
| --- | --- |
| `extrusion_multiplier` | `filament_flow_ratio` |
| `filament_retract_length`, `filament_retract_speed`, `filament_deretract_speed`, `filament_retract_lift` | `filament_retraction_length`, `filament_retraction_speed`, `filament_deretraction_speed`, `filament_z_hop` |
| `min_fan_speed`, `max_fan_speed`, `fan_below_layer_time` | `fan_min_speed`, `fan_max_speed`, `fan_cooling_layer_time` |
| `slowdown_below_layer_time`, `min_print_speed`, `disable_fan_first_layers` | `slow_down_layer_time`, `slow_down_min_speed`, `close_fan_the_first_x_layers` |

Other settings that are not valid in an OrcaSlicer filament preset are left out, including start and end G-code under PrusaSlicer's names. So are `inherits` and the `compatible_*` settings, which refer to PrusaSlicer's own presets and printers, and `nil` values, which mean the setting is not overridden. They stay in Filament DB untouched. An edit to a translated setting is sent under the name Filament DB has it, so its form and PrusaSlicer see the new value too.

Z shrinkage is converted too: Filament DB stores `0.2` for 0.2% and OrcaSlicer wants the remaining size, `99.8%`. Its export converts XY but not Z, and OrcaSlicer refuses to slice with a value outside 50% to 150%, so a preset with `0.2` would fail. A value of 50 or more came from an OrcaSlicer preset, and is left as it is.

## How it works

The presets live in a local preset bundle, `user/<user>/_local/filament-db/`. A bundle can be reloaded while OrcaSlicer runs, where ordinary user presets cannot.

That bundle also serves as the common ancestor of the two sides. Send compares it with Filament DB setting by setting, which is how it tells a conflict from an unrelated change without keeping any other sync state. If a write fails or OrcaSlicer refuses a reload, the old files go back, so the bundle matches what is loaded. If one of them cannot be put back, the notification names it, and a Pull repairs the bundle. After posting, Send reads the filament back and reports any value that Filament DB did not keep, such as a setting its export overrides from one of its own fields.

## Development

```bash
python -m venv .venv
.venv/bin/pip install pytest pytest-cov
.venv/bin/python -m pytest
```

`pytest` fails below 100% line and branch coverage. Tests run against a stand-in for OrcaSlicer's `orca` module and a stand-in Filament DB served over real HTTP, in `tests/conftest.py`. Both hold the rules found by testing in OrcaSlicer: a reload refuses a whole bundle over one invalid preset, `find_preset()` returns the edited copy of the selected preset, and the sync route takes a bare id.

## Testing in OrcaSlicer

The unit tests cannot reach the steps below, which need the running app. Use a copy of Filament DB with data you can lose.

- [ ] Pull. The filaments appear in the dropdown with their colors, and no permission prompt names a file operation.
- [ ] Edit a setting and send. The confirmation shows the old and new values. After OK, Filament DB has the new value, and the preset no longer shows as edited.
- [ ] Edit a setting, send and press Cancel. Filament DB is unchanged, and the edit stays.
- [ ] Edit a calibrated value and send. It is held back, and the calibration in Filament DB is unchanged.
- [ ] Change a value in Filament DB, edit it in OrcaSlicer and send. Nothing is sent. Pull and send again.
- [ ] Edit a variant and send. Its parent is unchanged.
- [ ] Save a preset under a new name and send it. Nothing is sent. Change a setting on the copy and send. Only that setting reaches the filament it came from.
- [ ] Slice a 100 mm cube with a filament whose Z shrinkage is 2%. The G-code reaches about 102 mm, which is `100 / 0.98`.
- [ ] Stop Filament DB and pull. The notification explains, and the plugin stays enabled.
