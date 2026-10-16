"""test_image_contents.py — every module the app imports is copied into the Docker image.

The image copies the repo-root scripts by name. A new root module that isn't on that list
(jellyfin_api.py once) builds fine but crashes at startup with ModuleNotFoundError, and no
other test notices, because tests run from the repo, not from the image.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _copied_root_files():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\r\n", "\n")
    text = re.sub(r"\\[ \t]*\n", " ", text)  # join continued lines ("... \" + newline)
    copied = set()
    for line in text.splitlines():
        words = line.split()
        if words[:1] == ["COPY"] and not any(w.startswith("--from") for w in words):
            copied |= set(words[1:-1])  # sources; the last word is the destination
    return copied


def _imported_names(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_the_dockerfile_is_read_correctly():
    copied = _copied_root_files()
    assert {"channel_engine.py", "export.py", "icon_engine.py", "badge_renderer.py"} <= copied


def test_every_root_module_the_app_imports_is_in_the_image():
    copied = _copied_root_files()
    shipped = [ROOT / f for f in copied if f.endswith(".py")]
    shipped += [p for p in (ROOT / "backend").rglob("*.py") if "tests" not in p.parts]
    root_modules = {p.stem for p in ROOT.glob("*.py")}
    missing = set()
    for path in shipped:
        for name in _imported_names(path) & root_modules:
            if f"{name}.py" not in copied:
                missing.add(f"{name}.py (imported by {path.relative_to(ROOT).as_posix()})")
    assert not missing, "add these to the Dockerfile COPY line: " + ", ".join(sorted(missing))


def test_every_script_the_app_runs_is_in_the_image():
    """The pipeline runs some root scripts by name (sync_plex.py, sync_jellyfin.py, ...)."""
    copied = _copied_root_files()
    text = (ROOT / "backend" / "routers" / "pipeline_router.py").read_text(encoding="utf-8")
    run = set(re.findall(r'_stream\(\s*"([\w.]+\.py)"', text))
    assert {"sync_plex.py", "sync_jellyfin.py", "export.py"} <= run
    assert not run - copied, "add these to the Dockerfile COPY line: " + ", ".join(sorted(run - copied))
