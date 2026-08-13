from __future__ import annotations

from pathlib import Path


def test_systemd_assets_use_non_root_service_and_absolute_commands() -> None:
    service = Path("deploy/systemd/rss-zen.service").read_text(encoding="utf-8")
    export_service = Path("deploy/systemd/rss-zen-export@.service").read_text(encoding="utf-8")
    backup_service = Path("deploy/systemd/rss-zen-backup.service").read_text(encoding="utf-8")
    health_service = Path("deploy/systemd/rss-zen-healthcheck.service").read_text(encoding="utf-8")

    assert "User=rss-zen" in service
    assert (
        "ExecStart=/opt/rss-zen/current/.venv/bin/rss-zen serve "
        "--config /etc/rss-zen/rss-zen.toml"
    ) in service
    assert "Restart=on-failure" in service
    assert "NoNewPrivileges=yes" in service
    assert "ProtectSystem=strict" in service
    assert "TimeoutStopSec=120s" in service
    assert "LoadCredential=rss-zen.env:/etc/rss-zen/rss-zen.env" in service
    assert "EnvironmentFile=%d/rss-zen.env" in service
    assert "SupplementaryGroups=" not in service
    assert "CapabilityBoundingSet=" in service
    assert "PrivateDevices=yes" in service
    assert "RestrictNamespaces=yes" in service
    assert "UMask=0077" in service
    assert (
        "/opt/rss-zen/current/.venv/bin/rss-zen export %i "
        "--config /etc/rss-zen/rss-zen.toml"
    ) in export_service
    assert (
        "/opt/rss-zen/current/.venv/bin/rss-zen backup "
        "--config /etc/rss-zen/rss-zen.toml"
    ) in backup_service
    assert "--backup-directory" not in backup_service
    assert "User=rss-zen" in health_service
    assert (
        "/opt/rss-zen/current/.venv/bin/rss-zen doctor --json "
        "--config /etc/rss-zen/rss-zen.toml"
    ) in health_service
    assert "Restart=" not in health_service


def test_timers_are_persistent_and_use_expected_units() -> None:
    export_timer = Path("deploy/systemd/rss-zen-export-daily.timer").read_text(encoding="utf-8")
    backup_timer = Path("deploy/systemd/rss-zen-backup.timer").read_text(encoding="utf-8")
    health_timer = Path("deploy/systemd/rss-zen-healthcheck.timer").read_text(encoding="utf-8")

    assert "Persistent=true" in export_timer
    assert "Unit=rss-zen-export@daily.service" in export_timer
    assert "Persistent=true" in backup_timer
    assert "Unit=rss-zen-backup.service" in backup_timer
    assert "Persistent=true" in health_timer
    assert "Unit=rss-zen-healthcheck.service" in health_timer


def test_linux_documentation_covers_install_restore_and_single_instance_limit() -> None:
    documentation = Path("docs/deployment-linux.md").read_text(encoding="utf-8")

    assert "one active process" in documentation
    assert "systemd-analyze verify" in documentation
    assert "Backup and restore" in documentation
    assert "PRAGMA integrity_check" in documentation


def test_guided_deployment_script_preserves_secrets_and_uses_locked_release() -> None:
    script = Path("scripts/deploy-linux.sh").read_text(encoding="utf-8")
    service = Path("deploy/systemd/rss-zen.service").read_text(encoding="utf-8")

    assert "apt-get install -y" in script
    assert "dnf install -y" in script
    assert "UV_PYTHON_INSTALL_DIR" in script
    assert "sync --locked --no-dev --python 3.13" in script
    assert "RELEASE_TMP_ROOT" in script
    assert "mv -T \"${staging_dir}\" \"${RELEASE_DIR}\"" in script
    assert "if [[ ! -e \"${CONFIG_PATH}\" ]]" in script
    assert "if [[ ! -e \"${ENV_PATH}\" ]]" in script
    assert "Configuration still has placeholders" in script
    assert "systemd-analyze verify" in script
    assert "bootstrap|release" in script
    assert "systemd 247+" in script
    assert "DEPLOY_USER=\"rss-zen-deploy\"" in script
    assert "require_deploy_user" in script
    assert "root -g root -m 0600" in script
    assert "LoadCredential=" in service
    assert "control wrapper self-check" in script
    assert "generated sudoers file failed validation" in script
    sudoers = Path("deploy/sudoers/rss-zen-deploy").read_text(encoding="utf-8")
    control = Path("deploy/sudoers/rss-zen-deploy-control").read_text(encoding="utf-8")
    assert sudoers.strip() == "rss-zen-deploy ALL=(root) NOPASSWD: @CONTROL@"
    assert "SUDO_USER" in control
    assert 'if [[ "${1:-}" == "--self-check" ]]' in control
    assert 'case "${1:-}" in' in control
    assert 'exec "${SYSTEMCTL}" enable --now' in control
