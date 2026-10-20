"""Offline deployment safety regression tests; never contact AWS."""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.session import get_session
from botocore.validate import validate_parameters

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

    def test_long_run_ids_do_not_exceed_nested_resource_name_limits(self):
        run = '38375628033'
        name = deploy.dev_stack_name(run)
        self.assertEqual(name, 'LMA-CI-628033')
        self.assertLessEqual(len(name), 13)
        deploy.check_context('dev', '135755363077', name, run)
        with self.assertRaises(ValueError):
            deploy.validate_cleanup(name, {'LmaCiRun': '123628033', 'LmaCiManaged': 'true'}, run)
        deploy.validate_cleanup(name, {'LmaCiRun': run, 'LmaCiManaged': 'true'}, run)

    def test_feature_branch_is_dev_only(self):
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/feature/upstream-v0.3.10'}):
            deploy.check_context('dev', '135755363077', 'LMA-CI-123', '123')
            with self.assertRaises(ValueError):
                deploy.check_context('production', '009853297978', 'LMA', '123')
            with self.assertRaises(ValueError):
                deploy.check_context('dev', '135755363077', 'LMA-Dev', '123')

    def test_keep_dev_preserves_data_without_widening_the_stack_target(self):
        base = json.loads((ROOT / 'deploy/params/dev.json').read_text())
        overrides = {p['ParameterKey']: '' for p in base
                     if 'FILL_IN_FROM_SECRET_STORE' in p['ParameterValue']}
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true'}):
            parameters = deploy.merge_parameters(base, overrides, 'dev')
        self.assertEqual(next(p['ParameterValue'] for p in parameters
                              if p['ParameterKey'] == 'EnableDataRetentionOnDelete'), 'true')

    def test_workflow_keep_dev_default_is_false_and_cleanup_is_conditional(self):
        import yaml
        workflow = yaml.load((ROOT / '.github/workflows/lma-deploy.yml').read_text(),
                             Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['keep_dev']['default'], 'false')
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['operation']['options'],
                         ['preflight', 'prepare', 'execute', 'deploy', 'dev-create', 'dev-update', 'dev-destroy'])
        checks = str(workflow['jobs']['checks']['if'])
        self.assertIn('EXECUTE REVIEWED LMA CHANGE SET', checks)
        self.assertIn("inputs.environment == 'production'", checks)
        steps = workflow['jobs']['deploy']['steps']
        prepare = next(step for step in steps if 'Prepare production change set' in step.get('name', ''))
        execute = next(step for step in steps if 'Execute the reviewed production change set' in step.get('name', ''))
        self.assertIn("inputs.operation == 'prepare'", prepare['if'])
        self.assertIn("inputs.operation == 'execute'", execute['if'])
        self.assertIn('LMA_CHANGE_SET_NAME', workflow['jobs']['deploy']['env'])
        cleanup = workflow['jobs']['deploy']['steps'][-1]
        self.assertIn('!inputs.keep_dev', cleanup['if'])
        self.assertIn("inputs.environment == 'dev'", cleanup['if'])

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
            if environment == 'production':
                overrides['GoogleOAuthClientSecret'] = 'test-secret-not-real'
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

    def test_cfn_role_rejects_legacy_admin_cross_account_and_wildcard(self):
        role = 'arn:aws:iam::009853297978:role/lma/isolation/LMA-CloudFormation'
        deploy.validate_cloudformation_role(role, '009853297978')
        for invalid in ['', role.replace('009853297978', '135755363077'),
                        'arn:aws:iam::009853297978:role/Admin', role + '*']:
            with self.subTest(role=invalid), self.assertRaises(ValueError):
                deploy.validate_cloudformation_role(invalid, '009853297978')

    def run_main(self, action, environment, existing=None, run_id='123', change_set_override=None,
                 capture_error=False, confirmation=None, preflight=False):
        account = deploy.ACCOUNTS[environment]
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {'Stacks': [existing]} if existing else {'Stacks': []}
        cfn.describe_change_set.return_value = {'Changes': []}
        cfn.create_change_set.return_value = {'Id': 'created-change-set'}
        # Use a patched read helper for the not-found case; all service clients are mocks.
        session = MagicMock()
        self.last_session = session
        session.client.return_value.get_caller_identity.return_value = {'Account': account}
        base = json.loads((ROOT / f"deploy/params/{'prod' if environment == 'production' else 'dev'}.json").read_text())
        overrides = {p['ParameterKey']: '' for p in base if 'TODO_' in p['ParameterValue'] or 'FILL_IN_FROM_SECRET_STORE' in p['ParameterValue']}
        if environment == 'production':
            overrides['GoogleOAuthClientSecret'] = 'test-secret-not-real'
        boundary = f'arn:aws:iam::{account}:policy/lma/isolation/application-boundary'
        overrides['PermissionsBoundaryArn'] = boundary
        session.client.return_value.get_secret_value.return_value = {
            'SecretString': json.dumps(overrides), 'VersionId': 'config-version'}
        if action == 'execute':
            prepared = {
                'ChangeSetId': 'prepared-change-set', 'Status': 'CREATE_COMPLETE',
                'ExecutionStatus': 'AVAILABLE', 'StackId': 'stack-123',
                'Description': ('LMA prepared release sha=abc configVersion=config-version '
                                'stackId=stack-123 stackVersion=2026-10-19T00:00:00+00:00'),
                'IncludeNestedStacks': True,
            }
            # Validate the happy-path fixture against the real AWS response
            # contract; invented fields must not hide a broken execution guard.
            response_shape = get_session().get_service_model('cloudformation').operation_model(
                'DescribeChangeSet').output_shape
            validate_parameters(prepared, response_shape)
            prepared.update(change_set_override or {})
            cfn.describe_change_set.return_value = prepared
        clients = {'cloudformation': cfn, 'sts': session.client.return_value, 'secretsmanager': session.client.return_value}
        session.client.side_effect = clients.get
        url = f'https://s3.us-east-1.amazonaws.com/test-us-east-1/releases/abc/{run_id}/1/lma-main.yaml'
        with patch.dict(os.environ, {
            'LMA_ARTIFACT_BUCKET': 'test-us-east-1', 'GITHUB_SHA': 'abc', 'GITHUB_RUN_ATTEMPT': '1',
            'LMA_CONFIG_SECRET_ARN': f'arn:aws:secretsmanager:us-east-1:{account}:secret:test',
            'LMA_CFN_ROLE_ARN': f'arn:aws:iam::{account}:role/lma/isolation/LMA-CloudFormation',
            'LMA_APPLICATION_BOUNDARY_ARN': boundary,
            'LMA_CONFIRMATION': confirmation if confirmation is not None else ('EXECUTE REVIEWED LMA CHANGE SET' if action == 'execute' else 'DEPLOY LMA'),
        }), patch.object(deploy.boto3, 'Session', return_value=session), patch.object(deploy, 'describe_stack', return_value=existing), patch.object(deploy.sys, 'argv', ['deploy.py', action, '--environment', environment, '--run-id', run_id, '--template-url', url] + (['--change-set-name', 'lma-ci-100-1'] if action == 'execute' else []) + (['--preflight'] if preflight else [])):
            try:
                deploy.main()
            except Exception as exc:
                if not capture_error:
                    raise
                return cfn, exc
        return (cfn, None) if capture_error else cfn

    def test_preflight_never_writes(self):
        cfn = self.run_main('preflight', 'production')
        cfn.create_change_set.assert_not_called()
        cfn.execute_change_set.assert_not_called()
        cfn.delete_stack.assert_not_called()

    def test_production_never_cleanup(self):
        with self.assertRaisesRegex(ValueError, 'Production cleanup'):
            self.run_main('cleanup', 'production')

    def on_demand_stack(self, owner='123', status='CREATE_COMPLETE'):
        return {
            'StackId': f'arn:aws:cloudformation:us-east-1:135755363077:stack/{deploy.dev_stack_name(owner)}/uuid-123',
            'StackStatus': status, 'EnableTerminationProtection': False,
            'RoleARN': 'arn:aws:iam::135755363077:role/lma/isolation/LMA-CloudFormation',
            'Parameters': [{'ParameterKey': 'EnableDataRetentionOnDelete', 'ParameterValue': 'false'}],
            'Tags': [{'Key': 'LmaCiManaged', 'Value': 'true'}, {'Key': 'LmaCiRun', 'Value': owner},
                     {'Key': 'LmaCiLifecycle', 'Value': 'on-demand'}],
        }

    def test_dev_create_leaves_stack_running_with_disposable_password_configuration(self):
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': ''}):
            cfn = self.run_main('dev-create', 'dev')
        request = cfn.create_change_set.call_args.kwargs
        params = {p['ParameterKey']: p['ParameterValue'] for p in request['Parameters']}
        self.assertEqual(params['EnableDataRetentionOnDelete'], 'false')
        self.assertEqual(params['EnableGoogleSso'], 'false')
        self.assertEqual(params['SsoPoolMode'], 'LEGACY')
        self.assertEqual(params['PasswordSignInEnabled'], 'true')
        self.assertIn({'Key': 'LmaCiLifecycle', 'Value': 'on-demand'}, request['Tags'])
        self.assertEqual(request['ChangeSetType'], 'CREATE')
        cfn.execute_change_set.assert_called_once()
        cfn.delete_stack.assert_not_called()

    def test_dev_update_uses_original_full_owner_and_preserves_stack(self):
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '123'}):
            cfn = self.run_main('dev-update', 'dev', self.on_demand_stack(), run_id='456')
        request = cfn.create_change_set.call_args.kwargs
        self.assertEqual(request['StackName'], 'LMA-CI-123')
        self.assertEqual(request['ChangeSetType'], 'UPDATE')
        self.assertIn({'Key': 'LmaCiRun', 'Value': '123'}, request['Tags'])
        cfn.delete_stack.assert_not_called()

    def test_dev_lifecycle_preflight_performs_no_mutations(self):
        for action, owner, existing in [('dev-create', '', None),
                                        ('dev-update', '123', self.on_demand_stack())]:
            with self.subTest(action=action), patch.dict(os.environ, {
                'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': owner,
            }):
                cfn = self.run_main(action, 'dev', existing, run_id='456', preflight=True)
                cfn.create_change_set.assert_not_called()
                cfn.execute_change_set.assert_not_called()
                cfn.delete_stack.assert_not_called()

    def test_destroy_binds_confirmation_to_full_owner_and_uses_stack_arn(self):
        stack = self.on_demand_stack()
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '123'}):
            cfn = self.run_main('dev-destroy', 'dev', stack, run_id='456', confirmation='DESTROY DEV 123')
        cfn.delete_stack.assert_called_once_with(StackName=stack['StackId'], RoleARN=stack['RoleARN'])
        validate_parameters(cfn.delete_stack.call_args.kwargs, get_session().get_service_model(
            'cloudformation').operation_model('DeleteStack').input_shape)
        cfn.create_change_set.assert_not_called()
        cfn.execute_change_set.assert_not_called()
        self.assertNotIn('secretsmanager', [c.args[0] for c in self.last_session.client.call_args_list])

    def test_destroy_missing_stack_is_safe_noop(self):
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '123'}):
            cfn = self.run_main('dev-destroy', 'dev', run_id='456', confirmation='DESTROY DEV 123')
        cfn.delete_stack.assert_not_called()

    def test_invalid_lifecycle_intent_is_rejected_before_aws(self):
        for action, environment, owner, confirmation, keep in [
            ('dev-create', 'production', '', 'DEPLOY LMA', 'true'),
            ('dev-update', 'production', '123', 'DEPLOY LMA', 'true'),
            ('dev-destroy', 'production', '123', 'DESTROY DEV 123', 'true'),
            ('dev-create', 'dev', '123', 'DEPLOY LMA', 'true'),
            ('dev-update', 'dev', '', 'DEPLOY LMA', 'true'),
            ('dev-destroy', 'dev', '', 'DESTROY DEV', 'true'),
            ('dev-destroy', 'dev', '123', 'DEPLOY LMA', 'true'),
            ('dev-destroy', 'dev', '123', 'DESTROY DEV 456', 'true'),
            ('dev-create', 'dev', '', 'DEPLOY LMA', 'false'),
            ('dev-destroy', 'dev', 'LMA-Dev', 'DESTROY DEV LMA-Dev', 'true'),
        ]:
            with self.subTest(action=action, environment=environment, owner=owner), patch.dict(os.environ, {
                'LMA_KEEP_DEV': keep, 'LMA_DEV_OWNER_RUN_ID': owner,
            }):
                _, error = self.run_main(action, environment, confirmation=confirmation, capture_error=True)
                self.assertIsInstance(error, ValueError)
                self.last_session.client.assert_not_called()

    def test_destroy_refuses_older_retained_protected_unowned_or_active_stacks(self):
        for field, value in [
            ('Tags', []), ('Parameters', []), ('EnableTerminationProtection', True),
            ('StackStatus', 'UPDATE_IN_PROGRESS'), ('StackStatus', 'DELETE_IN_PROGRESS'),
            ('RoleARN', 'arn:aws:iam::135755363077:role/Admin'),
            ('StackId', 'arn:aws:cloudformation:us-east-1:009853297978:stack/LMA/uuid'),
        ]:
            stack = dict(self.on_demand_stack(), **{field: value})
            with self.subTest(field=field, value=value), patch.dict(os.environ, {
                'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '123',
            }):
                cfn, error = self.run_main('dev-destroy', 'dev', stack, run_id='456',
                                           confirmation='DESTROY DEV 123', capture_error=True)
                self.assertIsInstance(error, ValueError)
                cfn.delete_stack.assert_not_called()

    def test_destroy_rejects_same_suffix_wrong_full_owner(self):
        owner = '100000000123'
        stack = self.on_demand_stack(owner=owner)
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '200000000123'}):
            cfn, error = self.run_main('dev-destroy', 'dev', stack, run_id='456',
                                      confirmation='DESTROY DEV 200000000123', capture_error=True)
        self.assertIsInstance(error, ValueError)
        cfn.delete_stack.assert_not_called()

    def test_on_demand_update_cannot_adopt_old_retained_validation_stack(self):
        stack = self.on_demand_stack()
        stack['Tags'] = stack['Tags'][:2]
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '123'}):
            cfn, error = self.run_main('dev-update', 'dev', stack, run_id='456', capture_error=True)
        self.assertIsInstance(error, ValueError)
        cfn.create_change_set.assert_not_called()

    def test_failed_on_demand_stack_requires_explicit_destroy_not_automatic_cleanup(self):
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '123'}):
            cfn = self.run_main('dev-destroy', 'dev', self.on_demand_stack(status='ROLLBACK_COMPLETE'),
                                run_id='456', confirmation='DESTROY DEV 123')
        cfn.delete_stack.assert_called_once()

    def test_workflow_lifecycle_operations_cannot_use_automatic_cleanup(self):
        import yaml
        workflow = yaml.load((ROOT / '.github/workflows/lma-deploy.yml').read_text(), Loader=yaml.BaseLoader)
        job = workflow['jobs']['deploy']
        cleanup = job['steps'][-1]
        self.assertIn("inputs.operation == 'deploy'", cleanup['if'])
        self.assertIn('startsWith(inputs.operation', job['env']['LMA_KEEP_DEV'])
        destroy = next(s for s in job['steps'] if 'explicitly confirmed on-demand' in s.get('name', ''))
        self.assertIn("inputs.environment == 'dev'", destroy['if'])
        self.assertIn("inputs.operation == 'dev-destroy'", destroy['if'])
        self.assertNotIn('lma publish', destroy['run'])
        self.assertNotIn('template-url', destroy['run'])
        self.assertIn("format('DESTROY DEV {0}', inputs.dev_owner_run_id)", workflow['jobs']['checks']['if'])
        build = next(s for s in job['steps'] if 'Build and publish' in s.get('name', ''))
        create = next(s for s in job['steps'] if 'Create or update on-demand' in s.get('name', ''))
        self.assertIn('!inputs.dev_preflight_only', build['if'])
        self.assertIn('!inputs.dev_preflight_only', create['if'])
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['dev_preflight_only']['default'], 'false')

    def test_lifecycle_does_not_change_existing_dev_json(self):
        base = json.loads((ROOT / 'deploy/params/dev.json').read_text())
        before = json.dumps(base, sort_keys=True)
        overrides = {p['ParameterKey']: '' for p in base
                     if 'FILL_IN_FROM_SECRET_STORE' in p['ParameterValue']}
        overrides.update(EnableGoogleSso='true', SsoPoolMode='ACTIVE', PasswordSignInEnabled='false')
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true'}):
            params = {p['ParameterKey']: p['ParameterValue'] for p in
                      deploy.merge_parameters(base, overrides, 'dev', disposable_dev=True)}
        self.assertEqual(params['EnableGoogleSso'], 'false')
        self.assertEqual(params['SsoPoolMode'], 'LEGACY')
        self.assertEqual(params['PasswordSignInEnabled'], 'true')
        self.assertEqual(params['EnableDataRetentionOnDelete'], 'false')
        self.assertEqual(json.dumps(base, sort_keys=True), before)

    def test_workflow_entry_guard_operation_matrix(self):
        import re
        from types import SimpleNamespace

        import yaml

        workflow = yaml.load((ROOT / '.github/workflows/lma-deploy.yml').read_text(), Loader=yaml.BaseLoader)
        expression = workflow['jobs']['checks']['if'].replace('&&', ' and ').replace('||', ' or ')
        expression = re.sub(r'!(?!=)', 'not ', expression)
        expression = expression.replace('startsWith(', 'starts_with(').replace('format(', 'gha_format(')
        expression = compile('(' + expression + ')', 'workflow-entry-guard', 'eval')

        def allowed(environment, operation, owner='', confirmation='DEPLOY LMA', preflight=False,
                    ref='refs/heads/allops-main', repository=deploy.REPOSITORY):
            return eval(expression, {'__builtins__': {}}, {
                'github': SimpleNamespace(repository=repository, ref=ref),
                'inputs': SimpleNamespace(environment=environment, operation=operation,
                                         dev_owner_run_id=owner, confirmation=confirmation,
                                         dev_preflight_only=preflight),
                'starts_with': lambda text, prefix: text.startswith(prefix),
                'gha_format': lambda text, value: text.format(value),
            })

        for operation in ['prepare', 'preflight']:
            self.assertTrue(allowed('production', operation))
        self.assertTrue(allowed('production', 'execute', confirmation='EXECUTE REVIEWED LMA CHANGE SET'))
        self.assertTrue(allowed('dev', 'deploy'))
        self.assertTrue(allowed('dev', 'dev-create'))
        self.assertTrue(allowed('dev', 'dev-update', owner='123'))
        self.assertTrue(allowed('dev', 'dev-destroy', owner='123', confirmation='DESTROY DEV 123'))
        self.assertTrue(allowed('dev', 'dev-create', preflight=True, ref='refs/heads/feature/example'))
        self.assertTrue(allowed('dev', 'dev-update', owner='123', preflight=True))
        for kwargs in [
            dict(environment='production', operation='dev-create'),
            dict(environment='production', operation='dev-update', owner='123'),
            dict(environment='production', operation='dev-destroy', owner='123', confirmation='DESTROY DEV 123'),
            dict(environment='dev', operation='dev-create', owner='123'),
            dict(environment='dev', operation='dev-update'),
            dict(environment='dev', operation='dev-destroy', owner='123'),
            dict(environment='dev', operation='dev-destroy', owner='123', confirmation='DESTROY DEV 456'),
            dict(environment='dev', operation='dev-destroy', owner='123', confirmation='DESTROY DEV 123', preflight=True),
            dict(environment='production', operation='prepare', preflight=True),
            dict(environment='production', operation='prepare', ref='refs/heads/feature/example'),
            dict(environment='dev', operation='dev-create', ref='refs/heads/develop'),
            dict(environment='dev', operation='dev-create', repository='attacker/fork'),
        ]:
            with self.subTest(**kwargs):
                self.assertFalse(allowed(**kwargs))

    def test_owned_dev_cleanup(self):
        cfn = self.run_main('cleanup', 'dev', {'Tags': [{'Key': 'LmaCiManaged', 'Value': 'true'}, {'Key': 'LmaCiRun', 'Value': '123'}]})
        cfn.delete_stack.assert_called_once_with(StackName='LMA-CI-123')

    def test_unowned_dev_cleanup_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.run_main('cleanup', 'dev', {'Tags': []})

    def test_existing_temporary_stack_cannot_be_overwritten(self):
        with self.assertRaisesRegex(ValueError, 'create-only'):
            self.run_main('deploy', 'dev', {'Tags': []})

    def test_retained_update_is_explicit_dev_only_and_requires_retention(self):
        self.assertEqual(deploy.deployment_owner_run('dev', '456', ''), '456')
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true'}):
            self.assertEqual(deploy.deployment_owner_run('dev', '456', '123'), '123')
            with self.assertRaises(ValueError):
                deploy.deployment_owner_run('production', '456', '123')
            with self.assertRaises(ValueError):
                deploy.deployment_owner_run('dev', '456', 'LMA-Dev')
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'false'}), self.assertRaises(ValueError):
            deploy.deployment_owner_run('dev', '456', '123')

    def test_retained_update_requires_full_owner_tags_and_healthy_state(self):
        existing = {'StackStatus': 'CREATE_COMPLETE', 'Tags': [
            {'Key': 'LmaCiRun', 'Value': '123'}, {'Key': 'LmaCiManaged', 'Value': 'true'}]}
        deploy.validate_retained_dev_update('LMA-CI-123', existing, '123')
        for stack, owner, record in [('LMA-Dev', '123', existing),
                                     ('LMA-CI-123', '999123', existing),
                                     ('LMA-CI-123', '123', None),
                                     ('LMA-CI-123', '123', dict(existing, StackStatus='UPDATE_IN_PROGRESS'))]:
            with self.subTest(stack=stack, owner=owner), self.assertRaises(ValueError):
                deploy.validate_retained_dev_update(stack, record, owner)

    def test_retained_update_preserves_original_owner_and_never_deletes(self):
        existing = {'StackStatus': 'CREATE_COMPLETE', 'Tags': [
            {'Key': 'LmaCiRun', 'Value': '123'}, {'Key': 'LmaCiManaged', 'Value': 'true'}]}
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '123'}):
            cfn = self.run_main('deploy', 'dev', existing, run_id='456')
        args = cfn.create_change_set.call_args.kwargs
        self.assertEqual(args['StackName'], 'LMA-CI-123')
        self.assertEqual(args['ChangeSetType'], 'UPDATE')
        self.assertFalse(args['IncludeNestedStacks'])
        self.assertIn({'Key': 'LmaCiRun', 'Value': '123'}, args['Tags'])
        cfn.delete_stack.assert_not_called()
        with patch.dict(os.environ, {'LMA_KEEP_DEV': 'true', 'LMA_DEV_OWNER_RUN_ID': '123'}):
            with self.assertRaisesRegex(ValueError, 'never perform automatic cleanup'):
                self.run_main('cleanup', 'dev', existing)

    def test_production_prepare_creates_reviewable_nested_change_set_without_execution(self):
        cfn = self.run_main('prepare', 'production')
        cfn.create_change_set.assert_called_once()
        args = cfn.create_change_set.call_args.kwargs
        self.assertEqual(args['StackName'], 'LMA')
        self.assertEqual(args['ChangeSetType'], 'CREATE')
        self.assertTrue(args['IncludeNestedStacks'])
        self.assertEqual(args['Description'], ('LMA prepared release sha=abc configVersion=config-version '
                                               'stackId=none stackVersion=none'))
        cfn.execute_change_set.assert_not_called()
        cfn.update_termination_protection.assert_not_called()
        cfn.delete_stack.assert_not_called()

    def test_production_direct_deploy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'prepare, review, then execute'):
            self.run_main('deploy', 'production')

    def test_execute_requires_reviewed_production_change_set(self):
        cfn = self.run_main('execute', 'production', {
            'StackStatus': 'UPDATE_COMPLETE', 'StackId': 'stack-123',
            'LastUpdatedTime': '2026-10-19T00:00:00+00:00'})
        cfn.execute_change_set.assert_called_once_with(ChangeSetName='prepared-change-set')
        cfn.update_termination_protection.assert_called_once_with(
            StackName='LMA', EnableTerminationProtection=True)

    def test_execute_rejects_stale_or_unreviewable_change_set(self):
        for field, value in [('Status', 'FAILED'), ('ExecutionStatus', 'EXECUTE_IN_PROGRESS'),
                             ('Description', 'another source'), ('IncludeNestedStacks', False),
                             ('StackId', 'another-stack'), ('StackId', None)]:
            with self.subTest(field=field):
                cfn, error = self.run_main('execute', 'production', {
                    'StackStatus': 'UPDATE_COMPLETE', 'StackId': 'stack-123',
                    'LastUpdatedTime': '2026-10-19T00:00:00+00:00'},
                                           change_set_override={field: value}, capture_error=True)
                self.assertIsInstance(error, ValueError)
                self.assertIn('stale, unreviewable', str(error))
                cfn.execute_change_set.assert_not_called()


if __name__ == '__main__':
    unittest.main()
