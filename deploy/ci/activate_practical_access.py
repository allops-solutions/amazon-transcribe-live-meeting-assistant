#!/usr/bin/env python3
"""Administrator activation of CI preparation only; never deploys the LMA app.

Default is read-only. --apply creates/updates the IAM-only access stack, pins the
configuration boundary without displaying secret values, then enables the GitHub
deployment identity in the existing runner stack. Existing dev is never touched.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

import boto3
from botocore.exceptions import ClientError

ACCOUNT = '009853297978'
REGION = 'us-east-1'
ACCESS_STACK = 'LMA-Isolation-Access'
RUNNER_STACK = 'LMA-CICD-Production'
SECRET = 'lma/ci/production/parameters'
ROOT = Path(__file__).resolve().parents[2]


def stack_or_none(client, name):
    """Return one exact preparation stack; unrelated errors are never suppressed."""
    if name not in (ACCESS_STACK, RUNNER_STACK):
        raise ValueError('Only preparation stacks are authorized')
    try:
        return client.describe_stacks(StackName=name)['Stacks'][0]
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ValidationError' and 'does not exist' in exc.response['Error']['Message']:
            return None
        raise


def execute_preparation(client, name, template, parameters, expected_logical_ids):
    """Execute only a reviewed change set for one of the two preparation stacks."""
    existing = stack_or_none(client, name)
    if existing and existing['StackStatus'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise ValueError('Preparation stack is not in a healthy completed state')
    change_type = 'UPDATE' if existing else 'CREATE'
    result = client.create_change_set(StackName=name, ChangeSetName='lma-preparation-' + uuid.uuid4().hex,
        ChangeSetType=change_type, TemplateBody=template, Parameters=parameters,
        Capabilities=['CAPABILITY_NAMED_IAM'],
        Description='Authorized conventional production CI preparation; no application deployment')
    identifier = result['Id']
    try:
        client.get_waiter('change_set_create_complete').wait(ChangeSetName=identifier,
            WaiterConfig={'Delay': 5, 'MaxAttempts': 120})
    except Exception:
        change = client.describe_change_set(ChangeSetName=identifier)
        if change['Status'] == 'FAILED' and "didn't contain changes" in change.get('StatusReason', ''):
            client.delete_change_set(ChangeSetName=identifier)
            return existing
        raise
    change = client.describe_change_set(ChangeSetName=identifier)
    changes = []
    while True:
        changes.extend(change.get('Changes', []))
        token = change.get('NextToken')
        if not token:
            break
        change = client.describe_change_set(ChangeSetName=identifier, NextToken=token)
    for item in changes:
        resource = item['ResourceChange']
        if resource['LogicalResourceId'] not in expected_logical_ids or resource['ResourceType'] not in ('AWS::IAM::Role', 'AWS::IAM::ManagedPolicy'):
            raise ValueError('Unexpected preparation change; no change set executed')
        print(json.dumps({'stack': name, 'action': resource['Action'],
                          'resource': resource['LogicalResourceId'], 'type': resource['ResourceType']}))
    client.execute_change_set(ChangeSetName=identifier)
    client.get_waiter('stack_create_complete' if change_type == 'CREATE' else 'stack_update_complete').wait(
        StackName=name, WaiterConfig={'Delay': 10, 'MaxAttempts': 120})
    return stack_or_none(client, name)


def pin_boundary(secrets, boundary):
    """Preserve all secret parameters; record a new recoverable version if needed."""
    original = secrets.get_secret_value(SecretId=SECRET)
    values = json.loads(original['SecretString'])
    if not isinstance(values, dict) or not all(isinstance(value, str) for value in values.values()):
        raise ValueError('Configuration must be an object of strings')
    current = values.get('PermissionsBoundaryArn', '')
    if current and current != boundary:
        raise ValueError('Different boundary already configured; not overwriting')
    if current == boundary:
        return
    latest = secrets.get_secret_value(SecretId=SECRET)
    if latest['VersionId'] != original['VersionId']:
        raise ValueError('Configuration changed concurrently; not overwriting')
    values['PermissionsBoundaryArn'] = boundary
    secrets.put_secret_value(SecretId=SECRET, SecretString=json.dumps(values),
                             ClientRequestToken=str(uuid.uuid4()))
    print('Approved boundary pinned in configuration; all other values preserved, previous secret version retained')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    session = boto3.Session(profile_name='default', region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise ValueError('Production administrator account required')
    cfn = session.client('cloudformation')
    runner = stack_or_none(cfn, RUNNER_STACK)
    if not runner or runner['StackStatus'] != 'UPDATE_COMPLETE':
        raise ValueError('Existing production runner must be healthy')
    params = {item['ParameterKey']: item['ParameterValue'] for item in runner['Parameters']}
    outputs = {item['OutputKey']: item['OutputValue'] for item in runner['Outputs']}
    bucket = outputs['ArtifactBucket']
    if bucket != f'allops-lma-ci-{ACCOUNT}-production-{REGION}' or params['Environment'] != 'production':
        raise ValueError('Runner identity does not match approved production bootstrap')
    if not args.apply:
        print(json.dumps({'account': ACCOUNT, 'preparationStacks': [ACCESS_STACK, RUNNER_STACK],
                          'deploymentRoleEnabled': params['EnableDeploymentRole'],
                          'applicationDeploymentAttempted': False, 'writes': False}))
        return
    access = execute_preparation(cfn, ACCESS_STACK,
        (ROOT / 'deploy/ci/practical-access.yaml').read_text(),
        [{'ParameterKey': 'ArtifactBucketName', 'ParameterValue': bucket}],
        {'ApplicationBoundary', 'CloudFormationRole'})
    configured = {item['OutputKey']: item['OutputValue'] for item in access['Outputs']}
    boundary = configured['ApplicationBoundaryArn']
    role = configured['CloudFormationRoleArn']
    if boundary != f'arn:aws:iam::{ACCOUNT}:policy/lma/isolation/LMA-ApplicationBoundary' or role != f'arn:aws:iam::{ACCOUNT}:role/lma/isolation/LMA-CloudFormation':
        raise ValueError('Unexpected preparation outputs')
    pin_boundary(session.client('secretsmanager'), boundary)
    updates = {'EnableDeploymentRole': 'true', 'CloudFormationServiceRoleArn': role}
    parameters = [{'ParameterKey': key, 'ParameterValue': updates[key]} if key in updates
                  else {'ParameterKey': key, 'UsePreviousValue': True} for key in params]
    updated = execute_preparation(cfn, RUNNER_STACK,
        (ROOT / 'deploy/ci/bootstrap.yaml').read_text(), parameters, {'DeploymentRole'})
    final = {item['OutputKey']: item['OutputValue'] for item in updated['Outputs']}
    print(json.dumps({'LMA_DEPLOY_ROLE_ARN': final['DeploymentRoleArn'],
                      'LMA_CFN_ROLE_ARN': role, 'LMA_APPLICATION_BOUNDARY_ARN': boundary,
                      'applicationDeploymentAttempted': False}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not leak SDK request data or secret values in error messages.
        print(f'Preparation activation stopped ({type(exc).__name__}); no secret values displayed', file=sys.stderr)
        sys.exit(1)
