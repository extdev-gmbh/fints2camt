"""The pinned build inputs: the hashed lock file and the base image of the Dockerfile."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _locked() -> dict[str, tuple[str, list[str]]]:
    """name -> (version, hashes) of requirements.txt."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    locked: dict[str, tuple[str, list[str]]] = {}
    for block in re.split(r"\n(?=\S)", text):
        head = re.match(r"([A-Za-z0-9_.\-]+)==(\S+)", block)
        if head:
            locked[_name(head.group(1))] = (head.group(2), re.findall(r"--hash=sha256:([0-9a-f]{64})", block))
    return locked


def _name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_the_lock_file_pins_the_direct_dependencies_of_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    locked = _locked()
    for requirement in project["dependencies"]:
        name, version = requirement.split("==")
        assert locked[_name(name)][0] == version, requirement


def test_every_locked_package_has_an_exact_version_and_hashes() -> None:
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    locked = _locked()
    assert len(locked) > 3  # the transitive dependencies are locked too
    requirement_lines = [line for line in text.splitlines() if line and not line[0].isspace() and line[0] != "#"]
    assert len(requirement_lines) == len(locked)
    assert all(hashes for _version, hashes in locked.values())
    assert "--index-url" not in text and "--extra-index-url" not in text


def test_the_image_is_built_from_the_lock_file_on_a_base_image_pinned_by_digest() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bases = re.findall(r"^FROM (?:--platform=\S+ )?(\S+) AS", dockerfile, re.MULTILINE)
    external = [base for base in bases if ":" in base]
    assert len(external) == 2
    assert all(re.fullmatch(r"python:3\.12-slim@sha256:[0-9a-f]{64}", base) for base in external)
    assert len(set(external)) == 1
    assert "--require-hashes --no-deps -r requirements.txt" in dockerfile
    assert "--upgrade pip" not in dockerfile
