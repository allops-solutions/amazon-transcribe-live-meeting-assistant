"""Bootstrap staging and permission regression checks; no AWS access."""
from pathlib import Path
import unittest

import yaml


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[3]
        self.template = yaml.load((root / 'deploy/ci/bootstrap.yaml').read_text(), Loader=yaml.BaseLoader)

    def test_deployment_disabled_by_default(self):
        self.assertEqual(self.template['Parameters']['EnableDeploymentRole']['Default'], 'false')
        self.assertEqual(self.template['Resources']['DeploymentRole']['Condition'], 'DeploymentEnabled')
        self.assertEqual(self.template['Outputs']['DeploymentRoleArn']['Condition'], 'DeploymentEnabled')
        self.assertIn('DeploymentInputs', self.template['Rules'])

    def test_change_sets_pin_service_role_and_private_release_source(self):
        statements = [s for s in self.template['Resources']['DeploymentRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
                      if isinstance(s, dict)]
        pinned = next(s for s in statements if s.get('Sid') == 'CreatePinnedReleaseChangeSet')
        self.assertEqual(pinned['Condition']['ArnEquals']['cloudformation:RoleArn'], 'CloudFormationServiceRoleArn')
        self.assertEqual(pinned['Condition']['StringLike']['cloudformation:TemplateUrl'],
                         'https://s3.${AWS::Region}.${AWS::URLSuffix}/${Artifacts}/releases/*/lma-main.yaml')
        self.assertEqual(pinned['Resource'][0], 'Production')
        self.assertIn('DenyUnapprovedChangeSetRole', [s.get('Sid') for s in statements])
        self.assertIn('DenyUnapprovedChangeSetSource', [s.get('Sid') for s in statements])
        other = [s for s in statements if s.get('Effect') == 'Allow' and 'cloudformation:CreateChangeSet'
                 in ([s.get('Action')] if isinstance(s.get('Action'), str) else s.get('Action', []))
                 and s is not pinned]
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0]['Sid'], 'PermitReviewedSamTransform')

    def test_runner_has_no_deployment_permissions(self):
        policies = self.template['Resources']['RunnerRole']['Properties']['Policies']
        statements = policies[0]['PolicyDocument']['Statement']
        actions = [action for statement in statements for action in statement['Action']]
        self.assertTrue(all(action.startswith(('logs:', 'codeconnections:')) for action in actions))
        self.assertIn('codeconnections:GetConnectionToken', actions)

    def test_runner_is_repository_specific(self):
        source = self.template['Resources']['Runner']['Properties']['Source']
        self.assertEqual(source['Location'], 'https://github.com/allops-solutions/amazon-transcribe-live-meeting-assistant')
        self.assertEqual(source['Auth']['Type'], 'CODECONNECTIONS')

    def test_authentication_role_is_metadata_only(self):
        self.assertEqual(self.template['Parameters']['EnableAuthenticationRole']['Default'], 'false')
        role = self.template['Resources']['AuthenticationRole']
        self.assertEqual(role['Condition'], 'AuthenticationEnabled')
        statements = role['Properties']['Policies'][0]['PolicyDocument']['Statement']
        actions = []
        for statement in statements:
            action = statement['Action']
            actions.extend(action if isinstance(action, list) else [action])
        self.assertEqual(set(actions), {'s3:GetBucketLocation', 's3:ListBucket', 'secretsmanager:DescribeSecret'})
        self.assertEqual(role['Properties']['AssumeRolePolicyDocument']['Statement'][0]['Condition']['StringEquals']['token.actions.githubusercontent.com:aud'], 'sts.amazonaws.com')
        self.assertIn('AuthenticationInputs', self.template['Rules'])

    def test_publish_role_cannot_deploy_or_read_configuration(self):
        self.assertEqual(self.template['Parameters']['EnablePublishRole']['Default'], 'false')
        role = self.template['Resources']['PublishRole']
        self.assertEqual(role['Condition'], 'PublishEnabled')
        statements = role['Properties']['Policies'][0]['PolicyDocument']['Statement']
        plain = [statement for statement in statements if 'Action' in statement]
        actions = {action for statement in plain for action in (
            statement['Action'] if isinstance(statement['Action'], list) else [statement['Action']])}
        self.assertEqual(actions, {'s3:GetBucketLocation', 's3:ListBucket', 's3:GetBucketVersioning',
                                  's3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload',
                                  'cloudformation:ValidateTemplate'})
        objects = next(statement for statement in plain if 's3:PutObject' in statement['Action'])
        self.assertEqual(objects['Resource'], '${Artifacts.Arn}/build-validation/*')
        self.assertIn('PublishInputs', self.template['Rules'])
        self.assertEqual(self.template['Outputs']['PublishRoleArn']['Condition'], 'PublishEnabled')

    def test_publish_validation_is_opt_in_and_has_no_deploy_step(self):
        root = Path(__file__).resolve().parents[3]
        workflow = yaml.load((root / '.github/workflows/lma-bootstrap-validation.yml').read_text(),
                             Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['publish_build']['default'], 'false')
        publish = workflow['jobs']['publish']
        self.assertIn('inputs.publish_build', publish['if'])
        self.assertIn("refs/heads/allops-main", publish['if'])
        scripts = '\n'.join(step.get('run', '') for step in publish['steps'])
        self.assertIn('build-validation/', scripts)
        self.assertIn('lma publish', scripts)
        self.assertNotIn('deploy.py', scripts)
        self.assertNotIn('secretsmanager', scripts)
        self.assertNotIn('execute-change-set', scripts)


if __name__ == '__main__':
    unittest.main()
