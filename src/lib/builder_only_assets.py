"""Canonical fresh-builder assets, read from reviewable source files."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
ASSETS = {
    '/opt/oxidex-builder/image/' + name: (_ROOT / 'builder/standalone' / name).read_text()
    for name in ('Dockerfile', 'standalone-entrypoint.py', 'worktree_commands.py', 'toolchain_probe.py')
}
ASSETS.update({
    '/etc/oxidex-remote-build/qualification.gitconfig': '[safe]\n\tdirectory = /target/checkout\n[core]\n\thooksPath = /dev/null\n\tfsmonitor = false\n[gpg]\n\tprogram = /usr/bin/gpg\n[gpg "ssh"]\n\tprogram = /usr/bin/ssh-keygen\n[protocol]\n\tallow = never\n[protocol "file"]\n\tallow = always\n[protocol "https"]\n\tallow = never\n',

    '/usr/local/bin/oxidex-remote-build': (_ROOT / 'builder/dedicated/host-launcher.py').read_text(),
    '/usr/local/bin/docker': (_ROOT / 'builder/dedicated/docker-admission.py').read_text(),
    '/etc/oxidex-remote-build/qualification-maintainer.allowed_signers': 'swackhamer@users.noreply.github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKyzTK7pVP3daBqt3csludP15zyIBcTL25JqGYgnRzl/\n',
})
STARTUP = (_ROOT / 'cloud-init/builder-startup.sh').read_text()
