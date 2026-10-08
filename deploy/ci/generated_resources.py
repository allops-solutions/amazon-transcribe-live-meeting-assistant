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
}
STABLE_STACK_STATES = {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'}
STABLE_RESOURCE_STATES = {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'IMPORT_COMPLETE'}


def validate_arn(kind, value, account, region, partition='aws'):
    """Validate exact account/region ARNs, never wildcard or cross-account scopes."""
    resource = {'kms': r'key/[0-9a-fA-F-]{36}', 'appsync': r'apis/[A-Za-z0-9]{10,40}'}[kind]
    pattern = rf'arn:{re.escape(partition)}:{kind}:{re.escape(region)}:{account}:{resource}'
    if not re.fullmatch(pattern, value):
        raise ValueError('Generated resource ARN is not an exact supported account/region resource')
    return value


def collect(cfn, account, region, partition='aws'):
    """Inventory the exact LMA root and verify every child's parent/root identity."""
    root = cfn.describe_stacks(StackName='LMA')['Stacks'][0]
    root_arn = root['StackId']
    prefix = f'arn:{partition}:cloudformation:{region}:{account}:stack/'
    if not root_arn.startswith(prefix + 'LMA/') or root.get('ParentId'):
        raise ValueError('Unexpected production root stack identity')
    resources = {'kms': [], 'appsync': []}
    seen = set()

    def walk(stack, parent=None):
        stack_arn = stack['StackId']
        if stack_arn in seen or not stack_arn.startswith(prefix):
            raise ValueError('Repeated or foreign nested stack')
        seen.add(stack_arn)
        if parent and (stack.get('ParentId') != parent or stack.get('RootId') != root_arn):
            raise ValueError('Nested stack is not owned by the verified LMA root')
        if stack['StackStatus'] not in STABLE_STACK_STATES:
            raise ValueError('Cannot approve resources from an unstable stack')
        paginator = cfn.get_paginator('list_stack_resources')
        for page in paginator.paginate(StackName=stack_arn):
            for entry in page['StackResourceSummaries']:
                kind = entry['ResourceType']
                if kind not in RESOURCE_TYPES and kind != 'AWS::CloudFormation::Stack':
                    continue
                if entry['ResourceStatus'] == 'DELETE_COMPLETE':
                    continue
                if entry['ResourceStatus'] not in STABLE_RESOURCE_STATES or not entry.get('PhysicalResourceId'):
                    raise ValueError('Cannot approve incomplete generated resources')
                physical = entry['PhysicalResourceId']
                if kind == 'AWS::CloudFormation::Stack':
                    child = cfn.describe_stacks(StackName=physical)['Stacks'][0]
                    if child['StackId'] != physical:
                        raise ValueError('Nested stack lookup changed identity')
                    walk(child, stack_arn)
                    continue
                group, service, resource_prefix = RESOURCE_TYPES[kind]
                arn = physical if physical.startswith('arn:') else (
                    f'arn:{partition}:{service}:{region}:{account}:{resource_prefix}{physical}')
                validate_arn(group, arn, account, region, partition)
                resources[group].append(arn)

    walk(root)
    return {'account': account, 'region': region, 'rootStackArn': root_arn,
            'stacksInspected': len(seen), 'resources': {k: sorted(set(v)) for k, v in resources.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]{12}', args.account):
        raise ValueError('Expected a 12-digit account ID')
    session = boto3.Session(profile_name='default', region_name=args.region)
    if session.client('sts').get_caller_identity()['Account'] != args.account:
        raise ValueError('Authenticated account differs from inventory target')
    result = collect(session.client('cloudformation'), args.account, args.region)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
