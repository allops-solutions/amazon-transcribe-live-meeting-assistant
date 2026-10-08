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


def build_policy(account, region, artifact_bucket, documents_bucket='', partition='aws', generated_resources=None):
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
        ('PassApplicationRoles', ['iam:PassRole'], [f'{arn}:iam::{account}:role/lma/application/LMA-*']),
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
    generated_resources = {} if generated_resources is None else generated_resources
    supported = {'kms', 'appsync', 'cognito', 'cloudfront', 'ecs_clusters', 'ecs_task_definitions'}
    if not isinstance(generated_resources, dict) or set(generated_resources) - supported:
        raise ValueError('Unsupported generated resource group')
    for service, values in generated_resources.items():
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError('Generated resources must be lists of exact ARNs')
        patterns = {
            'kms': ('kms', r'key/[0-9a-fA-F-]{36}'),
            'appsync': ('appsync', r'apis/[A-Za-z0-9]{10,40}'),
            'cognito': ('cognito-idp', rf'userpool/{re.escape(region)}_[A-Za-z0-9]+'),
            'cloudfront': ('cloudfront', r'distribution/[A-Z0-9]{10,20}'),
            'ecs_clusters': ('ecs', r'cluster/[A-Za-z0-9_-]{1,255}'),
            'ecs_task_definitions': ('ecs', r'task-definition/[A-Za-z0-9_-]{1,255}:[1-9][0-9]*'),
        }
        arn_service, resource = patterns[service]
        arn_region = '' if service == 'cloudfront' else region
        pattern = rf'arn:{re.escape(partition)}:{arn_service}:{re.escape(arn_region)}:{account}:{resource}'
        if any(not re.fullmatch(pattern, value) for value in values):
            raise ValueError('Generated resource scope must be exact and account/region-local')
        if not values:
            continue
        if service == 'kms':
            groups.append(('KmsData', ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*',
                                      'kms:GenerateDataKey*', 'kms:DescribeKey'], sorted(set(values))))
        elif service == 'appsync':
            groups.append(('AppSyncData', ['appsync:GraphQL', 'appsync:EventConnect',
                                           'appsync:EventPublish', 'appsync:EventSubscribe'],
                           [resource for value in sorted(set(values)) for resource in [value, value + '/*']]))
        elif service == 'cognito':
            groups.append(('CognitoUsers', ['cognito-idp:AdminGetUser', 'cognito-idp:AdminListGroupsForUser',
                                           'cognito-idp:ListUsers', 'cognito-idp:ListUsersInGroup'], values))
        elif service == 'cloudfront':
            groups.append(('CloudFrontInvalidation', ['cloudfront:CreateInvalidation',
                                                       'cloudfront:GetInvalidation'], values))
        elif service == 'ecs_clusters':
            tasks = [value.replace(':cluster/', ':task/') + '/*' for value in values]
            groups.append(('EcsClusters', ['ecs:DescribeClusters'], values))
            groups.append(('EcsTasks', ['ecs:StopTask', 'ecs:DescribeTasks', 'ecs:TagResource'], tasks))
        elif generated_resources.get('ecs_clusters'):
            groups.append(('EcsDefinitions', ['ecs:RunTask'], values))
        else:
            # DescribeTaskDefinition has no resource-level or definition-ID
            # condition scope. Do not enable account-wide metadata reads here.
            # A definition alone also cannot authorize launches into any cluster.
            groups.append(('EcsDefinitionsNoLaunch', [], values))
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
    # ListTasks is authorized through ecs:cluster context, not a cluster resource
    # ARN. Keep it outside named_actions and apply mandatory cluster Denies.
    conditional_actions = ['ecs:ListTasks'] if generated_resources.get('ecs_clusters') else []
    approved_actions = sorted(set(s3_actions + global_actions + conditional_actions +
                                  [a for _, actions, _ in groups for a in actions]))
    statements = [{'Sid': 'DenyUnreviewedActions', 'Effect': 'Deny',
                   'NotAction': approved_actions, 'Resource': '*'}]
    named_actions = sorted({action for _, actions, _ in groups for action in actions})
    named_resources = [resource for _, _, resources in groups for resource in resources]
    # Service-qualified ARNs retain separate namespaces; compact the common
    # allow/deny ceiling to stay within IAM's 6,144-character managed-policy cap.
    statements.extend([
        # DenyUnreviewedActions is exhaustive: actions not in its reviewed list
        # are explicitly forbidden. Repeating that long list here adds no
        # protection. Every listed non-IAM action also has a scope Deny below
        # or is an explicitly unscopable auth/telemetry operation. IAM stays
        # excluded from this ceiling and receives only scoped PassRole.
        {'Sid': 'AllowNonIamWithinExplicitDenies', 'Effect': 'Allow',
         'NotAction': 'iam:*', 'Resource': '*'},
        {'Sid': 'AllowPassApplicationRoles', 'Effect': 'Allow', 'Action': 'iam:PassRole',
         'Resource': f'{arn}:iam::{account}:role/lma/application/LMA-*'},
        {'Sid': 'DenyOutsideNamedResources', 'Effect': 'Deny', 'Action': named_actions, 'NotResource': named_resources},
    ])
    statements.extend([
        # The ceiling above already supplies the Allow. These explicit
        # Denies enforce both ARN and account constraints, including sessions.
        {'Sid': 'DenyS3OutsideLma', 'Effect': 'Deny', 'Action': s3_actions,
         'NotResource': owned_s3 + artifact_resources},
        {'Sid': 'DenyCrossAccountS3', 'Effect': 'Deny', 'Action': s3_actions, 'Resource': '*',
         'Condition': {'StringNotEqualsIfExists': {'aws:ResourceAccount': account}}},
        {'Sid': 'DenyMissingS3Owner', 'Effect': 'Deny', 'Action': s3_actions, 'Resource': '*',
         'Condition': {'Null': {'aws:ResourceAccount': 'true'}}},
        {'Sid': 'ArtifactsAreReadOnly', 'Effect': 'Deny', 'NotAction': read_artifacts,
         'Resource': [f'{arn}:s3:::{artifact_bucket}', f'{arn}:s3:::{artifact_bucket}/*']},
        {'Sid': 'ProtectCiResources', 'Effect': 'Deny', 'Action': '*', 'Resource': [
            f'{arn}:iam::{account}:role/lma/application/LMA-CICD-*',
            f'{arn}:lambda:{regional}:function:LMA-CICD-*',
            f'{arn}:codebuild:{regional}:project/LMA-CICD-*',
            f'{arn}:codebuild:{regional}:build/LMA-CICD-*',
            f'{arn}:logs:{regional}:log-group:/aws/codebuild/LMA-CICD-*',
        ]},
    ])
    if generated_resources.get('ecs_clusters'):
        cluster_actions = conditional_actions + (['ecs:RunTask'] if generated_resources.get('ecs_task_definitions') else [])
        statements.append({'Sid': 'DenyEcsLaunchOutsideReviewedClusters', 'Effect': 'Deny',
                           'Action': cluster_actions, 'Resource': '*',
                           'Condition': {'ArnNotEqualsIfExists': {
                               'ecs:cluster': generated_resources['ecs_clusters']}}})
        statements.append({'Sid': 'DenyMissingEcsLaunchCluster', 'Effect': 'Deny',
                           'Action': cluster_actions, 'Resource': '*',
                           'Condition': {'Null': {'ecs:cluster': 'true'}}})
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
        # The simulator supplies the caller as default ResourceOwner for S3;
        # absent ContextEntries does not simulate an absent resource owner.
        ('default-simulated-s3-owner', 's3:GetObject', 'arn:aws:s3:::lma-recordings-test/audio.wav', {}, 'allowed'),
        ('artifact-read', 's3:GetObject', f'arn:aws:s3:::{bucket}/releases/test/code.zip', {'aws:ResourceAccount': account}, 'allowed'),
        ('artifact-overwrite', 's3:PutObject', f'arn:aws:s3:::{bucket}/releases/test/code.zip', {'aws:ResourceAccount': account}, 'explicitDeny'),
        ('artifact-outside-release', 's3:GetObject', f'arn:aws:s3:::{bucket}/build-validation/test/code.zip', {'aws:ResourceAccount': account}, 'explicitDeny'),
        ('config-secret-read', 'secretsmanager:GetSecretValue', base.format(service='secretsmanager') + 'secret:lma/ci/production/parameters-test', {}, 'explicitDeny'),
        ('iam-escalation', 'iam:CreateRole', f'arn:aws:iam::{account}:role/LMA-Escalation', {}, 'explicitDeny'),
        ('pass-bounded-namespace', 'iam:PassRole', f'arn:aws:iam::{account}:role/lma/application/LMA-Worker', {}, 'allowed'),
        ('pass-legacy-role', 'iam:PassRole', f'arn:aws:iam::{account}:role/LMA-LegacyAdmin', {}, 'explicitDeny'),
        ('pass-ci-path-role', 'iam:PassRole', f'arn:aws:iam::{account}:role/lma/application/LMA-CICD-Worker', {}, 'explicitDeny'),
        ('assume-admin', 'sts:AssumeRole', f'arn:aws:iam::{account}:role/Admin', {}, 'explicitDeny'),
        ('unreviewed-kms', 'kms:Decrypt', base.format(service='kms') + 'key/test', {}, 'explicitDeny'),
        ('unreviewed-ec2', 'ec2:RunInstances', '*', {}, 'explicitDeny'),
        ('ecr-auth', 'ecr:GetAuthorizationToken', '*', {}, 'allowed'),
    ]


def simulate(client, policy, account, region, bucket):
    """Only call IAM's read-only simulator; never attach the hypothetical Allow."""
    permissive = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]}
    cases = simulation_cases(account, region, bucket)
    scopes = next(s['NotResource'] for s in policy['Statement'] if 'NotResource' in s and
                  'iam:PassRole' in s.get('Action', []))
    for resource in scopes:
        if ':kms:' in resource:
            cases.extend([('reviewed-key', 'kms:Decrypt', resource, {}, 'allowed'),
                          ('unrelated-key', 'kms:Decrypt', resource.rsplit('/', 1)[0] +
                           '/ffffffff-ffff-ffff-ffff-ffffffffffff', {}, 'explicitDeny')])
        elif ':appsync:' in resource and not resource.endswith('/*'):
            cases.extend([('reviewed-api-field', 'appsync:GraphQL', resource +
                           '/types/Query/fields/test', {}, 'allowed'),
                          ('unrelated-api-field', 'appsync:GraphQL', resource.rsplit('/', 1)[0] +
                           '/zzzzzzzzzzzzzzzz/types/Query/fields/test', {}, 'explicitDeny')])
        elif ':cognito-idp:' in resource:
            cases.extend([('reviewed-user-pool', 'cognito-idp:AdminGetUser', resource, {}, 'allowed'),
                          ('unrelated-user-pool', 'cognito-idp:AdminGetUser', resource.rsplit('_', 1)[0] +
                           '_Unrelated123', {}, 'explicitDeny')])
        elif ':cloudfront:' in resource:
            cases.extend([('reviewed-distribution', 'cloudfront:CreateInvalidation', resource, {}, 'allowed'),
                          ('unrelated-distribution', 'cloudfront:CreateInvalidation', resource.rsplit('/', 1)[0] +
                           '/EZZZZZZZZZZZZZ', {}, 'explicitDeny')])
        elif ':ecs:' in resource and ':cluster/' in resource:
            cases.extend([('reviewed-cluster', 'ecs:ListTasks', '*', {'ecs:cluster': resource}, 'allowed'),
                          ('unrelated-cluster', 'ecs:ListTasks', '*', {'ecs:cluster': resource.rsplit('/', 1)[0] +
                           '/UnrelatedCluster'}, 'explicitDeny'),
                          ('missing-cluster-list', 'ecs:ListTasks', '*', {}, 'explicitDeny')])
        elif ':ecs:' in resource and ':task/' in resource:
            cases.extend([('reviewed-cluster-task', 'ecs:StopTask', resource[:-1] + '1234567890abcdef', {}, 'allowed'),
                          ('unrelated-cluster-task', 'ecs:StopTask', resource.split(':task/')[0] +
                           ':task/UnrelatedCluster/1234567890abcdef', {}, 'explicitDeny')])
        elif ':ecs:' in resource and ':task-definition/' in resource:
            clusters = [value for value in scopes if ':ecs:' in value and ':cluster/' in value]
            if not clusters:
                cases.append(('no-launch-cluster', 'ecs:RunTask', resource, {}, 'explicitDeny'))
                continue
            cases.extend([('reviewed-launch', 'ecs:RunTask', resource, {'ecs:cluster': clusters[0]}, 'allowed'),
                          ('foreign-cluster-launch', 'ecs:RunTask', resource,
                           {'ecs:cluster': clusters[0].rsplit('/', 1)[0] + '/UnrelatedCluster'}, 'explicitDeny'),
                          ('missing-cluster-launch', 'ecs:RunTask', resource, {}, 'explicitDeny'),
                          ('unreviewed-definition-launch', 'ecs:RunTask', resource.rsplit('/', 1)[0] +
                           '/UnrelatedTask:1', {'ecs:cluster': clusters[0]}, 'explicitDeny')])
    for name, action, resource, context, expected in cases:
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
    parser.add_argument('--include-generated-resources', action='store_true',
                        help='Read exact resource IDs from the stable, verified LMA stack; no IAM writes')
    args = parser.parse_args()
    session = None
    generated = None
    if args.simulate or args.include_generated_resources:
        session = boto3.Session(profile_name='default', region_name=args.region)
        if session.client('sts').get_caller_identity()['Account'] != args.account:
            raise ValueError('Authenticated account differs from target')
    if args.include_generated_resources:
        from generated_resources import collect
        generated = collect(session.client('cloudformation'), args.account, args.region)['resources']
    policy = build_policy(args.account, args.region, args.artifact_bucket, args.documents_bucket,
                          generated_resources=generated)
    if not args.simulate:
        print(json.dumps(policy, indent=2))
        return
    simulate(session.client('iam'), policy, args.account, args.region, args.artifact_bucket)


if __name__ == '__main__':
    main()
