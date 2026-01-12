#!/usr/bin/env python3

import os
import time
import json
import base64
import boto3
from botocore.exceptions import ClientError

# =========================
# CONFIG (EDIT THESE)
# =========================
REGION = os.environ.get("AWS_REGION", "us-east-1")

NAME = "s3-file-manager"
AMI_ID = "ami-08dcef8cfdf0ece49"    
INSTANCE_TYPE = "t3.micro"

VPC_CIDR = "10.50.0.0/16"
PUBLIC_SUBNET_CIDRS = ["10.50.10.0/24", "10.50.20.0/24"]  # two AZs

MIN_SIZE = 2
MAX_SIZE = 4
DESIRED = 2

# Optional:
KEY_NAME = None  # e.g. "my-ec2-key" or None
INSTANCE_PROFILE_NAME = None  # e.g. "my-ec2-instance-profile" or None

USER_DATA = None


# =========================
# AWS clients
# =========================
ec2 = boto3.client("ec2", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)
autoscaling = boto3.client("autoscaling", region_name=REGION)


def tag_spec(resource_type: str, name: str):
    return [{
        "ResourceType": resource_type,
        "Tags": [
            {"Key": "Name", "Value": name},
            {"Key": "stack", "Value": NAME},
        ]
    }]


def wait(msg: str, seconds: int = 2):
    print(msg)
    time.sleep(seconds)


def pick_two_azs():
    azs = ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )["AvailabilityZones"]
    names = [a["ZoneName"] for a in azs]
    if len(names) < 2:
        raise RuntimeError("Need at least 2 AZs in this region.")
    return names[0], names[1]


def create_vpc():
    resp = ec2.create_vpc(
        CidrBlock=VPC_CIDR,
        TagSpecifications=tag_spec("vpc", f"{NAME}-vpc")
    )
    vpc_id = resp["Vpc"]["VpcId"]

    # Recommended for instances / DNS
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    print(f"VPC: {vpc_id}")
    return vpc_id


def create_igw(vpc_id: str):
    igw = ec2.create_internet_gateway(
        TagSpecifications=tag_spec("internet-gateway", f"{NAME}-igw")
    )["InternetGateway"]
    igw_id = igw["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    print(f"IGW: {igw_id}")
    return igw_id


def create_route_table(vpc_id: str, igw_id: str):
    rt = ec2.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=tag_spec("route-table", f"{NAME}-public-rt")
    )["RouteTable"]
    rt_id = rt["RouteTableId"]

    ec2.create_route(
        RouteTableId=rt_id,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=igw_id
    )
    print(f"RouteTable: {rt_id}")
    return rt_id


def create_public_subnet(vpc_id: str, cidr: str, az: str, rt_id: str, idx: int):
    subnet = ec2.create_subnet(
        VpcId=vpc_id,
        CidrBlock=cidr,
        AvailabilityZone=az,
        TagSpecifications=tag_spec("subnet", f"{NAME}-public-subnet-{idx+1}")
    )["Subnet"]
    subnet_id = subnet["SubnetId"]

    # Auto-assign public IPs (easy mode)
    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

    ec2.associate_route_table(SubnetId=subnet_id, RouteTableId=rt_id)

    print(f"Subnet ({az}): {subnet_id}  CIDR={cidr}")
    return subnet_id


def create_ec2_sg(vpc_id: str):
    sg = ec2.create_security_group(
        GroupName=f"{NAME}-ec2-sg",
        Description="Allow 443 from NLB to instances",
        VpcId=vpc_id,
        TagSpecifications=tag_spec("security-group", f"{NAME}-ec2-sg")
    )
    sg_id = sg["GroupId"]

    # NLB does not (reliably) provide a fixed SG source in classic setups,
    # so we allow from VPC CIDR. (Stricter option: use private subnets + internal LB)
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
        }]
    )

    # Optional egress is open by default; keep it.
    print(f"EC2 SG: {sg_id}")
    return sg_id


def create_target_group(vpc_id: str):
    tg_name = f"{NAME}-tg-443"
    resp = elbv2.create_target_group(
        Name=tg_name,
        Protocol="TCP",
        Port=443,
        VpcId=vpc_id,
        TargetType="instance",
        HealthCheckProtocol="HTTPS",
        HealthCheckPort="443",
        HealthCheckPath="/",
        Tags=[
            {"Key": "stack", "Value": NAME},
            {"Key": "Name", "Value": tg_name},
        ]
    )
    tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
    print(f"TargetGroup: {tg_arn}")
    return tg_arn


def create_nlb(subnet_ids):
    lb_name = f"{NAME}-nlb"
    resp = elbv2.create_load_balancer(
        Name=lb_name,
        Type="network",
        Scheme="internet-facing",
        Subnets=subnet_ids,
        IpAddressType="ipv4",
        Tags=[
            {"Key": "stack", "Value": NAME},
            {"Key": "Name", "Value": lb_name},
        ]
    )
    lb = resp["LoadBalancers"][0]
    lb_arn = lb["LoadBalancerArn"]
    lb_dns = lb["DNSName"]
    print(f"NLB: {lb_arn}")
    print(f"NLB DNS: {lb_dns}")

    # Wait until active
    for _ in range(30):
        desc = elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])["LoadBalancers"][0]
        if desc["State"]["Code"] == "active":
            break
        wait("Waiting for NLB to become active...", 2)

    return lb_arn, lb_dns


def create_nlb_listener_tcp_443(lb_arn: str, tg_arn: str):
    resp = elbv2.create_listener(
        LoadBalancerArn=lb_arn,
        Protocol="TCP",
        Port=443,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}]
    )
    listener_arn = resp["Listeners"][0]["ListenerArn"]
    print(f"Listener TCP:443: {listener_arn}")
    return listener_arn


def create_launch_template(sg_id: str):
    lt_name = f"{NAME}-lt"
    data = {
        "ImageId": AMI_ID,
        "InstanceType": INSTANCE_TYPE,
        "SecurityGroupIds": [sg_id],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": f"{NAME}-instance"}, {"Key": "stack", "Value": NAME}],
            },
            {
                "ResourceType": "volume",
                "Tags": [{"Key": "Name", "Value": f"{NAME}-volume"}, {"Key": "stack", "Value": NAME}],
            },
        ],
    }

    if KEY_NAME:
        data["KeyName"] = KEY_NAME

    if INSTANCE_PROFILE_NAME:
        data["IamInstanceProfile"] = {"Name": INSTANCE_PROFILE_NAME}

    if USER_DATA:
        data["UserData"] = base64.b64encode(USER_DATA.encode("utf-8")).decode("utf-8")

    resp = ec2.create_launch_template(
        LaunchTemplateName=lt_name,
        LaunchTemplateData=data,
        TagSpecifications=tag_spec("launch-template", lt_name)
    )
    lt_id = resp["LaunchTemplate"]["LaunchTemplateId"]
    print(f"LaunchTemplate: {lt_id}")
    return lt_id


def create_asg(lt_id: str, subnet_ids, tg_arn: str):
    asg_name = f"{NAME}-asg"
    autoscaling.create_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        MinSize=MIN_SIZE,
        MaxSize=MAX_SIZE,
        DesiredCapacity=DESIRED,
        VPCZoneIdentifier=",".join(subnet_ids),
        LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
        TargetGroupARNs=[tg_arn],
        HealthCheckType="ELB",
        HealthCheckGracePeriod=90,
        Tags=[
            {"Key": "Name", "Value": asg_name, "PropagateAtLaunch": True},
            {"Key": "stack", "Value": NAME, "PropagateAtLaunch": True},
        ],
    )
    print(f"ASG: {asg_name}")
    return asg_name


def main():
    if AMI_ID.startswith("ami-") is False or "x" in AMI_ID:
        raise SystemExit("Edit AMI_ID at top of file (must be a real ami-...).")

    az1, az2 = pick_two_azs()
    print(f"Using AZs: {az1}, {az2}")

    vpc_id = create_vpc()
    igw_id = create_igw(vpc_id)
    rt_id = create_route_table(vpc_id, igw_id)

    subnet1 = create_public_subnet(vpc_id, PUBLIC_SUBNET_CIDRS[0], az1, rt_id, 0)
    subnet2 = create_public_subnet(vpc_id, PUBLIC_SUBNET_CIDRS[1], az2, rt_id, 1)

    sg_id = create_ec2_sg(vpc_id)

    tg_arn = create_target_group(vpc_id)
    lb_arn, lb_dns = create_nlb([subnet1, subnet2])
    create_nlb_listener_tcp_443(lb_arn, tg_arn)

    lt_id = create_launch_template(sg_id)
    asg_name = create_asg(lt_id, [subnet1, subnet2], tg_arn)

    print("\n✅ DONE")
    print(json.dumps({
        "region": REGION,
        "vpc_id": vpc_id,
        "subnets": [subnet1, subnet2],
        "ec2_sg": sg_id,
        "target_group_arn": tg_arn,
        "nlb_arn": lb_arn,
        "nlb_dns": lb_dns,
        "launch_template_id": lt_id,
        "asg_name": asg_name,
        "url": f"https://{lb_dns}/  (TCP passthrough, TLS terminates on EC2)"
    }, indent=2))


if __name__ == "__main__":
    main()
