# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Filament DB Sync"
# description = "Keeps OrcaSlicer's filament presets in step with Filament DB, in both directions."
# author = "hyiger"
# version = "0.1.0"
# type = "script"
# ///
"""Filament DB Sync -- two-way sync of filament presets between OrcaSlicer and Filament DB.

Two commands do the work:

* "Pull from Filament DB" writes every filament to a local preset bundle and reloads it, with the
  calibration for the active nozzle on top. The bulk export leaves calibration out because it expects
  a slicer module to fetch it per nozzle, and this plugin is that module for a stock OrcaSlicer.
* "Send to Filament DB" shows the edits made to the selected preset, posts them once the user agrees,
  then pulls that filament back.

The second one exists because OrcaSlicer cannot save over a bundle preset: its Save dialog insists on
a new name. Sending the unsaved edits means nobody needs to press Save, which would leave a copy of
the preset behind. Only unsaved edits are ever sent. A saved copy keeps the filament's id, but it has
no recorded ancestor, so how it differs from the bundle says nothing about what the user changed.

The bundle on disk is the common ancestor of the two sides. Sending compares it with Filament DB,
setting by setting, so a value that changed on both sides is refused rather than overwritten, and no
extra sync state needs to be kept. That only holds while the files match what OrcaSlicer loaded, so a
reload that fails puts the old files back.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import orca

PULL_NAME = "Pull from Filament DB"
SEND_NAME = "Send to Filament DB"

DEFAULT_URL = "http://localhost:3456"
REQUEST_TIMEOUT_SECONDS = 30

BUNDLE_ID = "filament-db"
PROBE_ID = "filament-db-probe"
BUNDLE_METADATA = "bundle_metadata.json"
BUNDLE_PRESET_PREFIX = f"_local/{BUNDLE_ID}/"
ID_PREFIX = "fdb_"
MODIFIED_MARK = "* "
# Any valid version will do; a live reload refuses a preset without one.
PRESET_VERSION = "2.5.0.0"

METADATA_KEYS = ("name", "type", "from", "filament_id", "instantiation")
# Filaments imported from PrusaSlicer carry its parent presets and printer conditions, which mean
# nothing here. A missing parent fails the reload, and the [""] that compatible_printers often holds
# is a list naming one blank printer, so these never go into a preset.
SCOPE_KEYS = frozenset({
    "compatible_printers", "compatible_printers_condition", "compatible_prints", "compatible_prints_condition",
    "inherits",
})
UNSENT_KEYS = SCOPE_KEYS | {"filament_settings_id"}
Z_SHRINKAGE_KEY = "filament_shrinkage_compensation_z"
MAX_VOLUMETRIC_SPEED_KEY = "filament_max_volumetric_speed"
# PrusaSlicer's names for settings that mean the same here. OrcaSlicer has no such translation of its
# own, so a filament imported from PrusaSlicer would otherwise fall back to the default values.
PRUSASLICER_KEYS = {
    "disable_fan_first_layers": "close_fan_the_first_x_layers",
    "extrusion_multiplier": "filament_flow_ratio",
    "fan_below_layer_time": "fan_cooling_layer_time",
    "filament_deretract_speed": "filament_deretraction_speed",
    "filament_retract_length": "filament_retraction_length",
    "filament_retract_lift": "filament_z_hop",
    "filament_retract_speed": "filament_retraction_speed",
    "max_fan_speed": "fan_max_speed",
    "min_fan_speed": "fan_min_speed",
    "min_print_speed": "slow_down_min_speed",
    "slowdown_below_layer_time": "slow_down_layer_time",
}
ORCA_TO_PRUSASLICER = {orca_key: prusaslicer_key for prusaslicer_key, orca_key in PRUSASLICER_KEYS.items()}
# The sync route has no way to set the fields behind these, and a value sent to its settings bag
# would either be overridden on export or hide later changes to the field.
FIELD_ONLY_KEYS = frozenset({"filament_notes", "filament_shrink", Z_SHRINKAGE_KEY})
# Filament DB has one bed temperature, which Orca only reads for the High Temp Plate. A bed type with
# no value of its own would otherwise print at Orca's default of 45 C.
PLATE_KEYS = ("cool_plate_temp", "eng_plate_temp", "supertack_plate_temp", "textured_cool_plate_temp",
              "textured_plate_temp")
# Filament DB keeps these outside its settings bag, so they need its own names when sent.
STRUCTURED_FIELDS = {
    "default_filament_colour": "color",
    "filament_cost": "cost",
    "filament_density": "density",
    "filament_diameter": "diameter",
    MAX_VOLUMETRIC_SPEED_KEY: "maxVolumetricSpeed",
    "filament_type": "type",
    "filament_vendor": "vendor",
}
TEMPERATURE_FIELDS = {
    "hot_plate_temp": "bed",
    "hot_plate_temp_initial_layer": "bedFirstLayer",
    "nozzle_temperature": "nozzle",
    "nozzle_temperature_initial_layer": "nozzleFirstLayer",
    "nozzle_temperature_range_high": "nozzleRangeMax",
    "nozzle_temperature_range_low": "nozzleRangeMin",
}
# Filament DB keeps a bed type's own temperature in a row that its export prefers over the plain bed
# temperature, and the sync route can only write the plain one.
HOT_PLATE_KEYS = frozenset({"hot_plate_temp", "hot_plate_temp_initial_layer"})
HOT_PLATE_ROW = "Hot Plate"
# These fields hold one value, and a filament with several extruder variants has one per variant.
SINGLE_VALUE_KEYS = STRUCTURED_FIELDS.keys() | TEMPERATURE_FIELDS.keys()
MAX_LISTED_CHANGES = 15
MAX_SHOWN_VALUE_LENGTH = 40
NUMBER_LIST = re.compile(r"[+-]?\d*\.?\d+%?(,[+-]?\d*\.?\d+%?)*")
# A file name cannot hold these on every platform, and a preset takes its name from its file.
UNSAFE_NAME_CHARACTERS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# OrcaSlicer starts a new thread for every click of a Run button.
RUN_LOCK = threading.Lock()


class SyncError(Exception):
    """A failure to report to the user as it stands."""


class Api:
    def __init__(self, url, api_key, printer):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.printer = printer.strip()
        self.other_printers = set()
        if not self.url.startswith(("http://", "https://")):
            raise SyncError(f"The Filament DB URL must start with http:// or https://, not {url!r}.")

    def request(self, path, body=None, missing_ok=False):
        """Return the decoded response, or None for a 404 when missing_ok is set."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, headers=headers,
                                         method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except PermissionError as error:
            # OrcaSlicer asks in the background and denies this first attempt.
            raise SyncError("OrcaSlicer needs your permission to reach this address. "
                            "Allow it, then run this again.") from error
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            if error.code in (401, 403):
                raise SyncError("Filament DB refused the request. Check the API key in the plugin's config.") from error
            raise SyncError(f"Filament DB returned an error: {http_error_text(error)}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise SyncError(f"Could not reach Filament DB at {self.url}. Is it running?") from error
        except ValueError as error:
            raise SyncError(f"Filament DB at {self.url} sent a response that is not JSON.") from error


def http_error_text(error):
    try:
        return json.load(error)["error"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {error.code}"


def data_dir():
    """The folder holding OrcaSlicer's data. A plugin sits at a different depth once subscribed."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "user").is_dir() and (parent / "system").is_dir():
            return parent
    raise SyncError("Could not find OrcaSlicer's data folder.")


def write_metadata(bundle_dir, bundle_id=BUNDLE_ID):
    bundle_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": bundle_id,
        "name": "Filament DB",
        "version": "1",
        "description": "Filament presets synced from Filament DB",
        "author": "Filament DB Sync",
        "updated_time": int(time.time()),
    }
    (bundle_dir / BUNDLE_METADATA).write_text(json.dumps(metadata), encoding="utf-8")


def find_bundle_dir():
    """Return the bundle's folder for the active user.

    No API names the active user's folder, but list_ids() only looks there, so a probe bundle that it
    reports is in the right place. The probe needs an id of its own: the real bundle would be listed
    whichever folder was being tried, once the active user had one.
    """
    for user in sorted(path for path in (data_dir() / "user").iterdir() if path.is_dir()):
        local_dir = user / "_local"
        probe_dir = local_dir / PROBE_ID
        existed = local_dir.exists()
        write_metadata(probe_dir, PROBE_ID)
        try:
            found = PROBE_ID in orca.host.preset.bundles.list_ids()
        finally:
            (probe_dir / BUNDLE_METADATA).unlink()
            probe_dir.rmdir()
            if not existed:
                local_dir.rmdir()
        if found:
            return local_dir / BUNDLE_ID
    raise SyncError("Could not find the active user's preset folder.")


def preset_stem(name, taken):
    """Return a file name for the preset called *name* that does not collide with those in *taken*."""
    stem = UNSAFE_NAME_CHARACTERS.sub("-", name).strip(" .") or "Filament"
    candidate, number = stem, 1
    while candidate.casefold() in taken:
        number += 1
        candidate = f"{stem} ({number})"
    taken.add(candidate.casefold())
    return candidate


def write_file(path, data):
    """Replace *path* in one step, so that a failure cannot leave half a preset behind.

    The temporary file has to be opened by name. The host's sandbox cannot tell what a file
    descriptor refers to, so tempfile or os.fdopen would make OrcaSlicer ask the user for permission.
    """
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def write_json(path, content):
    write_file(path, json.dumps(content, indent=2, ensure_ascii=False).encode("utf-8"))


def preset_files(profiles):
    """Return {stem: profile}. Going by id keeps a stem with its filament when others come and go."""
    taken = set()
    return {preset_stem(profile["name"], taken): profile
            for profile in sorted(profiles, key=lambda profile: profile["filament_id"])}


def write_bundle(bundle_dir, presets):
    filament_dir = bundle_dir / "filament"
    filament_dir.mkdir(parents=True, exist_ok=True)
    # Stale files go first. On macOS and Windows a case-only rename would otherwise keep the old
    # file name, and the file name is the preset name.
    for path in filament_dir.glob("*.json"):
        if path.stem not in presets:
            path.unlink()
    for stem, profile in presets.items():
        write_json(filament_dir / f"{stem}.json", profile)
    write_metadata(bundle_dir)


def install_bundle(bundle_dir, presets):
    """Write the presets and load them, or leave everything as it was if OrcaSlicer refuses them."""
    filament_dir = bundle_dir / "filament"
    previous = {path.name: path.read_bytes() for path in filament_dir.glob("*.json")}
    try:
        write_bundle(bundle_dir, presets)
        orca.host.preset.bundles.reload_local(BUNDLE_ID)
    except Exception as error:
        restore_files(filament_dir, previous, error)
        raise


def restore_files(filament_dir, previous, cause):
    """Put back the files OrcaSlicer has loaded. One locked file must not cost the others."""
    stuck = []
    for path in filament_dir.glob("*.json"):
        if path.name not in previous:
            try:
                path.unlink()
            except OSError:
                stuck.append(path.name)
    for name, content in previous.items():
        try:
            write_file(filament_dir / name, content)
        except OSError:
            stuck.append(name)
    if stuck:
        raise SyncError(f"Could not put these synced presets back: {key_list(stuck)}. Close whatever is "
                        "using them and run Pull from Filament DB.") from cause


def read_bundle(bundle_dir):
    """Return {stem: profile} for the presets of the bundle."""
    return {path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in (bundle_dir / "filament").glob("*.json")}


def active_nozzle(bundle):
    value = bundle.full_config_value("nozzle_diameter") or ""
    nozzle = re.split("[,;]", value)[0].strip()
    try:
        float(nozzle)
    except ValueError:
        raise SyncError(f"Could not read the printer's nozzle diameter from {value!r}.") from None
    return nozzle


def bare_id(filament_id):
    return filament_id.removeprefix(ID_PREFIX)


def fetch_calibration(api, filament_id, nozzle):
    """Return the calibration for the nozzle, leaving out one that belongs to another printer.

    The route falls back to any row for the nozzle, and a machine's retraction, temperatures and
    chamber settings do not carry over to another one.
    """
    path = f"/api/filaments/{bare_id(filament_id)}/calibration?nozzle_diameter={nozzle}&format=orcaslicer"
    if api.printer:
        path += "&printer=" + urllib.parse.quote(api.printer)
    result = api.request(path, missing_ok=True)
    if not result:
        return {}
    printer = result.get("printer")
    if printer is None or printer.casefold() == api.printer.casefold():
        return result.get("calibration_orca", {})
    api.other_printers.add(printer)
    return {}


def unquoted(value):
    """Undo the INI quoting that values imported from PrusaSlicer keep in Filament DB."""
    return [decode_value(item)[0] if len(item) > 1 and item[0] == item[-1] == '"' else item for item in value]


def orca_z_shrinkage(value):
    """Convert Filament DB's shrinkage, 0.2 for 0.2%, to Orca's remaining size of 99.8%.

    The export converts XY and not Z, and Orca refuses to slice a value outside 50% to 150%. A value
    of 50 or more was imported from an Orca preset, so it is on Orca's scale already.
    """
    numbers = (float(item.rstrip("%")) for item in value)
    return [f"{round(number if number >= 50 else 100 - number, 4):g}%" for number in numbers]


def prepare_profile(profile, calibration, filament_keys):
    """Turn an exported profile into a preset that a live reload accepts.

    A reload refuses the whole bundle over one invalid preset, and a preset is invalid if it holds a
    known setting that does not belong in a filament preset. Filament DB's settings bag can hold
    anything, such as PrusaSlicer keys, so only the keys of a filament preset are kept. The same goes
    for "nil", which means not overridden, is refused by many settings, and is what leaving one out does.
    """
    settings = dict(profile)
    if "filament_colour" in settings:
        settings["default_filament_colour"] = settings.pop("filament_colour")
    for prusaslicer_key, orca_key in PRUSASLICER_KEYS.items():
        if prusaslicer_key in settings:
            settings.setdefault(orca_key, settings[prusaslicer_key])
    settings.update(calibration)
    if "pressure_advance" in calibration:
        settings["enable_pressure_advance"] = ["1"]
    # PrusaSlicer's 0 means no limit. OrcaSlicer turns it into 0.5 when the preset is selected, as an
    # edit the user never made, and Send would write that back.
    if same_value(settings.get(MAX_VOLUMETRIC_SPEED_KEY), ["0"]):
        del settings[MAX_VOLUMETRIC_SPEED_KEY]
    if "hot_plate_temp" in settings:
        settings.setdefault("hot_plate_temp_initial_layer", settings["hot_plate_temp"])
    for plate in PLATE_KEYS:
        for suffix in ("", "_initial_layer"):
            if "hot_plate_temp" + suffix in settings:
                settings.setdefault(plate + suffix, settings["hot_plate_temp" + suffix])
    if settings.get(Z_SHRINKAGE_KEY) not in (None, ["nil"]):
        settings[Z_SHRINKAGE_KEY] = orca_z_shrinkage(unquoted(settings[Z_SHRINKAGE_KEY]))

    prepared = {key: profile[key] for key in METADATA_KEYS if key in profile}
    prepared["version"] = PRESET_VERSION
    prepared.update({key: unquoted(value) for key, value in settings.items()
                     if key in filament_keys - SCOPE_KEYS and value != ["nil"]})
    return prepared


def fetch_profiles(api, nozzle, filament_keys):
    return [prepare_profile(profile, fetch_calibration(api, profile["filament_id"], nozzle), filament_keys)
            for profile in api.request("/api/filaments/orcaslicer")]


def fetch_export(api, filament_id):
    exported = api.request("/api/filaments/orcaslicer?ids=" + urllib.parse.quote(bare_id(filament_id)))
    if not exported:
        raise SyncError("Filament DB no longer has this filament. Run Pull from Filament DB to remove it.")
    return exported[0]


def decode_value(value):
    """Turn a serialized config value into the list form of a preset file."""
    if NUMBER_LIST.fullmatch(value):
        return value.split(",")
    if not value.startswith('"'):
        return [value]
    # Strings are double-quoted and C-escaped, and separated by semicolons.
    items, current, position = [], [], 1
    while position < len(value):
        character = value[position]
        if character == "\\":
            position += 1
            current.append({"n": "\n", "r": "\r", "t": "\t"}.get(value[position:position + 1], value[position:position + 1]))
        elif character == '"':
            items.append("".join(current))
            current = []
            position = value.find('"', position + 1)
            if position < 0:
                break
        else:
            current.append(character)
        position += 1
    return items


def same_value(first, second):
    """Compare preset values, so that a number written as 0.90 equals 0.9 and nil equals no value."""
    first, second = (None if value == ["nil"] else value for value in (first, second))
    if first is None or second is None:
        return first is second
    if len(first) != len(second):
        return False
    for one, other in zip(first, second):
        if one != other:
            try:
                if float(str(one).rstrip("%")) != float(str(other).rstrip("%")):
                    return False
            except ValueError:
                return False
    return True


def changed_settings(edited, base):
    """Return the settings of *edited* that differ from *base*, in preset file form."""
    return {
        key: decode_value(value)
        for key in edited.config_keys()
        if key not in UNSENT_KEYS and (value := edited.config_value(key)) != base.config_value(key)
    }


def request_body(changes, exported):
    body = {}
    for key, value in changes.items():
        if key in STRUCTURED_FIELDS:
            body[STRUCTURED_FIELDS[key]] = value[0]
        elif key in TEMPERATURE_FIELDS:
            body.setdefault("temperatures", {})[TEMPERATURE_FIELDS[key]] = value[0]
        elif ORCA_TO_PRUSASLICER.get(key) in exported and key not in exported:
            # A second key beside it would leave Filament DB's form and PrusaSlicer on the old value.
            body[ORCA_TO_PRUSASLICER[key]] = value
        else:
            body[key] = value
    return body


def preset_filament_id(preset):
    """Return the Filament DB id of *preset*, or None. The API does not expose a preset's filament id.

    A default preset has no file and a preset from a project names the project, which is outside the
    sandbox, so reading those would make OrcaSlicer ask the user for permission.
    """
    # data_dir() is resolved and preset.file is not, which matters if the data folder is a link.
    path = Path(preset.file).resolve()
    if path.suffix != ".json" or not path.is_relative_to(data_dir()):
        return None
    try:
        filament_id = json.loads(path.read_text(encoding="utf-8")).get("filament_id", "")
    except (OSError, ValueError):
        return None
    return filament_id if filament_id.startswith(ID_PREFIX) else None


def key_list(keys):
    return ", ".join(sorted(keys))


def display_name(preset):
    """Return the name OrcaSlicer shows, without the mark it puts on a preset with unsaved edits."""
    return preset.label().removeprefix(MODIFIED_MARK)


def refuse_moved_edits(edited, presets):
    """Stop unsent edits from landing on another filament.

    OrcaSlicer carries them across a reload by preset name, and the name may now be another filament's.
    """
    if edited.is_dirty and edited.name.startswith(BUNDLE_PRESET_PREFIX):
        filament_id = preset_filament_id(edited)
        kept = presets.get(edited.name.removeprefix(BUNDLE_PRESET_PREFIX))
        if kept and kept["filament_id"] == filament_id:
            return
        if any(profile["filament_id"] == filament_id for profile in presets.values()):
            raise SyncError(f'"{display_name(edited)}" has unsent edits, and it was renamed in Filament DB. '
                            "Send or discard them, then pull again. Nothing was changed.")
        raise SyncError(f'"{display_name(edited)}" has unsent edits, and it was removed from Filament DB. '
                        "Discard them, then pull again. Nothing was changed.")


def pull(api):
    bundle = orca.host.preset_bundle()
    filaments = bundle.filaments
    nozzle = active_nozzle(bundle)
    profiles = fetch_profiles(api, nozzle, set(filaments.edited_preset().config_keys()))
    bundle_dir = find_bundle_dir()
    if not profiles and any((bundle_dir / "filament").glob("*.json")):
        raise SyncError("Filament DB has no filaments, so the synced presets were left alone. "
                        "Check that the plugin is pointing at the right copy.")

    presets = preset_files(profiles)
    refuse_moved_edits(filaments.edited_preset(), presets)
    replaced = sorted(name.removeprefix(BUNDLE_PRESET_PREFIX) for name in set(bundle.current_filament_preset_names())
                      if name.startswith(BUNDLE_PRESET_PREFIX)
                      and name.removeprefix(BUNDLE_PRESET_PREFIX) not in presets)
    install_bundle(bundle_dir, presets)

    message = f"Pulled {len(profiles)} filaments from Filament DB for a {nozzle} mm nozzle."
    if replaced:
        message += f" These were removed, so OrcaSlicer chose other presets for them: {key_list(replaced)}."
    if api.other_printers:
        message += f" Calibration tied to other printers was left out: {key_list(api.other_printers)}."
    return message


def has_hot_plate_row(api, filament_id):
    record = api.request(f"/api/filaments/{bare_id(filament_id)}")
    return any(row.get("bedType") == HOT_PLATE_ROW for row in record.get("bedTypeTemps") or [])


def shown_value(value):
    if value is None:
        return "not set"
    text = ", ".join(str(item) for item in value)
    return text if len(text) <= MAX_SHOWN_VALUE_LENGTH else text[:MAX_SHOWN_VALUE_LENGTH] + "..."


def confirm_send(name, sendable, theirs):
    """Show what will change, since a project reopened over a newer value looks like an edit.

    OrcaSlicer loads a project's values on top of the preset as unsaved edits, so the plugin cannot
    tell them from changes the user made.
    """
    lines = [f"{key}: {shown_value(theirs.get(key))} -> {shown_value(value)}" for key, value in sorted(sendable.items())]
    if len(lines) > MAX_LISTED_CHANGES:
        lines[MAX_LISTED_CHANGES:] = [f"and {len(lines) - MAX_LISTED_CHANGES} more"]
    text = f'Send these changes to "{name}" in Filament DB?\n\n' + "\n".join(lines)
    if orca.host.ui.message(text, SEND_NAME, "ok_cancel", "question") != "ok":
        raise SyncError("Nothing was sent.")


def send(api):
    bundle = orca.host.preset_bundle()
    filaments = bundle.filaments
    edited = filaments.edited_preset()
    filament_id = preset_filament_id(edited)
    if not filament_id:
        raise SyncError(f'"{display_name(edited)}" did not come from Filament DB, so there is nowhere to send it.')

    bundle_dir = find_bundle_dir()
    presets = read_bundle(bundle_dir)
    stem = next((stem for stem, profile in presets.items() if profile["filament_id"] == filament_id), None)
    if stem is None:
        raise SyncError("This filament has not been pulled yet. Run Pull from Filament DB first.")
    ancestor = presets[stem]

    selected = filaments.selected_preset()
    changes = changed_settings(edited, selected)
    if not changes:
        if edited.name != BUNDLE_PRESET_PREFIX + stem:
            raise SyncError("Only unsaved edits can be sent. Make the change on the synced preset instead.")
        raise SyncError("There are no edits to send.")

    nozzle = active_nozzle(bundle)
    filament_keys = set(edited.config_keys())
    exported = fetch_export(api, filament_id)
    calibration = fetch_calibration(api, filament_id, nozzle)
    theirs = prepare_profile(exported, calibration, filament_keys)

    # Calibration has to be set aside first: after a nozzle change it differs from the ancestor
    # without being a conflict.
    held_back = set(calibration) | FIELD_ONLY_KEYS
    if "pressure_advance" in calibration:
        held_back.add("enable_pressure_advance")
    if HOT_PLATE_KEYS & set(changes) and has_hot_plate_row(api, filament_id):
        held_back |= HOT_PLATE_KEYS
    held_back |= {key for key, value in changes.items() if key in SINGLE_VALUE_KEYS and len(value) > 1}
    held_back &= set(changes)
    sendable = {key: value for key, value in changes.items() if key not in held_back}

    conflicts = [key for key in sendable if not same_value(theirs.get(key), ancestor.get(key))]
    if conflicts:
        raise SyncError(f"Nothing was sent, because these also changed in Filament DB: {key_list(conflicts)}. "
                        "Run Pull from Filament DB to see its values, then send again.")
    if edited.name != BUNDLE_PRESET_PREFIX + stem:
        # A saved copy stays as it was when a pull moves the bundle on, so the bundle says nothing
        # about what the user could see when making this edit.
        hidden = [key for key in sendable
                  if key in theirs and not same_value(theirs[key], decode_value(selected.config_value(key) or ""))]
        if hidden:
            raise SyncError(f"Nothing was sent, because Filament DB holds other values for: {key_list(hidden)}. "
                            "Make the change on the synced preset instead.")

    if sendable:
        confirm_send(display_name(edited), sendable, theirs)
        api.request(f"/api/filaments/{bare_id(filament_id)}/orcaslicer", request_body(sendable, exported))
        theirs = prepare_profile(fetch_export(api, filament_id), calibration, filament_keys)
    install_bundle(bundle_dir, {**presets, stem: theirs})

    dropped = [key for key, value in sendable.items() if not same_value(theirs.get(key), value)]
    message = f"Sent {len(sendable) - len(dropped)} of {len(changes)} edits to Filament DB."
    if held_back:
        message += f" Change these in Filament DB, as they cannot be sent: {key_list(held_back)}."
    if dropped:
        message += f" Filament DB did not keep: {key_list(dropped)}."
    return message


def run_command(command, config):
    """Run *command* and show its outcome, since a script's return value is easy to miss."""
    levels = orca.host.ui.NotificationLevel
    locked = RUN_LOCK.acquire(blocking=False)
    try:
        if not locked:
            raise SyncError("The last Filament DB command is still running.")
        api = Api(config.get("url") or DEFAULT_URL, config.get("api_key") or "", config.get("printer") or "")
        message = command(api)
    except SyncError as error:
        message = str(error)
    except Exception as error:
        # OrcaSlicer disables a plugin whose script raises, which would remove the commands from the
        # Plugins window over something as passing as a preset it refused to load.
        message = f"Filament DB Sync failed: {error}"
    else:
        orca.host.ui.push_notification(levels.RegularNotificationLevel, message)
        return orca.ExecutionResult.success(message)
    finally:
        if locked:
            RUN_LOCK.release()
    orca.host.ui.push_notification(levels.WarningNotificationLevel, message)
    return orca.ExecutionResult.skipped(message)


class PullFromFilamentDb(orca.script.ScriptPluginCapabilityBase):
    # The one place that holds the settings, so that they are only entered once.
    active = None

    def get_name(self):
        return PULL_NAME

    def get_default_config(self):
        return {"url": DEFAULT_URL, "api_key": "", "printer": ""}

    def on_load(self):
        PullFromFilamentDb.active = self

    def execute(self):
        return run_command(pull, stored_config())


class SendToFilamentDb(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return SEND_NAME

    def execute(self):
        return run_command(send, stored_config())


def stored_config():
    return json.loads(PullFromFilamentDb.active.get_config())


@orca.plugin
class FilamentDbSync(orca.base):
    def register_capabilities(self):
        orca.request_permissions(network=[DEFAULT_URL, "http://127.0.0.1"])
        orca.register_capability(PullFromFilamentDb)
        orca.register_capability(SendToFilamentDb)
