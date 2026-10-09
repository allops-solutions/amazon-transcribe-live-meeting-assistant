"""Ownership and pagination tests; fixtures are not production resource evidence."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('generated_resources', ROOT / 'deploy/ci/generated_resources.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)
ACCOUNT = '009853297978'
REGION = 'us-east-1'
ROOT_ARN = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/LMA/root-id'
CHILD_ARN = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/LMA-child/child-id'
KEY = '12345678-1234-1234-1234-123456789abc'


def entry(kind, physical, status='CREATE_COMPLETE'):
    return {'ResourceType': kind, 'PhysicalResourceId': physical, 'ResourceStatus': status}


class GeneratedResourceTests(unittest.TestCase):
    def client(self):
        client = MagicMock()
        root = {'StackId': ROOT_ARN, 'StackStatus': 'CREATE_COMPLETE'}
        child = {'StackId': CHILD_ARN, 'StackStatus': 'CREATE_COMPLETE',
                 'ParentId': ROOT_ARN, 'RootId': ROOT_ARN}
        client.describe_stacks.side_effect = [{'Stacks': [root]}, {'Stacks': [child]}]
        client.get_paginator.return_value.paginate.side_effect = [
            [{'StackResourceSummaries': [entry('AWS::KMS::Key', KEY)]},
             {'StackResourceSummaries': [entry('AWS::CloudFormation::Stack', CHILD_ARN)]}],
            [{'StackResourceSummaries': [entry('AWS::AppSync::GraphQLApi', 'abcdefghijklmnop')]}],
        ]
        return client, root, child

    def test_all_pages_and_verified_nested_resources(self):
        client, _, _ = self.client()
        result = inventory.collect(client, ACCOUNT, REGION)
        self.assertEqual(result['stacksInspected'], 2)
        self.assertEqual(result['resources']['kms'], [f'arn:aws:kms:{REGION}:{ACCOUNT}:key/{KEY}'])
        self.assertEqual(result['resources']['appsync'], [f'arn:aws:appsync:{REGION}:{ACCOUNT}:apis/abcdefghijklmnop'])
        self.assertEqual({call[0] for call in client.method_calls}, {'describe_stacks', 'get_paginator'})

    def test_wrong_parent_root_and_unstable_stack_rejected(self):
        for field, value in [('ParentId', 'other'), ('RootId', 'other'),
                             ('StackStatus', 'UPDATE_IN_PROGRESS'), ('StackId', ROOT_ARN)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                client, _, child = self.client()
                child[field] = value
                inventory.collect(client, ACCOUNT, REGION)

    def test_foreign_root_rejected(self):
        client, root, _ = self.client()
        root['StackId'] = ROOT_ARN.replace(ACCOUNT, '111111111111')
        with self.assertRaises(ValueError):
            inventory.collect(client, ACCOUNT, REGION)

    def test_initial_creation_pins_root_and_only_reports_completed_ids(self):
        client, root, child = self.client()
        root['StackStatus'] = child['StackStatus'] = 'CREATE_IN_PROGRESS'
        client.get_paginator.return_value.paginate.side_effect = [
            [{'StackResourceSummaries': [entry('AWS::KMS::Key', KEY),
                                         entry('AWS::CloudFormation::Stack', CHILD_ARN, 'CREATE_IN_PROGRESS')]}],
            [{'StackResourceSummaries': [entry('AWS::AppSync::GraphQLApi', 'abcdefghijklmnop', 'CREATE_IN_PROGRESS')]}],
        ]
        result = inventory.collect(client, ACCOUNT, REGION, creating_root_arn=ROOT_ARN)
        self.assertFalse(result['inventoryComplete'])
        self.assertEqual(result['resourcesPending'], 1)
        self.assertEqual(len(result['resources']['kms']), 1)
        self.assertEqual(result['resources']['appsync'], [])
        self.assertEqual({call[0] for call in client.method_calls}, {'describe_stacks', 'get_paginator'})

    def test_bootstrap_mode_cannot_accept_different_existing_or_updating_root(self):
        for arn, state in [(ROOT_ARN.replace('root-id', 'other-id'), 'CREATE_IN_PROGRESS'),
                           (ROOT_ARN, 'UPDATE_IN_PROGRESS'), (ROOT_ARN, 'CREATE_COMPLETE')]:
            with self.subTest(arn=arn, state=state), self.assertRaises(ValueError):
                client, root, _ = self.client()
                root['StackStatus'] = state
                inventory.collect(client, ACCOUNT, REGION, creating_root_arn=arn)

    def test_incomplete_resource_rejected(self):
        for status, physical in [('CREATE_IN_PROGRESS', KEY), ('CREATE_COMPLETE', ''), ('IMPORT_COMPLETE', KEY)]:
            client, _, _ = self.client()
            client.get_paginator.return_value.paginate.side_effect = [[
                {'StackResourceSummaries': [entry('AWS::KMS::Key', physical, status)]}]]
            with self.assertRaises(ValueError):
                inventory.collect(client, ACCOUNT, REGION)

    def test_knowledge_vector_scheduler_and_orchestration_ids(self):
        client, _, _ = self.client()
        vector = f'arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/lma-s3v-123456abcdef/index/lma-kb-index'
        machine = f'arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:LMA-LMAVirtualParticipantScheduler'
        client.get_paginator.return_value.paginate.side_effect = [[{'StackResourceSummaries': [
            entry('AWS::Bedrock::KnowledgeBase', 'ABC123DE45'),
            entry('AWS::S3Vectors::Index', vector),
            entry('AWS::StepFunctions::StateMachine', machine),
            entry('AWS::Scheduler::ScheduleGroup', 'LMA-vp-schedules'),
        ]}]]
        result = inventory.collect(client, ACCOUNT, REGION)['resources']
        self.assertEqual(result['knowledge_bases'], [f'arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/ABC123DE45'])
        self.assertEqual(result['vector_indexes'], [vector])
        self.assertEqual(result['state_machines'], [machine])
        self.assertEqual(result['schedule_groups'], [f'arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule-group/LMA-vp-schedules'])

    def test_cross_account_and_wildcard_arns_rejected(self):
        for value in [f'arn:aws:kms:{REGION}:111111111111:key/{KEY}',
                      f'arn:aws:kms:{REGION}:{ACCOUNT}:key/*']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                inventory.validate_arn('kms', value, ACCOUNT, REGION)

    def test_new_services_are_collected_with_correct_arn_forms(self):
        client, _, _ = self.client()
        ecs = f'arn:aws:ecs:{REGION}:{ACCOUNT}:'
        client.get_paginator.return_value.paginate.side_effect = [[{'StackResourceSummaries': [
            entry('AWS::Cognito::UserPool', REGION + '_Example12'),
            entry('AWS::CloudFront::Distribution', 'E123456789ABCD'),
            entry('AWS::ECS::Cluster', 'LMA-VP-Cluster'),
            entry('AWS::ECS::TaskDefinition', ecs + 'task-definition/LMA-VP-Task:1'),
        ]}]]
        result = inventory.collect(client, ACCOUNT, REGION)['resources']
        self.assertEqual(result['cognito'], [f'arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/{REGION}_Example12'])
        self.assertEqual(result['cloudfront'], [f'arn:aws:cloudfront::{ACCOUNT}:distribution/E123456789ABCD'])
        self.assertEqual(result['ecs_clusters'], [ecs + 'cluster/LMA-VP-Cluster'])
        self.assertEqual(result['ecs_task_definitions'], [ecs + 'task-definition/LMA-VP-Task:1'])

    def test_new_service_ids_fail_closed(self):
        for kind, arn in [
            ('cognito', f'arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/eu-west-1_Example'),
            ('cloudfront', f'arn:aws:cloudfront:{REGION}:{ACCOUNT}:distribution/E123456789ABCD'),
            ('ecs_clusters', f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/LMA-*'),
            ('ecs_task_definitions', f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/LMA-VP:*'),
        ]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                inventory.validate_arn(kind, arn, ACCOUNT, REGION)


if __name__ == '__main__':
    unittest.main()
