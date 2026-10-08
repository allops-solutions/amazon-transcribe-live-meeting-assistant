#!/usr/bin/env python3
"""Draft runtime ceiling for named LMA resources. Not production-ready or deployed.

Generated-ID services are intentionally denied until separately scoped. This is
a boundary, not an identity grant. Explicit Denies also constrain hypothetical
direct session resource-policy grants. No IAM writes are performed by this file.
"""
import argparse
import json
import re

import boto3


def build_policy(account, region, artifact_bucket, documents_bucket='', partition='aws'):
    """Return a deny-by-default boundary; accept only the reviewed bucket namespaces."""
    if not re.fullmatch(r'[0-9]{12}', account) or not re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-[0-9]', region):
        raise ValueError('Invalid account or region')
    if partition not in {'aws', 'aws-us-gov', 'aws-cn'}:
        raise ValueError('Unsupported partition')
    if artifact_bucket != f'allops-lma-ci-{account}-production-{region}':
        raise ValueError('Artifact bucket must be the reviewed account/environment bucket')
    if documents_bucket and documents_bucket != f'allops-lma-documents-{account}-{region}':
        raise ValueError('Document bucket must be the dedicated account/region bucket')
    arn = f'arn:{partition}'
    regional = f'{region}:{account}'
    groups = [
        ('DynamoDB', ['dynamodb:*'], [f'{arn}:dynamodb:{regional}:table/LMA-*']),
        ('Lambda', ['lambda:*'], [f'{arn}:lambda:{regional}:function:LMA-*',
                                f'{arn}:lambda:{regional}:layer:LMA-*:*']),
        ('Kinesis', ['kinesis:*'], [f'{arn}:kinesis:{regional}:stream/LMA-*']),
        ('Logs', ['logs:*'], [f'{arn}:logs:{regional}:log-group:/LMA-*',
                            f'{arn}:logs:{regional}:log-group:/aws/lambda/LMA-*',
                            f'{arn}:logs:{regional}:log-group:/aws/codebuild/LMA-*',
                            f'{arn}:logs:{regional}:log-group:/aws/vendedlogs/states/LMA-*']),
        ('CodeBuild', ['codebuild:StartBuild', 'codebuild:StopBuild', 'codebuild:BatchGetBuilds'],
         [f'{arn}:codebuild:{regional}:project/LMA-*', f'{arn}:codebuild:{regional}:build/LMA-*']),
        ('Secrets', ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
         [f'{arn}:secretsmanager:{regional}:secret:LMA-*']),
        ('Parameters', ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:PutParameter', 'ssm:DeleteParameter'],
         [f'{arn}:ssm:{regional}:parameter/LMA-*', f'{arn}:ssm:{regional}:parameter/LMA/*']),
        ('EcrRepositories', ['ecr:Batch*', 'ecr:*Layer*', 'ecr:PutImage', 'ecr:DescribeImages'],
         [f'{arn}:ecr:{regional}:repository/lma-*']),
    ]
    s3_actions = ['s3:GetObject', 's3:GetObjectVersion', 's3:PutObject', 's3:DeleteObject',
                  's3:DeleteObjectVersion', 's3:ListBucket', 's3:GetBucketLocation',
                  's3:AbortMultipartUpload', 's3:ListMultipartUploadParts',
                  's3:ListBucketMultipartUploads', 's3:DeleteBucket']
    # The trailing ARN wildcard also matches object paths beneath these buckets.
    owned_s3 = [f'{arn}:s3:::lma-*']
    if documents_bucket:
        owned_s3.extend([f'{arn}:s3:::{documents_bucket}', f'{arn}:s3:::{documents_bucket}/*'])
    artifact_resources = [f'{arn}:s3:::{artifact_bucket}',
                          f'{arn}:s3:::{artifact_bucket}/releases/*']
    read_artifacts = ['s3:GetObject', 's3:GetObjectVersion', 's3:ListBucket', 's3:GetBucketLocation']
    global_actions = ['ecr:GetAuthorizationToken', 'xray:PutTraceSegments', 'xray:PutTelemetryRecords']
    approved_actions = sorted(set(s3_actions + global_actions + [a for _, actions, _ in groups for a in actions]))
    statements = [{'Sid': 'DenyUnreviewedActions', 'Effect': 'Deny',
                   'NotAction': approved_actions, 'Resource': '*'}]
    named_actions = sorted({action for _, actions, _ in groups for action in actions})
    named_resources = [resource for _, _, resources in groups for resource in resources]
    # Service-qualified ARNs retain separate namespaces; compact the common
    # allow/deny ceiling to stay within IAM's 6,144-character managed-policy cap.
    statements.extend([
        {'Sid': 'AllowNamedResources', 'Effect': 'Allow', 'Action': named_actions, 'Resource': named_resources},
        {'Sid': 'DenyOutsideNamedResources', 'Effect': 'Deny', 'Action': named_actions, 'NotResource': named_resources},
    ])
    statements.extend([
        {'Sid': 'AllowOwnedS3', 'Effect': 'Allow', 'Action': s3_actions, 'Resource': owned_s3,
         'Condition': {'StringEquals': {'aws:ResourceAccount': account}}},
        {'Sid': 'AllowReleaseRead', 'Effect': 'Allow', 'Action': read_artifacts, 'Resource': artifact_resources,
         'Condition': {'StringEquals': {'aws:ResourceAccount': account}}},
        {'Sid': 'DenyS3OutsideLma', 'Effect': 'Deny', 'Action': s3_actions,
         'NotResource': owned_s3 + artifact_resources},
        {'Sid': 'DenyCrossAccountS3', 'Effect': 'Deny', 'Action': s3_actions, 'Resource': '*',
         'Condition': {'StringNotEqualsIfExists': {'aws:ResourceAccount': account}}},
        {'Sid': 'ArtifactsAreReadOnly', 'Effect': 'Deny', 'NotAction': read_artifacts,
         'Resource': [f'{arn}:s3:::{artifact_bucket}', f'{arn}:s3:::{artifact_bucket}/*']},
        {'Sid': 'ProtectCiResources', 'Effect': 'Deny', 'Action': '*', 'Resource': [
            f'{arn}:lambda:{regional}:function:LMA-CICD-*',
            f'{arn}:codebuild:{regional}:project/LMA-CICD-*',
            f'{arn}:codebuild:{regional}:build/LMA-CICD-*',
            f'{arn}:logs:{regional}:log-group:/aws/codebuild/LMA-CICD-*',
        ]},
        # These actions do not support resource-level scoping. Actual identity
        # grants still select which application roles receive them.
        {'Sid': 'AllowUnscopableTelemetryAndEcrAuth', 'Effect': 'Allow',
         'Action': global_actions, 'Resource': '*'},
    ])
    policy = {'Version': '2012-10-17', 'Statement': statements}
    if len(json.dumps(policy, separators=(',', ':'))) > 6144:
        # Statement IDs are diagnostic labels, not authorization semantics.
        # Longer partition/region names may require omitting them, never rules.
        for statement in statements:
            statement.pop('Sid', None)
    if len(json.dumps(policy, separators=(',', ':'))) > 6144:
        raise ValueError('Boundary exceeds the IAM managed-policy size limit')
    return policy


def simulation_cases(account, region, bucket):
    """Representative allowed application access and forbidden unrelated access."""
    base = f'arn:aws:{{service}}:{region}:{account}:'
    return [
        ('lma-table-write', 'dynamodb:PutItem', base.format(service='dynamodb') + 'table/LMA-Ai-Events-test', {}, 'allowed'),
        ('unrelated-table-write', 'dynamodb:PutItem', base.format(service='dynamodb') + 'table/Payroll', {}, 'explicitDeny'),
        ('lma-lambda-invoke', 'lambda:InvokeFunction', base.format(service='lambda') + 'function:LMA-Ai-Worker', {}, 'allowed'),
        ('unrelated-lambda-edit', 'lambda:UpdateFunctionCode', base.format(service='lambda') + 'function:BillingWorker', {}, 'explicitDeny'),
        ('ci-lambda-edit', 'lambda:UpdateFunctionCode', base.format(service='lambda') + 'function:LMA-CICD-Worker', {}, 'explicitDeny'),
        ('lma-build', 'codebuild:StartBuild', base.format(service='codebuild') + 'project/LMA-Ai-Builder', {}, 'allowed'),
        ('real-ci-build', 'codebuild:StartBuild', base.format(service='codebuild') + 'project/lma-ci-production', {}, 'explicitDeny'),
        ('ci-prefixed-build', 'codebuild:StartBuild', base.format(service='codebuild') + 'project/LMA-CICD-Builder', {}, 'explicitDeny'),
        ('unrelated-secret-read', 'secretsmanager:GetSecretValue', base.format(service='secretsmanager') + 'secret:Payroll-secret', {}, 'explicitDeny'),
        ('own-recording-write', 's3:PutObject', 'arn:aws:s3:::lma-recordings-test/audio.wav', {'aws:ResourceAccount': account}, 'allowed'),
        ('other-bucket-write', 's3:PutObject', 'arn:aws:s3:::company-finance/report.csv', {'aws:ResourceAccount': account}, 'explicitDeny'),
        ('cross-account-lma-bucket', 's3:GetObject', 'arn:aws:s3:::lma-external-test/audio.wav', {'aws:ResourceAccount': '111111111111'}, 'explicitDeny'),
        ('artifact-read', 's3:GetObject', f'arn:aws:s3:::{bucket}/releases/test/code.zip', {'aws:ResourceAccount': account}, 'allowed'),
        ('artifact-overwrite', 's3:PutObject', f'arn:aws:s3:::{bucket}/releases/test/code.zip', {'aws:ResourceAccount': account}, 'explicitDeny'),
        ('config-secret-read', 'secretsmanager:GetSecretValue', base.format(service='secretsmanager') + 'secret:lma/ci/production/parameters-test', {}, 'explicitDeny'),
        ('iam-escalation', 'iam:CreateRole', f'arn:aws:iam::{account}:role/LMA-Escalation', {}, 'explicitDeny'),
        ('assume-admin', 'sts:AssumeRole', f'arn:aws:iam::{account}:role/Admin', {}, 'explicitDeny'),
        ('unreviewed-kms', 'kms:Decrypt', base.format(service='kms') + 'key/test', {}, 'explicitDeny'),
        ('unreviewed-ec2', 'ec2:RunInstances', '*', {}, 'explicitDeny'),
        ('ecr-auth', 'ecr:GetAuthorizationToken', '*', {}, 'allowed'),
    ]


def simulate(client, policy, account, region, bucket):
    """Only call IAM's read-only simulator; never attach the hypothetical Allow."""
    permissive = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]}
    for name, action, resource, context, expected in simulation_cases(account, region, bucket):
        result = client.simulate_custom_policy(
            PolicyInputList=[json.dumps(permissive), json.dumps(policy)], ActionNames=[action],
            ResourceArns=[resource], ContextEntries=[{'ContextKeyName': key, 'ContextKeyValues': [value],
                                                     'ContextKeyType': 'string'} for key, value in context.items()],
        )['EvaluationResults'][0]['EvalDecision']
        if result != expected:
            raise RuntimeError(f'{name}: expected {expected}, got {result}')
        print(f'PASS {name}: {result}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--artifact-bucket', required=True)
    parser.add_argument('--documents-bucket', default='')
    parser.add_argument('--simulate', action='store_true')
    args = parser.parse_args()
    policy = build_policy(args.account, args.region, args.artifact_bucket, args.documents_bucket)
    if not args.simulate:
        print(json.dumps(policy, indent=2))
        return
    session = boto3.Session(profile_name='default', region_name=args.region)
    if session.client('sts').get_caller_identity()['Account'] != args.account:
        raise ValueError('Authenticated account differs from simulation target')
    simulate(session.client('iam'), policy, args.account, args.region, args.artifact_bucket)


if __name__ == '__main__':
    main()
