REGION = "us-east-1"

PROJECT_TAG = {
    "Key": "Project",
    "Value": "cpu-stress"
}

ENV_TAG = {
    "Key": "Env",
    "Value": "dev"
}

VPC_CIDR = "10.0.0.0/16"
SUBNET_CIDR = "10.0.1.0/24"

ASG_MIN = 1
ASG_DESIRED = 1
ASG_MAX = 5

CPU_TARGET = 50  # %
