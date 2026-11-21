"""The pinned build inputs: the hashed lock file and the base image of the Dockerfile."""

import re
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

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


def test_the_locked_versions_satisfy_the_requirements_of_the_locked_packages() -> None:
    # The hashed install (--no-deps) resolves nothing, so a lock file that breaks a requirement
    # of a locked package (e.g. an update of a transitive dependency beyond what its dependent
    # allows) would install and ship silently. The test environment holds the locked packages;
    # `packaging` comes with pytest.
    locked = {name: version for name, (version, _hashes) in _locked().items()}
    problems: list[str] = []
    for name, version in locked.items():
        installed = metadata.version(name)
        if installed != version:
            # e.g. `pip install ".[dev]"` replaced a locked version that a dependent excludes
            problems.append(f"{name}: locked {version}, installed {installed}")
            continue
        for line in metadata.requires(name) or []:
            requirement = Requirement(line)
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            dependency = _name(requirement.name)
            if dependency not in locked:
                problems.append(f"{name} requires {dependency}, which is not locked")
            elif not requirement.specifier.contains(locked[dependency], prereleases=True):
                problems.append(f"{name} requires {line}, locked is {locked[dependency]}")
    assert problems == []
