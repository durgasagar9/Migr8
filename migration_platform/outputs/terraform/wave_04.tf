# Wave 4


resource "aws_lb" "lb_orders" {
  name               = "lb-orders"
  internal           = false
  load_balancer_type = "application"

  tags = {
    Name     = "orders-loadbalancer"
    SourceId = "lb-orders"
  }
}
