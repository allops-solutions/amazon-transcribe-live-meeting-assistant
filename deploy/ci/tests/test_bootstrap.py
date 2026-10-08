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


if __name__ == '__main__':
    unittest.main()
