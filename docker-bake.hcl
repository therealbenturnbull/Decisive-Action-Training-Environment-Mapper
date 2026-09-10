// Aim: Define native and multi-architecture Docker build targets for DATE Mapper.
// Author: Benjamin Turnbull

variable "IMAGE_NAME" {
  default = "date-mapper"
}

variable "IMAGE_TAG" {
  default = "local"
}

target "image" {
  context    = "."
  dockerfile = "Dockerfile"
  tags       = ["${IMAGE_NAME}:${IMAGE_TAG}"]
}

target "amd64" {
  inherits  = ["image"]
  platforms = ["linux/amd64"]
  tags      = ["${IMAGE_NAME}:${IMAGE_TAG}-amd64"]
}

target "arm64" {
  inherits  = ["image"]
  platforms = ["linux/arm64"]
  tags      = ["${IMAGE_NAME}:${IMAGE_TAG}-arm64"]
}

group "all" {
  targets = ["amd64", "arm64"]
}

target "multiarch" {
  inherits  = ["image"]
  platforms = ["linux/amd64", "linux/arm64"]
}
