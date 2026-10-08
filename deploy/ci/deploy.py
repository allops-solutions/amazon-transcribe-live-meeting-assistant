#!/usr/bin/env python3
"""Guarded CI deployment. Secrets stay in memory and are never logged."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError

ACCOUNTS = {"production": "009853297978", "dev": "135755363077"}
REPOSITORY = "allops-solutions/amazon-transcribe-live-meeting-assistant"


def check_context(environment, account, stack, run_id):
    if account != ACCOUNTS[environment]:
        raise ValueError("Authenticated AWS account does not match the target environment")
    if not re.fullmatch(r"[0-9]+", run_id):
        raise ValueError("Invalid workflow run ID")
    expected = "LMA" if environment == "production" else f"LMA-CI-{run_id}"
    if stack != expected:
        raise ValueError("Stack name does not match the protected target")
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("Deployment must run from the maintained allOps repository")
    if os.environ.get("GITHUB_REF") != "refs/heads/allops-main":
        raise ValueError("Deployment must run from allops-main")
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ValueError("Deployment requires a manual workflow dispatch")


def merge_parameters(base, overrides, environment):
    if not isinstance(overrides, dict) or not all(isinstance(v, str) for v in overrides.values()):
        raise ValueError("Configuration secret must be a JSON object of string parameter values")
    values = {p["ParameterKey"]: p["ParameterValue"] for p in base}
    if len(values) != len(base) or set(overrides) - set(values):
        raise ValueError("Duplicate or unknown deployment parameter names")
    values.update(overrides)
    unresolved = [k for k, v in values.items() if "TODO_" in v or "FILL_IN_FROM_SECRET_STORE" in v]
    if unresolved:
        raise ValueError("Unresolved parameter names: " + ", ".join(sorted(unresolved)))
    if values.get("SummaryBedrockModelId") != "global.moonshotai.kimi-k3":
        raise ValueError("This release must keep the approved Kimi summary model")
    if values.get("ModelValidation") != "true":
        raise ValueError("Model validation must remain enabled")
    # Only brand-new, run-owned temporary dev stacks are disposable.
    values["EnableDataRetentionOnDelete"] = "true" if environment == "production" else "false"
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()]


def validate_cleanup(stack, tags, run_id):
    if stack != f"LMA-CI-{run_id}" or tags.get("LmaCiRun") != run_id or tags.get("LmaCiManaged") != "true":
        raise ValueError("Refusing cleanup: stack identity or workflow ownership tags do not match")


def validate_application_boundary(parameters, account, environment):
    """Pin production's boundary independently of the editable configuration secret."""
    if environment != "production":
        return
    approved = os.environ.get("LMA_APPLICATION_BOUNDARY_ARN", "")
    if not re.fullmatch(
        rf"arn:aws:iam::{re.escape(account)}:policy/lma/isolation/[A-Za-z0-9+=,.@_-]+",
        approved,
    ):
        raise ValueError("An approved production application boundary must be configured")
    values = {p["ParameterKey"]: p["ParameterValue"] for p in parameters}
    if values.get("PermissionsBoundaryArn") != approved:
        raise ValueError("Production parameters must use the exact approved application boundary")


def describe_stack(cfn, name):
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ValidationError" and "does not exist" in exc.response["Error"]["Message"]:
            return None
        raise


def validate_cloudformation_role(role, account):
    """Refuse legacy, cross-account or arbitrary administrator service roles."""
    if not re.fullmatch(rf"arn:aws:iam::{account}:role/lma/isolation/LMA-[A-Za-z0-9+=,.@_-]+", role):
        raise ValueError("CloudFormation service role must use the reviewed account-local isolation path")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preflight", "deploy", "cleanup"])
    parser.add_argument("--environment", choices=list(ACCOUNTS), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--template-url")
    args = parser.parse_args()
    session = boto3.Session(region_name="us-east-1")
    stack = "LMA" if args.environment == "production" else f"LMA-CI-{args.run_id}"
    account = session.client("sts").get_caller_identity()["Account"]
    check_context(args.environment, account, stack, args.run_id)
    cfn = session.client("cloudformation")
    existing = describe_stack(cfn, stack)
    if args.action == "cleanup":
        if args.environment != "dev":
            raise ValueError("Production cleanup is not supported")
        if not existing:
            print("No temporary stack exists; nothing to clean up")
            return
        validate_cleanup(stack, {t["Key"]: t["Value"] for t in existing.get("Tags", [])}, args.run_id)
        cfn.delete_stack(StackName=stack)
        cfn.get_waiter("stack_delete_complete").wait(StackName=stack, WaiterConfig={"Delay": 30, "MaxAttempts": 240})
        print("Temporary dev stack deleted; external configuration and artifact storage were not deleted")
        return
    if args.environment == "dev" and existing:
        raise ValueError("Temporary dev deployment is create-only; existing stacks are not updated")
    bucket = os.environ["LMA_ARTIFACT_BUCKET"]
    prefix = f"releases/{os.environ['GITHUB_SHA']}/{args.run_id}/{os.environ['GITHUB_RUN_ATTEMPT']}"
    expected_url = f"https://s3.us-east-1.amazonaws.com/{bucket}/{prefix}/lma-main.yaml"
    if args.template_url != expected_url:
        raise ValueError("Template URL must reference this workflow run's private release prefix")
    secret_arn = os.environ["LMA_CONFIG_SECRET_ARN"]
    if not secret_arn.startswith(f"arn:aws:secretsmanager:us-east-1:{account}:secret:"):
        raise ValueError("Configuration secret must belong to the target account and region")
    overrides = json.loads(session.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"])
    base = json.loads(Path(f"deploy/params/{'prod' if args.environment == 'production' else 'dev'}.json").read_text())
    parameters = merge_parameters(base, overrides, args.environment)
    validate_application_boundary(parameters, account, args.environment)
    role = os.environ["LMA_CFN_ROLE_ARN"]
    validate_cloudformation_role(role, account)
    if args.action == "preflight":
        print("Target account, stack, release URL and configuration validated; no AWS writes performed")
        return
    name = f"lma-ci-{args.run_id}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    response = cfn.create_change_set(
        StackName=stack, ChangeSetName=name, ChangeSetType="UPDATE" if existing else "CREATE",
        TemplateURL=args.template_url, Parameters=parameters, RoleARN=role,
        Capabilities=["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
        Tags=[{"Key": "LmaCiManaged", "Value": "true"}, {"Key": "LmaCiRun", "Value": args.run_id},
              {"Key": "LmaSourceSha", "Value": os.environ["GITHUB_SHA"]}],
    )
    change_id = response["Id"]
    try:
        cfn.get_waiter("change_set_create_complete").wait(ChangeSetName=change_id, WaiterConfig={"Delay": 10, "MaxAttempts": 120})
    except WaiterError:
        detail = cfn.describe_change_set(ChangeSetName=change_id)
        if existing and detail.get("Status") == "FAILED" and any(
            phrase in detail.get("StatusReason", "") for phrase in ["didn't contain changes", "No updates are to be performed"]
        ):
            cfn.delete_change_set(ChangeSetName=change_id)
            print("No CloudFormation changes required")
            return
        raise
    detail = cfn.describe_change_set(ChangeSetName=change_id)
    for change in detail.get("Changes", []):
        resource = change.get("ResourceChange", {})
        print("Change:", resource.get("Action"), resource.get("LogicalResourceId"), "replacement:", resource.get("Replacement"))
    cfn.execute_change_set(ChangeSetName=change_id)
    cfn.get_waiter("stack_update_complete" if existing else "stack_create_complete").wait(
        StackName=stack, WaiterConfig={"Delay": 30, "MaxAttempts": 240}
    )
    if args.environment == "production":
        cfn.update_termination_protection(StackName=stack, EnableTerminationProtection=True)
    print("CloudFormation deployment completed:", stack)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # AWS exception text/parameters can contain secrets. Never dump traceback/payload.
        print(f"CI operation failed ({type(exc).__name__}); inspect CloudFormation events securely", file=sys.stderr)
        sys.exit(1)
