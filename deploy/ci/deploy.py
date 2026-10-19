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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from production_config import issues as production_config_issues

ACCOUNTS = {"production": "009853297978", "dev": "135755363077"}
REPOSITORY = "allops-solutions/amazon-transcribe-live-meeting-assistant"


def dev_stack_name(run_id):
    """Keep nested Lambda/Scheduler names within limits; full run ID stays in tags.

    A suffix collision never overwrites a stack: existing dev stacks are rejected
    before deployment and cleanup checks the full owning run ID.
    """
    if not re.fullmatch(r"[0-9]+", run_id):
        raise ValueError("Invalid workflow run ID")
    return f"LMA-CI-{run_id[-6:]}"


def check_context(environment, account, stack, run_id):
    if account != ACCOUNTS[environment]:
        raise ValueError("Authenticated AWS account does not match the target environment")
    if not re.fullmatch(r"[0-9]+", run_id):
        raise ValueError("Invalid workflow run ID")
    expected = "LMA" if environment == "production" else dev_stack_name(run_id)
    if stack != expected:
        raise ValueError("Stack name does not match the protected target")
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("Deployment must run from the maintained allOps repository")
    ref = os.environ.get("GITHUB_REF", "")
    if ref != "refs/heads/allops-main" and not (
        environment == "dev" and re.fullmatch(r"refs/heads/feature/[A-Za-z0-9._/-]+", ref)
    ):
        raise ValueError("Production requires allops-main; dev permits maintained feature branches")
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
    values["EnableDataRetentionOnDelete"] = "true" if (
        environment == "production" or os.environ.get("LMA_KEEP_DEV") == "true"
    ) else "false"
    if environment == 'production':
        found = production_config_issues(
            [{'ParameterKey': key, 'ParameterValue': value} for key, value in values.items()], {})
        if found:
            raise ValueError('Production configuration requires attention: ' +
                             ', '.join(sorted({item['parameter'] for item in found})))
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()]


def validate_cleanup(stack, tags, run_id):
    if stack != dev_stack_name(run_id) or tags.get("LmaCiRun") != run_id or tags.get("LmaCiManaged") != "true":
        raise ValueError("Refusing cleanup: stack identity or workflow ownership tags do not match")


def deployment_owner_run(environment, run_id, owner_run):
    """An explicit retained-dev update never changes production or cleanup scope."""
    if not owner_run:
        return run_id
    if environment != "dev" or os.environ.get("LMA_KEEP_DEV") != "true":
        raise ValueError("Retained-stack updates require dev and keep_dev=true")
    dev_stack_name(owner_run)  # validates the full numeric ID, not just its suffix
    return owner_run


def validate_retained_dev_update(stack, existing, owner_run):
    if not existing:
        raise ValueError("Retained dev update requires an existing owned stack")
    tags = {t["Key"]: t["Value"] for t in existing.get("Tags", [])}
    validate_cleanup(stack, tags, owner_run)
    if existing.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise ValueError("Retained dev stack must be in a healthy complete state")


def validate_application_boundary(parameters, account, environment):
    """Pin production's boundary independently of the editable configuration secret."""
    if environment != "production" and not os.environ.get("LMA_APPLICATION_BOUNDARY_ARN"):
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


def stack_baseline(stack):
    """Return a stable, nonsecret marker for the stack version used to prepare a plan."""
    if not stack:
        return "none", "none"
    updated = stack.get("LastUpdatedTime") or stack.get("CreationTime")
    if hasattr(updated, "isoformat"):
        updated = updated.isoformat()
    return stack.get("StackId", "none"), str(updated or "none")


def validate_cloudformation_role(role, account):
    """Refuse legacy, cross-account or arbitrary administrator service roles."""
    if not re.fullmatch(rf"arn:aws:iam::{account}:role/lma/isolation/LMA-[A-Za-z0-9+=,.@_-]+", role):
        raise ValueError("CloudFormation service role must use the reviewed account-local isolation path")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preflight", "prepare", "execute", "deploy", "cleanup"])
    parser.add_argument("--environment", choices=list(ACCOUNTS), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--template-url")
    parser.add_argument("--change-set-name")
    args = parser.parse_args()
    owner_override = os.environ.get("LMA_DEV_OWNER_RUN_ID", "")
    owner_run = deployment_owner_run(args.environment, args.run_id, owner_override)
    if args.action == "prepare" and args.environment != "production":
        raise ValueError("Change-set preparation is only enabled for production")
    session = boto3.Session(region_name="us-east-1")
    stack = "LMA" if args.environment == "production" else dev_stack_name(owner_run)
    account = session.client("sts").get_caller_identity()["Account"]
    check_context(args.environment, account, stack, owner_run)
    cfn = session.client("cloudformation")
    existing = describe_stack(cfn, stack)
    if args.action == "cleanup":
        if owner_override:
            raise ValueError("Retained dev updates never perform automatic cleanup")
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
    if owner_override:
        validate_retained_dev_update(stack, existing, owner_run)
    elif args.environment == "dev" and existing:
        raise ValueError("Temporary dev deployment is create-only; existing stacks are not updated")
    if existing and existing.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise ValueError("Target stack must be in a healthy complete state")
    bucket = os.environ["LMA_ARTIFACT_BUCKET"]
    if args.action != "execute":
        prefix = f"releases/{os.environ['GITHUB_SHA']}/{args.run_id}/{os.environ['GITHUB_RUN_ATTEMPT']}"
        expected_url = f"https://s3.us-east-1.amazonaws.com/{bucket}/{prefix}/lma-main.yaml"
        if args.template_url != expected_url:
            raise ValueError("Template URL must reference this workflow run's private release prefix")
    secret_arn = os.environ["LMA_CONFIG_SECRET_ARN"]
    if not secret_arn.startswith(f"arn:aws:secretsmanager:us-east-1:{account}:secret:"):
        raise ValueError("Configuration secret must belong to the target account and region")
    secret = session.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    overrides = json.loads(secret["SecretString"])
    base = json.loads(Path(f"deploy/params/{'prod' if args.environment == 'production' else 'dev'}.json").read_text())
    parameters = merge_parameters(base, overrides, args.environment)
    validate_application_boundary(parameters, account, args.environment)
    role = os.environ["LMA_CFN_ROLE_ARN"]
    validate_cloudformation_role(role, account)
    if args.action == "preflight":
        print("Target account, stack, configuration and release path validated; no AWS writes performed")
        return
    if args.action == "execute":
        if args.environment != "production":
            raise ValueError("Reviewed change-set execution is only enabled for production")
        if os.environ.get("LMA_CONFIRMATION") != "EXECUTE REVIEWED LMA CHANGE SET":
            raise ValueError("Explicit reviewed-change-set confirmation is required")
        if not args.change_set_name or not re.fullmatch(r"lma-ci-[0-9]+-[0-9]+", args.change_set_name):
            raise ValueError("Provide the exact prepared production change-set name")
        if not existing:
            raise ValueError("Production stack no longer exists; refusing to execute a stale change set")
        stack_id, stack_version = stack_baseline(existing)
        change = cfn.describe_change_set(ChangeSetName=args.change_set_name, StackName=stack)
        expected_description = (
            f"LMA prepared release sha={os.environ['GITHUB_SHA']} "
            f"configVersion={secret['VersionId']} stackId={stack_id} stackVersion={stack_version}"
        )
        # DescribeChangeSet omits ChangeSetType; match the actual existing root
        # stack ID as well as the source/configuration/version recorded at prepare.
        if (change.get("Status") != "CREATE_COMPLETE"
                or change.get("ExecutionStatus") != "AVAILABLE"
                or change.get("StackId") != stack_id
                or change.get("Description") != expected_description
                or not change.get("IncludeNestedStacks")):
            raise ValueError("Change set is stale, unreviewable, or does not match the current release/configuration")
        cfn.execute_change_set(ChangeSetName=change["ChangeSetId"])
        cfn.get_waiter("stack_update_complete").wait(
            StackName=stack, WaiterConfig={"Delay": 30, "MaxAttempts": 240}
        )
        cfn.update_termination_protection(StackName=stack, EnableTerminationProtection=True)
        print("Reviewed CloudFormation deployment completed:", stack)
        return
    if args.action == "deploy" and args.environment == "production":
        raise ValueError("Production requires prepare, review, then execute of a change set")
    name = f"lma-ci-{args.run_id}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    stack_id, stack_version = stack_baseline(existing)
    response = cfn.create_change_set(
        StackName=stack, ChangeSetName=name, ChangeSetType="UPDATE" if existing else "CREATE",
        TemplateURL=args.template_url, Parameters=parameters, RoleARN=role,
        Capabilities=["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
        IncludeNestedStacks=args.action == "prepare",
        Description=(f"LMA prepared release sha={os.environ['GITHUB_SHA']} "
                     f"configVersion={secret['VersionId']} stackId={stack_id} stackVersion={stack_version}"),
        Tags=[{"Key": "LmaCiManaged", "Value": "true"}, {"Key": "LmaCiRun", "Value": owner_run},
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
    changes = list(detail.get("Changes", []))
    while detail.get("NextToken"):
        detail = cfn.describe_change_set(ChangeSetName=change_id, NextToken=detail["NextToken"])
        changes.extend(detail.get("Changes", []))
    for change in changes:
        resource = change.get("ResourceChange", {})
        print("Change:", resource.get("Action"), resource.get("LogicalResourceId"),
              "type:", resource.get("ResourceType"), "replacement:", resource.get("Replacement"),
              "scope:", resource.get("Scope"))
    if args.action == "prepare":
        print("Prepared production change set (not executed):", name)
        print("CloudFormation ChangeSetId:", change_id)
        print("Review root and nested changes in CloudFormation, then dispatch operation=execute "
              "with this exact name and confirmation EXECUTE REVIEWED LMA CHANGE SET.")
        return
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
