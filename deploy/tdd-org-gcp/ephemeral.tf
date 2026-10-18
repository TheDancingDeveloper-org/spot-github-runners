# Throwaway runner VMs from the gha-rust-heavy golden image (WI-1155, 2026-10-11).
# The poller creates one VM per queued job from a class template (large or small), with a
# just-in-time runner registration in metadata, and deletes it after the job.
# Nothing is billed while idle except the image (~US$1/month).
# Off until enable_ephemeral = true (needs the runner VM service account and the
# sccache bucket, both created out of band).

variable "enable_ephemeral" {
  description = "Create the instance template for throwaway runner VMs."
  type        = bool
  default     = true
}

variable "runner_vm_sa_email" {
  description = "Service account attached to throwaway VMs (objectUser on the sccache bucket only)."
  type        = string
  default     = ""
}

variable "sccache_bucket" {
  description = "GCS bucket for the shared sccache (same region). Empty = local sccache on the VM disk."
  type        = string
  default     = ""
}

variable "ts_client_id" {
  description = "Tailscale federated identity client ID (not secret). Empty = no tailnet on throwaway VMs."
  type        = string
  default     = ""
}

variable "ts_audience" {
  type    = string
  default = ""
}

variable "ephemeral_classes" {
  description = <<-EOT
    Throwaway VM classes. The project's vCPU quota is 32 (CPUS_ALL_REGIONS and the
    C3D family in australia-southeast1), so the poller runs either one large VM or
    up to four small ones. Disks are pd-balanced (SSD_TOTAL_GB quota 500).
    large: codex fork-ci / fork-release (rust-heavy); a combined codex job peaks above 80 GB of disk.
    small: routed heavy Rust jobs from vogt, rustnzb and rustTorrent (gcp-rust).
  EOT
  type = map(object({
    machine_type = string
    disk_gb      = number
  }))
  default = {
    large = { machine_type = "c3d-highcpu-30", disk_gb = 160 }
    small = { machine_type = "c3d-highcpu-8", disk_gb = 100 }
  }
}

data "google_compute_image" "rust_heavy" {
  count   = var.enable_ephemeral ? 1 : 0
  family  = "gha-rust-heavy"
  project = var.project_id
}

moved {
  from = google_compute_instance_template.rust_heavy[0]
  to   = google_compute_instance_template.rust_heavy["large"]
}

resource "google_compute_instance_template" "rust_heavy" {
  for_each     = var.enable_ephemeral ? var.ephemeral_classes : {}
  name_prefix  = "gha-${each.key}-"
  machine_type = each.value.machine_type
  region       = var.region
  labels       = { "gha-ephemeral" = "true", "gha-class" = each.key }

  scheduling {
    provisioning_model          = "SPOT"
    preemptible                 = true
    automatic_restart           = false
    on_host_maintenance         = "TERMINATE"
    instance_termination_action = "DELETE" # preempted VMs vanish with their disk
  }

  disk {
    source_image = data.google_compute_image.rust_heavy[0].self_link
    disk_size_gb = each.value.disk_gb
    disk_type    = "pd-balanced"
    auto_delete  = true
    boot         = true
  }

  network_interface {
    subnetwork = google_compute_subnetwork.ci.id
    access_config {}
  }

  service_account {
    email  = var.runner_vm_sa_email
    scopes = ["https://www.googleapis.com/auth/devstorage.read_write"] # identity tokens need no scope
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    startup-script = templatefile("${path.module}/image/runtime.sh.tpl", {
      sccache_bucket       = var.sccache_bucket
      ts_client_id         = var.ts_client_id
      ts_audience          = var.ts_audience
      idle_limit_minutes   = 20
      max_lifetime_minutes = 360
    })
  }

  lifecycle {
    create_before_destroy = true
  }
}

output "runner_templates" {
  description = "Instance template name per class, for the poller's LARGE_TEMPLATE / SMALL_TEMPLATE."
  value       = { for k, t in google_compute_instance_template.rust_heavy : k => t.name }
}
