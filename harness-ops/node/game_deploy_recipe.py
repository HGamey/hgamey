"""Pure validation for an owned Android resource deployment recipe."""
import copy
import re


SCHEMA = "phonefarm/game-resources-recipe@1"
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def validate_recipe(value, assets):
    """Return a detached recipe; asset targets and metadata confer no authority."""
    if (not isinstance(value, dict)
            or set(value) != {"schema", "package", "version_code", "apk_sha256", "files"}
            or value.get("schema") != SCHEMA):
        raise ValueError("unsupported deployment recipe schema or fields")
    if not isinstance(value["package"], str) or not PACKAGE.fullmatch(value["package"]):
        raise ValueError("deployment package must be an Android package name")
    if type(value["version_code"]) is not int or value["version_code"] <= 0:
        raise ValueError("deployment version_code must be a positive integer")
    if not isinstance(value["apk_sha256"], str) or not SHA256.fullmatch(value["apk_sha256"]):
        raise ValueError("deployment apk_sha256 must be 64 lowercase hexadecimal characters")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 64:
        raise ValueError("deployment files must contain 1–64 entries")
    if not isinstance(assets, list):
        raise ValueError("deployment requires the manifest asset list")
    names = [a.get("name") for a in assets if isinstance(a, dict)]
    paths = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"asset_name", "path"}:
            raise ValueError("deployment file requires only asset_name and path")
        name, path = item["asset_name"], item["path"]
        if not isinstance(name, str) or not name or names.count(name) != 1:
            raise ValueError("deployment asset_name must identify exactly one manifest asset")
        if not isinstance(path, str):
            raise ValueError("deployment path must be a relative resource path")
        parts = path.split("/")
        if any(not COMPONENT.fullmatch(part) or part in {".", ".."}
               or part.startswith(".phonefarm-") for part in parts):
            raise ValueError("deployment path contains an invalid or reserved component")
        if any(path == old or path.startswith(old + "/") or old.startswith(path + "/") for old in paths):
            raise ValueError("deployment paths must be distinct and cannot contain another file path")
        paths.add(path)
    return copy.deepcopy(value)


def validate_installation(value, assets):
    """Select one verified-cache APK; never infer metadata from a file name."""
    if (not isinstance(value, dict)
            or set(value) != {"schema", "package", "version_code", "apk_sha256", "apk_asset_name"}
            or value.get("schema") != "phonefarm/game-install-recipe@1"):
        raise ValueError("unsupported installation recipe schema or fields")
    if not isinstance(value["package"], str) or not PACKAGE.fullmatch(value["package"]):
        raise ValueError("installation package must be an Android package name")
    if type(value["version_code"]) is not int or value["version_code"] <= 0:
        raise ValueError("installation version_code must be a positive integer")
    if not isinstance(value["apk_sha256"], str) or not SHA256.fullmatch(value["apk_sha256"]):
        raise ValueError("installation apk_sha256 must be 64 lowercase hexadecimal characters")
    name = value["apk_asset_name"]
    if (not isinstance(name, str) or not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or not isinstance(assets, list)):
        raise ValueError("installation requires an explicit APK asset name and manifest assets")
    selected = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
    if (len(selected) != 1 or selected[0].get("sha256") != value["apk_sha256"]
            or type(selected[0].get("size")) is not int or selected[0]["size"] <= 0):
        raise ValueError("installation must select one positive-size asset with the declared APK sha256")
    return copy.deepcopy(value)


def validate_initialization(value, assets):
    """A bounded launcher attempt uses installed identity, not arbitrary commands."""
    if (not isinstance(value, dict)
            or set(value) != {"schema", "package", "version_code", "apk_sha256", "wait_seconds"}
            or value.get("schema") != "phonefarm/game-initialize-recipe@1"):
        raise ValueError("unsupported initialization recipe schema or fields")
    if not isinstance(value["package"], str) or not PACKAGE.fullmatch(value["package"]):
        raise ValueError("initialization package must be an Android package name")
    if type(value["version_code"]) is not int or value["version_code"] <= 0:
        raise ValueError("initialization version_code must be a positive integer")
    if not isinstance(value["apk_sha256"], str) or not SHA256.fullmatch(value["apk_sha256"]):
        raise ValueError("initialization apk_sha256 must be 64 lowercase hexadecimal characters")
    if type(value["wait_seconds"]) is not int or not 1 <= value["wait_seconds"] <= 30:
        raise ValueError("initialization wait_seconds must be an integer from 1 to 30")
    return copy.deepcopy(value)


def validate_scene(value, assets):
    """A bounded v73 demo attempt; paths and console text are derived by PF."""
    fields = {"schema", "adapter", "package", "version_code", "apk_sha256", "resource_prefix",
              "demo_name", "demo_asset_name", "compat_asset_name", "renderer", "wait_seconds"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("schema") != "phonefarm/game-scene-recipe@1"
            or value.get("adapter") != "doom3-v73-render-demo-v1"
            or value.get("package") != "com.karin.idTech4Amm"
            or type(value.get("version_code")) is not int or value["version_code"] != 11073):
        raise ValueError("scene requires the explicit Doom v73 render-demo recipe")
    if not isinstance(value["apk_sha256"], str) or not SHA256.fullmatch(value["apk_sha256"]):
        raise ValueError("scene apk_sha256 must be 64 lowercase hexadecimal characters")
    prefix = value["resource_prefix"]
    if (not isinstance(prefix, str) or prefix and any(
            not COMPONENT.fullmatch(part) or part in {".", ".."} or part.startswith(".phonefarm-")
            for part in prefix.split("/"))):
        raise ValueError("scene resource_prefix must be empty or a relative resource directory")
    if not isinstance(value["demo_name"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value["demo_name"]):
        raise ValueError("scene demo_name must be a simple name without extension")
    renderer = value["renderer"]
    if (not isinstance(renderer, dict) or set(renderer) != {"opengl", "multithread"}
            or renderer["opengl"] not in ("GLES2", "GLES3.0") or type(renderer["multithread"]) is not bool):
        raise ValueError("scene renderer requires opengl GLES2/GLES3.0 and boolean multithread")
    if type(value["wait_seconds"]) is not int or not 1 <= value["wait_seconds"] <= 30:
        raise ValueError("scene wait_seconds must be an integer from 1 to 30")
    names = [value["demo_asset_name"], value["compat_asset_name"]]
    if (not all(isinstance(name, str) and name and not any(ord(c) < 32 for c in name) for name in names)
            or names[0] == names[1] or not isinstance(assets, list)):
        raise ValueError("scene requires two distinct explicit seed asset names")
    selected = [a for a in assets if isinstance(a, dict) and a.get("name") in names]
    if (len(selected) != 2 or {a["name"] for a in selected} != set(names)
            or any(type(a.get("size")) is not int or a["size"] <= 0 for a in selected)
            or sum(a["size"] for a in selected) > 1 << 20):
        raise ValueError("scene seeds must be two positive-size manifest assets totaling at most 1 MiB")
    return copy.deepcopy(value)
