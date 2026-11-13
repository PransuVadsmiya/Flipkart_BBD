variable "region" {
  type    = string
  default = "ap-south-1" # Mumbai
}

variable "instance_type" {
  type    = string
  default = "t3.small" # 2 vCPU; t3 "unlimited" credits avoid CPU throttling during the test
}

variable "min_size" {
  type    = number
  default = 1
}

variable "max_size" {
  type    = number
  default = 4 # new AWS accounts often have an 8 vCPU limit; a t3.small has 2 vCPU, so 4 is the cap
}

variable "cpu_target" {
  type    = number
  default = 50 # faculty rule: scale out when average CPU goes above 50%
}

variable "suspend_az_rebalance" {
  type    = bool
  default = true # false reproduces the first run, where AZ rebalancing terminated a serving instance at peak
}
