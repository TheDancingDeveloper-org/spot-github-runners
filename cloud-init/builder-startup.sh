#!/usr/bin/env bash
# Fresh dedicated builder only. Never use to update an occupied/shared host.
set -Eeuo pipefail
set +x
umask 077
exec > >(tee -a /var/log/oxidex-builder-bootstrap.log) 2>&1
exec 9>/run/oxidex-builder-bootstrap.lock
flock -n 9 || exit 75
[[ ! -e /var/lib/oxidex-builder-bootstrap.complete ]] || exit 75
metadata() { curl --fail --silent --show-error --max-time 10 -H Metadata-Flavor:Google "http://metadata.google.internal/computeMetadata/v1/$1"; }
[[ "$(metadata instance/name)" == '{{INSTANCE_NAME}}' ]]
[[ "$(metadata project/project-id)" == '{{PROJECT}}' ]]
[[ "$(metadata instance/zone)" == 'projects/'*'/zones/{{ZONE}}' ]]
DEVICE=/dev/disk/by-id/google-{{DISK_DEVICE_NAME}}
[[ -b "$DEVICE" ]]
# Complete native process/service observation before storage or service changes.
python3 - <<'FRESH_OCCUPANCY'
{{FRESH_GUARD_SOURCE}}
fresh_runtime_guard()
FRESH_OCCUPANCY
# Refuse an existing Docker workload or lease before installation/service changes.
if [[ -x /usr/bin/docker ]]; then
  inventory=$(/usr/bin/docker ps -aq --no-trunc) || exit 78
  [[ -z "$inventory" ]]
fi
python3 - <<'EMPTY_LEDGER'
import json
from pathlib import Path
p=Path('/var/lib/gha-admission/leases.json')
if p.exists() and json.loads(p.read_text())!={}:raise SystemExit('Existing lease ledger: hold')
if Path('/etc/gha-admission.json').exists():raise SystemExit('Existing admission deployment: hold')
EMPTY_LEDGER
# This renderer is issued only for a new disk. Existing filesystems are preserved.
if ! blkid "$DEVICE" >/dev/null 2>&1; then
  [[ "$(lsblk -nr -o TYPE "$DEVICE")" == disk ]]
  signatures=$(wipefs --no-act --noheadings --output TYPE "$DEVICE") || exit 78
  [[ -z "$signatures" ]]
  mkfs.ext4 "$DEVICE"
fi
[[ "$(blkid -s TYPE -o value "$DEVICE")" == ext4 ]]
MOUNT_POINT=/mnt/runner-data
mkdir -p "$MOUNT_POINT"
UUID=$(blkid -s UUID -o value "$DEVICE")
if mountpoint -q "$MOUNT_POINT"; then [[ "$(findmnt -n -o UUID "$MOUNT_POINT")" == "$UUID" ]];
else mount "$DEVICE" "$MOUNT_POINT"; fi
grep -Fq "UUID=$UUID $MOUNT_POINT ext4" /etc/fstab || printf 'UUID=%s %s ext4 defaults,nofail 0 2\n' "$UUID" "$MOUNT_POINT" >>/etc/fstab
python3 - <<'FRESH_DESTINATION'
{{FRESH_GUARD_SOURCE}}
runtime_destination_guard('/mnt/runner-data')
FRESH_DESTINATION
# The launch command binds the official Debian13 family; do not use a foreign repository.
. /etc/os-release
[[ "$ID" == debian && "$VERSION_CODENAME" == trixie ]]
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg git openssh-client rsync python3 jq util-linux sudo
install -d -m0755 /etc/apt/keyrings
curl --proto '=https' --tlsv1.2 -fsS https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian %s stable\n' "$(dpkg --print-architecture)" "$(. /etc/os-release && echo "$VERSION_CODENAME")" >/etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io
inventory=$(/usr/bin/docker ps -aq --no-trunc) || exit 78
[[ -z "$inventory" ]]
python3 - <<'RECHECK_DESTINATION'
{{FRESH_GUARD_SOURCE}}
fresh_runtime_guard()
runtime_destination_guard('/mnt/runner-data')
RECHECK_DESTINATION
mkdir -p "$MOUNT_POINT/docker" "$MOUNT_POINT/containerd" "$MOUNT_POINT/remote-build"
# Fresh-host service configuration only; inventory was proven empty above.
systemctl stop docker containerd
cp -a /var/lib/containerd/. "$MOUNT_POINT/containerd/"
install -d -m0755 /etc/systemd/system/containerd.service.d /etc/systemd/system/docker.service.d /etc/docker
cat >/etc/systemd/system/containerd.service.d/builder-data.conf <<EOF
[Unit]
RequiresMountsFor=$MOUNT_POINT/containerd
[Service]
ExecStart=
ExecStart=/usr/bin/containerd --root $MOUNT_POINT/containerd --state /run/containerd
EOF
cat >/etc/systemd/system/docker.service.d/builder-data.conf <<EOF
[Unit]
RequiresMountsFor=$MOUNT_POINT/docker
EOF
printf '{"data-root":"%s/docker","features":{"containerd-snapshotter":false},"log-driver":"local","log-opts":{"max-size":"20m","max-file":"3"}}\n' "$MOUNT_POINT" >/etc/docker/daemon.json
systemctl daemon-reload
systemctl enable --now containerd docker
python3 - <<'CLASSIC_STORE'
{{FRESH_GUARD_SOURCE}}
classic_storage_guard(json.loads(command(['/usr/bin/docker','info','--format=json'])),'/mnt/runner-data/docker')
CLASSIC_STORE
getent group 1001 >/dev/null && [[ "$(getent group 1001 | cut -d: -f1)" == oxidex-build ]] || { getent group 1001 >/dev/null && exit 78; groupadd -g1001 oxidex-build; }
# Validate the complete host group/account inventory before granting filesystem access.
python3 - <<'RESTRICTED_GROUP'
{{ACCESS_GUARD_SOURCE}}
restricted_group_guard(json.loads('{{UPLOADER_CONFIG}}'))
RESTRICTED_GROUP
# Enrolled uploaders may search the persistent source path, never write ancestors.
chown 0:1001 "$MOUNT_POINT" "$MOUNT_POINT/remote-build"
chmod 0710 "$MOUNT_POINT" "$MOUNT_POINT/remote-build"
# Restricted uploaders can traverse targets for owned artifact export.
install -d -o0 -g1001 -m0710 "$MOUNT_POINT/remote-build/targets"
install -d -m0755 /opt/gha-runners /opt/oxidex-builder/image
install -d -m0700 /var/lib/oxidex-builder
install -d -o root -g root -m0700 "$MOUNT_POINT/remote-build/qualification-reservations"
install -d -o root -g root -m 0755 /opt/oxidex-builder/qualification_trust /etc/oxidex-remote-build
install -d -o root -g root -m 0755 "$MOUNT_POINT/remote-build/qualification-sealed"
{{ASSET_WRITES}}
chmod 0444 /etc/oxidex-remote-build/qualification-maintainer.allowed_signers /etc/oxidex-remote-build/qualification.gitconfig
chmod 0644 /opt/oxidex-builder/qualification_trust/*.py
chmod 0555 /opt/oxidex-builder/qualification_trust/runtime_git.py
printf '%s\n' '{{BUILD_CONFIG}}' >/etc/oxidex-remote-build.json
printf '%s\n' '{{ADMISSION_CONFIG}}' >/etc/gha-admission.json
printf '%s\n' '{{UPLOADER_CONFIG}}' >/etc/oxidex-builder-uploader.json
chmod 0600 /etc/oxidex-builder-uploader.json
chmod 0644 /etc/oxidex-remote-build.json /etc/gha-admission.json
python3 -B - <<'RESERVATION_ENROLLMENT'
import json,os,sys
from pathlib import Path
sys.path.insert(0,'/opt/oxidex-builder')
from qualification_trust.reservation import subprocess_storage
device='/dev/disk/by-id/google-{{DISK_DEVICE_NAME}}'
observed=subprocess_storage(device)
path=Path('/etc/oxidex-remote-build.json')
config=json.loads(path.read_text())
config['reservation_enrollment']={'schema':2,'enabled':True,
    'uuid':observed['uuid'],'by_id':observed['by_id'],'serial':observed['serial']}
with path.open('w') as output:
    json.dump(config,output,sort_keys=True);output.flush();os.fsync(output.fileno())
RESERVATION_ENROLLMENT
chmod 0755 /usr/local/bin/oxidex-remote-build /usr/local/bin/docker
chmod 0700 /usr/local/sbin/oxidex-reservation-control
chmod 0755 /usr/local/sbin/oxidex-uploader-session
python3 - <<'HOST_HEADROOM'
import os,runpy
from pathlib import Path
module=runpy.run_path('/opt/gha-runners/host_admission.py',run_name='builder_headroom')
memory={line.split(':')[0]:int(line.split()[1])/1024**2 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
if os.cpu_count()!=16 or module['builder_memory_budget'](memory['MemTotal'],memory['MemAvailable'],0)!=28:raise SystemExit('Actual host cannot fit28GiB builder with host reserve')
HOST_HEADROOM
# Bound image-build stages too. Classic engine honors these flags; BuildKit may not.
DOCKER_BUILDKIT=0 /usr/bin/docker build --memory=28g --memory-swap=28g --cpu-period=100000 --cpu-quota=1600000 --iidfile /var/lib/oxidex-builder/image-id -t oxidex-remote-builder:1.99.0 /opt/oxidex-builder/image
python3 - <<'PIN_BUILT_IMAGE'
import json,re,subprocess
from pathlib import Path
image=Path('/var/lib/oxidex-builder/image-id').read_text().strip()
if not re.fullmatch('sha256:[0-9a-f]{64}',image):raise SystemExit('Malformed built image ID')
actual=json.loads(subprocess.check_output(['/usr/bin/docker','image','inspect','oxidex-remote-builder:1.99.0'],text=True,timeout=10))
if len(actual)!=1 or actual[0]['Id']!=image:raise SystemExit('Built tag differs from actual image ID')
path=Path('/etc/oxidex-remote-build.json');record=json.loads(path.read_text());record['approved_image_id']=image
temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(record,sort_keys=True)+'\n');temporary.chmod(0o644);temporary.replace(path)
PIN_BUILT_IMAGE
cat >/etc/systemd/system/gha-admission.service <<'ADMISSION_SERVICE'
[Unit]
Description=Dedicated builder admission (no runner service)
After=docker.service
Requires=docker.service
[Service]
ExecStart=/usr/bin/python3 /opt/gha-runners/host_admission.py serve
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
ADMISSION_SERVICE
systemctl daemon-reload
systemctl enable --now gha-admission
for attempt in $(seq 1 30); do [[ ! -S /run/gha-admission/socket ]] || break; sleep 1; done
[[ -S /run/gha-admission/socket ]]
/usr/local/bin/oxidex-remote-build bootstrap-tools prepare >/dev/null
printf '[toolchain]\nchannel = "1.99.0"\n' >/mnt/runner-data/remote-build/sources/bootstrap-tools/rust-toolchain.toml
chmod 0444 /mnt/runner-data/remote-build/sources/bootstrap-tools/rust-toolchain.toml
/usr/local/bin/oxidex-remote-build bootstrap-tools python3 /usr/local/bin/oxidex-toolchain-probe > /var/lib/oxidex-builder/tool-probe-output
python3 - <<'OBSERVED_READINESS'
{{READINESS_SOURCE}}
OBSERVED_READINESS
# Install and activate the SSH restriction before the uploader account or key exists.
python3 -B - <<'UPLOAD_FENCE'
import json
from pathlib import Path
import runpy
mapping=json.loads(Path('/etc/oxidex-builder-uploader.json').read_text())
policy=runpy.run_path('/opt/gha-runners/builder_access.py')['uploader_policy_bytes'](mapping)
for name,raw in policy.items():Path(name).write_bytes(raw)
Path('/etc/ssh/sshd_config.d/40-oxidex-uploader-fence.conf').chmod(0o644)
Path('/etc/sudoers.d/oxidex-uploader-session').chmod(0o440)
UPLOAD_FENCE
/usr/sbin/sshd -t
/usr/sbin/visudo -cf /etc/sudoers.d/oxidex-uploader-session
systemctl reload ssh
python3 -B - <<'ACTIVE_UPLOAD_FENCE'
import subprocess
mapping_name='{{UPLOADER_USERNAME}}'
subprocess.run(['systemctl','is-active','--quiet','ssh'],check=True,timeout=10)
result=subprocess.run(['/usr/sbin/sshd','-T','-C',
    'user='+mapping_name+',host=localhost,addr=127.0.0.1'],
    check=True,capture_output=True,text=True,timeout=10)
if result.stderr:raise SystemExit('Uploader SSH effective fence observation incomplete')
rows=[row.split(' ',1) for row in result.stdout.splitlines() if row]
required={'forcecommand':'/usr/local/sbin/oxidex-uploader-session',
          'disableforwarding':'yes','permittty':'no'}
for name,value in required.items():
    if [item[1] for item in rows if len(item)==2 and item[0]==name]!=[value]:
        raise SystemExit('Uploader SSH effective fence differs before key publication')
ACTIVE_UPLOAD_FENCE
# Only after the active fence is observed may the account gain sudo and its key.
printf '%%oxidex-build ALL=(root) NOPASSWD: /usr/local/bin/oxidex-remote-build\n' >/etc/sudoers.d/oxidex-build
chmod 0440 /etc/sudoers.d/oxidex-build
visudo -cf /etc/sudoers.d/oxidex-build
python3 -B /opt/gha-runners/builder_access.py /etc/oxidex-builder-uploader.json '{{UPLOADER_SHA}}' >/var/lib/oxidex-builder/uploader-observed.json
python3 -B - <<'UPLOAD_POLICY_CHECK'
import runpy
policy=runpy.run_path('/opt/gha-runners/builder_access.py')
policy['effective_sudo_guard']('{{UPLOADER_USERNAME}}',require_uploader_session=True)
UPLOAD_POLICY_CHECK
printf '%s\n' '{{DEPLOYMENT_ID}}' >/var/lib/oxidex-builder-bootstrap.complete
chmod 0444 /var/lib/oxidex-builder-bootstrap.complete
