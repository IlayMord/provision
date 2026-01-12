#!/usr/bin/env python3
import boto3
import time
import botocore

REGION = "us-east-1"
STACK = "s3-file-manager"

ec2 = boto3.client("ec2", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)
asg = boto3.client("autoscaling", region_name=REGION)

# ---------- Helpers ----------
def retry(msg, fn, delay=15):
    while True:
        try:
            fn()
            return
        except botocore.exceptions.ClientError as e:
            if "DependencyViolation" in str(e) or "ResourceInUse" in str(e):
                print(f"   {msg} — waiting...")
                time.sleep(delay)
            else:
                raise

def has_stack_tag(obj):
    return any(t["Key"] == "stack" and t["Value"] == STACK for t in obj.get("Tags", []))

# ---------- ASG ----------
print("🧨 Deleting Auto Scaling Groups")
for g in asg.describe_auto_scaling_groups()["AutoScalingGroups"]:
    if has_stack_tag(g):
        name = g["AutoScalingGroupName"]
        print("  ASG:", name)
        asg.update_auto_scaling_group(AutoScalingGroupName=name, MinSize=0, MaxSize=0, DesiredCapacity=0)
        time.sleep(20)
        asg.delete_auto_scaling_group(AutoScalingGroupName=name, ForceDelete=True)

# ---------- NLB ----------
print("🧨 Deleting Network Load Balancers")
for lb in elbv2.describe_load_balancers()["LoadBalancers"]:
    if STACK in lb["LoadBalancerName"]:
        print("  NLB:", lb["LoadBalancerName"])
        elbv2.delete_load_balancer(LoadBalancerArn=lb["LoadBalancerArn"])

print("   Waiting for NLBs to release ENIs...")
time.sleep(60)

# ---------- Target Groups ----------
print("🧨 Deleting Target Groups")
for tg in elbv2.describe_target_groups()["TargetGroups"]:
    if STACK in tg["TargetGroupName"]:
        print("  TG:", tg["TargetGroupName"])
        retry("TG busy", lambda: elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"]))

# ---------- Launch Templates ----------
print("🧨 Deleting Launch Templates")
for lt in ec2.describe_launch_templates()["LaunchTemplates"]:
    if STACK in lt["LaunchTemplateName"]:
        print("  LT:", lt["LaunchTemplateName"])
        ec2.delete_launch_template(LaunchTemplateId=lt["LaunchTemplateId"])

# ---------- Security Groups ----------
print("🧨 Deleting Security Groups")
for sg in ec2.describe_security_groups()["SecurityGroups"]:
    if has_stack_tag(sg):
        print("  SG:", sg["GroupId"])
        retry("SG in use", lambda: ec2.delete_security_group(GroupId=sg["GroupId"]))

# ---------- VPC ----------
print("🧨 Deleting VPC and network")
for vpc in ec2.describe_vpcs()["Vpcs"]:
    if has_stack_tag(vpc):
        vpc_id = vpc["VpcId"]
        print("  VPC:", vpc_id)

        # IGW
        igws = ec2.describe_internet_gateways(Filters=[{"Name":"attachment.vpc-id","Values":[vpc_id]}])["InternetGateways"]
        for igw in igws:
            igw_id = igw["InternetGatewayId"]
            print("   IGW:", igw_id)
            retry("IGW detach blocked", lambda: ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id))
            retry("IGW delete blocked", lambda: ec2.delete_internet_gateway(InternetGatewayId=igw_id))

        # Subnets
        for s in ec2.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["Subnets"]:
            print("   Subnet:", s["SubnetId"])
            retry("Subnet in use", lambda sid=s["SubnetId"]: ec2.delete_subnet(SubnetId=sid))

        # Route Tables
        for rt in ec2.describe_route_tables(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["RouteTables"]:
            if not any(a.get("Main") for a in rt["Associations"]):
                print("   Route Table:", rt["RouteTableId"])
                retry("RT in use", lambda rid=rt["RouteTableId"]: ec2.delete_route_table(RouteTableId=rid))

        # VPC
        retry("VPC busy", lambda: ec2.delete_vpc(VpcId=vpc_id))

print("\n🔥 ALL STACK RESOURCES DESTROYED 🔥")
