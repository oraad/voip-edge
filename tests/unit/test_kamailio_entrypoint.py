"""The Kamailio entrypoint derives the tenant suffix gate from the env list."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_entrypoint_generates_tenant_suffix_regex(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "kamailio"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    config_dir = tmp_path / "config"
    config_dir.mkdir()

    env = os.environ.copy()
    env.update(
        {
            "TENANT_ALLOWED_SUFFIXES": "home.example.net, edge.example.net",
            "KAM_CONFIG_DIR": str(config_dir),
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "SHM_MEMORY": "128",
            "PKG_MEMORY": "32",
        }
    )
    subprocess.run(
        ["sh", str(ROOT / "kamailio" / "entrypoint.sh")],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    generated = (config_dir / "tenant_suffix.cfg").read_text()
    assert "home[.]example[.]net|edge[.]example[.]net" in generated
    regex = re.search(r'#!substdef "!TENANTSUFFIXRE!(.*?)!"', generated).group(1)
    regex = regex.replace(r"\$", "$")
    assert re.fullmatch(regex, "sip.alpha.home.example.net")
    assert re.fullmatch(regex, "sip.beta.edge.example.net")
    assert not re.fullmatch(regex, "sip.alpha.other.example.net")


def test_entrypoint_rejects_an_empty_suffix_list(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "kamailio"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "TENANT_ALLOWED_SUFFIXES": " , ",
            "KAM_CONFIG_DIR": str(tmp_path / "config"),
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        }
    )
    result = subprocess.run(
        ["sh", str(ROOT / "kamailio" / "entrypoint.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "empty" in result.stderr
