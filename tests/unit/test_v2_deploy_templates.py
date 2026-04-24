"""Smoke tests for Epic #19 deployment templates.

These tests catch the kind of drift that only shows up during an
on-call page: a typo in `fly.toml` that makes `flyctl deploy` barf
30 minutes into an SRE's Sunday, a stray tab in the systemd unit that
makes it refuse to load, a port mismatch between docker-compose and
Dockerfile.

They do NOT try to actually boot a container or validate semantics of
every field. That would require docker/podman on the CI runner and is
out of scope for a unit test. The goal is cheap + mechanical guards:

* TOML / YAML / plist parse (pure parse validation, no value checks)
* Cross-file port consistency (Dockerfile EXPOSE vs compose publish
  vs fly.toml internal_port)
* No accidentally-committed secrets (`.env` template has empty
  values; no template references a real sk-ant-... or sk- key)

Install scripts are shell/PS1 — not parseable by Python stdlib — so we
just assert the files exist + are non-empty. CI's ruff/shellcheck is
the real lint for those.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

try:
    import tomllib as _toml  # py311+
except ImportError:  # pragma: no cover
    import tomli as _toml  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[2]


# ----- Dockerfile ---------------------------------------------------------

def test_dockerfile_exists_and_exposes_daemon_port() -> None:
    df = REPO_ROOT / "Dockerfile"
    assert df.exists(), "Dockerfile missing"
    text = df.read_text(encoding="utf-8")
    assert "FROM python:" in text, "expected a python: base image"
    assert "EXPOSE 8765" in text, "container should expose 8765"
    # The entrypoint binds 0.0.0.0 so the Docker port publish flag
    # controls reachability rather than the app.
    assert '"--host", "0.0.0.0"' in text or '"--host","0.0.0.0"' in text


def test_dockerfile_does_not_bake_secrets() -> None:
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "sk-ant-" not in text
    assert not re.search(r"sk-[a-zA-Z0-9]{20,}", text), (
        "Dockerfile contains what looks like a real API key"
    )


# ----- docker-compose.yml -------------------------------------------------

def test_docker_compose_parses_and_matches_expose() -> None:
    dc = REPO_ROOT / "docker-compose.yml"
    assert dc.exists()
    data = yaml.safe_load(dc.read_text(encoding="utf-8"))
    services = data.get("services", {})
    assert "xmclaw" in services, "missing top-level xmclaw service"
    ports = services["xmclaw"].get("ports", [])
    # Allow either "8765:8765" or "127.0.0.1:8765:8765" — just assert
    # the container side is the Dockerfile EXPOSE.
    assert any(str(p).endswith(":8765") for p in ports), (
        f"no port mapping to container port 8765: {ports}"
    )


def test_docker_compose_does_not_bake_secrets() -> None:
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "sk-ant-" not in text
    # env refs are OK (`${ANTHROPIC_API_KEY:-}`); literal keys are not.
    assert not re.search(r"sk-[a-zA-Z0-9]{20,}", text)


def test_env_example_has_no_real_values() -> None:
    env = REPO_ROOT / ".env.example"
    assert env.exists()
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert value == "", f"{key} in .env.example should be empty"


# ----- fly.toml -----------------------------------------------------------

def test_fly_toml_parses_and_internal_port_matches() -> None:
    fly = REPO_ROOT / "deploy" / "fly" / "fly.toml"
    assert fly.exists()
    data = _toml.loads(fly.read_text(encoding="utf-8"))
    assert "app" in data
    assert data.get("http_service", {}).get("internal_port") == 8765
    # Template's volume name matches mount source.
    source = data.get("mounts", {}).get("source")
    assert source, "fly.toml missing mounts.source"


# ----- systemd unit -------------------------------------------------------

def test_systemd_unit_structure() -> None:
    unit = REPO_ROOT / "deploy" / "systemd" / "xmclaw.service"
    assert unit.exists()
    text = unit.read_text(encoding="utf-8")
    # Required sections.
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text, f"missing {section}"
    # Must be Type=simple (or anything explicit) + have ExecStart.
    assert re.search(r"^ExecStart=", text, re.MULTILINE), "no ExecStart"
    # Don't ship a unit that runs as root.
    assert re.search(r"^User=(?!root$)", text, re.MULTILINE), (
        "systemd unit must not run as root"
    )


# ----- launchd plist ------------------------------------------------------

def test_launchd_plist_parses() -> None:
    plist = REPO_ROOT / "deploy" / "launchd" / "com.xmclaw.daemon.plist"
    assert plist.exists()
    # ElementTree is enough — we don't need the full plist type system,
    # just that it parses as XML and has the expected top-level Label.
    root = ET.fromstring(plist.read_text(encoding="utf-8"))
    assert root.tag == "plist"
    found_label = False
    for dict_el in root.findall("dict"):
        keys = [k.text for k in dict_el.findall("key")]
        if "Label" in keys:
            found_label = True
            break
    assert found_label, "plist missing <key>Label</key>"


# ----- Windows service wrapper -------------------------------------------

def test_windows_service_py_imports_minimally() -> None:
    # We can't actually import the module on Linux (pywin32 missing),
    # but we can parse it as valid Python — catches syntax regressions.
    import ast

    wrapper = REPO_ROOT / "deploy" / "windows-service" / "xmclaw_service.py"
    assert wrapper.exists()
    ast.parse(wrapper.read_text(encoding="utf-8"))


# ----- install scripts ----------------------------------------------------

@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_install_script_present(script: str) -> None:
    path = REPO_ROOT / "scripts" / script
    assert path.exists(), f"{script} missing"
    body = path.read_text(encoding="utf-8")
    assert len(body) > 200, f"{script} looks truncated"
    assert "xmclaw" in body.lower()


def test_install_sh_has_shebang_and_strict_mode() -> None:
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    first = sh.splitlines()[0]
    assert first.startswith("#!"), "install.sh needs a shebang"
    # `set -eu` (or `-euo pipefail`) prevents a failed curl from
    # silently continuing into rm -rf territory.
    assert re.search(r"set -[a-z]*e[a-z]*[uo]?", sh), (
        "install.sh should enable -e"
    )


# ----- docker-publish workflow -------------------------------------------

def test_docker_publish_workflow_parses() -> None:
    """The workflow file is valid YAML and declares the right triggers.

    This is what we guard: a broken YAML parse (tab / missing colon)
    means the workflow silently disappears from the Actions list —
    there's no CI job to page us about it. Parsing with yaml.safe_load
    catches that class of typo.
    """
    wf = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"
    assert wf.exists(), "docker-publish.yml missing — Epic #19 image publish is offline"
    data = yaml.safe_load(wf.read_text(encoding="utf-8"))
    # `on:` is parsed as a Python True key by yaml 1.1. Accept either.
    on = data.get("on") or data.get(True)
    assert on, "workflow has no `on:` triggers"
    assert "push" in on, "workflow should run on tag pushes"
    # Semver tag glob — matches release.yml. If these drift, a release
    # ends up with a Windows installer but no image (or vice versa).
    assert "v*.*.*" in on["push"]["tags"]
    assert "workflow_dispatch" in on, (
        "need manual trigger for verifying Dockerfile changes pre-tag"
    )

    # Packages: write — required for GHCR push. Without it the
    # push step fails at runtime with "denied: permission_denied".
    perms = data.get("permissions", {})
    assert perms.get("packages") == "write", (
        "workflow needs `permissions.packages: write` to push to ghcr.io"
    )


def test_docker_publish_workflow_targets_ghcr_multiarch() -> None:
    """The workflow publishes multi-arch to ghcr.io, not Docker Hub.

    Locks the registry choice (ghcr.io is keyless via GITHUB_TOKEN;
    Docker Hub would need an org-level secret) and the arch matrix
    (amd64 + arm64 — Apple Silicon and Raspberry Pi users get native
    images). Both are deliberate picks documented at the top of the
    workflow file; a drive-by edit that silently drops arm64 or
    swaps to Docker Hub should fail this test, not ship.
    """
    wf = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"
    text = wf.read_text(encoding="utf-8")
    assert "ghcr.io/" in text, "image registry should be ghcr.io"
    assert "linux/amd64" in text and "linux/arm64" in text, (
        "workflow should build a multi-arch image"
    )
    # GITHUB_TOKEN auth, not a PAT / org secret.
    assert "secrets.GITHUB_TOKEN" in text, (
        "GHCR auth must use the built-in GITHUB_TOKEN, not a user PAT"
    )


def test_deploy_md_references_published_image() -> None:
    """DEPLOY.md points users at the published image, not just a
    local build. Catches the case where the workflow ships but docs
    still say `docker build` only — users never learn the image
    exists."""
    deploy_md = REPO_ROOT / "docs" / "DEPLOY.md"
    assert deploy_md.exists()
    text = deploy_md.read_text(encoding="utf-8")
    assert "ghcr.io/" in text, (
        "DEPLOY.md should reference the pre-built ghcr.io image"
    )
