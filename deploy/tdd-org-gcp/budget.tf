# $30 budget. Billing budgets only notify, so a function subscribed to the
# budget topic makes it a hard stop: at >= 100% of actual spend it stops every
# gha-spot VM and labels it gha-budget-locked=true, which the poller honours.
# Billing data lags by several hours, so set the poller's hour cap as well.

resource "google_pubsub_topic" "budget" {
  name       = "gha-spot-budget"
  depends_on = [google_project_service.apis]
}

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account_id
  display_name    = "gha-spot monthly cap"

  budget_filter {
    projects        = ["projects/${data.google_project.this.number}"]
    calendar_period = "MONTH"
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.budget_amount)
    }
  }

  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.9 }
  threshold_rules { threshold_percent = 1.0 }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    pubsub_topic                   = google_pubsub_topic.budget.id
    schema_version                 = "1.0"
    disable_default_iam_recipients = false # billing admins still get e-mail
  }
}

resource "google_service_account" "budget_guard" {
  account_id   = "gha-spot-budget-guard"
  display_name = "Stops gha-spot VMs when the budget is exhausted"
}

resource "google_project_iam_member" "budget_guard_compute" {
  project = var.project_id
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.budget_guard.email}"
}

resource "google_project_iam_member" "budget_guard_events" {
  project = var.project_id
  role    = "roles/eventarc.eventReceiver"
  member  = "serviceAccount:${google_service_account.budget_guard.email}"
}

resource "google_storage_bucket" "fn_src" {
  name                        = "${var.project_id}-gha-spot-fn"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  depends_on                  = [google_project_service.apis]
}

data "archive_file" "budget_guard" {
  type        = "zip"
  source_dir  = "${path.module}/budget-guard"
  output_path = "${path.module}/.build/budget-guard.zip"
}

resource "google_storage_bucket_object" "budget_guard" {
  name   = "budget-guard-${data.archive_file.budget_guard.output_md5}.zip"
  bucket = google_storage_bucket.fn_src.name
  source = data.archive_file.budget_guard.output_path
}

resource "google_cloudfunctions2_function" "budget_guard" {
  name     = "gha-spot-budget-guard"
  location = var.region

  build_config {
    runtime     = "python312"
    entry_point = "on_budget"
    source {
      storage_source {
        bucket = google_storage_bucket.fn_src.name
        object = google_storage_bucket_object.budget_guard.name
      }
    }
  }

  service_config {
    max_instance_count    = 1
    available_memory      = "256M"
    timeout_seconds       = 120
    service_account_email = google_service_account.budget_guard.email
    environment_variables = {
      GCP_PROJECT = var.project_id
      GCP_ZONE    = var.zone
    }
  }

  event_trigger {
    trigger_region        = var.region
    event_type            = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic          = google_pubsub_topic.budget.id
    retry_policy          = "RETRY_POLICY_RETRY"
    service_account_email = google_service_account.budget_guard.email
  }
}

resource "google_cloud_run_service_iam_member" "budget_guard_invoker" {
  location = google_cloudfunctions2_function.budget_guard.location
  service  = google_cloudfunctions2_function.budget_guard.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.budget_guard.email}"
}
