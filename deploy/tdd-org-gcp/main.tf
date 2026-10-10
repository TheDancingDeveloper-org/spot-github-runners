# One Spot GitHub Actions runner VM for TheDancingDeveloper-org.
# Prepared, not applied. Nothing here should be applied without operator approval.

locals {
  name = "gha-spot-1"
  apis = [
    "compute.googleapis.com",
    "billingbudgets.googleapis.com",
    "pubsub.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "eventarc.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
  ]
}

data "google_project" "this" {}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- network
# Dedicated VPC with no inbound rules: the runner only dials out (GitHub,
# crates.io, Docker Hub, Tailscale DERP/peers). Implied egress-allow applies.
resource "google_compute_network" "ci" {
  name                    = "gha-spot"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "ci" {
  name          = "gha-spot-${var.region}"
  network       = google_compute_network.ci.id
  region        = var.region
  ip_cidr_range = "10.80.0.0/24"
}

resource "google_compute_firewall" "iap_ssh" {
  count         = var.allow_iap_ssh ? 1 : 0
  name          = "gha-spot-iap-ssh"
  network       = google_compute_network.ci.id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# ---------------------------------------------------------------- runner VM
resource "google_compute_instance" "runner" {
  name         = local.name
  machine_type = var.machine_type
  zone         = var.zone
  labels       = { "gha-spot" = "true" }

  # scale_to_zero starts stopped; the poller starts it when a job queues.
  desired_status = var.mode == "always_on" ? "RUNNING" : "TERMINATED"

  scheduling {
    provisioning_model          = "SPOT"
    preemptible                 = true
    automatic_restart           = false
    on_host_maintenance         = "TERMINATE"
    instance_termination_action = "STOP" # keep the disk (caches) on preemption
  }

  boot_disk {
    auto_delete = false # caches outlive an accidental VM delete
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.boot_disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.ci.id
    access_config {} # ephemeral public IPv4: $0.005/h only while running; cheaper than Cloud NAT
  }

  # No service account: the VM holds no GCP credentials at all.
  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    startup-script = templatefile("${path.module}/startup.sh.tpl", {
      github_org        = var.github_org
      runner_group      = var.runner_group
      runner_labels     = var.runner_labels
      runner_version    = var.runner_version
      runner_name       = local.name
      idle_minutes      = var.mode == "always_on" ? 0 : var.idle_minutes
      sccache_redis_url = var.sccache_redis_url
    })
    tailscale-auth-key = var.tailscale_auth_key
  }

  lifecycle {
    # The poller and budget guard own power state, the one-hour registration
    # token and the budget lock label.
    ignore_changes = [desired_status, metadata["gha-reg-token"], labels["gha-budget-locked"]]
  }
}

# ---------------------------------------------------------------- poller identity
# Runs on Node B (estate). It may only read, start, stop and set metadata on
# instances in this project. Create its key out of band (README) so the private
# key never lands in Terraform state.
resource "google_project_iam_custom_role" "poller" {
  role_id = "ghaSpotPoller"
  title   = "GHA Spot runner poller"
  permissions = [
    "compute.instances.get",
    "compute.instances.start",
    "compute.instances.stop",
    "compute.instances.setMetadata",
    "compute.zoneOperations.get",
  ]
}

resource "google_service_account" "poller" {
  account_id   = "gha-spot-poller"
  display_name = "GHA Spot runner poller (Node B)"
}

resource "google_project_iam_member" "poller" {
  project = var.project_id
  role    = google_project_iam_custom_role.poller.id
  member  = "serviceAccount:${google_service_account.poller.email}"
}
