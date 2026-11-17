# Filament DB Sync for OrcaSlicer

Keeps OrcaSlicer's filament presets in step with [Filament DB](https://github.com/hyiger/filament-db), in both directions. Requires OrcaSlicer 2.5.0-rc or later and a running copy of Filament DB.

## Using it

1. Install `orca_filament_db_sync_any.py` from OrcaSlicer's Plugins window. OrcaSlicer asks once for permission to reach Filament DB.
2. Run **Pull from Filament DB**. Your filaments appear in the filament dropdown.
3. Change a setting in OrcaSlicer and run **Send to Filament DB**. Do not press Save.

**Pull from Filament DB** replaces the synced presets with what Filament DB holds, including the calibration for the nozzle on the active printer. Run it again after you change something in Filament DB or switch to another nozzle size, or between a standard and a high-flow nozzle. Unsent edits in OrcaSlicer are kept.

**Send to Filament DB** shows your unsaved changes to the selected filament, old value and new, and sends them once you press OK. It then pulls that filament back. It sends only those settings, so a variant keeps inheriting everything else from its parent.

To select the filaments loaded in a Prusa printer, use [PrusaLink Filament Sync](https://github.com/hyiger/orcaslicer-prusalink-filament-sync), a separate plugin. It can select the presets this one pulls.

## Settings

These are in the Config tab of **Pull from Filament DB**.

| Setting | Use |
| --- | --- |
| `printer` | Your printer's name in Filament DB, such as `Prusa Core One INDX`. Set this if your calibrations are tied to printers: without it, calibration for a named printer is left out, so tuning for one machine never lands on another. Pull names any printer whose calibration it left out. |
| `url` | Where Filament DB runs, if not `http://localhost:3456`. Keep it on `localhost` or `127.0.0.1`: elsewhere, OrcaSlicer asks for permission for each individual address the plugin reads, and each prompt stops the command, so a Pull would need one run per filament. |
| `api_key` | The key, if your Filament DB sets `FILAMENTDB_API_KEY`. |

## Good to know

- **Do not press Save on a synced preset.** OrcaSlicer cannot overwrite a preset from a bundle, so Save makes you pick a new name and leaves a separate copy. Changes already saved in a copy are not sent, because the plugin cannot tell them from values that have since moved on in Filament DB. Unsaved changes made on top of a copy are sent, to the filament it came from, unless the synced preset shows a different value for that setting than the copy, for example because Filament DB has a newer one or has stopped setting it.
- **Check the list before pressing OK, especially after opening a project.** OrcaSlicer loads a project's filament values on top of the preset as unsaved edits, so the plugin cannot tell an old project value from a change you made. Press Cancel if a value is one you did not mean to change.
- **Calibration, shrinkage and notes cannot be sent.** Filament DB offers no way to write them through this route, so a changed flow ratio, pressure advance or calibrated temperature is held back, and the notification lists it. The edit stays in OrcaSlicer until you discard it, or change the value in Filament DB to match and pull. A filament with no calibration for the active nozzle and printer sends flow ratio and pressure advance normally, as a value for the whole filament. So does a filament whose calibration for them is made for one bed type, since the preset leaves out everything in that calibration but its plate's bed temperatures.
- **A plate temperature that Filament DB holds in a bed type row is also held back,** including a row a variant takes from its parent, because Filament DB would store the edit where its row hides it. So is an edit to one of several extruder variants. Change those in Filament DB too.
- **Nothing is sent if the same setting also changed in Filament DB.** The notification lists the settings. Pull, then send again: your edit is kept, and it wins. Send checks again after you press OK, so a change made in Filament DB while the confirmation was open stops it the same way. Filament DB cannot refuse a write that arrives after a newer change, so one made in the instant between that check and the send can still be overwritten.
- **If Send saves your edits but cannot load them back,** because Filament DB stopped answering or OrcaSlicer refused the reload, the notification says the edits were saved. Pull to bring OrcaSlicer up to date before you change those settings again. Until then, sending again without changing them also works: a setting Filament DB already holds at your value is not a conflict, and it is not sent twice. A further change to one of them is reported as a change made in Filament DB until you pull.
- **Pull stops if a preset with unsent edits would get a new name or be removed,** so the edits cannot land on another filament. A rename in Filament DB gives it a new name, and so can the file name rules in the last point below, such as for a name an earlier version pulled under a Windows device name. For a new name, send or discard them, then pull again. For a filament removed from Filament DB, discard them.
- **If Filament DB has no filaments left, Pull asks before removing the synced presets,** because a plugin pointed at the wrong copy of Filament DB gets the same empty answer. The question names the address and how many presets would go. Press OK to remove them, or Cancel to leave everything as it was. A preset with unsent edits still stops the pull after OK: discard them, then pull again.
- **Settings Filament DB does not hold use OrcaSlicer's defaults,** not the ones of the material's generic preset. A filament imported from PrusaSlicer or Bambu Studio is the most complete. This includes a max volumetric speed of 0, which means no limit in PrusaSlicer and which OrcaSlicer does not accept.
- **Every bed type gets the filament's bed temperature,** unless Filament DB has one for that bed type. A bed type's own temperature stays on its plate, the Hot Plate's included. A calibration for every bed type gives its bed temperature to each plate that has none of its own.
- **A calibration made for one bed type only sets that plate's bed temperatures.** Filament DB gives these only when the filament has no calibration for every bed type, and each plate gets its own. A plate's first layer takes its calibrated bed temperature, unless the calibration or the filament has a first layer temperature. A preset has a single value of everything else, such as the nozzle temperature or pressure advance, for all plates. Those values are left out, and so is the whole calibration when its bed type is not named exactly like one of OrcaSlicer's plates: Cool Plate, Engineering Plate, Hot Plate, Textured PEI Plate or Textured Cool Plate. The notification names the bed type in either case. To use those values on every plate, add a calibration without a bed type in Filament DB.
- **The calibration comes from the first nozzle of the active printer.** A toolchanger with mixed nozzle sizes gets the first tool's values.
- **A standard and a high-flow nozzle of the same size get their own calibration,** going by the printer's nozzle volume type. Every high-flow type counts as high flow, as Filament DB has one high-flow flag. A high-flow nozzle never gets a filament's standard calibration, or the reverse: a filament calibrated for only one kind has no calibration on the other. Telling them apart needs a printer profile that lets you choose a high-flow volume type. On one that offers only Standard, such as Prusa's HF nozzle profiles, the volume type says nothing about the nozzle, and neither does Hybrid, which mixes both kinds on one extruder. Such a nozzle gets whichever of the two Filament DB gives first.
- **Syncing is by command.** Nothing happens until you run one, and a change made in Filament DB does not appear by itself.
- **Filaments deleted in OrcaSlicer are not deleted in Filament DB,** and a new filament made in OrcaSlicer cannot be sent. Create it in Filament DB. Neither can a saved copy of a filament that was removed from Filament DB.
- **Filament DB's template filaments are not pulled,** because its export leaves them out.
- **A preset's name can differ a little from the filament's,** because OrcaSlicer names a preset after its file, and some names cannot be file names everywhere. Characters such as `/` and `:` become `-`, a Windows device name such as `NUL` or `COM1` gets a `_` after it, and a name too long for a file name is cut short and ends with a short code that keeps it apart from names that start the same way. On Windows the path of the preset folder counts too, because OrcaSlicer cannot use a path longer than 259 characters there, so a name can be cut sooner. Two names that end up alike get a number, such as `(2)`. A preset keeps its name, number included, for as long as its filament keeps its name in Filament DB, so adding or deleting another filament does not move it. A name that a filament slot uses is never given to a different filament, because OrcaSlicer would quietly put that filament in the slot: the slot gets another preset, the pull names it, and the other filament gets a number. A preset that an earlier version pulled under a device name moves to its new name on the next pull: send or discard its unsent edits first, then select it again.

## How filaments imported from PrusaSlicer are handled

Filament DB keeps settings from a PrusaSlicer import under PrusaSlicer's names. The plugin changes them as follows, and a value under OrcaSlicer's own name always wins:

| In Filament DB | In the preset |
| --- | --- |
| `extrusion_multiplier` | `filament_flow_ratio` |
| `filament_retract_length`, `filament_retract_speed`, `filament_deretract_speed`, `filament_retract_lift` | `filament_retraction_length`, `filament_retraction_speed`, `filament_deretraction_speed`, `filament_z_hop` |
| `min_fan_speed`, `max_fan_speed`, `fan_below_layer_time` | `fan_min_speed`, `fan_max_speed`, `fan_cooling_layer_time` |
| `slowdown_below_layer_time`, `min_print_speed`, `disable_fan_first_layers` | `slow_down_layer_time`, `slow_down_min_speed`, `close_fan_the_first_x_layers` |

Other settings that are not valid in an OrcaSlicer filament preset are left out, including start and end G-code under PrusaSlicer's names. So are `inherits` and the `compatible_*` settings, which refer to PrusaSlicer's own presets and printers, and `nil` values, which mean the setting is not overridden. They stay in Filament DB untouched. An edit to a translated setting is sent under the name Filament DB has it, so its form and PrusaSlicer see the new value too. The quotes PrusaSlicer puts around a value such as `"Generic PLA"` are removed. Filament DB keeps such a value as text, and it keeps a one-line value from an OrcaSlicer or Bambu Studio .json import the same way, so quotes around the whole of that value are removed too. Quotes in Filament DB's own fields, such as a vendor typed as `"Maker"`, and in values sent from OrcaSlicer are part of the value and are kept.

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

## Publishing

Each GitHub release publishes `orca_filament_db_sync_any.py` to the plugin's Orca Cloud listing, through `.github/workflows/publish-orcacloud.yml`, which follows [Orca Cloud's guide](https://cloud.orcaslicer.com/wiki/). The repository must first be connected to the listing, in its **Edit plugin** > **GitHub publishing**.

1. Raise `version` in the plugin's header and push.
2. Create a release whose tag is that version with a `v`, such as `v1.2.0`. Orca Cloud needs it higher than the listing's current version. The release notes become the changelog.

The workflow stops if the tag is not the header's version.

## Testing in OrcaSlicer

The unit tests cannot reach the steps below, which need the running app. Use a copy of Filament DB with data you can lose.

- [ ] Pull. The filaments appear in the dropdown with their colors, and no permission prompt names a file operation.
- [ ] Edit a setting and send. The confirmation shows the old and new values. After OK, Filament DB has the new value, and the preset no longer shows as edited.
- [ ] Edit a setting, send and press Cancel. Filament DB is unchanged, and the edit stays.
- [ ] Edit a calibrated value and send. It is held back, and the calibration in Filament DB is unchanged.
- [ ] On a printer whose nozzle size has a standard and a high-flow calibration in Filament DB, pull, switch the nozzle volume type and pull again. Each pull gets that nozzle's calibration.
- [ ] Change a value in Filament DB, edit it in OrcaSlicer and send. Nothing is sent. Pull and send again.
- [ ] Edit a variant and send. Its parent is unchanged.
- [ ] Save a preset under a new name and send it. Nothing is sent. Change a setting on the copy and send. Only that setting reaches the filament it came from.
- [ ] Slice a 100 mm cube with a filament whose Z shrinkage is 2%. The G-code reaches about 102 mm, which is `100 / 0.98`.
- [ ] Stop Filament DB and pull. The notification explains, and the plugin stays enabled.
