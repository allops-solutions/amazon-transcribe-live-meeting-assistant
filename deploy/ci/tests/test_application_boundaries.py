"""Verify boundary coverage without deploying or changing runtime permissions."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = (
    'lma-main.yaml',
    'lma-vpc-stack/template.yaml',
    'lma-browser-extension-stack/template.yaml',
    'lma-desktop-capture-app-stack/template.yaml',
    'lma-virtual-participant-stack/template.yaml',
    'lma-asr-microvm-stack/template.yaml',
    'lma-cognito-stack/deployment/lma-cognito-stack.yaml',
    'lma-meetingassist-setup-stack/template.yaml',
    'lma-bedrockkb-stack/template.yaml',
    'lma-websocket-transcriber-stack/deployment/lma-websocket-transcriber.yaml',
    'lma-ai-stack/deployment/lma-ai-stack.yaml',
    'lma-llm-template-setup-stack/deployment/llm-template-setup.yaml',
    'lma-chat-button-config-stack/deployment/chat-button-config.yaml',
    'lma-nova-sonic-config-stack/deployment/nova-sonic-config.yaml',
)
BOUNDARY = ['HasPermissionsBoundary', 'PermissionsBoundaryArn', 'AWS::NoValue']


class ApplicationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.templates = {path: yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)
                          for path in TEMPLATES}

    def test_boundary_defaults_preserve_unconfigured_environments(self):
        for path, template in self.templates.items():
            with self.subTest(template=path):
                self.assertEqual(template['Parameters']['PermissionsBoundaryArn']['Default'], '')
                self.assertIn('HasPermissionsBoundary', template['Conditions'])

    def test_all_explicit_roles_have_conditional_boundary(self):
        count = 0
        for path, template in self.templates.items():
            for name, resource in template['Resources'].items():
                if resource['Type'] == 'AWS::IAM::Role':
                    count += 1
                    with self.subTest(template=path, role=name):
                        self.assertEqual(resource['Properties']['PermissionsBoundary'], BOUNDARY)
        self.assertGreaterEqual(count, 102)

    def test_sam_generated_function_roles_have_conditional_boundary(self):
        count = 0
        for path, template in self.templates.items():
            global_role = template.get('Globals', {}).get('Function', {}).get('Role')
            for name, resource in template['Resources'].items():
                if resource['Type'] == 'AWS::Serverless::Function':
                    properties = resource['Properties']
                    if 'Role' not in properties and not global_role:
                        count += 1
                        with self.subTest(template=path, function=name):
                            self.assertEqual(properties['PermissionsBoundary'], BOUNDARY)
        self.assertGreater(count, 0)

    def test_every_nested_stack_receives_boundary_parameter(self):
        count = 0
        for path, template in self.templates.items():
            for name, resource in template['Resources'].items():
                if resource['Type'] == 'AWS::CloudFormation::Stack':
                    count += 1
                    with self.subTest(template=path, stack=name):
                        self.assertEqual(resource['Properties']['Parameters']['PermissionsBoundaryArn'],
                                         'PermissionsBoundaryArn')
        self.assertEqual(count, 15)


if __name__ == '__main__':
    unittest.main()
