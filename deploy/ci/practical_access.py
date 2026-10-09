#!/usr/bin/env python3
"""Read-only policy validation/simulation for conventional deployment access.

Never creates policies, roles, stacks or secrets. This validates IAM decisions,
not end-to-end application compatibility or strict non-IAM account isolation.
"""
import argparse
import json
from pathlib import Path

import boto3
from samtranslator.yaml_helper import yaml_parse


def policies(account, region='us-east-1', bucket=None):
    """Resolve only this administrator template's policy documents for testing."""
    bucket = bucket or f'allops-lma-ci-{account}-production-{region}'
    boundary = f'arn:aws:iam::{account}:policy/lma/isolation/LMA-ApplicationBoundary'
    variables = {'AWS::Partition': 'aws', 'AWS::AccountId': account,
                 'AWS::Region': region, 'AWS::URLSuffix': 'amazonaws.com',
                 'ArtifactBucketName': bucket, 'ApplicationBoundary': boundary}

    def resolve(value):
        if isinstance(value, dict):
            if set(value) == {'Ref'}:
                return variables[value['Ref']]
            if set(value) == {'Fn::Sub'}:
                text = value['Fn::Sub']
                for key, item in variables.items():
                    text = text.replace('${' + key + '}', item)
                if '${' in text:
                    raise ValueError('Unresolved template variable')
                return text
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    source = yaml_parse(Path(__file__).with_name('practical-access.yaml').read_text())
    resources = source['Resources']
    common = resolve(resources['ApplicationBoundary']['Properties']['PolicyDocument'])
    provisioning = resolve(resources['CloudFormationRole']['Properties']['Policies'][0]['PolicyDocument'])
    return common, provisioning, boundary


def validate(session, account):
    """Validate size/syntax and positive/negative IAM decisions with real AWS APIs."""
    common, provision, boundary = policies(account)
    analyzer = session.client('accessanalyzer')
    for name, policy, limit in [('application-service-ceiling', common, 6144),
                                 ('provisioning-inline', provision, 10240)]:
        document = json.dumps(policy, separators=(',', ':'))
        if len(document) > limit:
            raise ValueError(f'{name} exceeds IAM size limit')
        findings = []
        token = None
        while True:
            response = analyzer.validate_policy(policyDocument=document,
                policyType='IDENTITY_POLICY', **({'nextToken': token} if token else {}))
            findings.extend(response.get('findings', []))
            token = response.get('nextToken')
            if not token:
                break
        errors = [item for item in findings if item['findingType'] == 'ERROR']
        print(json.dumps({'policy': name, 'chars': len(document),
                         'errors': len(errors), 'warnings': len(findings) - len(errors)}))
        for item in findings:
            print(json.dumps({'issueCode': item['issueCode'], 'detail': item['findingDetails']}))
        if errors:
            raise ValueError('AWS rejected policy syntax')

    iam = session.client('iam')
    role = f'arn:aws:iam::{account}:role/lma/application/LMA-Worker'
    ci_role = f'arn:aws:iam::{account}:role/LMA-CICD-Production-Runner'
    contexts = [{'ContextKeyName': 'aws:RequestedRegion', 'ContextKeyValues': ['us-east-1'], 'ContextKeyType': 'string'}]
    cases = [
        ('bounded-role-create', 'iam:CreateRole', role, boundary, 'allowed'),
        ('unbounded-role-create', 'iam:CreateRole', role, None, 'explicitDeny'),
        ('wrong-boundary', 'iam:CreateRole', role, boundary + '-other', 'explicitDeny'),
        ('outside-role', 'iam:PutRolePolicy', f'arn:aws:iam::{account}:role/OtherApplication', None, 'implicitDeny'),
        ('edit-ci-role', 'iam:PutRolePolicy', ci_role, None, 'explicitDeny'),
        ('edit-boundary', 'iam:CreatePolicyVersion', boundary, None, 'explicitDeny'),
        ('remove-boundary', 'iam:DeleteRolePermissionsBoundary', role, None, 'explicitDeny'),
        ('pass-application-role', 'iam:PassRole', role, None, 'allowed'),
        ('pass-outside-role', 'iam:PassRole', f'arn:aws:iam::{account}:role/Admin', None, 'implicitDeny'),
        ('assume-outside-role', 'sts:AssumeRole', f'arn:aws:iam::{account}:role/Admin', None, 'explicitDeny'),
        ('own-bucket', 's3:PutObject', 'arn:aws:s3:::lma-production-recordings/test', None, 'allowed'),
        ('outside-bucket', 's3:GetObject', 'arn:aws:s3:::other-production-data/test', None, 'implicitDeny'),
        ('private-release-read', 's3:GetObject', f'arn:aws:s3:::allops-lma-ci-{account}-production-us-east-1/releases/test/code.zip', None, 'allowed'),
        ('private-release-overwrite', 's3:PutObject', f'arn:aws:s3:::allops-lma-ci-{account}-production-us-east-1/releases/test/code.zip', None, 'implicitDeny'),
        ('configuration-secret', 'secretsmanager:GetSecretValue', f'arn:aws:secretsmanager:us-east-1:{account}:secret:lma/ci/production/parameters-test', None, 'explicitDeny'),
        ('vp-schedule', 'scheduler:CreateSchedule', f'arn:aws:scheduler:us-east-1:{account}:schedule/LMA-vp-schedules/vp-uuid', None, 'allowed'),
        ('generated-log-create', 'logs:CreateLogGroup', f'arn:aws:logs:us-east-1:{account}:log-group:LMA-ToJSONFunctionLogGroup-test', None, 'allowed'),
        ('generated-log-delete', 'logs:DeleteLogGroup', f'arn:aws:logs:us-east-1:{account}:log-group:LMA-ToJSONFunctionLogGroup-test', None, 'allowed'),
        ('explicit-log-create', 'logs:CreateLogGroup', f'arn:aws:logs:us-east-1:{account}:log-group:/LMA/lambda/KMSKeyMonitoringFunction', None, 'allowed'),
        ('outside-log-delete', 'logs:DeleteLogGroup', f'arn:aws:logs:us-east-1:{account}:log-group:OtherApplication-test', None, 'implicitDeny'),
        ('outside-lambda', 'lambda:UpdateFunctionCode', f'arn:aws:lambda:us-east-1:{account}:function:OtherApplication', None, 'implicitDeny'),
        ('ci-build', 'codebuild:StartBuild', f'arn:aws:codebuild:us-east-1:{account}:project/lma-ci-production', None, 'explicitDeny'),
        ('nested-stack', 'cloudformation:CreateStack', f'arn:aws:cloudformation:us-east-1:{account}:stack/LMA-Ai/test', None, 'allowed'),
        ('ci-stack', 'cloudformation:UpdateStack', f'arn:aws:cloudformation:us-east-1:{account}:stack/LMA-CICD-Production/test', None, 'explicitDeny'),
        ('outside-stack', 'cloudformation:UpdateStack', f'arn:aws:cloudformation:us-east-1:{account}:stack/OtherApplication/test', None, 'implicitDeny'),
    ]
    for name, action, resource, requested, expected in cases:
        extra = [] if requested is None else [{'ContextKeyName': 'iam:PermissionsBoundary', 'ContextKeyValues': [requested], 'ContextKeyType': 'string'}]
        result = iam.simulate_custom_policy(PolicyInputList=[json.dumps(common), json.dumps(provision)],
            ActionNames=[action], ResourceArns=[resource], ContextEntries=contexts + extra)['EvaluationResults'][0]
        if result['EvalDecision'] != expected:
            raise ValueError(f'{name}: {result["EvalDecision"]}, expected {expected}')
        print(f'PASS {name}: {expected}')
    # True boundary evaluation: a malicious application's broad identity policy
    # cannot grant IAM administration through the boundary or assume another role.
    broad = json.dumps({'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]})
    for action, resource, expected in [
        ('iam:CreateRole', role, 'implicitDeny'),
        ('sts:AssumeRole', ci_role, 'explicitDeny'),
        ('s3:PutObject', 'arn:aws:s3:::lma-production-recordings/test', 'allowed'),
        ('s3:GetObject', 'arn:aws:s3:::other-production-data/test', 'implicitDeny'),
        ('bedrock:InvokeModel', 'arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0', 'allowed'),
        ('transcribe:StartStreamTranscription', '*', 'allowed'),
    ]:
        result = iam.simulate_custom_policy(PolicyInputList=[broad],
            PermissionsBoundaryPolicyInputList=[json.dumps(common)], ActionNames=[action],
            ResourceArns=[resource], ContextEntries=contexts)['EvaluationResults'][0]
        if result['EvalDecision'] != expected:
            raise ValueError(f'boundary {action}: {result["EvalDecision"]}, expected {expected}')
        print(f'PASS boundary {action}: {expected}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    session = boto3.Session(profile_name='default', region_name='us-east-1')
    if session.client('sts').get_caller_identity()['Account'] != args.account:
        raise ValueError('Wrong AWS account')
    validate(session, args.account)
