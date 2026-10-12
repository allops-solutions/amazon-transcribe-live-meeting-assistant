"""Verify boundary coverage without deploying or changing runtime permissions."""

import datetime
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import yaml
from samtranslator.translator.transform import transform
from samtranslator.yaml_helper import yaml_parse


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

    def test_application_build_projects_have_stack_scoped_names(self):
        count = 0
        for path, template in self.templates.items():
            for logical, resource in template['Resources'].items():
                if resource['Type'] == 'AWS::CodeBuild::Project':
                    count += 1
                    with self.subTest(template=path, resource=logical):
                        name = resource['Properties']['Name']
                        if isinstance(name, list):
                            self.assertEqual(name[0], 'HasPermissionsBoundary')
                            self.assertEqual(name[2], 'AWS::NoValue')
                            name = name[1]
                        self.assertTrue(name.startswith('${AWS::StackName}-'))
        self.assertEqual(count, 6)

    def test_application_secret_names_preserve_legacy_when_unconfigured(self):
        count = 0
        for path, template in self.templates.items():
            for logical, resource in template['Resources'].items():
                if resource['Type'] == 'AWS::SecretsManager::Secret':
                    count += 1
                    with self.subTest(template=path, resource=logical):
                        name = resource['Properties']['Name']
                        self.assertEqual(name[0], 'HasPermissionsBoundary')
                        self.assertTrue(name[1].startswith('${AWS::StackName}-'))
                        self.assertEqual(name[2], 'AWS::NoValue')
        self.assertEqual(count, 3)

    def test_sam_layers_and_legacy_schedule_groups_are_scoped(self):
        layer = self.templates['lma-ai-stack/deployment/lma-ai-stack.yaml']['Resources']['TranscriptEnrichmentPythonLayer']
        self.assertEqual(layer['Properties']['LayerName'],
                         ['HasPermissionsBoundary', '${AWS::StackName}-TranscriptEnrichment', 'AWS::NoValue'])
        group = self.templates['lma-virtual-participant-stack/template.yaml']['Resources']['ScheduleGroup']
        self.assertEqual(group['Properties']['Name'],
                         ['HasPermissionsBoundary', '${AWS::StackName}-Schedules', 'AWS::NoValue'])

    def test_all_lambda_layer_and_explicit_function_names_are_scoped(self):
        layers = 0
        for path, template in self.templates.items():
            for logical, resource in template['Resources'].items():
                resource_type = resource['Type']
                if resource_type in {'AWS::Lambda::LayerVersion', 'AWS::Serverless::LayerVersion'}:
                    field = 'LayerName'
                    layers += 1
                elif resource_type in {'AWS::Lambda::Function', 'AWS::Serverless::Function'}:
                    field = 'FunctionName'
                    if field not in resource['Properties']:
                        continue  # CloudFormation-generated names include the stack prefix.
                else:
                    continue
                with self.subTest(template=path, resource=logical):
                    name = resource['Properties'][field]
                    if isinstance(name, list):
                        self.assertEqual(name[0], 'HasPermissionsBoundary')
                        name = name[1]
                    self.assertTrue(name.startswith(('${AWS::StackName}-', '${LMAStackName}-')))
        self.assertEqual(layers, 3)

    def test_meetingassist_scoped_names_preserve_legacy_defaults(self):
        resources = self.templates['lma-meetingassist-setup-stack/template.yaml']['Resources']
        self.assertEqual(resources['Boto3Layer']['Properties']['LayerName'],
                         ['HasPermissionsBoundary', '${AWS::StackName}-Boto3', 'AWS::NoValue'])
        self.assertEqual(resources['StrandsMeetingAssistFunction']['Properties']['FunctionName'],
                         ['HasPermissionsBoundary', '${LMAStackName}-StrandsMeetingAssist',
                          'STRANDS-${LMAStackName}-MeetingAssist'])
        main = self.templates['lma-main.yaml']['Resources']
        self.assertEqual(main['VIRTUALPARTICIPANTSTACK']['Properties']['Parameters']['StrandsLambdaArn'],
                         ['ShouldUseAnyStrandsBedrock',
                          'MEETINGASSISTSETUP.Outputs.StrandsMeetingAssistFunctionArn', ''])
        arn = main['AISTACK']['Properties']['Parameters']['AgentAssistExistingLambdaFunctionArn']
        self.assertEqual(arn[0], 'HasPermissionsBoundary')
        self.assertTrue(arn[1].endswith(':function:${AWS::StackName}-StrandsMeetingAssist'))
        self.assertTrue(arn[2].endswith(':function:STRANDS-${AWS::StackName}-MeetingAssist'))
        project = self.templates['lma-ai-stack/deployment/lma-ai-stack.yaml']['Resources']['MCPLayerBuildProject']
        env = {item['Name']: item['Value'] for item in project['Properties']['Environment']['EnvironmentVariables']}
        self.assertEqual(env['MEETING_ASSIST_FUNCTION_NAME'],
                         resources['StrandsMeetingAssistFunction']['Properties']['FunctionName'])
        buildspec = project['Properties']['Source']['BuildSpec']
        self.assertIn('FUNCTION_NAME="$MEETING_ASSIST_FUNCTION_NAME"', buildspec)
        self.assertNotIn('FUNCTION_NAME="STRANDS-', buildspec)

    def test_all_explicit_roles_have_conditional_boundary(self):
        count = 0
        for path, template in self.templates.items():
            for name, resource in template['Resources'].items():
                if resource['Type'] == 'AWS::IAM::Role':
                    count += 1
                    with self.subTest(template=path, role=name):
                        self.assertEqual(resource['Properties']['PermissionsBoundary'], BOUNDARY)
        self.assertGreaterEqual(count, 102)

    def test_application_roles_do_not_preflight_fixed_names_when_bounded(self):
        count = 0
        for path, template in self.templates.items():
            for logical, resource in template['Resources'].items():
                if resource['Type'] == 'AWS::IAM::Role' and 'RoleName' in resource['Properties']:
                    count += 1
                    with self.subTest(template=path, role=logical):
                        name = resource['Properties']['RoleName']
                        self.assertEqual(name[:2], ['HasPermissionsBoundary', 'AWS::NoValue'])
                        self.assertTrue(name[2].startswith('${LMAStackName}-'))
        self.assertEqual(count, 1)

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

    def test_sam_transformed_roles_retain_boundary(self):
        def normalize(value):
            # The SAM YAML loader reads unquoted dates as date objects; the
            # transform consumes the JSON representation CloudFormation sends.
            if isinstance(value, datetime.date):
                return str(value)
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value

        count = 0
        generated = 0
        expected = {'Fn::If': ['HasPermissionsBoundary', {'Ref': 'PermissionsBoundaryArn'},
                              {'Ref': 'AWS::NoValue'}]}
        expected_path = {'Fn::If': ['HasPermissionsBoundary', '/lma/application/',
                                   {'Ref': 'AWS::NoValue'}]}
        for path in TEMPLATES:
            source = normalize(yaml_parse((ROOT / path).read_text()))
            original_roles = {name for name, resource in source['Resources'].items()
                              if resource['Type'] == 'AWS::IAM::Role'}
            if 'AWS::Serverless-2016-10-31' in str(source.get('Transform', '')):
                # Packaging replaces local artifact paths with S3 URIs. Stand-ins
                # are in-memory only: no files, uploads or network are involved.
                for resource in source['Resources'].values():
                    if resource['Type'].startswith('AWS::Serverless::'):
                        for key in ('CodeUri', 'ContentUri', 'DefinitionUri'):
                            if key in resource.get('Properties', {}):
                                resource['Properties'][key] = 's3://test-artifacts/test.zip'
                loader = MagicMock()
                loader.load.return_value = {}
                with patch.dict(os.environ, {'AWS_DEFAULT_REGION': 'us-east-1',
                                             'AWS_EC2_METADATA_DISABLED': 'true'}):
                    source = transform(source, {'PermissionsBoundaryArn':
                        'arn:aws:iam::009853297978:policy/lma/isolation/application-boundary'}, loader)
            for name, resource in source['Resources'].items():
                if resource['Type'] == 'AWS::IAM::Role':
                    count += 1
                    generated += name not in original_roles
                    with self.subTest(template=path, role=name):
                        self.assertEqual(resource['Properties']['PermissionsBoundary'], expected)
                        self.assertEqual(resource['Properties']['Path'], expected_path)
        self.assertGreater(count, 102)
        self.assertGreater(generated, 0)

    def test_application_iam_namespaces_preserve_default_paths(self):
        for path, template in self.templates.items():
            for name, resource in template['Resources'].items():
                if resource['Type'] in {'AWS::IAM::Role', 'AWS::IAM::ManagedPolicy', 'AWS::IAM::InstanceProfile'}:
                    with self.subTest(template=path, resource=name):
                        self.assertEqual(resource['Properties']['Path'],
                                         ['HasPermissionsBoundary', '/lma/application/', 'AWS::NoValue'])


if __name__ == '__main__':
    unittest.main()
