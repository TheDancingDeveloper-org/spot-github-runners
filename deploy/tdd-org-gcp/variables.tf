variable "project_id" {
  description = "GCP project that will own the runner VM. Supplied by the operator."
  type        = string
}

variable "billing_account_id" {
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX) linked to project_id, for the budget."
  type        = string
}

variable "budget_amount" {
  description = "Monthly budget in budget_currency (A$45 is about US$30). At 100% of actual spend the budget guard stops and locks the runner VM."
  type        = number
  default     = 60
}

variable "budget_currency" {
  description = "Must equal the billing account's currency (USD, AUD, NZD...), or the budget is rejected."
  type        = string
  default     = "AUD"
}

variable "poller_sa_email" {
  description = "Service account the Node B poller uses (created out of band, e.g. gha-spot-poller@PROJECT.iam.gserviceaccount.com)."
  type        = string
}

variable "region" {
  type    = string
  default = "australia-southeast1"
}

variable "zone" {
  description = "c3d is offered only in australia-southeast1-c; fall back to c2d-highcpu-16 in -a."
  type        = string
  default     = "australia-southeast1-c"
}

variable "machine_type" {
  description = "Spot shape. c3d-highcpu-16 was $0.068/h Spot in Sydney on 2026-10-04."
  type        = string
  default     = "c3d-highcpu-16"
}

variable "mode" {
  description = "scale_to_zero: VM powers off after idle_minutes and the poller starts it on demand. always_on: never self-stops."
  type        = string
  default     = "scale_to_zero"
  validation {
    condition     = contains(["scale_to_zero", "always_on"], var.mode)
    error_message = "mode must be scale_to_zero or always_on."
  }
}

variable "idle_minutes" {
  description = "Minutes with no running job before the VM powers itself off (scale_to_zero only)."
  type        = number
  default     = 15
}

variable "boot_disk_gb" {
  description = "Persistent pd-balanced boot disk; holds Docker images, cargo registry and the sccache dir."
  type        = number
  default     = 100
}

variable "github_org" {
  type    = string
  default = "TheDancingDeveloper-org"
}

variable "runner_group" {
  description = "Org runner group restricted to the selected repos; create it before first boot (see README)."
  type        = string
  default     = "gcp-spot"
}

variable "runner_labels" {
  description = "Extra labels; GitHub adds self-hosted,linux,x64. Workflows opt in with runs-on: [self-hosted, rust-heavy]."
  type        = string
  default     = "rust-heavy"
}

variable "runner_version" {
  description = "actions/runner release (matches runner/Dockerfile in this repo)."
  type        = string
  default     = "2.337.0"
}

variable "sccache_redis_url" {
  description = "Optional remote sccache, e.g. redis://100.92.54.45:6380. Requires tailscale_auth_key. Empty = local disk sccache only."
  type        = string
  default     = ""
}

variable "tailscale_auth_key" {
  description = "Optional pre-authorised, tagged (tag:ci-gcp) Tailscale key. Stored in instance metadata; leave empty to skip the tailnet."
  type        = string
  default     = ""
  sensitive   = true
}

variable "allow_iap_ssh" {
  description = "Open tcp/22 from Google IAP only, for debugging."
  type        = bool
  default     = false
}
