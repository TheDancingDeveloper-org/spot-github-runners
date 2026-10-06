"""One exact mount identity for admission, inspect, cancellation and cleanup."""
import re

ANCHOR = '/etc/oxidex-remote-build/qualification-maintainer.allowed_signers'
GITCONFIG = '/etc/oxidex-remote-build/qualification.gitconfig'
RUNTIME = '/opt/oxidex-builder/qualification_trust/runtime.py'
RUNTIME_GIT = '/opt/oxidex-builder/qualification_trust/runtime_git.py'


def qualification_project(project):
    return isinstance(project, str) and re.fullmatch(r'qualification-[0-9a-f]{12}-[0-9a-f]{32}', project) is not None


def expected_mounts(root, project):
    root = str(root)
    common = {'/target': (f'{root}/targets/{project}', True), '/cargo': (f'{root}/cargo', True)}
    if not qualification_project(project):
        return {**common, '/src': (f'{root}/sources/{project}', True)}
    sealed = f'{root}/qualification-sealed/{project}'
    return {**common, '/src': (sealed + '/incoming', False),
            '/target/checkout': (sealed + '/checkout', False),
            '/target/checkout/.git': (sealed + '/runtime-git', True),
            ANCHOR: (ANCHOR, False), '/etc/gitconfig': (GITCONFIG, False),
            '/qualification-runtime.py': (RUNTIME, False),
            '/qualification-bin/git': (RUNTIME_GIT, False)}


def volume_arguments(root, project):
    result = []
    for destination, (source, writable) in expected_mounts(root, project).items():
        result += ['-v', source + ':' + destination + ('' if writable else ':ro')]
    return result


def check_inspected_mounts(mounts, root, project):
    expected = expected_mounts(root, project)
    if (not isinstance(mounts, list) or len(mounts) != len(expected)
            or any(not isinstance(m, dict) for m in mounts)):
        raise ValueError('Incomplete builder mounts')
    observed = {}
    for mount in mounts:
        destination = mount.get('Destination')
        if destination in observed or mount.get('Type') != 'bind' or type(mount.get('RW')) is not bool:
            raise ValueError('Ambiguous builder mount')
        observed[destination] = (mount.get('Source'), mount['RW'])
    if observed != expected:
        raise ValueError('Builder mounts differ from exact namespace and permissions')
