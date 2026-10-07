#!/usr/bin/env python3
"""Render a self-contained dedicated builder startup payload and GCE command."""
import argparse
import base64
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

from lib.builder_startup import render_builder_startup


def builder_config(config):
    """Supply fixed deployment defaults without inventing identity or authority."""
    defaults = dict(role='builder', cpu=16, ram=32, runner_count=0,
                    builder_cpus=16, builder_memory_gib=28,
                    disk_device_name='builder-cache', disk_size_gb=100,
                    disk_type='hyperdisk-balanced', data_disk_iops=3000,
                    data_disk_throughput=140, boot_disk_size_gb=40,
                    boot_disk_iops=3000, boot_disk_throughput=140)
    # Explicit values, including invalid ones, still reach the strict validator.
    return {**defaults, **config}


def render_files(config, deployment_id, output_directory):
    args = SimpleNamespace(**builder_config(config))
    script = render_builder_startup(args, deployment_id)
    directory = Path(output_directory).absolute()
    # Refuse reuse: these files describe a fresh host, not an in-place upgrade.
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    startup = directory / 'startup.sh'
    cloud = {
        'write_files': [{'path': '/usr/local/sbin/oxidex-builder-bootstrap',
                         'permissions': '0700', 'encoding': 'b64',
                         'content': base64.b64encode(script.encode()).decode()}],
        'runcmd': [['/usr/local/sbin/oxidex-builder-bootstrap']],
    }
    command = ['gcloud', 'compute', 'instances', 'create', args.instance_name,
               '--project=' + args.project, '--zone=' + args.zone,
               '--machine-type=n4-highcpu-16', '--provisioning-model=SPOT',
               '--instance-termination-action=DELETE', '--maintenance-policy=TERMINATE',
               '--image-family=debian-13', '--image-project=debian-cloud',
               '--boot-disk-type=hyperdisk-balanced', '--boot-disk-size=40GB',
               '--boot-disk-provisioned-iops=3000', '--boot-disk-provisioned-throughput=140',
               '--boot-disk-interface=NVME',
               '--create-disk=name=' + args.instance_name + '-cache,device-name=' + args.disk_device_name
               + ',type=hyperdisk-balanced,size=100GB,provisioned-iops=' + str(args.data_disk_iops)
               + ',provisioned-throughput=' + str(args.data_disk_throughput)
               + ',auto-delete=no,interface=NVME',
               '--network-interface=network=default,nic-type=GVNIC',
               '--no-service-account', '--no-scopes',
               '--metadata=enable-oslogin=FALSE,block-project-ssh-keys=TRUE',
               '--metadata-from-file=startup-script=' + str(startup)]
    # Only startup inputs are known before launch. Live ID and SSH trust remain
    # explicit observations; a template is not permission to start a build.
    client_env = dict(OXIDEX_REMOTE_PROJECT=args.project,
                      OXIDEX_REMOTE_INSTANCE=args.instance_name,
                      OXIDEX_REMOTE_ZONE=args.zone,
                      OXIDEX_REMOTE_SSH_USER=args.authorized_uploader['username'])
    client_config = ('# Merge into the existing Cargo [env] table; do not replace it.\n'
                     '# Complete the commented settings after verifying startup and SSH trust.\n'
                     '[env]\n' + ''.join(key + ' = ' + json.dumps(value) + '\n'
                                          for key, value in client_env.items()) +
                     '# OXIDEX_REMOTE_INSTANCE_ID = "actual numeric GCE instance ID"\n'
                     '# OXIDEX_REMOTE_SSH_KEY = "/absolute/path/to/private-key"\n'
                     '# OXIDEX_REMOTE_SSH_KNOWN_HOSTS = "/absolute/path/to/trusted-known-hosts"\n')
    outputs = {'startup.sh': script,
               'cargo-remote-env.toml': client_config,
               'cloud-config.yaml': '#cloud-config\n' + json.dumps(cloud, indent=2) + '\n',
               'launch-command.sh': '#!/usr/bin/env bash\nset -Eeuo pipefail\nexec ' + shlex.join(command) + '\n'}
    for name, contents in outputs.items():
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(contents)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--deployment-id', required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    directory = render_files(json.loads(args.config.read_text()), args.deployment_id, args.output_directory)
    print(directory / 'launch-command.sh')


if __name__ == '__main__':
    main()
