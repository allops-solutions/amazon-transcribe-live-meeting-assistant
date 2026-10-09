#!/usr/bin/env python3
"""Read-only ownership inventory for generated IDs in the production LMA stack.

Walk every CloudFormation page and nested stack. Never trust a name prefix as
proof that a generated key/API belongs to LMA. No policies are updated here.
"""
import argparse
import json
import re

import boto3


RESOURCE_TYPES = {
    'AWS::KMS::Key': ('kms', 'kms', 'key/'),
    'AWS::AppSync::GraphQLApi': ('appsync', 'appsync', 'apis/'),
    'AWS::AppSync::Api': ('appsync', 'appsync', 'apis/'),
    'AWS::Cognito::UserPool': ('cognito', 'cognito-idp', 'userpool/'),
    'AWS::CloudFront::Distribution': ('cloudfront', 'cloudfront', 'distribution/'),
    'AWS::ECS::Cluster': ('ecs_clusters', 'ecs', 'cluster/'),
    'AWS::ECS::TaskDefinition': ('ecs_task_definitions', 'ecs', 'task-definition/'),
    'AWS::Bedrock::KnowledgeBase': ('knowledge_bases', 'bedrock', 'knowledge-base/'),
    'AWS::S3Vectors::Index': ('vector_indexes', 's3vectors', ''),
    'AWS::StepFunctions::StateMachine': ('state_machines', 'states', 'stateMachine:'),
    'AWS::Scheduler::ScheduleGroup': ('schedule_groups', 'scheduler', 'schedule-group/'),
}
STABLE_STACK_STATES = {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'}
STABLE_RESOURCE_STATES = {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'IMPORT_COMPLETE'}


def validate_arn(kind, value, account, region, partition='aws'):
    """Validate exact account/region ARNs, never wildcard or cross-account scopes."""
    patterns = {
        'kms': ('kms', r'key/[0-9a-fA-F-]{36}'),
        'appsync': ('appsync', r'apis/[A-Za-z0-9]{10,40}'),
        'cognito': ('cognito-idp', rf'userpool/{re.escape(region)}_[A-Za-z0-9]+'),
        'cloudfront': ('cloudfront', r'distribution/[A-Z0-9]{10,20}'),
        'ecs_clusters': ('ecs', r'cluster/[A-Za-z0-9_-]{1,255}'),
        'ecs_task_definitions': ('ecs', r'task-definition/[A-Za-z0-9_-]{1,255}:[1-9][0-9]*'),
        'knowledge_bases': ('bedrock', r'knowledge-base/[A-Za-z0-9]{10}'),
        'vector_indexes': ('s3vectors', r'bucket/[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/index/[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]'),
        'state_machines': ('states', r'stateMachine:[A-Za-z0-9_-]{1,80}'),
        'schedule_groups': ('scheduler', r'schedule-group/[A-Za-z0-9_.-]{1,64}'),
    }
    service, resource = patterns[kind]
    arn_region = '' if kind == 'cloudfront' else region
    pattern = rf'arn:{re.escape(partition)}:{service}:{re.escape(arn_region)}:{account}:{resource}'
    if not re.fullmatch(pattern, value):
        raise ValueError('Generated resource ARN is not an exact supported account/region resource')
    return value


def collect(cfn, account, region, partition='aws', creating_root_arn=None):
    """Inventory the exact LMA root and verify every child's parent/root identity."""
    root = cfn.describe_stacks(StackName='LMA')['Stacks'][0]
    root_arn = root['StackId']
    prefix = f'arn:{partition}:cloudformation:{region}:{account}:stack/'
    if not root_arn.startswith(prefix + 'LMA/') or root.get('ParentId'):
        raise ValueError('Unexpected production root stack identity')
    if creating_root_arn is not None:
        if root_arn != creating_root_arn or not re.fullmatch(
            re.escape(prefix + 'LMA/') + r'[A-Za-z0-9-]+', creating_root_arn
        ):
            raise ValueError('First-deployment inventory must pin the exact newly-created root ARN')
        if root['StackStatus'] != 'CREATE_IN_PROGRESS':
            raise ValueError('First-deployment inventory only accepts a new root being created')
    resources = {group: [] for group, _, _ in RESOURCE_TYPES.values()}
    seen = set()
    pending = 0

    def walk(stack, parent=None):
        nonlocal pending
        stack_arn = stack['StackId']
        if stack_arn in seen or not stack_arn.startswith(prefix):
            raise ValueError('Repeated or foreign nested stack')
        seen.add(stack_arn)
        if parent and (stack.get('ParentId') != parent or stack.get('RootId') != root_arn):
            raise ValueError('Nested stack is not owned by the verified LMA root')
        allowed_states = {'CREATE_IN_PROGRESS', 'CREATE_COMPLETE'} if creating_root_arn else STABLE_STACK_STATES
        if stack['StackStatus'] not in allowed_states:
            raise ValueError('Cannot approve resources from an unstable stack')
        paginator = cfn.get_paginator('list_stack_resources')
        for page in paginator.paginate(StackName=stack_arn):
            for entry in page['StackResourceSummaries']:
                kind = entry['ResourceType']
                if kind not in RESOURCE_TYPES and kind != 'AWS::CloudFormation::Stack':
                    continue
                if entry['ResourceStatus'] == 'DELETE_COMPLETE':
                    continue
                if creating_root_arn and entry['ResourceStatus'] == 'CREATE_IN_PROGRESS':
                    if kind == 'AWS::CloudFormation::Stack' and entry.get('PhysicalResourceId'):
                        child = cfn.describe_stacks(StackName=entry['PhysicalResourceId'])['Stacks'][0]
                        if child['StackId'] != entry['PhysicalResourceId']:
                            raise ValueError('Nested stack lookup changed identity')
                        walk(child, stack_arn)
                    else:
                        pending += 1
                    continue
                allowed_resource_states = {'CREATE_COMPLETE'} if creating_root_arn else STABLE_RESOURCE_STATES
                if entry['ResourceStatus'] not in allowed_resource_states or not entry.get('PhysicalResourceId'):
                    raise ValueError('Cannot approve incomplete generated resources')
                if entry['ResourceStatus'] == 'IMPORT_COMPLETE':
                    raise ValueError('Imported resources require separate administrator origin review')
                physical = entry['PhysicalResourceId']
                if kind == 'AWS::CloudFormation::Stack':
                    child = cfn.describe_stacks(StackName=physical)['Stacks'][0]
                    if child['StackId'] != physical:
                        raise ValueError('Nested stack lookup changed identity')
                    walk(child, stack_arn)
                    continue
                group, service, resource_prefix = RESOURCE_TYPES[kind]
                arn_region = '' if service == 'cloudfront' else region
                arn = physical if physical.startswith('arn:') else (
                    f'arn:{partition}:{service}:{arn_region}:{account}:{resource_prefix}{physical}')
                validate_arn(group, arn, account, region, partition)
                resources[group].append(arn)

    walk(root)
    return {'account': account, 'region': region, 'rootStackArn': root_arn,
            'stacksInspected': len(seen), 'inventoryComplete': creating_root_arn is None,
            'resourcesPending': pending, 'resources': {k: sorted(set(v)) for k, v in resources.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--creating-root-arn', help='Read completed IDs during initial CREATE only; partial inventory, no IAM writes')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]{12}', args.account):
        raise ValueError('Expected a 12-digit account ID')
    session = boto3.Session(profile_name='default', region_name=args.region)
    if session.client('sts').get_caller_identity()['Account'] != args.account:
        raise ValueError('Authenticated account differs from inventory target')
    result = collect(session.client('cloudformation'), args.account, args.region,
                     creating_root_arn=args.creating_root_arn)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
