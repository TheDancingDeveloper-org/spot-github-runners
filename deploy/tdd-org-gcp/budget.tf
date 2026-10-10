# A$45 (about US$30) budget. Billing budgets only notify, so the budget
# publishes to Pub/Sub and the Node B poller pulls that subscription: when
# actual spend reaches 100% it stops the VM and labels it
# gha-budget-locked=true, and it will not start a locked VM. Billing data lags
# by hours, so the poller's monthly hour cap is the first line of defence.

resource "google_pubsub_topic" "budget" {
  name = "gha-spot-budget"
}

resource "google_pubsub_subscription" "budget_poller" {
  name                       = "gha-spot-budget-poller"
  topic                      = google_pubsub_topic.budget.id
  ack_deadline_seconds       = 60
  message_retention_duration = "604800s" # 7 days, so a poller outage loses nothing
  expiration_policy {
    ttl = "" # never expire
  }
}

resource "google_pubsub_subscription_iam_member" "poller" {
  subscription = google_pubsub_subscription.budget_poller.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.poller_sa_email}"
}

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account_id
  display_name    = "gha-spot monthly cap (${data.google_project.this.project_id})"

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

  all_updates_rule {
    pubsub_topic                   = google_pubsub_topic.budget.id
    schema_version                 = "1.0"
    disable_default_iam_recipients = false # billing admins still get e-mail
  }
}
