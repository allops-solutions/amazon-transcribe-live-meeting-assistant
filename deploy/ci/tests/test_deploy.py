"""Offline deployment safety regression tests; never contact AWS."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('ci_deploy', ROOT / 'deploy/ci/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'GITHUB_REPOSITORY': deploy.REPOSITORY,
            'GITHUB_REF': 'refs/heads/allops-main',
            'GITHUB_EVENT_NAME': 'workflow_dispatch',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_production_context(self):
        deploy.check_context('production', '009853297978', 'LMA', '123')

    def test_dev_context(self):
        deploy.check_context('dev', '135755363077', 'LMA-CI-123', '123')

    def test_wrong_account(self):
        with self.assertRaises(ValueError):
            deploy.check_context('production', '135755363077', 'LMA', '123')

    def test_existing_dev_is_never_a_target(self):
        with self.assertRaises(ValueError):
            deploy.check_context('dev', '135755363077', 'LMA-Dev', '123')
        with self.assertRaises(ValueError):
            deploy.validate_cleanup('LMA-Dev', {'LmaCiRun': '123', 'LmaCiManaged': 'true'}, '123')

    def test_untrusted_contexts(self):
        for key, value in [('GITHUB_REF', 'refs/heads/develop'), ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REPOSITORY', 'attacker/fork')]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                deploy.check_context('production', '009853297978', 'LMA', '123')

    def test_cleanup_requires_both_ownership_tags(self):
        for tags in [{}, {'LmaCiRun': '999', 'LmaCiManaged': 'true'}, {'LmaCiRun': '123'}]:
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                deploy.validate_cleanup('LMA-CI-123', tags, '123')
        deploy.validate_cleanup('LMA-CI-123', {'LmaCiRun': '123', 'LmaCiManaged': 'true'}, '123')

    def test_parameter_files_resolve_without_exposing_secrets(self):
        for environment, filename in [('production', 'prod'), ('dev', 'dev')]:
            base = json.loads((ROOT / f'deploy/params/{filename}.json').read_text())
            overrides = {p['ParameterKey']: '' for p in base if 'TODO_' in p['ParameterValue'] or 'FILL_IN_FROM_SECRET_STORE' in p['ParameterValue']}
            result = {p['ParameterKey']: p['ParameterValue'] for p in deploy.merge_parameters(base, overrides, environment)}
            self.assertEqual(result['EnableDataRetentionOnDelete'], 'true' if environment == 'production' else 'false')

    def test_bad_parameter_overrides(self):
        base = [{'ParameterKey': 'SummaryBedrockModelId', 'ParameterValue': 'global.moonshotai.kimi-k3'}, {'ParameterKey': 'ModelValidation', 'ParameterValue': 'true'}]
        for overrides in [[], {'Unknown': 'x'}, {'ModelValidation': False}, {'ModelValidation': 'false'}, {'SummaryBedrockModelId': 'other'}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                deploy.merge_parameters(base, overrides, 'production')

    def test_placeholder_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'AdminEmail'):
            deploy.merge_parameters([{'ParameterKey': 'AdminEmail', 'ParameterValue': 'TODO_ADMIN'}], {}, 'production')

    def test_production_boundary_is_mandatory_and_pinned(self):
        approved = 'arn:aws:iam::009853297978:policy/lma/isolation/application-boundary'
        parameters = [{'ParameterKey': 'PermissionsBoundaryArn', 'ParameterValue': approved}]
        with patch.dict(os.environ, {'LMA_APPLICATION_BOUNDARY_ARN': approved}):
            deploy.validate_application_boundary(parameters, '009853297978', 'production')
            for supplied in ['', 'arn:aws:iam::aws:policy/AdministratorAccess',
                             approved.replace('application-boundary', 'other')]:
                with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                    deploy.validate_application_boundary(
                        [{'ParameterKey': 'PermissionsBoundaryArn', 'ParameterValue': supplied}],
                        '009853297978', 'production')
            with self.assertRaises(ValueError):
                deploy.validate_application_boundary([], '009853297978', 'production')
        for configured in ['', approved.replace('009853297978', '135755363077'),
                           'arn:aws:iam::aws:policy/AdministratorAccess']:
            with patch.dict(os.environ, {'LMA_APPLICATION_BOUNDARY_ARN': configured}), self.assertRaises(ValueError):
                deploy.validate_application_boundary(parameters, '009853297978', 'production')

    def test_dev_boundary_remains_optional(self):
        deploy.validate_application_boundary([], '135755363077', 'dev')

    def run_main(self, action, environment, existing=None):
        account = deploy.ACCOUNTS[environment]
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {'Stacks': [existing]} if existing else {'Stacks': []}
        # Use a patched read helper for the not-found case; all service clients are mocks.
        session = MagicMock()
        session.client.return_value.get_caller_identity.return_value = {'Account': account}
        base = json.loads((ROOT / f"deploy/params/{'prod' if environment == 'production' else 'dev'}.json").read_text())
        overrides = {p['ParameterKey']: '' for p in base if 'TODO_' in p['ParameterValue'] or 'FILL_IN_FROM_SECRET_STORE' in p['ParameterValue']}
        boundary = f'arn:aws:iam::{account}:policy/lma/isolation/application-boundary'
        overrides['PermissionsBoundaryArn'] = boundary if environment == 'production' else ''
        session.client.return_value.get_secret_value.return_value = {'SecretString': json.dumps(overrides)}
        clients = {'cloudformation': cfn, 'sts': session.client.return_value, 'secretsmanager': session.client.return_value}
        session.client.side_effect = clients.get
        url = 'https://s3.us-east-1.amazonaws.com/test-us-east-1/releases/abc/123/1/lma-main.yaml'
        with patch.dict(os.environ, {
            'LMA_ARTIFACT_BUCKET': 'test-us-east-1', 'GITHUB_SHA': 'abc', 'GITHUB_RUN_ATTEMPT': '1',
            'LMA_CONFIG_SECRET_ARN': f'arn:aws:secretsmanager:us-east-1:{account}:secret:test',
            'LMA_CFN_ROLE_ARN': f'arn:aws:iam::{account}:role/cfn',
            'LMA_APPLICATION_BOUNDARY_ARN': boundary,
        }), patch.object(deploy.boto3, 'Session', return_value=session), patch.object(deploy, 'describe_stack', return_value=existing), patch.object(deploy.sys, 'argv', ['deploy.py', action, '--environment', environment, '--run-id', '123', '--template-url', url]):
            deploy.main()
        return cfn

    def test_preflight_never_writes(self):
        cfn = self.run_main('preflight', 'production')
        cfn.create_change_set.assert_not_called()
        cfn.execute_change_set.assert_not_called()
        cfn.delete_stack.assert_not_called()

    def test_production_never_cleanup(self):
        with self.assertRaisesRegex(ValueError, 'Production cleanup'):
            self.run_main('cleanup', 'production')

    def test_owned_dev_cleanup(self):
        cfn = self.run_main('cleanup', 'dev', {'Tags': [{'Key': 'LmaCiManaged', 'Value': 'true'}, {'Key': 'LmaCiRun', 'Value': '123'}]})
        cfn.delete_stack.assert_called_once_with(StackName='LMA-CI-123')

    def test_unowned_dev_cleanup_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.run_main('cleanup', 'dev', {'Tags': []})

    def test_existing_temporary_stack_cannot_be_overwritten(self):
        with self.assertRaisesRegex(ValueError, 'create-only'):
            self.run_main('deploy', 'dev', {'Tags': []})

    def test_production_deploy_protects_and_never_deletes(self):
        cfn = self.run_main('deploy', 'production')
        cfn.create_change_set.assert_called_once()
        args = cfn.create_change_set.call_args.kwargs
        self.assertEqual(args['StackName'], 'LMA')
        self.assertEqual(args['ChangeSetType'], 'CREATE')
        cfn.execute_change_set.assert_called_once()
        cfn.update_termination_protection.assert_called_once_with(StackName='LMA', EnableTerminationProtection=True)
        cfn.delete_stack.assert_not_called()


if __name__ == '__main__':
    unittest.main()
