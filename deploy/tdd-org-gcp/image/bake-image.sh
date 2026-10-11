#!/usr/bin/env bash
# Bake the gha-rust-heavy golden image.
#   image/bake-image.sh [version]
# Creates a short-lived builder VM that runs image/bake.sh, waits for it to power
# off, captures its boot disk as an image in family gha-rust-heavy, then deletes
# the builder. Needs gcloud authenticated as an identity with compute.admin
# (CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE works).
set -euo pipefail
cd "$(dirname "$0")"
PROJECT=${PROJECT:-email-rs-493900}
ZONE=${ZONE:-australia-southeast1-c}
SUBNET=${SUBNET:-gha-spot-australia-southeast1}
VERSION=${1:-$(date -u +%Y%m%d-%H%M)}
BUILDER="gha-bake-$VERSION"
IMAGE="gha-rust-heavy-$VERSION"
G=(--project "$PROJECT")
Z=(--project "$PROJECT" --zone "$ZONE")

cleanup() { gcloud compute instances delete "$BUILDER" "${Z[@]}" --quiet >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "bake: builder $BUILDER -> image $IMAGE"
# On-demand (not Spot) so a preemption cannot cut the bake short; ~15 min of e2-standard-4.
gcloud compute instances create "$BUILDER" "${Z[@]}" \
  --machine-type=e2-standard-4 --subnet="$SUBNET" \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-balanced --boot-disk-auto-delete \
  --no-service-account --no-scopes --shielded-secure-boot \
  --labels=gha-bake=true \
  --metadata-from-file=startup-script=bake.sh \
  --metadata=image-version="$VERSION",enable-oslogin=TRUE,block-project-ssh-keys=TRUE >/dev/null

echo "bake: waiting for the builder to finish and power off"
for _ in $(seq 1 90); do
  sleep 20
  status=$(gcloud compute instances describe "$BUILDER" "${Z[@]}" --format='value(status)')
  [[ "$status" == TERMINATED ]] && break
done
log=$(gcloud compute instances get-serial-port-output "$BUILDER" "${Z[@]}" 2>/dev/null | grep -a 'gha-bake:' | sed 's/.*startup-script: //' | awk '!s[$0]++')
echo "$log"
[[ "$status" == TERMINATED ]] || { echo "bake: builder did not finish" >&2; exit 1; }
grep -q "gha-bake: done $VERSION" <<<"$log" || { echo "bake: provisioning failed; image not created" >&2; exit 1; }

gcloud compute images create "$IMAGE" "${G[@]}" --source-disk="$BUILDER" --source-disk-zone="$ZONE" \
  --family=gha-rust-heavy --storage-location=australia-southeast1 \
  --labels=gha-image=true --description="rust-heavy runner image $VERSION" >/dev/null
echo "bake: image $IMAGE created in family gha-rust-heavy"
gcloud compute images describe "$IMAGE" "${G[@]}" --format='value(name,diskSizeGb,archiveSizeBytes,status)'
