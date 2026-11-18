# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Filament DB Sync"
# description = "Keeps OrcaSlicer's filament presets in step with Filament DB, in both directions."
# author = "hyiger"
# version = "0.2.0"
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
import functools
import hashlib
import http.client
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
# A URL that leaves the port out means these, so http://host and http://host:80 are one origin.
DEFAULT_PORTS = {"http": 80, "https": 443}

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
# filament_plugin_config_overrides holds this install's settings for its plugins, which can include an
# API key like this plugin's own. It is not filament data, so it stays out of Filament DB.
UNSENT_KEYS = SCOPE_KEYS | {"filament_settings_id", "filament_plugin_config_overrides"}
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
# Filament DB keeps a bed type's own temperatures in a row, which its export puts over the settings bag
# and the plain bed temperature. The sync route cannot write a row, so an edit sent for one of these
# settings would be stored out of sight and turn up once the row is removed. Each row gives the
# settings for its temperature and then for its first layer temperature.
PLATE_ROWS = {
    "Cool Plate": ("cool_plate_temp", "cool_plate_temp_initial_layer"),
    "Engineering Plate": ("eng_plate_temp", "eng_plate_temp_initial_layer"),
    "Hot Plate": ("hot_plate_temp", "hot_plate_temp_initial_layer"),
    "Textured PEI Plate": ("textured_plate_temp", "textured_plate_temp_initial_layer"),
    "Textured Cool Plate": ("textured_cool_plate_temp", "textured_cool_plate_temp_initial_layer"),
}
PLATE_ROW_KEYS = frozenset(key for keys in PLATE_ROWS.values() for key in keys)
HOT_PLATE_KEYS = PLATE_ROWS["Hot Plate"]
# The plate settings that take the filament's bed temperature when Filament DB has none of their own.
PLATE_FALLBACK_KEYS = tuple(plate + suffix for plate in PLATE_KEYS for suffix in ("", "_initial_layer"))
# These fields hold one value, and a filament with several extruder variants has one per variant.
SINGLE_VALUE_KEYS = STRUCTURED_FIELDS.keys() | TEMPERATURE_FIELDS.keys()
# OrcaSlicer's nozzle volume types, as Filament DB's one high-flow flag sees them. Hybrid is left out: it
# is an extruder holding nozzles of both kinds, so it does not say which calibration a filament prints with.
HIGH_FLOW_VOLUME_TYPES = {"Standard": False, "High Flow": True, "TPU High Flow": True, "E3D High Flow": True,
                          "Extra High Flow": True}
MAX_LISTED_CHANGES = 15
MAX_SHOWN_VALUE_LENGTH = 40
# The lists of strings in a filament preset, from OrcaSlicer's PrintConfig.cpp. The host hands values over
# as text without their type, and OrcaSlicer writes these with semicolons between the values, quoting
# one only when it holds a space, a semicolon, a quote, a backslash or a line break. Every other list is
# numbers, percents, booleans or option names with commas between them, and nil where not overridden.
STRING_LIST_KEYS = frozenset({
    "adaptive_pressure_advance_model", "compatible_printers", "compatible_prints", "default_filament_colour",
    "filament_change_extrusion_role_gcode", "filament_colour", "filament_colour_type",
    "filament_dev_ams_drying_ams_limitations", "filament_end_gcode", "filament_extruder_variant", "filament_ids",
    "filament_multi_colour", "filament_notes", "filament_ramming_parameters", "filament_settings_id",
    "filament_start_gcode", "filament_type", "filament_vendor", "volumetric_speed_coefficients",
})
QUOTED_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)
# One value of a list of strings: quoted, or bare up to the next semicolon.
STRING_ITEM = re.compile(QUOTED_STRING.pattern + r'|([^";]*)', re.DOTALL)
# A file name cannot hold these on every platform, and a preset takes its name from its file.
UNSAFE_NAME_CHARACTERS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Windows opens a device for these names, also with an extension or spaces after them.
WINDOWS_DEVICE_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(?= *(?:\.|$))",
                                 re.IGNORECASE)
# Common file systems allow 255 bytes in a file name, and a preset is first written to its name plus this.
# Windows counts UTF-16 units instead, of which a name never has more than it has UTF-8 bytes.
MAX_FILE_NAME_BYTES = 255
TEMPORARY_SUFFIX = ".json.tmp"
# On Windows a whole path is held to 259 UTF-16 units unless the program's manifest asks for long paths.
# OrcaSlicer's does not, and the plugin runs inside it, so the folder takes room from the file name.
MAX_PATH_UNITS = 259 if os.name == "nt" else None
# The number preset_stem() puts after a file name that another filament has already.
NUMBERED_STEM = re.compile(r".* \(([0-9]+)\)", re.DOTALL)
# OrcaSlicer starts a new thread for every click of a Run button.
RUN_LOCK = threading.Lock()
# OrcaSlicer asks in the background and denies the attempt that made it ask.
PERMISSION_TEXT = "OrcaSlicer needs your permission to reach this address. Allow it, then run this again."


class SyncError(Exception):
    """A failure to report to the user as it stands."""


class Calibration(dict):
    """The settings a calibration gives a preset. *every_bed* is False for one made for a single bed type,
    whose bed temperatures are then that plate's alone."""

    def __init__(self, keys=(), every_bed=True):
        super().__init__(keys)
        self.every_bed = every_bed


class Api:
    def __init__(self, url, api_key, printer):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.printer = printer.strip()
        self.other_printers = set()
        self.other_beds = set()
        self.unknown_beds = set()
        if not self.url.startswith(("http://", "https://")):
            raise SyncError(f"The Filament DB URL must start with http:// or https://, not {url!r}.")
        self.opener = urllib.request.build_opener(SameOriginRedirects(self.url))

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
            with self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except PermissionError as error:
            raise SyncError(PERMISSION_TEXT) from error
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            if error.code in (401, 403):
                raise SyncError("Filament DB refused the request. Check the API key in the plugin's config.") from error
            raise SyncError(f"Filament DB returned an error: {http_error_text(error)}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise SyncError(f"Could not reach Filament DB at {self.url}. Is it running?") from error
        # urllib wraps only the errors of sending the request: these come from a bad port or from the reply.
        except http.client.InvalidURL as error:
            raise SyncError(f"The Filament DB URL {self.url} is not valid. Check it in the plugin's config.") from error
        except (http.client.HTTPException, ConnectionError) as error:
            raise SyncError(f"Filament DB at {self.url} closed the connection before it answered.") from error
        except ValueError as error:
            raise SyncError(f"Filament DB at {self.url} sent a response that is not JSON.") from error


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Stops a request that carries the API key from following a redirect to another origin.

    urllib copies every header but the content ones onto the redirected request, so left to itself it
    would hand the key to whatever host the server, or anything between, points it at. Following such
    a redirect without the key would not help: the new host would refuse it and the user would be told
    to check a key that is fine, after OrcaSlicer asked them to allow a host they never entered.
    Stopping at the first hop off the origin also means a later hop back is never reached.
    """

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.origin = origin(url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = origin(newurl)
        # None is a target urllib would not dial as written, so it never matches, even a configured None.
        if req.has_header("Authorization") and (target is None or target != self.origin):
            fp.close()
            raise SyncError(f"Filament DB at {self.url} redirected to {site(newurl)}, which was not followed so "
                            "that the API key stays with the configured address. If Filament DB has moved, "
                            "put its new address in the plugin's config.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def origin(url):
    """Return the scheme, host and port that the API key is scoped to, or None when they cannot be trusted.

    That is a port that is not a number, or credentials before the host: urlsplit skips those, but urllib
    dials them as part of the host name, so http://x@db:3456 would go to a host called "x@db".
    """
    parts = urllib.parse.urlsplit(url)
    if "@" in parts.netloc:
        return None
    try:
        port = parts.port or DEFAULT_PORTS.get(parts.scheme)
    except ValueError:
        return None
    return parts.scheme, parts.hostname, port


def site(url):
    """Return *url* without its path, which can hold a token, or any credentials before the host."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


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


def utf16_units(text):
    return len(text.encode("utf-16-le")) // 2


def file_name_room(filament_dir):
    """Return how many UTF-16 units a file name in *filament_dir* can have, the temporary suffix included."""
    if MAX_PATH_UNITS is None:
        return MAX_FILE_NAME_BYTES
    return min(MAX_FILE_NAME_BYTES, MAX_PATH_UNITS - utf16_units(str(filament_dir)) - 1)


def fitted(stem, name, suffix, room):
    """Return *stem* and *suffix*, with the stem cut short if the file name would be too long.

    A cut ends with a short hash of the whole *name*, so that two long names that start alike still
    get different file names, and the same ones on every pull. The cut never splits a character.
    """
    def fits(text):
        whole = text + suffix + TEMPORARY_SUFFIX
        return len(whole.encode("utf-8")) <= MAX_FILE_NAME_BYTES and utf16_units(whole) <= room

    if fits(stem):
        return stem + suffix
    mark = f"… {hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"
    # Every character takes at least a byte, so no more than this many can fit.
    cut = stem[:MAX_FILE_NAME_BYTES]
    while cut and not fits(cut + mark):
        cut = cut[:-1]
    return cut.rstrip(" .") + mark + suffix


def safe_stem(name):
    """Return *name* as a file name that every platform can use, before any cut or number."""
    stem = UNSAFE_NAME_CHARACTERS.sub("-", name).strip(" .") or "Filament"
    if device := WINDOWS_DEVICE_NAME.match(stem):
        stem = stem[:device.end()] + "_" + stem[device.end():]
    return stem


def preset_stem(name, taken, room):
    """Return a file name for the preset called *name* that does not collide with those in *taken*."""
    stem = safe_stem(name)
    candidate, number = fitted(stem, name, "", room), 1
    while candidate.casefold() in taken:
        number += 1
        candidate = fitted(stem, name, f" ({number})", room)
    return candidate


def is_stem_of(stem, name, room, old_name):
    """Return whether preset_stem() can give *name* the file name *stem*, with or without a number.

    The file was named for *old_name*. A number that name had of its own, as in "PLA (2)", looks just like
    one preset_stem() added, so only the same name, as a file name, keeps the number.
    """
    if not isinstance(old_name, str) or safe_stem(old_name) != safe_stem(name):
        return False
    numbered = NUMBERED_STEM.fullmatch(stem)
    suffixes = ["", f" ({numbered[1]})"] if numbered else [""]
    return any(fitted(safe_stem(name), name, suffix, room) == stem for suffix in suffixes)


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


def preset_files(profiles, filament_dir, previous, in_use):
    """Return {stem: profile}, given the bundle's *previous* {stem: profile} and the stems *in_use* by a slot.

    OrcaSlicer restores the preset of every filament slot by name, and a preset's name is its file name, so
    a file name that went to another filament would quietly put that filament in the slot. A filament
    keeps its file name for as long as its name gives it, number included, so that another one coming or
    going does not move it. A new name goes by id, and never takes a file name that a slot holds for
    another filament: that slot then gets another preset, and the pull says so.
    """
    room = file_name_room(filament_dir)
    old_stems = {profile.get("filament_id"): (stem, profile.get("name")) for stem, profile in previous.items()}
    held = {stem.casefold(): profile.get("filament_id") for stem, profile in previous.items() if stem in in_use}
    presets, unplaced = {}, []
    for profile in sorted(profiles, key=lambda profile: profile["filament_id"]):
        stem, old_name = old_stems.get(profile["filament_id"], (None, None))
        if stem is not None and is_stem_of(stem, profile["name"], room, old_name):
            presets[stem] = profile
        else:
            unplaced.append(profile)
    taken = {stem.casefold() for stem in presets}
    for profile in unplaced:
        others = {stem for stem, filament_id in held.items() if filament_id != profile["filament_id"]}
        stem = preset_stem(profile["name"], taken | others, room)
        taken.add(stem.casefold())
        presets[stem] = profile
    return presets


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


def read_pulled(bundle_dir):
    """Return {stem: profile} for the presets of the bundle that can be read.

    A pull replaces or removes every file, so one it cannot read, such as a half-saved hand edit, is
    left for it to fix rather than stopping it.
    """
    pulled = {}
    for path in (bundle_dir / "filament").glob("*.json"):
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(profile, dict):
            pulled[path.stem] = profile
    return pulled


def active_nozzle(bundle):
    """Return the first extruder's nozzle diameter, and whether that nozzle is a high-flow one."""
    value = bundle.full_config_value("nozzle_diameter") or ""
    nozzle = re.split("[,;]", value)[0].strip()
    try:
        float(nozzle)
    except ValueError:
        raise SyncError(f"Could not read the printer's nozzle diameter from {value!r}.") from None
    return nozzle, high_flow(bundle)


def high_flow(bundle):
    """Return whether the first extruder's nozzle is a high-flow one, or None if the printer cannot say.

    A filament can have a standard and a high-flow row for the same diameter on the same printer, and
    without saying which, Filament DB answers with either.
    """
    volume_type = (bundle.full_config_value("nozzle_volume_type") or "").split(",")[0].strip()
    # Hybrid holds nozzles of both kinds, so it says nothing either, nor does a type this does not know.
    if volume_type not in HIGH_FLOW_VOLUME_TYPES:
        return None
    if HIGH_FLOW_VOLUME_TYPES[volume_type] or offers_high_flow(bundle):
        return HIGH_FLOW_VOLUME_TYPES[volume_type]
    # Profiles such as Prusa's HF nozzles do not list a high-flow variant, so OrcaSlicer leaves their volume
    # type at Standard and offers no other. Standard says nothing about such a nozzle.
    return None


def offers_high_flow(bundle):
    """Return whether OrcaSlicer lets the first extruder's volume type be set to a high-flow one."""
    value = bundle.full_config_value("extruder_variant_list") or ""
    # Each extruder has one value, its variants such as "Direct Drive High Flow" with commas between them.
    variants = (string_list(value) or [""])[0].split(",")
    return any(variant.strip().endswith(" High Flow") for variant in variants)


def bare_id(filament_id):
    return filament_id.removeprefix(ID_PREFIX)


def fetch_calibration(api, filament_id, nozzle):
    """Return the calibration for the nozzle, leaving out one that belongs to another printer, and
    keeping one made for a single bed type to that bed's plate.

    The route falls back to any row for the nozzle, and a machine's retraction, temperatures and
    chamber settings do not carry over to another one.
    """
    diameter, is_high_flow = nozzle
    path = f"/api/filaments/{bare_id(filament_id)}/calibration?nozzle_diameter={diameter}&format=orcaslicer"
    if api.printer:
        path += "&printer=" + urllib.parse.quote(api.printer)
    result = request_calibration(api, path, is_high_flow)
    if not result:
        return Calibration()
    ours = of_this_printer(api, result)
    if ours and not result.get("bedType"):
        return Calibration(result.get("calibration_orca", {}))
    # Filament DB answers with a row for one bed type only when the filament has none for every bed type,
    # and then with the first one. Each other plate's row has to be asked for by name. With no printer set,
    # it also puts another printer's row for every bed type ahead of a row for any printer made for one,
    # so then every plate's row has to be asked for.
    calibration = scoped_to_bed(api, result) if ours else Calibration(every_bed=False)
    answered = result["bedType"]["name"] if ours else None
    for bed_type in [name for name in PLATE_ROWS if name != answered]:
        row = request_calibration(api, f"{path}&bed_type={urllib.parse.quote(bed_type)}", is_high_flow)
        # The route falls back to another bed type's row when this one has none.
        if ((row or {}).get("bedType") or {}).get("name") == bed_type and of_this_printer(api, row):
            calibration.update(scoped_to_bed(api, row))
    return calibration


def of_this_printer(api, result):
    """Return whether the route's answer is a calibration for any printer or for this one."""
    printer = result.get("printer")
    if printer is None or printer.casefold() == api.printer.casefold():
        return True
    api.other_printers.add(printer)
    return False


def scoped_to_bed(api, result):
    """Return the settings of a calibration made for a single bed type, kept to that bed's plate.

    A preset has a bed temperature for each plate but one value for everything else, so only the bed
    temperatures can stay with their plate, and none can when OrcaSlicer has no plate by that name.
    """
    keys = result.get("calibration_orca", {})
    bed_type = result["bedType"]["name"]
    if bed_type not in PLATE_ROWS:
        api.unknown_beds.add(bed_type)
        return Calibration(every_bed=False)
    kept = {plate_key: keys[key] for key, plate_key in zip(HOT_PLATE_KEYS, PLATE_ROWS[bed_type]) if key in keys}
    if len(kept) < len(keys):
        api.other_beds.add(bed_type)
    return Calibration(kept, every_bed=False)


def request_calibration(api, path, is_high_flow):
    """Return the route's answer for the kind of nozzle, or None if it has no row for it."""
    if is_high_flow is None:
        return api.request(path, missing_ok=True)
    result = api.request(f"{path}&high_flow={int(is_high_flow)}", missing_ok=True)
    if result or is_high_flow:
        return result
    # A nozzle saved without a high-flow flag matches neither high_flow=0 nor 1, though Filament DB
    # counts it as standard. Ask again without the flag, and keep only a row that is not high flow.
    result = api.request(path, missing_ok=True)
    if result and (result.get("nozzle") or {}).get("highFlow") is True:
        return None
    return result


def unescaped(text):
    """Undo C escapes as OrcaSlicer and PrusaSlicer do, where only \\n and \\r stand for other characters."""
    return re.sub(r"\\(.)", lambda escape: {"n": "\n", "r": "\r"}.get(escape[1], escape[1]), text, flags=re.DOTALL)


def unquoted(value):
    """Undo the INI quoting that values imported from PrusaSlicer keep in Filament DB."""
    return [unescaped(quoted[1]) if (quoted := QUOTED_STRING.fullmatch(item)) else item for item in value]


def record_reader(api, profile):
    """Return a function that reads the filament's record from Filament DB the first time it is called."""
    return functools.cache(lambda: api.request(f"/api/filaments/{bare_id(profile['filament_id'])}"))


def ini_keys(profile, record):
    """Return the settings whose exported value is the text of a PrusaSlicer INI line, quotes and all.

    Filament DB keeps such a setting as a string in its settings bag, and its own fields and the lists sent
    from OrcaSlicer as the value itself, where a quote is part of the value. The export makes a list of
    every one of them, so only the record tells them apart, and it is only read when a value is quoted.
    Its OrcaSlicer and Bambu Studio .json import also stores a one-line value as a string, and the bag
    does not say where a string came from, so Filament DB treats every string as INI text and so does this.
    """
    quoted = [key for key, value in profile.items()
              if isinstance(value, list) and len(value) == 1 and QUOTED_STRING.fullmatch(value[0])]
    if not quoted:
        return set()
    settings = record().get("settings") or {}
    # The export decodes INI text that runs over several lines itself, and a field wins over the
    # settings bag, so only a value exported just as the string is stored is still INI text.
    return {key for key in quoted if isinstance(settings.get(key), str) and profile[key] == [settings[key]]}


def orca_z_shrinkage(value):
    """Convert Filament DB's shrinkage, 0.2 for 0.2%, to Orca's remaining size of 99.8%.

    The export converts XY and not Z, and Orca refuses to slice a value outside 50% to 150%. A value
    of 50 or more was imported from an Orca preset, so it is on Orca's scale already.
    """
    numbers = (float(item.rstrip("%")) for item in value)
    return [f"{round(number if number >= 50 else 100 - number, 4):g}%" for number in numbers]


def bag_value(value):
    """Return a value from Filament DB's settings bag as its export gives it, without the INI quotes."""
    return [str(item) for item in value] if isinstance(value, list) else unquoted([str(value)])


def own_bed_temperatures(settings, record):
    """Return the filament's own bed temperatures, which a plate without one of its own takes.

    The export puts a Hot Plate row over them, and that value is the Hot Plate's alone. Only the record
    says whether there is such a row, so it is read only when a plate needs the value.
    """
    own = {key: settings[key] for key in HOT_PLATE_KEYS if key in settings}
    if not own or all(key in settings for key in PLATE_FALLBACK_KEYS):
        return own
    filament = record()
    temperatures = filament.get("temperatures") or {}
    bag = filament.get("settings") or {}
    for key in own.keys() & row_keys(filament):
        # What the export gives without the row: the temperature field, or else the settings bag.
        if temperatures.get(TEMPERATURE_FIELDS[key]) not in (None, ""):
            own[key] = [str(temperatures[TEMPERATURE_FIELDS[key]])]
        elif bag.get(key) is not None:
            own[key] = bag_value(bag[key])
        else:
            del own[key]
    return own


def prepare_profile(profile, calibration, filament_keys, record):
    """Turn an exported profile into a preset that a live reload accepts.

    A reload refuses the whole bundle over one invalid preset, and a preset is invalid if it holds a
    known setting that does not belong in a filament preset. Filament DB's settings bag can hold
    anything, such as PrusaSlicer keys, so only the keys of a filament preset are kept. The same goes
    for "nil", which means not overridden, is refused by many settings, and is what leaving one out does.
    Settings that are PrusaSlicer INI text lose its quoting. *record* reads the filament's record.
    """
    ini = ini_keys(profile, record)
    settings = {key: unquoted(value) if key in ini else value for key, value in profile.items()}
    if "filament_colour" in settings:
        settings["default_filament_colour"] = settings.pop("filament_colour")
    for prusaslicer_key, orca_key in PRUSASLICER_KEYS.items():
        if prusaslicer_key in settings:
            settings.setdefault(orca_key, settings[prusaslicer_key])
    beds = own_bed_temperatures(settings, record)
    settings.update(calibration)
    if calibration.every_bed:
        beds.update({key: calibration[key] for key in HOT_PLATE_KEYS if key in calibration})
    # A calibrated plate's first layer follows its calibrated bed temperature, as the Hot Plate's does
    # below, unless the filament has a first layer temperature of its own.
    for temperature_key, first_layer_key in PLATE_ROWS.values():
        if temperature_key in calibration:
            settings.setdefault(first_layer_key, beds.get(HOT_PLATE_KEYS[1], calibration[temperature_key]))
    if "pressure_advance" in calibration:
        settings["enable_pressure_advance"] = ["1"]
    # PrusaSlicer's 0 means no limit. OrcaSlicer turns it into 0.5 when the preset is selected, as an
    # edit the user never made, and Send would write that back.
    if same_value(settings.get(MAX_VOLUMETRIC_SPEED_KEY), ["0"]):
        del settings[MAX_VOLUMETRIC_SPEED_KEY]
    for temperatures in (settings, beds):
        if "hot_plate_temp" in temperatures:
            temperatures.setdefault("hot_plate_temp_initial_layer", temperatures["hot_plate_temp"])
    for plate in PLATE_KEYS:
        for suffix in ("", "_initial_layer"):
            if "hot_plate_temp" + suffix in beds:
                settings.setdefault(plate + suffix, beds["hot_plate_temp" + suffix])
    if settings.get(Z_SHRINKAGE_KEY) not in (None, ["nil"]):
        settings[Z_SHRINKAGE_KEY] = orca_z_shrinkage(settings[Z_SHRINKAGE_KEY])

    prepared = {key: profile[key] for key in METADATA_KEYS if key in profile}
    prepared["version"] = PRESET_VERSION
    prepared.update({key: value for key, value in settings.items()
                     if key in filament_keys - SCOPE_KEYS and value != ["nil"]})
    return prepared


def fetch_profiles(api, nozzle, filament_keys):
    return [prepare_profile(profile, fetch_calibration(api, profile["filament_id"], nozzle), filament_keys,
                            record_reader(api, profile))
            for profile in api.request("/api/filaments/orcaslicer")]


def exports_of(api, filament_id):
    """Return the export of one filament, an empty list if Filament DB no longer has it."""
    return api.request("/api/filaments/orcaslicer?ids=" + urllib.parse.quote(bare_id(filament_id)))


def fetch_export(api, filament_id):
    exported = exports_of(api, filament_id)
    if not exported:
        # Pull refuses to remove a preset that has unsent edits, so they have to go first. A plugin pointed at
        # the wrong copy of Filament DB gets the same answer, and there the edits are worth keeping.
        raise SyncError(f"Filament DB at {api.url} does not have this filament. If that is the right address, "
                        "discard the edits to it, then run Pull from Filament DB to remove its synced preset.")
    return exported[0]


def string_list(value):
    """Split a list of strings as OrcaSlicer writes it, or return None if it is malformed.

    A value is quoted only when it needs to be, so quoted and bare values can sit side by side.
    """
    items, position = [], 0
    while True:
        item = STRING_ITEM.match(value, position)
        items.append(item[2] if item[1] is None else unescaped(item[1]))
        position = item.end()
        if position == len(value):
            return items
        # An unclosed quote, or something after a closing one, stops short of the next semicolon.
        if value[position] != ";":
            return None
        position += 1


def decode_value(key, value):
    """Turn the serialized value of setting *key* into the list form of a preset file."""
    # A quote or a semicolon never appears in a list of numbers or options, so a value holding one is a
    # list of strings even when its setting is newer than STRING_LIST_KEYS.
    if key not in STRING_LIST_KEYS and '"' not in value and ";" not in value:
        return value.split(",")
    items = string_list(value)
    if items is not None:
        return items
    # OrcaSlicer never writes a list of strings this way, so outside STRING_LIST_KEYS the value is a
    # single string setting, which is written as it is, with only its backslashes and line breaks escaped.
    if key not in STRING_LIST_KEYS:
        return [unescaped(value)]
    raise SyncError(f"Nothing was sent, because OrcaSlicer's value for {key} could not be read.")


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
        key: decode_value(key, value)
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
        # A rename in Filament DB is not the only cause: a name an earlier version could not use everywhere,
        # or a Windows folder path that grew, moves the file too.
        if any(profile["filament_id"] == filament_id for profile in presets.values()):
            raise SyncError(f'"{display_name(edited)}" has unsent edits, and this pull gives its preset a new '
                            "name. Send or discard them, then pull again. Nothing was changed.")
        raise SyncError(f'"{display_name(edited)}" has unsent edits, and it was removed from Filament DB. '
                        "Discard them, then pull again. Nothing was changed.")


def confirm_empty(api, bundle_dir):
    """Ask before an empty export removes every synced preset.

    An empty export is what deleting the last filament gives, but also what a plugin pointed at the wrong
    copy of Filament DB gets. This comes before the check for unsent edits, which tells the user to
    discard them, and that is the wrong advice when the copy is the wrong one.
    """
    count = len(list((bundle_dir / "filament").glob("*.json")))
    if not count:
        return
    presets = "the 1 synced preset" if count == 1 else f"all {count} synced presets"
    question = (f"Filament DB at {api.url} has no filaments. Remove {presets} from OrcaSlicer?\n\n"
                "Press Cancel if the plugin is pointing at the wrong copy of Filament DB.")
    if not confirmed(question, PULL_NAME):
        raise SyncError("Filament DB has no filaments, and the synced presets were left alone.")


def pull(api):
    bundle = orca.host.preset_bundle()
    filaments = bundle.filaments
    nozzle = active_nozzle(bundle)
    profiles = fetch_profiles(api, nozzle, set(filaments.edited_preset().config_keys()))
    bundle_dir = find_bundle_dir()
    if not profiles:
        confirm_empty(api, bundle_dir)

    in_use = {name.removeprefix(BUNDLE_PRESET_PREFIX) for name in bundle.current_filament_preset_names()
              if name.startswith(BUNDLE_PRESET_PREFIX)}
    presets = preset_files(profiles, bundle_dir / "filament", read_pulled(bundle_dir), in_use)
    refuse_moved_edits(filaments.edited_preset(), presets)
    replaced = sorted(in_use - presets.keys())
    install_bundle(bundle_dir, presets)

    diameter, is_high_flow = nozzle
    kind = "high-flow nozzle" if is_high_flow else "nozzle"
    message = f"Pulled {len(profiles)} filaments from Filament DB for a {diameter} mm {kind}."
    if replaced:
        message += f" These were removed, so OrcaSlicer chose other presets for them: {key_list(replaced)}."
    if api.other_printers:
        message += f" Calibration tied to other printers was left out: {key_list(api.other_printers)}."
    if api.other_beds:
        message += (" Calibration tied to one bed type was left out, other than any bed temperature for its "
                    f"plate: {key_list(api.other_beds)}.")
    if api.unknown_beds:
        message += (" Calibration for a bed type OrcaSlicer has no plate for was left out: "
                    f"{key_list(api.unknown_beds)}.")
    return message


def plate_row_keys(api, filament_id):
    return row_keys(api.request(f"/api/filaments/{bare_id(filament_id)}"))


def row_keys(record):
    """Return the plate settings that Filament DB's bed type rows give the filament of *record*.

    The record lists a variant's rows as the export uses them, which are its parent's when it has none of
    its own. The export skips an empty value, so that setting still comes from the settings bag.
    """
    return {key for row in record.get("bedTypeTemps") or []
            for field, key in zip(("temperature", "firstLayerTemperature"), PLATE_ROWS.get(row.get("bedType"), ()))
            if row.get(field) not in (None, "")}


def confirmed(question, title):
    """Ask *question* with OK and Cancel. OrcaSlicer shows it in a label, where a lone & marks a shortcut key
    and disappears, so names and values keep theirs."""
    return orca.host.ui.message(question.replace("&", "&&"), title, "ok_cancel", "question") == "ok"


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
    if not confirmed(text, SEND_NAME):
        raise SyncError("Nothing was sent.")


def fetch_theirs(api, filament_id, nozzle, filament_keys):
    """Return Filament DB's export of the filament, its calibration for the nozzle, and the preset they make."""
    exported = fetch_export(api, filament_id)
    calibration = fetch_calibration(api, filament_id, nozzle)
    return exported, calibration, prepare_profile(exported, calibration, filament_keys, record_reader(api, exported))


def held_back_keys(api, filament_id, changes, calibration):
    """Return the edits that cannot be sent, and so are left out of the conflict check."""
    # Calibration has to be set aside first: after a nozzle change it differs from the ancestor
    # without being a conflict.
    held_back = set(calibration) | FIELD_ONLY_KEYS
    if "pressure_advance" in calibration:
        held_back.add("enable_pressure_advance")
    # Only an edited plate setting is worth asking Filament DB for the record.
    if PLATE_ROW_KEYS & set(changes):
        held_back |= plate_row_keys(api, filament_id)
    held_back |= {key for key, value in changes.items() if key in SINGLE_VALUE_KEYS and len(value) > 1}
    return held_back & set(changes)


def recheck(api, filament_id, nozzle, filament_keys, changes, sendable, seen):
    """Check Filament DB again once the user has agreed, and return its export and calibration.

    The confirmation stays open for as long as the user likes, and a change made in Filament DB meanwhile
    would otherwise be overwritten without a word. Filament DB has no conditional write, so a change that
    lands between this read and the post can still be lost, but that window is a request long, not a
    dialog long. *seen* is the preset made from Filament DB's values when the user was asked.
    """
    exported, calibration, theirs = fetch_theirs(api, filament_id, nozzle, filament_keys)
    held_back = held_back_keys(api, filament_id, changes, calibration)
    # An edit that is now held back, or no longer is, would change what the user agreed to send. So would
    # a new value for one that is sent, or for one left out because Filament DB already had it.
    changed = [key for key in changes if (key in held_back) == (key in sendable)
               or key in sendable and not same_value(theirs.get(key), seen.get(key))]
    if changed:
        raise SyncError(f"Nothing was sent, because these changed in Filament DB while you were deciding: "
                        f"{key_list(changed)}. Run Pull from Filament DB to see its values, then send again.")
    return exported, calibration


def load_back(api, filament_id, calibration, filament_keys, bundle_dir, presets, stem, posted):
    """Read the filament back from Filament DB after a post and load it, then return its preset.

    The post has already saved the edits, so a failure here must not read as nothing having been sent.
    install_bundle() puts the old files back, and the next send finds the edits in Filament DB already.
    Only while they are unchanged, though: the bundle cannot remember the posted values, so a further
    edit to one of them reads as a change made in Filament DB until a pull catches the bundle up.
    """
    try:
        exported = fetch_export(api, filament_id)
        theirs = prepare_profile(exported, calibration, filament_keys, record_reader(api, exported))
        install_bundle(bundle_dir, {**presets, stem: theirs})
    except Exception as error:
        raise SyncError(f"These edits were saved in Filament DB, but OrcaSlicer could not load them back: "
                        f"{key_list(posted)}. Run Pull from Filament DB to bring OrcaSlicer up to date "
                        f"before you change them again. "
                        f"The error was: {error}") from error
    return theirs


def synced_values(filaments, stem, keys, theirs):
    """What the synced preset shows for *keys*: Filament DB's value, or OrcaSlicer's own where it has none."""
    shown = {key: theirs[key] for key in keys if key in theirs}
    unexported = [key for key in keys if key not in theirs]
    if not unexported:
        # Only a setting Filament DB leaves out needs the loaded preset, so a send that has none
        # does not depend on OrcaSlicer having loaded it.
        return shown
    synced = filaments.find_preset(BUNDLE_PRESET_PREFIX + stem)
    # The bundle's files can be on disk without OrcaSlicer having loaded them, and then there is
    # nothing to compare the copy with.
    if synced is None:
        raise SyncError("Nothing was sent, because OrcaSlicer has not loaded this filament's synced preset. "
                        "Run Pull from Filament DB, then send again.")
    return {**shown, **{key: decode_value(key, synced.config_value(key) or "") for key in unexported}}


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
        # A pull removes a filament's synced preset once Filament DB drops it, but a saved copy stays,
        # and telling the user to pull again would send them round in circles.
        if not exports_of(api, filament_id):
            raise SyncError(f'"{display_name(edited)}" came from a filament that Filament DB at {api.url} does '
                            "not have, so there is nowhere to send it.")
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
    exported, calibration, theirs = fetch_theirs(api, filament_id, nozzle, filament_keys)
    held_back = held_back_keys(api, filament_id, changes, calibration)
    sendable = {key: value for key, value in changes.items() if key not in held_back}
    # An earlier send can have saved these and then failed to load them back, which leaves the bundle on
    # the old value. Filament DB holding the edit is no conflict, and it needs no second post.
    already = {key for key, value in sendable.items() if same_value(theirs.get(key), value)}
    posting = {key: value for key, value in sendable.items() if key not in already}

    conflicts = [key for key in posting if not same_value(theirs.get(key), ancestor.get(key))]
    if conflicts:
        raise SyncError(f"Nothing was sent, because these also changed in Filament DB: {key_list(conflicts)}. "
                        "Run Pull from Filament DB to see its values, then send again.")
    if edited.name != BUNDLE_PRESET_PREFIX + stem:
        # A saved copy stays as it was when a pull moves the bundle on, so the bundle says nothing
        # about what the user could see when making this edit. A setting Filament DB leaves out is not
        # a missing value: the synced preset shows OrcaSlicer's own for it, and a copy saved before an
        # override was removed still shows the old one.
        shown = synced_values(filaments, stem, posting, theirs)
        hidden = [key for key in posting
                  if not same_value(shown[key], decode_value(key, selected.config_value(key) or ""))]
        if hidden:
            raise SyncError(f"Nothing was sent, because the synced preset shows other values for: {key_list(hidden)}. "
                            "Make the change on the synced preset instead.")

    if posting:
        confirm_send(display_name(edited), posting, theirs)
        exported, calibration = recheck(api, filament_id, nozzle, filament_keys, changes, sendable, theirs)
        api.request(f"/api/filaments/{bare_id(filament_id)}/orcaslicer", request_body(posting, exported))
        theirs = load_back(api, filament_id, calibration, filament_keys, bundle_dir, presets, stem, posting)
    else:
        install_bundle(bundle_dir, {**presets, stem: theirs})

    dropped = [key for key, value in sendable.items() if not same_value(theirs.get(key), value)]
    sent = [key for key in posting if key not in dropped]
    message = f"Sent {len(sent)} of {len(changes)} edits to Filament DB."
    if already:
        message += f" These were in Filament DB already: {key_list(already)}."
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
