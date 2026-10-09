#!/usr/bin/env python3
"""Administrator-owned first-CREATE boundary reconciler; inactive draft.

Package this code and its reviewed imports in an administrator-owned immutable
artifact, never load builders from application S3 releases or accept policies
in invocation payloads. This engine does not make the deployment ready.
"""
import hashlib
import json
import re
from urllib.parse import unquote

from generated_resources import collect
from model_resources import collect as collect_models
from runtime_boundary import build_policy


def canonical(document):
    """Normalize an SDK-decoded or URL-encoded IAM policy without logging it."""
    if isinstance(document, str):
        document = json.loads(unquote(document))
    if not isinstance(document, dict):
        raise ValueError('Invalid policy document')
    return json.dumps(document, sort_keys=True, separators=(',', ':'))


def digest(document):
    """Return a policy fingerprint for administrator review, not secret content."""
    return hashlib.sha256(canonical(document).encode()).hexdigest()


def validate_config(config):
    """Pins come from administrator-controlled code/configuration, not events."""
    keys = {'account', 'region', 'partition', 'rootStackArn', 'boundaryArn',
            'artifactBucket', 'documentsBucket', 'expectedPolicySha256'}
    if not isinstance(config, dict) or set(config) != keys:
        raise ValueError('Expected the complete administrator bootstrap configuration')
    if not all(isinstance(value, str) for value in config.values()):
        raise ValueError('Bootstrap pins must be strings')
    account, region, partition = config['account'], config['region'], config['partition']
    if not re.fullmatch(r'[0-9]{12}', account) or not re.fullmatch(
        r'[a-z]{2}(?:-gov)?-[a-z]+-[0-9]', region
    ) or partition not in {'aws', 'aws-cn', 'aws-us-gov'}:
        raise ValueError('Invalid bootstrap account, region or partition')
    prefix = f'arn:{partition}:cloudformation:{region}:{account}:stack/LMA/'
    if not re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9-]+', config['rootStackArn']):
        raise ValueError('Bootstrap requires an exact administrator-pinned LMA root')
    if not re.fullmatch(
        rf'arn:{partition}:iam::{account}:policy/lma/isolation/LMA-[A-Za-z0-9+=,.@_-]+',
        config['boundaryArn'],
    ):
        raise ValueError('Bootstrap can update only an exact administrator isolation policy')
    if not re.fullmatch(r'[0-9a-f]{64}', config['expectedPolicySha256']):
        raise ValueError('Initial policy fingerprint must be administrator-pinned')
    # Also validate the fixed bucket namespaces before any AWS call.
    build_policy(account, region, config['artifactBucket'], config['documentsBucket'], partition)


def verify_creation_history(cfn, root, account, region, partition):
    """Reject adoption/import/update histories, including hidden earlier pages.

First-CREATE is the only supported lifecycle. Even an import followed by an
apparently normal CREATE record cannot become an automatically approved scope.
This is not a general replacement for administrator origin review.
"""
    seen = set()
    prefix = f'arn:{partition}:cloudformation:{region}:{account}:stack/'

    def walk(arn, parent=None):
        if arn in seen or not arn.startswith(prefix):
            raise ValueError('Foreign or repeated creation history')
        seen.add(arn)
        stacks = cfn.describe_stacks(StackName=arn)['Stacks']
        if len(stacks) != 1 or stacks[0]['StackId'] != arn:
            raise ValueError('Creation history changed stack identity')
        stack = stacks[0]
        if parent and (stack.get('ParentId') != parent or stack.get('RootId') != root):
            raise ValueError('Creation history has a foreign parent/root')
        if not parent and stack.get('ParentId'):
            raise ValueError('Pinned root cannot be nested')
        if stack['StackStatus'] not in {'CREATE_IN_PROGRESS', 'CREATE_COMPLETE'}:
            raise ValueError('Only a new CREATE lifecycle can be reconciled')
        if not parent and stack['StackStatus'] != 'CREATE_IN_PROGRESS':
            raise ValueError('First-CREATE controller stops when root creation ends')
        found_start = False
        children = set()
        for page in cfn.get_paginator('describe_stack_events').paginate(StackName=arn):
            for event in page['StackEvents']:
                if event.get('StackId') != arn:
                    raise ValueError('Foreign event in creation history')
                status = event.get('ResourceStatus', '')
                if status not in {'CREATE_IN_PROGRESS', 'CREATE_COMPLETE', 'REVIEW_IN_PROGRESS'}:
                    raise ValueError('Import, update, failure or rollback requires administrator review')
                if event.get('PhysicalResourceId') == arn and status == 'CREATE_IN_PROGRESS':
                    found_start = True
                if event.get('ResourceType') == 'AWS::CloudFormation::Stack':
                    physical = event.get('PhysicalResourceId', '')
                    if physical.startswith('arn:') and physical != arn:
                        children.add(physical)
        if not found_start:
            raise ValueError('Complete initial creation history could not be established')
        for child in sorted(children):
            walk(child, arn)

    walk(root)


def current_policy(iam, arn):
    """Read the default version and decoded document, never accept caller data."""
    policy = iam.get_policy(PolicyArn=arn)['Policy']
    if policy['Arn'] != arn or policy.get('Path') != '/lma/isolation/':
        raise ValueError('Target policy changed identity/path')
    version = policy['DefaultVersionId']
    document = iam.get_policy_version(PolicyArn=arn, VersionId=version)['PolicyVersion']
    if document['VersionId'] != version or not document.get('IsDefaultVersion'):
        raise ValueError('Default policy version changed during read')
    return version, document['Document']


def verified_previous_document(existing, inventory, models, config):
    """Recognize only an internally generated prior subset of this same root.

This permits successive first-CREATE polls without accepting an arbitrary IAM
document. Rebuilding the entire policy also checks every Allow, Deny, action,
condition and protected resource; comparing only a few guard statements would
leave room for a malicious extra Allow or weakened Deny.
"""
    document = json.loads(canonical(existing))
    scopes = next((statement['NotResource'] for statement in document.get('Statement', [])
                   if 'NotResource' in statement and 'NotAction' in statement), None)
    if not isinstance(scopes, list) or not all(isinstance(value, str) for value in scopes):
        return False
    previous = {}
    for group, values in inventory['resources'].items():
        previous[group] = [value for value in values if value in scopes or (
            group == 'schedule_groups' and value.replace(':schedule-group/', ':schedule/') + '/*' in scopes)]
    previous_models = {key: [value for value in values if value in scopes]
                       for key, values in models.items()}
    rebuilt = build_policy(config['account'], config['region'], config['artifactBucket'],
                           config['documentsBucket'], config['partition'],
                           generated_resources=previous, model_resources=previous_models)
    return digest(rebuilt) == digest(existing)


def reconcile(cfn, iam, bedrock, analyzer, config, *, write_enabled=False):
    """Compute a scope internally; apply only with trusted writer activation.

One administrator-owned writer (reserved concurrency 1) is required. IAM has no
compare-and-swap API: administrator policy edits must not run concurrently.
Never prune versions automatically, persist caller-provided scopes, attach a
policy, create a role, or fall back to a broader document on failure.
"""
    validate_config(config)
    account, region, partition = config['account'], config['region'], config['partition']
    verify_creation_history(cfn, config['rootStackArn'], account, region, partition)
    inventory = collect(cfn, account, region, partition, creating_root_arn=config['rootStackArn'])
    models = collect_models(bedrock, account, region, partition)
    target = build_policy(account, region, config['artifactBucket'], config['documentsBucket'],
                          partition, generated_resources=inventory['resources'], model_resources=models)
    target_hash = digest(target)
    version, existing = current_policy(iam, config['boundaryArn'])
    existing_hash = digest(existing)
    if existing_hash not in {config['expectedPolicySha256'], target_hash} and not verified_previous_document(
        existing, inventory, models, config
    ):
        raise ValueError('Unexpected policy drift; administrator review required')
    result = {'changed': False, 'writeEnabled': write_enabled, 'policySha256': target_hash,
              'policyVersion': version, 'resourcesPending': inventory['resourcesPending'],
              'inventoryComplete': False, 'deploymentReady': False}
    if existing_hash == target_hash:
        return result
    validation = analyzer.validate_policy(policyDocument=json.dumps(target), policyType='IDENTITY_POLICY')
    if not isinstance(validation.get('findings'), list) or validation.get('nextToken') or validation['findings']:
        raise ValueError('Policy validation requires review; no IAM changes made')
    if not write_enabled:
        return result
    # Reject races/new failed or adopted resources immediately before IAM write.
    verify_creation_history(cfn, config['rootStackArn'], account, region, partition)
    newer = collect(cfn, account, region, partition, creating_root_arn=config['rootStackArn'])
    if newer['resources'] != inventory['resources']:
        raise ValueError('Resource inventory changed; rerun the read-only plan')
    latest_version, latest = current_policy(iam, config['boundaryArn'])
    if latest_version != version or digest(latest) != existing_hash:
        raise ValueError('Default policy drift detected before write')
    versions = iam.list_policy_versions(PolicyArn=config['boundaryArn'])
    if versions.get('IsTruncated') or len(versions['Versions']) >= 5:
        raise ValueError('Policy version quota requires administrator review; nothing deleted')
    written = iam.create_policy_version(PolicyArn=config['boundaryArn'],
                                        PolicyDocument=canonical(target), SetAsDefault=True)['PolicyVersion']
    result.update(changed=True, policyVersion=written['VersionId'])
    return result
