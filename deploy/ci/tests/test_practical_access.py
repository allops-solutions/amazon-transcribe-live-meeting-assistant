"""Conventional deployment-access contracts; no live AWS writes."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock

import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('practical_access', ROOT / 'deploy/ci/practical_access.py')
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)
ACTIVATION_SPEC = importlib.util.spec_from_file_location('activate_practical_access',
    ROOT / 'deploy/ci/activate_practical_access.py')
activation = importlib.util.module_from_spec(ACTIVATION_SPEC)
ACTIVATION_SPEC.loader.exec_module(activation)


class PracticalAccessTests(unittest.TestCase):
    def test_policy_sizes_and_no_admin_grant(self):
        boundary, provisioning, _ = access.policies('009853297978')
        self.assertLess(len(json.dumps(boundary, separators=(',', ':'))), 6144)
        self.assertLess(len(json.dumps(provisioning, separators=(',', ':'))), 10240)
        for policy in (boundary, provisioning):
            for statement in policy['Statement']:
                if statement['Effect'] == 'Allow':
                    self.assertNotEqual(statement['Action'], '*')
                    self.assertNotIn('iam:*', statement['Action'])
                    self.assertNotIn('sts:AssumeRole', statement['Action'])
        iam_grants = [s for s in boundary['Statement'] if s['Effect'] == 'Allow'
                      and 'iam:' in str(s['Action'])]
        self.assertEqual([s['Action'] for s in iam_grants], ['iam:PassRole'])
        self.assertIn('/lma/application/LMA-*', iam_grants[0]['Resource'])

    def test_boundary_pinned_on_role_creation(self):
        _, policy, boundary = access.policies('009853297978')
        required = next(s for s in policy['Statement'] if s['Sid'] == 'RequireApprovedBoundary')
        self.assertEqual(required['Effect'], 'Deny')
        self.assertEqual(required['Condition']['ArnNotEquals']['iam:PermissionsBoundary'], boundary)
        removal = next(s for s in policy['Statement'] if s['Sid'] == 'NeverRemoveBoundaries')
        self.assertEqual(removal['Action'], 'iam:DeleteRolePermissionsBoundary')
        application = next(s for s in policy['Statement'] if s['Sid'] == 'ApplicationIam')
        self.assertTrue(all('/lma/application/LMA-*' in arn for arn in application['Resource']))

    def test_service_linked_exception_is_create_only(self):
        _, policy, _ = access.policies('009853297978')
        linked = next(s for s in policy['Statement'] if s['Sid'] == 'RequiredServiceLinkedRolesOnly')
        self.assertEqual(linked['Action'], 'iam:CreateServiceLinkedRole')
        self.assertEqual(len(linked['Resource']), 3)
        self.assertTrue(all('009853297978:role/aws-service-role/' in arn for arn in linked['Resource']))
        self.assertEqual(len(linked['Condition']['StringEquals']['iam:AWSServiceName']), 3)

    def test_runtime_schedule_names_and_logs_supported(self):
        policy, _, _ = access.policies('009853297978')
        resources = next(s for s in policy['Statement'] if s['Sid'] == 'NamedApplicationServices')['Resource']
        self.assertIn('arn:aws:scheduler:us-east-1:009853297978:schedule/LMA-*/*', resources)
        self.assertIn('arn:aws:logs:us-east-1:009853297978:log-group:/aws/vendedlogs/states/LMA-*', resources)
        self.assertTrue(any('event-source-mapping:*' in arn for arn in resources))

    def test_preflight_is_default_and_skips_all_writes(self):
        workflow = yaml.load((ROOT / '.github/workflows/lma-deploy.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['operation']['default'], 'preflight')
        steps = workflow['jobs']['deploy']['steps']
        for step in steps:
            run = step.get('run', '')
            if 'lma publish' in run or 'deploy.py deploy' in run or 'deploy.py cleanup' in run:
                self.assertIn("inputs.operation == 'deploy'", step['if'])
        self.assertTrue(any('deploy.py preflight' in step.get('run', '') for step in steps))

    def test_template_only_creates_administrator_owned_iam(self):
        template = yaml.load((ROOT / 'deploy/ci/practical-access.yaml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual({r['Type'] for r in template['Resources'].values()},
                         {'AWS::IAM::ManagedPolicy', 'AWS::IAM::Role'})
        role = template['Resources']['CloudFormationRole']['Properties']
        self.assertEqual(role['Path'], '/lma/isolation/')
        self.assertEqual(role['RoleName'], 'LMA-CloudFormation')
        trust = role['AssumeRolePolicyDocument']['Statement']
        self.assertEqual(len(trust), 1)
        self.assertEqual(trust[0]['Principal']['Service'], 'cloudformation.${AWS::URLSuffix}')

    def test_activation_refuses_application_or_dev_targets(self):
        client = MagicMock()
        for name in ('LMA', 'LMA-Dev', 'OtherApplication'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                activation.stack_or_none(client, name)
        client.describe_stacks.assert_not_called()

    def test_activation_preserves_secret_and_retains_version(self):
        client = MagicMock()
        client.get_secret_value.return_value = {'VersionId': 'original',
            'SecretString': json.dumps({'GoogleOAuthClientSecret': 'test-not-real', 'Other': 'keep'})}
        activation.pin_boundary(client, 'approved-boundary')
        values = json.loads(client.put_secret_value.call_args.kwargs['SecretString'])
        self.assertEqual(values, {'GoogleOAuthClientSecret': 'test-not-real', 'Other': 'keep',
                                  'PermissionsBoundaryArn': 'approved-boundary'})
        self.assertEqual({call[0] for call in client.method_calls},
                         {'get_secret_value', 'put_secret_value'})

    def test_activation_refuses_boundary_change_or_concurrent_secret_edit(self):
        for responses in [
            [{'VersionId': 'one', 'SecretString': '{"PermissionsBoundaryArn":"other"}'}],
            [{'VersionId': 'one', 'SecretString': '{}'}, {'VersionId': 'two', 'SecretString': '{}'}],
        ]:
            client = MagicMock()
            client.get_secret_value.side_effect = responses
            with self.assertRaises(ValueError):
                activation.pin_boundary(client, 'approved')
            client.put_secret_value.assert_not_called()

    def test_unexpected_resource_change_is_never_executed(self):
        client = MagicMock()
        client.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE'}]}
        client.create_change_set.return_value = {'Id': 'test-changeset'}
        client.describe_change_set.return_value = {'Changes': [{'ResourceChange': {
            'LogicalResourceId': 'Unexpected', 'ResourceType': 'AWS::Lambda::Function', 'Action': 'Add'}}]}
        with self.assertRaises(ValueError):
            activation.execute_preparation(client, activation.RUNNER_STACK, 'template', [], {'DeploymentRole'})
        client.execute_change_set.assert_not_called()


if __name__ == '__main__':
    unittest.main()
