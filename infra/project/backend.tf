terraform {
  backend "gcs" {
    bucket = "live-on-473112-tf-state"
    prefix = "project"
  }
}
