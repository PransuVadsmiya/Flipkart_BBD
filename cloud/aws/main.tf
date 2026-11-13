terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# ---- network: default VPC, one subnet per AZ ------------------------------------------------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# ---- security groups: internet -> ALB:80 -> instances:80 (only from the ALB) ----------------
resource "aws_security_group" "alb" {
  name_prefix = "bbd-alb-"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "app" {
  name_prefix = "bbd-app-"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---- launch template: Docker + the storefront, started from user data -----------------------
resource "aws_launch_template" "app" {
  name_prefix            = "bbd-app-"
  image_id               = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.app.id]
  update_default_version = true

  monitoring {
    enabled = true # 1-minute CPU metrics so scaling reacts faster
  }

  credit_specification {
    cpu_credits = "unlimited"
  }

  user_data = base64encode(templatefile("${path.module}/user_data.sh.tftpl", {
    app_gz_b64 = base64gzip(file("${path.module}/../../app/main.py"))
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "bbd-app"
    }
  }
}

# ---- load balancer ---------------------------------------------------------------------------
resource "aws_lb" "alb" {
  name               = "bbd-alb"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
}

resource "aws_lb_target_group" "tg" {
  name                 = "bbd-tg"
  port                 = 80
  protocol             = "HTTP"
  vpc_id               = data.aws_vpc.default.id
  deregistration_delay = 30

  health_check {
    path                = "/health"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.alb.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.tg.arn
  }
}

# ---- auto scaling group + CPU target-tracking policy -----------------------------------------
resource "aws_autoscaling_group" "asg" {
  name                      = "bbd-asg"
  min_size                  = var.min_size
  max_size                  = var.max_size
  desired_capacity          = var.min_size
  vpc_zone_identifier       = data.aws_subnets.default.ids
  target_group_arns         = [aws_lb_target_group.tg.arn]
  health_check_type         = "ELB"
  health_check_grace_period = 180
  default_instance_warmup   = 120
  # The first AWS run lost a serving instance to availability-zone rebalancing at peak. true = suspend that process.
  suspended_processes       = var.suspend_az_rebalance ? ["AZRebalance"] : []
  metrics_granularity       = "1Minute"
  enabled_metrics = [
    "GroupMinSize", "GroupMaxSize", "GroupDesiredCapacity", "GroupInServiceInstances",
    "GroupPendingInstances", "GroupTerminatingInstances", "GroupTotalInstances",
  ]

  launch_template {
    id      = aws_launch_template.app.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "bbd-app"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_policy" "cpu" {
  name                   = "bbd-cpu-target-${var.cpu_target}"
  autoscaling_group_name = aws_autoscaling_group.asg.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
    target_value = var.cpu_target
  }
}

# ---- CloudWatch dashboard: instance count, CPU, ALB latency and request rate in one place ----
resource "aws_cloudwatch_dashboard" "scaling" {
  dashboard_name = "bbd-scaling"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Instances: in service vs desired"
          region = var.region, period = 60, stat = "Average"
          metrics = [
            ["AWS/AutoScaling", "GroupInServiceInstances", "AutoScalingGroupName", aws_autoscaling_group.asg.name],
            ["AWS/AutoScaling", "GroupDesiredCapacity", "AutoScalingGroupName", aws_autoscaling_group.asg.name],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Average CPU (scaling target 50%)"
          region = var.region, period = 60, stat = "Average"
          metrics = [["AWS/EC2", "CPUUtilization", "AutoScalingGroupName", aws_autoscaling_group.asg.name]]
          annotations = { horizontal = [{ label = "target", value = var.cpu_target }] }
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "ALB response time (p95 and average)"
          region = var.region, period = 60
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.alb.arn_suffix, { stat = "p95" }],
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.alb.arn_suffix, { stat = "Average" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "ALB requests per minute"
          region = var.region, period = 60, stat = "Sum"
          metrics = [["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.alb.arn_suffix]]
        }
      },
    ]
  })
}
