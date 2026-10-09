#!/usr/bin/env python3
"""Read-only simulation of pinned production change-set inputs. No IAM writes."""
import argparse
import json
from pathlib import Path
import re

import boto3
import yaml


def load_policy(account):
    """Resolve only the three production request guard statements for testing."""
    if not re.fullmatch(r'[0-9]{12}', account):
        raise ValueError('Expected a 12-digit account ID')
    region = 'us-east-1'
    bucket = f'allops-lma-ci-{account}-production-{region}'
    role = f'arn:aws:iam::{account}:role/lma/isolation/LMA-CloudFormation'
    template = yaml.load(Path(__file__).with_name('bootstrap.yaml').read_text(), Loader=yaml.BaseLoader)
    source = template['Resources']['DeploymentRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']

    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return resolve(value[1]) if len(value) == 3 and value[0] == 'Production' else [resolve(x) for x in value]
        if value == 'CloudFormationServiceRoleArn':
            return role
        for key, replacement in {'AWS::Partition': 'aws', 'AWS::Region': region,
                                 'AWS::AccountId': account, 'AWS::URLSuffix': 'amazonaws.com',
                                 'Artifacts': bucket}.items():
            value = value.replace('${' + key + '}', replacement)
        return value

    names = {'CreatePinnedReleaseChangeSet', 'DenyUnapprovedChangeSetRole', 'DenyUnapprovedChangeSetSource'}
    statements = [resolve(x) for x in source if isinstance(x, dict) and x.get('Sid') in names]
    if len(statements) != len(names):
        raise ValueError('Missing release guard statements')
    return {'Version': '2012-10-17', 'Statement': statements}, role, bucket


def simulate(client, account):
    """Test explicit request denies against hypothetical broad permissions."""
    policy, role, bucket = load_policy(account)
    url = f'https://s3.us-east-1.amazonaws.com/{bucket}/releases/abc/123/1/lma-main.yaml'
    cases = [
        ('approved', role, url, 'allowed'),
        ('wrong-role', role + '-other', url, 'explicitDeny'),
        ('missing-role', None, url, 'explicitDeny'),
        ('wrong-bucket', role, url.replace(bucket, 'company-unrelated'), 'explicitDeny'),
        ('inline-template', role, None, 'explicitDeny'),
        ('wrong-prefix', role, url.replace('/releases/', '/build-validation/'), 'explicitDeny'),
    ]
    permissive = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]}
    for name, requested_role, source, expected in cases:
        contexts = [{'ContextKeyName': key, 'ContextKeyValues': [value], 'ContextKeyType': 'string'}
                    for key, value in [('cloudformation:RoleArn', requested_role),
                                       ('cloudformation:TemplateUrl', source)] if value is not None]
        actual = client.simulate_custom_policy(
            PolicyInputList=[json.dumps(permissive), json.dumps(policy)],
            ActionNames=['cloudformation:CreateChangeSet'],
            ResourceArns=[f'arn:aws:cloudformation:us-east-1:{account}:stack/LMA/test-id'],
            ContextEntries=contexts,
        )['EvaluationResults'][0]['EvalDecision']
        if actual != expected:
            raise RuntimeError(f'{name}: expected {expected}, got {actual}')
        print(f'PASS {name}: {actual}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    load_policy(args.account)
    session = boto3.Session(profile_name='default', region_name='us-east-1')
    if session.client('sts').get_caller_identity()['Account'] != args.account:
        raise ValueError('Authenticated account differs from simulation target')
    simulate(session.client('iam'), args.account)


if __name__ == '__main__':
    main()
