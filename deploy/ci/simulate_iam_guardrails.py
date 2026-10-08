#!/usr/bin/env python3
"""Read-only AWS IAM simulation of the draft deny overlay. Never attaches policies.

An intentionally overprivileged Allow is included only in the simulator input,
to prove the explicit Denies survive a broader future provisioning policy.
This does NOT validate the future application boundary or non-IAM containment.
"""
import argparse
import json
from pathlib import Path
import re

import boto3
import yaml


def load_policy(account):
    template = yaml.load(Path(__file__).with_name('iam-guardrails.yaml').read_text(),
                         Loader=yaml.BaseLoader)
    boundary = f'arn:aws:iam::{account}:policy/lma/isolation/application-boundary'

    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if value == 'ApplicationBoundaryArn':
            return boundary
        return value.replace('${AWS::Partition}', 'aws').replace('${AWS::AccountId}', account)

    policy = template['Resources']['ProvisioningIamGuardrails']['Properties']['PolicyDocument']
    return resolve(policy), boundary


def cases(account, boundary):
    role = f'arn:aws:iam::{account}:role/lma/application/LMA-Ai-Worker-example'
    other = f'arn:aws:iam::{account}:role/UnrelatedProductionAdmin'
    ci = f'arn:aws:iam::{account}:role/LMA-CICD-Production-RunnerRole-example'
    policy = f'arn:aws:iam::{account}:policy/lma/isolation/provisioning-guardrails'
    # name, action, resource, requested boundary (None = missing), decision
    return [
        ('create-bounded-role', 'iam:CreateRole', role, boundary, 'allowed'),
        ('create-without-boundary', 'iam:CreateRole', role, None, 'explicitDeny'),
        ('create-with-wrong-boundary', 'iam:CreateRole', role, boundary + '-other', 'explicitDeny'),
        ('create-unrelated-role', 'iam:CreateRole', other, boundary, 'explicitDeny'),
        ('replace-with-approved-boundary', 'iam:PutRolePermissionsBoundary', role, boundary, 'allowed'),
        ('replace-with-wrong-boundary', 'iam:PutRolePermissionsBoundary', role, boundary + '-other', 'explicitDeny'),
        ('remove-boundary', 'iam:DeleteRolePermissionsBoundary', role, None, 'explicitDeny'),
        ('edit-ci-role', 'iam:PutRolePolicy', ci, None, 'explicitDeny'),
        ('create-ci-role', 'iam:CreateRole', ci, boundary, 'explicitDeny'),
        ('pass-ci-role', 'iam:PassRole', ci, None, 'explicitDeny'),
        ('pass-unrelated-role', 'iam:PassRole', other, None, 'explicitDeny'),
        ('pass-legacy-unbounded-lma-role', 'iam:PassRole', f'arn:aws:iam::{account}:role/LMA-LegacyAdmin', None, 'explicitDeny'),
        ('create-iam-user', 'iam:CreateUser', f'arn:aws:iam::{account}:user/LMA-user', None, 'explicitDeny'),
        ('edit-boundary', 'iam:CreatePolicyVersion', boundary, None, 'explicitDeny'),
        ('change-boundary-default', 'iam:SetDefaultPolicyVersion', boundary, None, 'explicitDeny'),
        ('delete-boundary', 'iam:DeletePolicy', boundary, None, 'explicitDeny'),
        ('edit-guardrails', 'iam:CreatePolicyVersion', policy, None, 'explicitDeny'),
        ('assume-unrelated-role', 'sts:AssumeRole', other, None, 'explicitDeny'),
        ('read-application-role', 'iam:GetRole', role, None, 'allowed'),
        ('delete-application-role', 'iam:DeleteRole', role, None, 'allowed'),
    ]


def simulate(client, account):
    overlay, boundary = load_policy(account)
    permissive = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]}
    for name, action, resource, requested, expected in cases(account, boundary):
        contexts = [] if requested is None else [{
            'ContextKeyName': 'iam:PermissionsBoundary',
            'ContextKeyValues': [requested], 'ContextKeyType': 'string'}]
        result = client.simulate_custom_policy(
            PolicyInputList=[json.dumps(permissive), json.dumps(overlay)],
            ActionNames=[action], ResourceArns=[resource], ContextEntries=contexts,
        )['EvaluationResults'][0]
        actual = result['EvalDecision']
        if actual != expected:
            raise RuntimeError(f'{name}: expected {expected}, got {actual}')
        print(f'PASS {name}: {actual}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]{12}', args.account):
        raise ValueError('Expected a 12-digit account ID')
    session = boto3.Session(profile_name='default', region_name='us-east-1')
    if session.client('sts').get_caller_identity()['Account'] != args.account:
        raise ValueError('Authenticated account differs from simulation target')
    simulate(session.client('iam'), args.account)


if __name__ == '__main__':
    main()
