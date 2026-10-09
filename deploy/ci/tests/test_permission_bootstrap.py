"""Synthetic first-CREATE controller tests; no AWS resources or IAM writes."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock
from urllib.parse import quote

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import permission_bootstrap as bootstrap
from model_resources import PROFILES
from runtime_boundary import build_policy

ACCOUNT = '009853297978'
REGION = 'us-east-1'
ROOT = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/LMA/root-id'
BOUNDARY = f'arn:aws:iam::{ACCOUNT}:policy/lma/isolation/LMA-ApplicationBoundary'
BUCKET = f'allops-lma-ci-{ACCOUNT}-production-{REGION}'
KEY = f'arn:aws:kms:{REGION}:{ACCOUNT}:key/12345678-1234-1234-1234-123456789abc'


class PermissionBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.cfn, self.iam, self.bedrock, self.analyzer = [MagicMock() for _ in range(4)]
        self.base = build_policy(ACCOUNT, REGION, BUCKET)
        self.config = {'account': ACCOUNT, 'region': REGION, 'partition': 'aws',
                       'rootStackArn': ROOT, 'boundaryArn': BOUNDARY,
                       'artifactBucket': BUCKET, 'documentsBucket': '',
                       'expectedPolicySha256': bootstrap.digest(self.base)}
        self.cfn.describe_stacks.return_value = {'Stacks': [{'StackId': ROOT, 'StackStatus': 'CREATE_IN_PROGRESS'}]}
        self.paginators = {name: MagicMock() for name in ['describe_stack_events', 'list_stack_resources']}
        self.cfn.get_paginator.side_effect = lambda name: self.paginators[name]
        self.paginators['describe_stack_events'].paginate.return_value = [{'StackEvents': [
            {'StackId': ROOT, 'ResourceType': 'AWS::CloudFormation::Stack',
             'PhysicalResourceId': ROOT, 'ResourceStatus': 'CREATE_IN_PROGRESS'}]}]
        self.paginators['list_stack_resources'].paginate.return_value = [{'StackResourceSummaries': [
            {'ResourceType': 'AWS::KMS::Key', 'PhysicalResourceId': KEY, 'ResourceStatus': 'CREATE_COMPLETE'}]}]
        self.bedrock.get_inference_profile.side_effect = lambda inferenceProfileIdentifier: {
            'inferenceProfileArn': f'arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{inferenceProfileIdentifier}',
            'status': 'ACTIVE', 'models': [{'modelArn': f'arn:aws:bedrock:{REGION}::foundation-model/' +
                                          inferenceProfileIdentifier.removeprefix('global.')}],
        }
        self.iam.get_policy.return_value = {'Policy': {'Arn': BOUNDARY, 'Path': '/lma/isolation/',
                                                     'DefaultVersionId': 'v1'}}
        self.set_current(self.base)
        self.iam.list_policy_versions.return_value = {'Versions': [{'VersionId': 'v1', 'IsDefaultVersion': True}]}
        self.iam.create_policy_version.return_value = {'PolicyVersion': {'VersionId': 'v2'}}
        self.analyzer.validate_policy.return_value = {'findings': []}

    def set_current(self, document):
        self.iam.get_policy_version.return_value = {'PolicyVersion': {
            'VersionId': 'v1', 'IsDefaultVersion': True, 'Document': document}}

    def run_controller(self, **kwargs):
        return bootstrap.reconcile(self.cfn, self.iam, self.bedrock, self.analyzer, self.config, **kwargs)

    def test_default_mode_is_read_only_and_never_claims_ready(self):
        result = self.run_controller()
        self.assertFalse(result['changed'])
        self.assertFalse(result['deploymentReady'])
        self.assertFalse(result['inventoryComplete'])
        self.assertEqual({call[0] for call in self.iam.method_calls}, {'get_policy', 'get_policy_version'})

    def test_trusted_write_targets_only_pinned_policy_and_repeated_run_noops(self):
        result = self.run_controller(write_enabled=True)
        self.assertTrue(result['changed'])
        params = self.iam.create_policy_version.call_args.kwargs
        self.assertEqual(params['PolicyArn'], BOUNDARY)
        self.assertTrue(params['SetAsDefault'])
        self.set_current(json.loads(params['PolicyDocument']))
        self.assertFalse(self.run_controller(write_enabled=True)['changed'])
        self.assertEqual(self.iam.create_policy_version.call_count, 1)
        self.iam.delete_policy_version.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()

    def test_prior_subset_can_grow_but_tampered_guard_cannot(self):
        self.run_controller(write_enabled=True)
        prior = json.loads(self.iam.create_policy_version.call_args.kwargs['PolicyDocument'])
        self.set_current(prior)
        page = self.paginators['list_stack_resources'].paginate.return_value[0]
        page['StackResourceSummaries'].append({'ResourceType': 'AWS::KMS::Key',
            'PhysicalResourceId': KEY.replace('123456789abc', '123456789def'), 'ResourceStatus': 'CREATE_COMPLETE'})
        self.assertTrue(self.run_controller(write_enabled=True)['changed'])
        prior['Statement'].append({'Effect': 'Allow', 'Action': '*', 'Resource': '*'})
        self.set_current(prior)
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.run_controller(write_enabled=True)
        self.assertEqual(self.iam.create_policy_version.call_count, 2)

    def test_foreign_or_caller_broadened_configuration_is_rejected_before_calls(self):
        for key, value in [('account', '111111111111'), ('rootStackArn', ROOT + '*'),
                           ('boundaryArn', BOUNDARY.replace('/lma/isolation/', '/lma/application/')),
                           ('artifactBucket', 'finance-bucket'), ('expectedPolicySha256', '*')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                bootstrap.reconcile(self.cfn, self.iam, self.bedrock, self.analyzer,
                                    dict(self.config, **{key: value}), write_enabled=True)
        self.cfn.describe_stacks.assert_not_called()
        self.iam.create_policy_version.assert_not_called()

    def test_historical_import_update_failure_and_rollback_all_fail_closed(self):
        for status in ['IMPORT_COMPLETE', 'UPDATE_COMPLETE', 'CREATE_FAILED', 'ROLLBACK_IN_PROGRESS']:
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.paginators['describe_stack_events'].paginate.return_value = [
                    {'StackEvents': [{'StackId': ROOT, 'PhysicalResourceId': ROOT,
                                     'ResourceStatus': 'CREATE_IN_PROGRESS'}]},
                    {'StackEvents': [{'StackId': ROOT, 'ResourceStatus': status}]},
                ]
                self.run_controller(write_enabled=True)
        self.iam.create_policy_version.assert_not_called()

    def test_incomplete_history_foreign_event_or_finished_root_is_not_approved(self):
        for events in [[], [{'StackId': ROOT.replace(ACCOUNT, '111111111111'),
                            'ResourceStatus': 'CREATE_IN_PROGRESS'}]]:
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.paginators['describe_stack_events'].paginate.return_value = [{'StackEvents': events}]
                self.run_controller(write_enabled=True)
        self.cfn.describe_stacks.return_value['Stacks'][0]['StackStatus'] = 'CREATE_COMPLETE'
        with self.assertRaises(ValueError):
            self.run_controller(write_enabled=True)
        self.iam.create_policy_version.assert_not_called()

    def test_nested_history_verifies_parent_and_every_child_page(self):
        child = ROOT.replace('stack/LMA/root-id', 'stack/LMA-child/child-id')
        root_state = {'StackId': ROOT, 'StackStatus': 'CREATE_IN_PROGRESS'}
        child_state = {'StackId': child, 'StackStatus': 'CREATE_COMPLETE', 'ParentId': ROOT, 'RootId': ROOT}
        self.cfn.describe_stacks.side_effect = lambda StackName: {'Stacks': [
            root_state if StackName == ROOT else child_state]}
        root_events = [{'StackEvents': [
            {'StackId': ROOT, 'PhysicalResourceId': ROOT, 'ResourceStatus': 'CREATE_IN_PROGRESS'},
            {'StackId': ROOT, 'ResourceType': 'AWS::CloudFormation::Stack',
             'PhysicalResourceId': child, 'ResourceStatus': 'CREATE_COMPLETE'}]}]
        child_events = [{'StackEvents': [
            {'StackId': child, 'PhysicalResourceId': child, 'ResourceStatus': 'CREATE_IN_PROGRESS'}]}]
        self.paginators['describe_stack_events'].paginate.side_effect = lambda StackName: (
            root_events if StackName == ROOT else child_events)
        bootstrap.verify_creation_history(self.cfn, ROOT, ACCOUNT, REGION, 'aws')
        child_events.append({'StackEvents': [{'StackId': child, 'ResourceStatus': 'IMPORT_COMPLETE'}]})
        with self.assertRaisesRegex(ValueError, 'Import'):
            bootstrap.verify_creation_history(self.cfn, ROOT, ACCOUNT, REGION, 'aws')
        child_events.pop()
        child_state['ParentId'] = ROOT + '-foreign'
        with self.assertRaisesRegex(ValueError, 'parent/root'):
            bootstrap.verify_creation_history(self.cfn, ROOT, ACCOUNT, REGION, 'aws')

    def test_inventory_race_before_write_stops_reconciliation(self):
        original = self.paginators['list_stack_resources'].paginate.return_value
        changed = [{'StackResourceSummaries': [*original[0]['StackResourceSummaries'],
            {'ResourceType': 'AWS::KMS::Key', 'PhysicalResourceId': KEY.replace('123456789abc', '123456789def'),
             'ResourceStatus': 'CREATE_COMPLETE'}]}]
        self.paginators['list_stack_resources'].paginate.side_effect = [original, changed]
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            self.run_controller(write_enabled=True)
        self.iam.create_policy_version.assert_not_called()

    def test_version_quota_never_deletes_prior_versions(self):
        self.iam.list_policy_versions.return_value = {'Versions': [{'VersionId': f'v{i}'} for i in range(1, 6)]}
        with self.assertRaisesRegex(ValueError, 'quota'):
            self.run_controller(write_enabled=True)
        self.iam.create_policy_version.assert_not_called()
        self.iam.delete_policy_version.assert_not_called()

    def test_validator_findings_or_pagination_stop_before_iam_writes(self):
        for response in [{'findings': [{'findingType': 'ERROR'}]}, {'findings': [], 'nextToken': 'more'}, {}]:
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, 'validation'):
                self.analyzer.validate_policy.return_value = response
                self.run_controller(write_enabled=True)
        self.iam.create_policy_version.assert_not_called()

    def test_unexpected_default_policy_and_prewrite_version_race_reject(self):
        self.set_current({'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]})
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.run_controller(write_enabled=True)
        self.set_current(self.base)
        original = self.iam.get_policy.return_value
        self.iam.get_policy.side_effect = [original, {'Policy': dict(original['Policy'], DefaultVersionId='v2')}]
        self.iam.get_policy_version.side_effect = [self.iam.get_policy_version.return_value,
            {'PolicyVersion': {'VersionId': 'v2', 'IsDefaultVersion': True, 'Document': self.base}}]
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.run_controller(write_enabled=True)
        self.iam.create_policy_version.assert_not_called()

    def test_url_encoded_default_policy_is_supported(self):
        self.set_current(quote(json.dumps(self.base), safe=''))
        self.assertTrue(self.run_controller(write_enabled=True)['changed'])

    def test_large_realistic_scope_fails_without_dropping_resources(self):
        self.paginators['list_stack_resources'].paginate.return_value = [{'StackResourceSummaries': [
            {'ResourceType': 'AWS::KMS::Key', 'PhysicalResourceId': KEY[:-12] + f'{i:012x}',
             'ResourceStatus': 'CREATE_COMPLETE'} for i in range(100)]}]
        with self.assertRaisesRegex(ValueError, 'size limit'):
            self.run_controller(write_enabled=True)
        self.iam.create_policy_version.assert_not_called()

    def test_template_is_off_and_only_admin_pinned_policy_can_be_written(self):
        path = Path(__file__).resolve().parents[1] / 'permission-bootstrap.yaml'
        template = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(template['Parameters']['EnableController']['Default'], 'false')
        self.assertEqual(template['Parameters']['EnablePolicyWrites']['Default'], 'false')
        self.assertTrue(all(value['Condition'] == 'Enabled' for value in template['Resources'].values()))
        controller = template['Resources']['Controller']['Properties']
        self.assertEqual(controller['ReservedConcurrentExecutions'], '1')
        self.assertIn('S3ObjectVersion', controller['Code'])
        self.assertFalse(any(value['Type'] == 'AWS::Lambda::Permission' for value in template['Resources'].values()))
        policy = template['Resources']['ControllerRole']['Properties']['Policies'][0]['PolicyDocument']
        self.assertNotIn('iam:DeletePolicyVersion', json.dumps(policy))
        self.assertNotIn('iam:PassRole', json.dumps(policy))


if __name__ == '__main__':
    unittest.main()
