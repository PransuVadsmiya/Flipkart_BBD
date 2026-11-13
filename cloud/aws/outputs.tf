output "alb_dns" {
  value = aws_lb.alb.dns_name
}

output "live_url" {
  value = "http://${aws_lb.alb.dns_name}/status"
}

output "asg_name" {
  value = aws_autoscaling_group.asg.name
}

output "dashboard" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=bbd-scaling"
}
